"""
Windows-specific functionality for stdio client operations.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, TextIO, cast

import anyio
from anyio import to_thread
from anyio.abc import Process
from anyio.streams.file import FileReadStream, FileWriteStream


def get_windows_executable_command(command: str) -> str:
    """
    获取适用于 Windows 系统的可执行命令路径。

    在 Windows 上，命令可能有扩展名（.exe, .cmd 等），
    需要查找完整路径才能正确执行。
    """
    try:
        # 先尝试直接查找该命令是否已在 PATH 中
        if command_path := shutil.which(command):
            return command_path

        # 尝试添加常见的 Windows 扩展名进行查找
        for ext in [".cmd", ".bat", ".exe", ".ps1"]:
            ext_version = f"{command}{ext}"
            if ext_path := shutil.which(ext_version):
                return ext_path

        # 若都找不到，就返回原始命令（可能后续处理）
        return command
    except OSError:
        # 如果查找过程中发生文件系统错误（例如权限问题）
        return command


class FallbackProcess:
    """
    Windows 平台上 subprocess.Popen 的异步封装器。

    由于 subprocess 的 stdin/stdout 是同步 FileIO 对象，
    MCP 需要异步流支持，因此此类将其包装为异步流。
    """

    def __init__(self, popen_obj: subprocess.Popen[bytes]):
        self.popen: subprocess.Popen[bytes] = popen_obj
        self.stdin_raw = popen_obj.stdin  # 原始同步输入流
        self.stdout_raw = popen_obj.stdout  # 原始同步输出流
        self.stderr = popen_obj.stderr  # 错误输出

        # 包装成异步写入流
        self.stdin = FileWriteStream(cast(BinaryIO, self.stdin_raw)) if self.stdin_raw else None
        # 包装成异步读取流
        self.stdout = FileReadStream(cast(BinaryIO, self.stdout_raw)) if self.stdout_raw else None

    async def __aenter__(self):
        """支持异步上下文管理器入口"""
        return self

    async def __aexit__(
        self,
        exc_type: BaseException | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """退出上下文时终止进程并清理资源"""
        self.popen.terminate()
        await to_thread.run_sync(self.popen.wait)  # 阻塞等待在子线程中完成

        # 关闭文件句柄
        if self.stdin:
            await self.stdin.aclose()
        if self.stdout:
            await self.stdout.aclose()
        if self.stdin_raw:
            self.stdin_raw.close()
        if self.stdout_raw:
            self.stdout_raw.close()
        if self.stderr:
            self.stderr.close()

    async def wait(self):
        """异步等待子进程结束"""
        return await to_thread.run_sync(self.popen.wait)

    def terminate(self):
        """立即终止子进程"""
        return self.popen.terminate()

    def kill(self) -> None:
        """强制杀死子进程（等价于 terminate）"""
        self.terminate()


# ------------------------
# Updated function
# ------------------------


async def create_windows_process(
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    errlog: TextIO | None = sys.stderr,
    cwd: Path | str | None = None,
) -> FallbackProcess:
    """
    在 Windows 上创建异步兼容的子进程。

    由于 asyncio.create_subprocess_exec 在 Windows 下不完全支持，
    所以使用 subprocess.Popen，并使用 FallbackProcess 异步封装。

    参数：
        command：命令可执行文件
        args：命令参数列表
        env：环境变量
        errlog：标准错误输出流
        cwd：工作目录

    返回：
        FallbackProcess：包装后的异步子进程
    """
    try:
        # 优先尝试使用 creationflags，防止打开新控制台窗口
        popen_obj = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errlog,
            env=env,
            cwd=cwd,
            bufsize=0,  # 不缓冲输出
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return FallbackProcess(popen_obj)

    except Exception:
        # 如果设置 creationflags 报错，则不使用它重试
        popen_obj = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errlog,
            env=env,
            cwd=cwd,
            bufsize=0,
        )
        return FallbackProcess(popen_obj)


async def terminate_windows_process(process: Process | FallbackProcess):
    """
    终止一个 Windows 子进程。

    注意：在 Windows 上调用 process.terminate() 不总是立即生效，
    所以给它最多 2 秒时间退出，若未退出则调用 kill 强制终止。
    """
    try:
        process.terminate()
        with anyio.fail_after(2.0):
            await process.wait()
    except TimeoutError:
        # 超时未退出则强制杀死进程
        process.kill()
