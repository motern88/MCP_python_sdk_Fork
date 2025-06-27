import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Subprotocol

import mcp.types as types
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def websocket_client(
    url: str,
) -> AsyncGenerator[
    tuple[MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]],
    None,
]:
    """
    WebSocket client transport for MCP，对应服务器端的对称实现。

    连接到指定的 URL，使用 "mcp" 子协议，然后返回：
        (read_stream, write_stream)

    - read_stream：用于接收从服务器发来的消息（可能是 JSONRPC 消息或异常）
    - write_stream：用于发送 JSONRPC 消息到服务器
    """

    # 创建两个内存对象流：
    # - read_stream 用于从 WebSocket 读取消息（由 ws_reader 写入）
    # - write_stream 用于向 WebSocket 写入消息（由 ws_writer 读取）
    # 定义四个内存流变量类型。
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    # 使用指定的子协议 "mcp" 建立 WebSocket 连接。
    async with ws_connect(url, subprotocols=[Subprotocol("mcp")]) as ws:

        async def ws_reader():
            """
            从 WebSocket 中读取文本消息，解析成 JSONRPC 消息对象，
            并写入 read_stream。
            """
            async with read_stream_writer:
                async for raw_text in ws:
                    try:
                        message = types.JSONRPCMessage.model_validate_json(raw_text)
                        session_message = SessionMessage(message)
                        await read_stream_writer.send(session_message)  # 发送到读取流
                    except ValidationError as exc:
                        # 如果解析失败，发送异常
                        await read_stream_writer.send(exc)

        async def ws_writer():
            """
            从 write_stream 读取 JSONRPC 消息，
            将其编码为 JSON 字符串后发送给服务器。
            """
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    # Convert to a dict, then to JSON
                    msg_dict = session_message.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                    await ws.send(json.dumps(msg_dict))  # 序列化为 JSON 后发送

        async with anyio.create_task_group() as tg:
            # 并发启动读写协程
            tg.start_soon(ws_reader)
            tg.start_soon(ws_writer)

            # 返回读取/写入流给调用方
            yield (read_stream, write_stream)

            # 调用方退出上下文时，取消任务组中所有任务
            tg.cancel_scope.cancel()
