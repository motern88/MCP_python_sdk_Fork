"""
StreamableHTTP 客户端传输模块

本模块实现了 MCP 客户端的 StreamableHTTP 传输，
支持带有可选 SSE 流式响应的 HTTP POST 请求
以及会话管理。
"""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import anyio
import httpx
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from httpx_sse import EventSource, ServerSentEvent, aconnect_sse

from mcp.shared._httpx_utils import McpHttpClientFactory, create_mcp_http_client
from mcp.shared.message import ClientMessageMetadata, SessionMessage
from mcp.types import (
    ErrorData,
    InitializeResult,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    RequestId,
)

logger = logging.getLogger(__name__)  # 获取当前模块的日志记录器

# 定义类型别名：SessionMessage 或 Exception
SessionMessageOrError = SessionMessage | Exception
# 发送流类型，发送 SessionMessage 或 Exception 对象
StreamWriter = MemoryObjectSendStream[SessionMessageOrError]
# 接收流类型，只接收 SessionMessage 对象
StreamReader = MemoryObjectReceiveStream[SessionMessage]
# 获取 Session ID 的回调类型，返回字符串或 None
GetSessionIdCallback = Callable[[], str | None]


# 定义 HTTP 头部常量
MCP_SESSION_ID = "mcp-session-id"
MCP_PROTOCOL_VERSION = "mcp-protocol-version"
LAST_EVENT_ID = "last-event-id"
CONTENT_TYPE = "content-type"
ACCEPT = "Accept"

# 定义 MIME 类型常量
JSON = "application/json"
SSE = "text/event-stream"

# StreamableHTTP 传输层错误基类
class StreamableHTTPError(Exception):
    """StreamableHTTP 传输错误基类。"""

# 当恢复请求无效时抛出该异常
class ResumptionError(StreamableHTTPError):
    """恢复请求无效时抛出。"""

# 请求上下文数据类，封装请求时所需的状态和信息
@dataclass
class RequestContext:
    """请求操作的上下文。"""

    client: httpx.AsyncClient  # HTTPX 异步客户端实例
    headers: dict[str, str]    # HTTP 请求头
    session_id: str | None     # 当前会话 ID（可空）
    session_message: SessionMessage  # 当前会话消息
    metadata: ClientMessageMetadata | None  # 消息元数据（可空）
    read_stream_writer: StreamWriter  # 读流的发送端
    sse_read_timeout: float  # SSE 读取超时时间（秒）

# StreamableHTTP 传输实现类
class StreamableHTTPTransport:
    """StreamableHTTP 客户端传输实现。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | timedelta = 30,
        sse_read_timeout: float | timedelta = 60 * 5,
        auth: httpx.Auth | None = None,
    ) -> None:
        """初始化 StreamableHTTP 传输。

        参数：
            url: 终端 URL。
            headers: 请求时可选的 HTTP 头。
            timeout: 常规 HTTP 操作超时（秒）。
            sse_read_timeout: SSE 读取事件超时（秒）。
            auth: HTTPX 认证处理器（可选）。
        """
        self.url = url
        self.headers = headers or {}
        # 统一将 timeout 转换为秒数（支持 timedelta）
        self.timeout = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
        self.sse_read_timeout = (
            sse_read_timeout.total_seconds() if isinstance(sse_read_timeout, timedelta) else sse_read_timeout
        )
        self.auth = auth
        self.session_id = None  # 会话 ID，初始化为 None
        self.protocol_version = None  # 协议版本，初始化为 None
        # 默认请求头，支持 Accept JSON 和 SSE 流
        self.request_headers = {
            ACCEPT: f"{JSON}, {SSE}",
            CONTENT_TYPE: JSON,
            **self.headers,
        }

    def _prepare_request_headers(self, base_headers: dict[str, str]) -> dict[str, str]:
        """更新请求头，加入会话 ID 和协议版本（如果可用）。"""
        headers = base_headers.copy()
        if self.session_id:
            headers[MCP_SESSION_ID] = self.session_id
        if self.protocol_version:
            headers[MCP_PROTOCOL_VERSION] = self.protocol_version
        return headers

    def _is_initialization_request(self, message: JSONRPCMessage) -> bool:
        """判断消息是否为初始化请求。"""
        return isinstance(message.root, JSONRPCRequest) and message.root.method == "initialize"

    def _is_initialized_notification(self, message: JSONRPCMessage) -> bool:
        """判断消息是否为初始化通知。"""
        return isinstance(message.root, JSONRPCNotification) and message.root.method == "notifications/initialized"

    def _maybe_extract_session_id_from_response(
        self,
        response: httpx.Response,
    ) -> None:
        """尝试从响应头中提取并保存会话 ID。"""
        new_session_id = response.headers.get(MCP_SESSION_ID)
        if new_session_id:
            self.session_id = new_session_id
            logger.info(f"Received session ID: {self.session_id}")

    def _maybe_extract_protocol_version_from_message(
        self,
        message: JSONRPCMessage,
    ) -> None:
        """尝试从初始化响应消息中提取协议版本。"""
        if isinstance(message.root, JSONRPCResponse) and message.root.result:
            try:
                # 将结果反序列化为 InitializeResult，确保类型安全
                init_result = InitializeResult.model_validate(message.root.result)
                self.protocol_version = str(init_result.protocolVersion)
                logger.info(f"Negotiated protocol version: {self.protocol_version}")
            except Exception as exc:
                logger.warning(f"Failed to parse initialization response as InitializeResult: {exc}")
                logger.warning(f"Raw result: {message.root.result}")

    async def _handle_sse_event(
        self,
        sse: ServerSentEvent,
        read_stream_writer: StreamWriter,
        original_request_id: RequestId | None = None,
        resumption_callback: Callable[[str], Awaitable[None]] | None = None,
        is_initialization: bool = False,
    ) -> bool:
        """处理 SSE 事件，返回 True 表示响应已完成。

        参数：
            sse: 接收到的 SSE 事件对象。
            read_stream_writer: 用于发送解析后的会话消息的流。
            original_request_id: 原始请求 ID（用于替换响应中的 ID）。
            resumption_callback: 恢复令牌回调。
            is_initialization: 是否为初始化阶段，用于提取协议版本。
        """
        if sse.event == "message":
            try:
                # 解析 SSE 数据为 JSONRPC 消息
                message = JSONRPCMessage.model_validate_json(sse.data)
                logger.debug(f"SSE message: {message}")

                # 初始化阶段提取协议版本
                if is_initialization:
                    self._maybe_extract_protocol_version_from_message(message)

                # 如果存在原始请求 ID 且消息是响应或错误，替换消息 ID
                if original_request_id is not None and isinstance(message.root, JSONRPCResponse | JSONRPCError):
                    message.root.id = original_request_id

                # 包装为 SessionMessage 并发送到读流
                session_message = SessionMessage(message)
                await read_stream_writer.send(session_message)

                # 如果 SSE 包含 ID，调用恢复令牌回调
                if sse.id and resumption_callback:
                    await resumption_callback(sse.id)

                # 如果消息为响应或错误，则表示处理完成，返回 True
                # 否则返回 False，继续监听 SSE
                return isinstance(message.root, JSONRPCResponse | JSONRPCError)

            except Exception as exc:
                logger.exception("Error parsing SSE message")
                await read_stream_writer.send(exc)
                return False
        else:
            # 收到未知的 SSE 事件类型，记录警告
            logger.warning(f"Unknown SSE event: {sse.event}")
            return False

    async def handle_get_stream(
        self,
        client: httpx.AsyncClient,
        read_stream_writer: StreamWriter,
    ) -> None:
        """处理服务器发起的 GET SSE 流。"""
        try:
            if not self.session_id:  # 如果没有会话 ID，直接返回
                return

            headers = self._prepare_request_headers(self.request_headers)  # 准备请求头

            async with aconnect_sse(
                client,
                "GET",
                self.url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout, read=self.sse_read_timeout),
            ) as event_source:
                event_source.response.raise_for_status()  # 确认响应状态正常
                logger.debug("GET SSE connection established")  # 连接建立日志

                async for sse in event_source.aiter_sse():  # 异步迭代 SSE 事件
                    await self._handle_sse_event(sse, read_stream_writer)  # 处理 SSE 事件

        except Exception as exc:
            logger.debug(f"GET stream error (non-fatal): {exc}")  # 记录非致命错误

    async def _handle_resumption_request(self, ctx: RequestContext) -> None:
        """使用 GET SSE 处理恢复请求。"""
        headers = self._prepare_request_headers(ctx.headers)  # 准备请求头
        if ctx.metadata and ctx.metadata.resumption_token:  # 如果有恢复令牌
            headers[LAST_EVENT_ID] = ctx.metadata.resumption_token  # 设置 Last-Event-ID
        else:
            raise ResumptionError("Resumption request requires a resumption token")  # 无恢复令牌则抛异常

        # 提取原始请求 ID，用于映射响应
        original_request_id = None
        if isinstance(ctx.session_message.message.root, JSONRPCRequest):
            original_request_id = ctx.session_message.message.root.id

        async with aconnect_sse(
            ctx.client,
            "GET",
            self.url,
            headers=headers,
            timeout=httpx.Timeout(self.timeout, read=self.sse_read_timeout),
        ) as event_source:
            event_source.response.raise_for_status()
            logger.debug("Resumption GET SSE connection established")

            async for sse in event_source.aiter_sse():
                is_complete = await self._handle_sse_event(
                    sse,
                    ctx.read_stream_writer,
                    original_request_id,
                    ctx.metadata.on_resumption_token_update if ctx.metadata else None,
                )
                if is_complete:  # 收到完成事件后跳出循环
                    break

    async def _handle_post_request(self, ctx: RequestContext) -> None:
        """处理带响应的 POST 请求。"""
        headers = self._prepare_request_headers(ctx.headers)
        message = ctx.session_message.message
        is_initialization = self._is_initialization_request(message)  # 判断是否初始化请求

        async with ctx.client.stream(
            "POST",
            self.url,
            json=message.model_dump(by_alias=True, mode="json", exclude_none=True),
            headers=headers,
        ) as response:
            if response.status_code == 202:
                logger.debug("Received 202 Accepted")  # 202 接收状态日志
                return

            if response.status_code == 404:
                if isinstance(message.root, JSONRPCRequest):
                    await self._send_session_terminated_error(
                        ctx.read_stream_writer,
                        message.root.id,
                    )
                return

            response.raise_for_status()
            if is_initialization:
                self._maybe_extract_session_id_from_response(response)  # 提取会话 ID

            content_type = response.headers.get(CONTENT_TYPE, "").lower()

            if content_type.startswith(JSON):
                await self._handle_json_response(response, ctx.read_stream_writer, is_initialization)
            elif content_type.startswith(SSE):
                await self._handle_sse_response(response, ctx, is_initialization)
            else:
                await self._handle_unexpected_content_type(
                    content_type,
                    ctx.read_stream_writer,
                )

    async def _handle_json_response(
        self,
        response: httpx.Response,
        read_stream_writer: StreamWriter,
        is_initialization: bool = False,
    ) -> None:
        """处理服务器返回的 JSON 响应。"""
        try:
            content = await response.aread()  # 异步读取响应内容
            message = JSONRPCMessage.model_validate_json(content)  # 解析 JSONRPC 消息

            # Extract protocol version from initialization response
            if is_initialization:
                self._maybe_extract_protocol_version_from_message(message)  # 初始化阶段提取协议版本

            session_message = SessionMessage(message)
            await read_stream_writer.send(session_message)  # 发送消息到读流
        except Exception as exc:
            logger.error(f"Error parsing JSON response: {exc}")
            await read_stream_writer.send(exc)

    async def _handle_sse_response(
        self,
        response: httpx.Response,
        ctx: RequestContext,
        is_initialization: bool = False,
    ) -> None:
        """处理服务器返回的 SSE 响应。"""
        try:
            event_source = EventSource(response)  # 创建 SSE 事件源
            async for sse in event_source.aiter_sse():
                is_complete = await self._handle_sse_event(
                    sse,
                    ctx.read_stream_writer,
                    resumption_callback=(ctx.metadata.on_resumption_token_update if ctx.metadata else None),
                    is_initialization=is_initialization,
                )
                # If the SSE event indicates completion, like returning respose/error
                # break the loop
                if is_complete:  # 处理完成则跳出循环
                    break
        except Exception as e:
            logger.exception("Error reading SSE stream:")
            await ctx.read_stream_writer.send(e)

    async def _handle_unexpected_content_type(
        self,
        content_type: str,
        read_stream_writer: StreamWriter,
    ) -> None:
        """处理响应中意外的内容类型。"""
        error_msg = f"Unexpected content type: {content_type}"
        logger.error(error_msg)
        await read_stream_writer.send(ValueError(error_msg))

    async def _send_session_terminated_error(
        self,
        read_stream_writer: StreamWriter,
        request_id: RequestId,
    ) -> None:
        """发送会话终止错误响应。"""
        jsonrpc_error = JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=ErrorData(code=32600, message="Session terminated"),
        )
        session_message = SessionMessage(JSONRPCMessage(jsonrpc_error))
        await read_stream_writer.send(session_message)

    async def post_writer(
        self,
        client: httpx.AsyncClient,
        write_stream_reader: StreamReader,
        read_stream_writer: StreamWriter,
        write_stream: MemoryObjectSendStream[SessionMessage],
        start_get_stream: Callable[[], None],
        tg: TaskGroup,
    ) -> None:
        """处理向服务器写请求。"""
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    message = session_message.message
                    metadata = (
                        session_message.metadata
                        if isinstance(session_message.metadata, ClientMessageMetadata)
                        else None
                    )

                    # Check if this is a resumption request
                    is_resumption = bool(metadata and metadata.resumption_token)

                    logger.debug(f"Sending client message: {message}")

                    # 处理初始化通知消息
                    if self._is_initialized_notification(message):
                        start_get_stream()

                    ctx = RequestContext(
                        client=client,
                        headers=self.request_headers,
                        session_id=self.session_id,
                        session_message=session_message,
                        metadata=metadata,
                        read_stream_writer=read_stream_writer,
                        sse_read_timeout=self.sse_read_timeout,
                    )

                    async def handle_request_async():
                        if is_resumption:
                            await self._handle_resumption_request(ctx)
                        else:
                            await self._handle_post_request(ctx)

                    # 如果是请求，开启新任务处理；否则直接处理
                    if isinstance(message.root, JSONRPCRequest):
                        tg.start_soon(handle_request_async)
                    else:
                        await handle_request_async()

        except Exception as exc:
            logger.error(f"Error in post_writer: {exc}")
        finally:
            await read_stream_writer.aclose()
            await write_stream.aclose()

    async def terminate_session(self, client: httpx.AsyncClient) -> None:
        """通过发送 DELETE 请求来终止会话。"""
        if not self.session_id:
            return  # 如果当前没有会话 ID，直接返回

        try:
            headers = self._prepare_request_headers(self.request_headers)  # 准备带会话信息的请求头
            response = await client.delete(self.url, headers=headers)  # 向服务器发送 DELETE 请求

            if response.status_code == 405:
                logger.debug("Server does not allow session termination")  # 405 表示不允许 DELETE 方法
            elif response.status_code not in (200, 204):
                logger.warning(f"Session termination failed: {response.status_code}")  # 其他状态码表示失败
        except Exception as exc:
            logger.warning(f"Session termination failed: {exc}")  # 捕获异常记录日志

    def get_session_id(self) -> str | None:
        """获取当前的会话 ID。"""
        return self.session_id


@asynccontextmanager
async def streamablehttp_client(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float | timedelta = 30,
    sse_read_timeout: float | timedelta = 60 * 5,
    terminate_on_close: bool = True,
    httpx_client_factory: McpHttpClientFactory = create_mcp_http_client,
    auth: httpx.Auth | None = None,
) -> AsyncGenerator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
        GetSessionIdCallback,
    ],
    None,
]:
    """
    StreamableHTTP 客户端传输器。

    参数：
        url：StreamableHTTP 的服务端地址。
        headers：HTTP 请求头，可选。
        timeout：普通请求超时时间。
        sse_read_timeout：读取 SSE 的超时时间。
        terminate_on_close：退出时是否自动终止会话。
        httpx_client_factory：用于创建 HTTP 客户端的工厂函数。
        auth：HTTP 认证方式，可选。

    返回：
        包含：
            - 读取服务器消息的流（read_stream）
            - 向服务器发送消息的流（write_stream）
            - 获取当前会话 ID 的回调函数
    """
    # 实例化底层传输类，封装请求地址和行为
    transport = StreamableHTTPTransport(url, headers, timeout, sse_read_timeout, auth)

    # 创建内存对象流，用于异步通信（接收服务器消息）
    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    # 创建内存对象流，用于异步通信（发送客户端消息）
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    # 创建任务组，允许并发处理多个协程任务
    async with anyio.create_task_group() as tg:
        try:
            logger.debug(f"Connecting to StreamableHTTP endpoint: {url}")

            # 使用 HTTP 客户端工厂创建 httpx 客户端（支持自定义 headers、超时、认证等）
            async with httpx_client_factory(
                headers=transport.request_headers,
                timeout=httpx.Timeout(transport.timeout, read=transport.sse_read_timeout),
                auth=transport.auth,
            ) as client:
                # 定义一个回调，用于当接收到初始化完成通知时启动 SSE 流监听
                def start_get_stream() -> None:
                    tg.start_soon(transport.handle_get_stream, client, read_stream_writer)

                # 启动一个异步任务处理客户端发送的消息
                tg.start_soon(
                    transport.post_writer,  # 处理客户端的发送逻辑
                    client,
                    write_stream_reader,
                    read_stream_writer,
                    write_stream,
                    start_get_stream,
                    tg,
                )

                try:
                    # 将读写流和 session_id 获取函数返回给调用方
                    yield (
                        read_stream,
                        write_stream,
                        transport.get_session_id,
                    )
                finally:
                    # 如果设置为退出时自动关闭，并且 session_id 存在，则尝试终止会话
                    if transport.session_id and terminate_on_close:
                        await transport.terminate_session(client)

                    # 取消所有任务（包括 SSE 监听和写入任务）
                    tg.cancel_scope.cancel()
        finally:
            # 清理流资源，防止阻塞或泄露
            await read_stream_writer.aclose()
            await write_stream.aclose()
