import argparse
import logging
import sys
from functools import partial
from urllib.parse import urlparse

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp.shared.session import RequestResponder

# 如果没有设置 Python 警告选项
if not sys.warnoptions:
    import warnings
    # 忽略所有警告
    warnings.simplefilter("ignore")

# 设置日志记录器，日志等级为 INFO
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("client")


# 定义异步消息处理函数
async def message_handler(
    message: RequestResponder[types.ServerRequest, types.ClientResult] | types.ServerNotification | Exception,
) -> None:
    # 如果收到的是异常
    if isinstance(message, Exception):
        logger.error("Error: %s", message)
        return

    # 打印收到的服务器消息
    logger.info("Received message from server: %s", message)


# 定义异步会话运行函数
async def run_session(
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
    client_info: types.Implementation | None = None,
):
    # 使用 ClientSession 管理会话
    async with ClientSession(
        read_stream,
        write_stream,
        message_handler=message_handler,
        client_info=client_info,
    ) as session:
        logger.info("Initializing session")     # 记录初始化日志
        await session.initialize()              # 异步初始化会话
        logger.info("Initialized")              # 记录初始化完成日志


# 定义主入口异步函数
async def main(command_or_url: str, args: list[str], env: list[tuple[str, str]]):
    env_dict = dict(env)  # 将环境变量列表转换为字典

    # 如果输入是一个 http 或 https URL
    if urlparse(command_or_url).scheme in ("http", "https"):
        # 使用 SSE 客户端连接服务器
        async with sse_client(command_or_url) as streams:
            await run_session(*streams)  # 启动会话
    else:
        # 否则，使用标准输入输出连接服务端
        server_parameters = StdioServerParameters(command=command_or_url, args=args, env=env_dict)
        async with stdio_client(server_parameters) as streams:
            await run_session(*streams)  # 启动会话


# 命令行接口函数
def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("command_or_url", help="要连接的命令或 URL")
    parser.add_argument("args", nargs="*", help="附加参数")
    parser.add_argument(
        "-e",
        "--env",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="设置环境变量，可多次使用",
        default=[],
    )

    args = parser.parse_args()  # 解析命令行参数
    # 使用 anyio 启动主函数，运行在 trio 后端
    anyio.run(partial(main, args.command_or_url, args.args, args.env), backend="trio")


if __name__ == "__main__":
    cli()  # 启动命令行接口
