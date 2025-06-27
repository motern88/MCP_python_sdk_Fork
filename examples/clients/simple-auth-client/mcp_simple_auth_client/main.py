#!/usr/bin/env python3
"""
简单的 MCP 客户端示例，支持 OAuth 认证。

该客户端使用可流式传输的 HTTP 协议，通过 OAuth 连接到 MCP 服务器。
"""

import asyncio
import os
import threading
import time
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class InMemoryTokenStorage(TokenStorage):
    """简单的内存中令牌存储实现类。"""

    def __init__(self):
        # 初始化，令牌和客户端信息默认为 None
        self._tokens: OAuthToken | None = None  # 存储 OAuth 令牌的变量
        self._client_info: OAuthClientInformationFull | None = None  # 存储 OAuth 客户端信息的变量

    async def get_tokens(self) -> OAuthToken | None:
        """
        异步方法：获取当前存储的令牌
        返回:
            OAuthToken 对象或 None（如果尚未设置）
        """
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """
        异步方法：设置新的令牌
        参数:
            tokens: 要存储的 OAuthToken 对象
        """
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """
        异步方法：获取当前存储的客户端信息
        返回:
            OAuthClientInformationFull 对象或 None（如果尚未设置）
        """
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """
        异步方法：设置客户端信息
        参数:
            client_info: 要存储的 OAuthClientInformationFull 对象
        """
        self._client_info = client_info


class CallbackHandler(BaseHTTPRequestHandler):
    """简单的 HTTP 请求处理器，用于接收 OAuth 回调请求。"""

    def __init__(self, request, client_address, server, callback_data):
        """
        初始化处理器，并存储回调数据的引用。

        参数:
            request: 客户端的请求对象
            client_address: 客户端地址
            server: 当前 HTTP 服务器实例
            callback_data: 用于存储回调信息（如授权码）的字典
        """
        self.callback_data = callback_data  # 用于在外部访问回调中的 code/state/error 数据
        super().__init__(request, client_address, server)  # 调用父类构造方法初始化

    def do_GET(self):
        """
        处理 OAuth 回调中的 GET 请求。
        从 URL 中解析授权码或错误信息。
        """
        parsed = urlparse(self.path)  # 解析请求路径（含参数部分）
        query_params = parse_qs(parsed.query)  # 解析查询参数为字典（值为列表）

        if "code" in query_params:
            # 成功获取到授权码
            self.callback_data["authorization_code"] = query_params["code"][0]
            self.callback_data["state"] = query_params.get("state", [None])[0]
            # 返回 200 成功响应
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            # 向浏览器返回一段 HTML，显示成功信息，并2秒后自动关闭窗口
            self.wfile.write(b"""
            <html>
            <body>
                <h1>Authorization Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>setTimeout(() => window.close(), 2000);</script>
            </body>
            </html>
            """)
        elif "error" in query_params:
            # 授权失败，返回错误信息
            self.callback_data["error"] = query_params["error"][0]  # 存储错误信息

            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            # 返回 HTML 页面提示用户授权失败
            self.wfile.write(
                f"""
            <html>
            <body>
                <h1>授权失败</h1>
                <p>错误信息: {query_params['error'][0]}</p>
                <p>你可以关闭此窗口并返回终端。</p>
            </body>
            </html>
            """.encode()
            )
        else:
            # 未识别的请求，返回 404
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """
        重写日志方法，禁止默认打印日志到终端（避免控制台污染）。
        """
        pass


class CallbackServer:
    """简单的服务器类，用于处理 OAuth 回调。"""

    def __init__(self, port=3000):
        """
        初始化回调服务器。

        参数:
            port: 监听端口，默认是 3000
        """
        self.port = port
        self.server = None
        self.thread = None
        self.callback_data = {"authorization_code": None, "state": None, "error": None}

    def _create_handler_with_data(self):
        """
        创建一个自定义的 HTTP 请求处理类，使其能够访问 callback_data。

        返回:
            继承自 CallbackHandler 的处理类，注入 callback_data
        """
        callback_data = self.callback_data

        class DataCallbackHandler(CallbackHandler):
            # 内部定义的处理类，向其构造函数中注入 callback_data
            def __init__(self, request, client_address, server):
                # 将 callback_data 传入父类 CallbackHandler
                super().__init__(request, client_address, server, callback_data)

        return DataCallbackHandler  # 返回这个带 callback_data 的处理器类

    def start(self):
        """
        启动回调服务器，在后台线程中运行。
        """
        handler_class = self._create_handler_with_data()  # 创建处理器类
        # 创建 HTTPServer 监听 localhost 指定端口
        self.server = HTTPServer(("localhost", self.port), handler_class)
        # 使用后台线程异步运行服务器
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()  # 启动线程
        print(f"🖥️  回调服务器已启动于 http://localhost:{self.port}")

    def stop(self):
        """
        停止回调服务器，关闭线程和连接。
        """
        if self.server:
            self.server.shutdown()     # 停止 HTTP 服务
            self.server.server_close() # 关闭 socket 连接
        if self.thread:
            self.thread.join(timeout=1)  # 等待线程退出

    def wait_for_callback(self, timeout=300):
        """
        等待 OAuth 回调的到来，直到超时。

        参数:
            timeout: 等待的最大秒数（默认 300 秒）

        返回:
            授权码 authorization_code，如果有错误则抛出异常
        """
        start_time = time.time()  # 记录开始时间
        while time.time() - start_time < timeout:
            if self.callback_data["authorization_code"]:
                # 收到授权码，返回
                return self.callback_data["authorization_code"]
            elif self.callback_data["error"]:
                # 收到错误信息，抛出异常
                raise Exception(f"OAuth 错误: {self.callback_data['error']}")
            time.sleep(0.1)  # 每 100ms 检查一次
        raise Exception("等待 OAuth 回调超时")

    def get_state(self):
        """
        获取收到的 state 参数值（通常用于防止 CSRF 攻击）。

        返回:
            state 字符串或 None
        """
        return self.callback_data["state"]


class SimpleAuthClient:
    """带有认证支持的简单 MCP 客户端。"""

    def __init__(self, server_url: str, transport_type: str = "streamable_http"):
        """
        初始化 MCP 客户端。

        参数:
            server_url: MCP 服务端地址
            transport_type: 传输类型（默认为 streamable_http，可选 sse）
        """
        self.server_url = server_url  # MCP 服务地址
        self.transport_type = transport_type  # 传输协议类型
        self.session: ClientSession | None = None  # MCP 会话对象

    async def connect(self):
        """
        连接到 MCP 服务，处理 OAuth 授权流程并启动会话。
        """
        print(f"🔗 正在尝试连接到 {self.server_url}...")

        try:
            # 启动 OAuth 回调服务器，监听浏览器授权回调
            callback_server = CallbackServer(port=3030)
            callback_server.start()

            async def callback_handler() -> tuple[str, str | None]:
                """
                等待 OAuth 授权回调，返回授权码和 state。
                """
                print("⏳ 等待授权回调...")
                try:
                    auth_code = callback_server.wait_for_callback(timeout=300)
                    return auth_code, callback_server.get_state()
                finally:
                    callback_server.stop()  # 无论成功或失败，回调服务器都要停止

            # 定义 OAuth 客户端元数据
            client_metadata_dict = {
                "client_name": "Simple Auth Client",  # 客户端名称
                "redirect_uris": ["http://localhost:3030/callback"],  # 回调地址
                "grant_types": ["authorization_code", "refresh_token"],  # 授权方式
                "response_types": ["code"],  # 响应类型
                "token_endpoint_auth_method": "client_secret_post",  # 令牌端点认证方式
            }

            async def _default_redirect_handler(authorization_url: str) -> None:
                """
                默认的跳转处理器，自动在浏览器中打开授权地址。
                """
                print(f"正在打开浏览器进行授权: {authorization_url}")
                webbrowser.open(authorization_url)

            # 创建 OAuth 客户端提供器，处理完整的 OAuth 授权流程
            oauth_auth = OAuthClientProvider(
                server_url=self.server_url.replace("/mcp", ""),  # 去掉路径尾部的 /mcp
                client_metadata=OAuthClientMetadata.model_validate(
                    client_metadata_dict
                ),
                storage=InMemoryTokenStorage(),  # 使用内存存储令牌
                redirect_handler=_default_redirect_handler,  # 打开浏览器处理
                callback_handler=callback_handler,  # 等待回调处理
            )

            # 根据传输协议类型选择不同的连接方式
            if self.transport_type == "sse":
                print("📡 正在使用 SSE 协议连接（带认证）...")
                async with sse_client(
                    url=self.server_url,
                    auth=oauth_auth,
                    timeout=60,
                ) as (read_stream, write_stream):
                    await self._run_session(read_stream, write_stream, None)
            else:
                print("📡 正在使用 StreamableHTTP 协议连接（带认证）...")
                async with streamablehttp_client(
                    url=self.server_url,
                    auth=oauth_auth,
                    timeout=timedelta(seconds=60),
                ) as (read_stream, write_stream, get_session_id):
                    await self._run_session(read_stream, write_stream, get_session_id)

        except Exception as e:
            # 连接失败处理
            print(f"❌ 连接失败: {e}")
            import traceback

            traceback.print_exc()

    async def _run_session(self, read_stream, write_stream, get_session_id):
        """
        使用 MCP 读写流对象启动并运行会话。

        参数:
            read_stream: 读取消息的异步流
            write_stream: 发送消息的异步流
            get_session_id: 可选，获取会话 ID 的方法
        """
        print("🤝 正在初始化 MCP 会话...")
        async with ClientSession(read_stream, write_stream) as session:
            self.session = session
            print("⚡ 始初始化会话...")
            await session.initialize()
            print("✨ 会话初始化完成！")

            print(f"\n✅ 成功连接到 MCP 服务端 {self.server_url}")
            if get_session_id:
                session_id = get_session_id()
                if session_id:
                    print(f"Session ID: {session_id}")  # 打印会话 ID

            # 启动交互式会话循环
            await self.interactive_loop()

    async def list_tools(self):
        """从服务器获取并列出可用工具列表。"""
        if not self.session:
            print("❌ 未连接到服务器")
            return

        try:
            result = await self.session.list_tools()  # 异步调用服务器获取工具列表
            if hasattr(result, "tools") and result.tools:
                print("\n📋 Available tools:")
                for i, tool in enumerate(result.tools, 1):
                    print(f"{i}. {tool.name}")  # 打印工具名称
                    if tool.description:
                        print(f"   Description: {tool.description}")  # 打印工具描述（如果有）
                    print()
            else:
                print("No tools available")
        except Exception as e:
            print(f"❌ Failed to list tools: {e}")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None):
        """
        调用指定的工具。

        参数:
            tool_name: 工具名称
            arguments: 工具参数（字典形式，可为 None）
        """
        if not self.session:
            print("❌ Not connected to server")
            return

        try:
            result = await self.session.call_tool(tool_name, arguments or {})
            print(f"\n🔧 Tool '{tool_name}' result:")
            if hasattr(result, "content"):
                # 打印内容（可为文本或其他类型）
                for content in result.content:
                    if content.type == "text":
                        print(content.text)
                    else:
                        print(content)
            else:
                print(result)
        except Exception as e:
            print(f"❌ Failed to call tool '{tool_name}': {e}")

    async def interactive_loop(self):
        """运行交互式命令行循环，供用户调用工具。"""
        print("\n🎯 进入 MCP 客户端交互模式")
        print("可用命令:")
        print("  list - 列出可用工具")
        print("  call <tool_name> [args] - 调用指定工具（可选参数）")
        print("  quit - 退出客户端")
        print()

        while True:
            try:
                command = input("mcp> ").strip()  # 获取用户输入并去除首尾空白字符

                if not command:
                    continue  # 空输入跳过

                if command == "quit":
                    break  # 退出交互循环

                elif command == "list":
                    await self.list_tools()  # 列出工具

                elif command.startswith("call "):
                    # 解析调用命令
                    parts = command.split(maxsplit=2)
                    tool_name = parts[1] if len(parts) > 1 else ""

                    if not tool_name:
                        print("❌ Please specify a tool name")
                        continue

                    # 解析 JSON 格式参数
                    arguments = {}
                    if len(parts) > 2:
                        import json

                        try:
                            arguments = json.loads(parts[2])  # 将字符串解析为 dict
                        except json.JSONDecodeError:
                            print("❌ 参数格式无效（请使用 JSON 格式）")
                            continue

                    await self.call_tool(tool_name, arguments)

                else:
                    print(
                        "❌ 未知命令。可用命令包括: 'list', 'call <tool_name>', 'quit'"
                    )

            except KeyboardInterrupt:
                print("\n\n👋 再见！")
                break
            except EOFError:
                break


async def main():
    """主入口函数。"""
    # 默认服务器端口，可通过环境变量 MCP_SERVER_PORT 覆盖
    # 大多数 MCP 的 streamable HTTP 服务器使用 /mcp 作为接口路径
    server_url = os.getenv("MCP_SERVER_PORT", 8000)  # 从环境变量获取端口，默认 8000
    transport_type = os.getenv("MCP_TRANSPORT_TYPE", "streamable_http")  # 获取传输类型，默认 streamable_http

    # 根据传输类型拼接完整服务器地址
    server_url = (
        f"http://localhost:{server_url}/mcp"
        if transport_type == "streamable_http"
        else f"http://localhost:{server_url}/sse"
    )

    print("🚀 简易 MCP 认证客户端启动")
    print(f"连接地址: {server_url}")
    print(f"传输类型: {transport_type}")

    # 创建 SimpleAuthClient 实例，开始连接流程（包含自动处理 OAuth）
    client = SimpleAuthClient(server_url, transport_type)
    await client.connect()  # 运行异步主函数


def cli():
    """作为命令行工具的入口函数，用于 uvicorn 等运行。"""
    asyncio.run(main())


if __name__ == "__main__":
    cli()  # 如果作为脚本直接执行，则调用 cli 函数启动
