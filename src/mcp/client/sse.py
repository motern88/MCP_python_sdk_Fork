import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlparse

import anyio
import httpx
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from httpx_sse import aconnect_sse

import mcp.types as types
from mcp.shared._httpx_utils import McpHttpClientFactory, create_mcp_http_client
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)


def remove_request_params(url: str) -> str:
    # 移除 URL 中的请求参数，只保留协议、主机和路径部分
    return urljoin(url, urlparse(url).path)


@asynccontextmanager
async def sse_client(
    url: str,
    headers: dict[str, Any] | None = None,
    timeout: float = 5,
    sse_read_timeout: float = 60 * 5,
    httpx_client_factory: McpHttpClientFactory = create_mcp_http_client,
    auth: httpx.Auth | None = None,
):
    """
    基于 SSE（服务器发送事件）的客户端传输实现。

    参数说明:
        url: SSE 服务端点 URL。
        headers: 请求时可选的 HTTP 头。
        timeout: 普通 HTTP 请求的超时时间（秒）。
        sse_read_timeout: SSE 读取事件的超时时间（秒）。
        auth: 可选的 HTTPX 认证处理器。

    sse_read_timeout 控制客户端等待新事件的最长时间，超过该时间则断开连接。
    其他 HTTP 请求超时则由 timeout 控制。
    """
    # 定义读写流的发送和接收端，数据类型为 SessionMessage 或 Exception。
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]

    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    # 创建内存对象流（容量为0，表示无缓冲）
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    # 创建任务组管理异步任务
    async with anyio.create_task_group() as tg:
        try:
            # 记录连接日志，移除 URL 请求参数以简化显示
            logger.debug(f"Connecting to SSE endpoint: {remove_request_params(url)}")

            # 创建 HTTPX 客户端实例，使用传入的工厂函数
            async with httpx_client_factory(
                headers=headers, auth=auth, timeout=httpx.Timeout(timeout, read=sse_read_timeout)
            ) as client:
                # 使用 aconnect_sse 建立 SSE 连接，异步上下文管理
                async with aconnect_sse(
                    client,
                    "GET",
                    url,
                ) as event_source:
                    # 检查服务器响应状态码是否成功
                    event_source.response.raise_for_status()
                    logger.debug("SSE connection established")
                    # 启动异步任务读取 SSE 事件流
                    async def sse_reader(
                        task_status: TaskStatus[str] = anyio.TASK_STATUS_IGNORED,
                    ):
                        try:
                            # 异步遍历 SSE 事件流
                            async for sse in event_source.aiter_sse():
                                logger.debug(f"Received SSE event: {sse.event}")
                                match sse.event:
                                    # 当事件类型为 "endpoint" 时，表示服务器发来了新的 POST 端点 URL
                                    case "endpoint":
                                        # 拼接完整的 endpoint URL
                                        endpoint_url = urljoin(url, sse.data)
                                        logger.debug(f"Received endpoint URL: {endpoint_url}")

                                        # 解析原始 URL 和 endpoint URL，验证协议和主机是否匹配
                                        url_parsed = urlparse(url)
                                        endpoint_parsed = urlparse(endpoint_url)
                                        if (
                                            url_parsed.netloc != endpoint_parsed.netloc
                                            or url_parsed.scheme != endpoint_parsed.scheme
                                        ):
                                            error_msg = (
                                                "Endpoint origin does not match " f"connection origin: {endpoint_url}"
                                            )
                                            logger.error(error_msg)
                                            # 如果不匹配，抛出异常
                                            raise ValueError(error_msg)

                                        # 通知任务组此任务已启动，endpoint_url 作为状态信息
                                        task_status.started(endpoint_url)

                                    # 当事件类型为 "message" 时，服务器发送了消息数据
                                    case "message":
                                        try:
                                            # 尝试将 SSE 数据解析成 JSON-RPC 消息对象
                                            message = types.JSONRPCMessage.model_validate_json(  # noqa: E501
                                                sse.data
                                            )
                                            logger.debug(f"Received server message: {message}")
                                        except Exception as exc:
                                            # 解析异常时记录错误，并将异常发送给读流
                                            logger.error(f"Error parsing server message: {exc}")
                                            await read_stream_writer.send(exc)
                                            continue

                                        # 包装成 SessionMessage 类型
                                        session_message = SessionMessage(message)
                                        # 发送到读流
                                        await read_stream_writer.send(session_message)
                                    # 其他未知事件类型，打印警告日志
                                    case _:
                                        logger.warning(f"Unknown SSE event: {sse.event}")
                        except Exception as exc:
                            # 读取 SSE 过程中出现异常，记录错误并发送异常到读流
                            logger.error(f"Error in sse_reader: {exc}")
                            await read_stream_writer.send(exc)
                        finally:
                            # 关闭读流写入端
                            await read_stream_writer.aclose()

                    # 定义异步协程用于向服务器 POST 发送消息
                    async def post_writer(endpoint_url: str):
                        try:
                            # 监听写流的读取端
                            async with write_stream_reader:
                                # 异步遍历客户端待发送的消息
                                async for session_message in write_stream_reader:
                                    logger.debug(f"Sending client message: {session_message}")
                                    # POST 请求发送消息 JSON 数据
                                    response = await client.post(
                                        endpoint_url,
                                        json=session_message.message.model_dump(
                                            by_alias=True,
                                            mode="json",
                                            exclude_none=True,
                                        ),
                                    )
                                    # 抛出异常（如果响应状态异常）
                                    response.raise_for_status()
                                    logger.debug("Client message sent successfully: " f"{response.status_code}")
                        except Exception as exc:
                            # 发送异常时记录错误日志
                            logger.error(f"Error in post_writer: {exc}")
                        finally:
                            # 关闭写流发送端
                            await write_stream.aclose()

                    # 启动 sse_reader 协程，等待其报告启动状态（endpoint_url）
                    endpoint_url = await tg.start(sse_reader)
                    logger.debug(f"Starting post writer with endpoint URL: {endpoint_url}")
                    # 启动 post_writer 协程，将 endpoint_url 传入
                    tg.start_soon(post_writer, endpoint_url)

                    try:
                        # 将读写流对象通过 yield 暴露给调用方
                        yield read_stream, write_stream
                    finally:
                        # 退出时取消所有任务组中的任务
                        tg.cancel_scope.cancel()
        finally:
            # 最终关闭读写流写入和发送端，释放资源
            await read_stream_writer.aclose()
            await write_stream.aclose()
