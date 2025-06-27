from datetime import timedelta
from typing import Any, Protocol

import anyio.lowlevel
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import AnyUrl, TypeAdapter

import mcp.types as types
from mcp.shared.context import RequestContext
from mcp.shared.message import SessionMessage
from mcp.shared.session import BaseSession, ProgressFnT, RequestResponder
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

# 默认客户端信息，用于标识客户端身份
DEFAULT_CLIENT_INFO = types.Implementation(name="mcp", version="0.1.0")

# 生成消息回调类型
class SamplingFnT(Protocol):
    async def __call__(
        self,
        context: RequestContext["ClientSession", Any],
        params: types.CreateMessageRequestParams,
    ) -> types.CreateMessageResult | types.ErrorData: ...

# 引导提问回调类型
class ElicitationFnT(Protocol):
    async def __call__(
        self,
        context: RequestContext["ClientSession", Any],
        params: types.ElicitRequestParams,
    ) -> types.ElicitResult | types.ErrorData: ...

# 获取根节点列表的回调类型
class ListRootsFnT(Protocol):
    async def __call__(
        self, context: RequestContext["ClientSession", Any]
    ) -> types.ListRootsResult | types.ErrorData: ...

# 日志回调类型
class LoggingFnT(Protocol):
    async def __call__(
        self,
        params: types.LoggingMessageNotificationParams,
    ) -> None: ...

# 接收消息处理函数类型（可处理请求、通知或异常）
class MessageHandlerFnT(Protocol):
    async def __call__(
        self,
        message: RequestResponder[types.ServerRequest, types.ClientResult] | types.ServerNotification | Exception,
    ) -> None: ...

# 默认消息处理器（什么都不处理，仅做一次协程调度让出控制权）
async def _default_message_handler(
    message: RequestResponder[types.ServerRequest, types.ClientResult] | types.ServerNotification | Exception,
) -> None:
    await anyio.lowlevel.checkpoint()

# 默认采样处理函数：返回错误（不支持采样）
async def _default_sampling_callback(
    context: RequestContext["ClientSession", Any],
    params: types.CreateMessageRequestParams,
) -> types.CreateMessageResult | types.ErrorData:
    return types.ErrorData(
        code=types.INVALID_REQUEST,
        message="Sampling not supported",
    )

# 默认引导提问处理函数：返回错误（不支持）
async def _default_elicitation_callback(
    context: RequestContext["ClientSession", Any],
    params: types.ElicitRequestParams,
) -> types.ElicitResult | types.ErrorData:
    return types.ErrorData(
        code=types.INVALID_REQUEST,
        message="Elicitation not supported",
    )

# 默认根节点请求函数：返回错误（不支持）
async def _default_list_roots_callback(
    context: RequestContext["ClientSession", Any],
) -> types.ListRootsResult | types.ErrorData:
    return types.ErrorData(
        code=types.INVALID_REQUEST,
        message="List roots not supported",
    )

# 默认日志处理函数：不做任何操作
async def _default_logging_callback(
    params: types.LoggingMessageNotificationParams,
) -> None:
    pass

# 定义一个类型适配器（ClientResult | ErrorData）用于统一解码响应
ClientResponse: TypeAdapter[types.ClientResult | types.ErrorData] = TypeAdapter(types.ClientResult | types.ErrorData)

# ClientSession 继承自 BaseSession，用于管理与服务端的会话交互
class ClientSession(
    BaseSession[
        types.ClientRequest,
        types.ClientNotification,
        types.ClientResult,
        types.ServerRequest,
        types.ServerNotification,
    ]
):
    def __init__(
        self,
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
        write_stream: MemoryObjectSendStream[SessionMessage],
        read_timeout_seconds: timedelta | None = None,
        sampling_callback: SamplingFnT | None = None,
        elicitation_callback: ElicitationFnT | None = None,
        list_roots_callback: ListRootsFnT | None = None,
        logging_callback: LoggingFnT | None = None,
        message_handler: MessageHandlerFnT | None = None,
        client_info: types.Implementation | None = None,
    ) -> None:
        super().__init__(
            read_stream,
            write_stream,
            types.ServerRequest,
            types.ServerNotification,
            read_timeout_seconds=read_timeout_seconds,
        )
        # 设置客户端信息
        self._client_info = client_info or DEFAULT_CLIENT_INFO
        # 设置各类回调函数（如果用户未提供则使用默认实现）
        self._sampling_callback = sampling_callback or _default_sampling_callback
        self._elicitation_callback = elicitation_callback or _default_elicitation_callback
        self._list_roots_callback = list_roots_callback or _default_list_roots_callback
        self._logging_callback = logging_callback or _default_logging_callback
        self._message_handler = message_handler or _default_message_handler

    async def initialize(self) -> types.InitializeResult:
        # 根据是否启用某能力来构建 capabilities 字段
        sampling = types.SamplingCapability() if self._sampling_callback is not _default_sampling_callback else None
        elicitation = (
            types.ElicitationCapability() if self._elicitation_callback is not _default_elicitation_callback else None
        )
        roots = (
            # TODO: Should this be based on whether we
            # _will_ send notifications, or only whether
            # they're supported?
            types.RootsCapability(listChanged=True)
            if self._list_roots_callback is not _default_list_roots_callback
            else None
        )

        # 向服务端发送初始化请求，携带版本、能力和客户端信息
        result = await self.send_request(
            types.ClientRequest(
                types.InitializeRequest(
                    method="initialize",
                    params=types.InitializeRequestParams(
                        protocolVersion=types.LATEST_PROTOCOL_VERSION,
                        capabilities=types.ClientCapabilities(
                            sampling=sampling,
                            elicitation=elicitation,
                            experimental=None,
                            roots=roots,
                        ),
                        clientInfo=self._client_info,
                    ),
                )
            ),
            types.InitializeResult,
        )

        # 如果服务端返回的协议版本不被支持，则抛出异常
        if result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
            raise RuntimeError("Unsupported protocol version from the server: " f"{result.protocolVersion}")

        # 向服务端发送 "已初始化" 通知
        await self.send_notification(
            types.ClientNotification(types.InitializedNotification(method="notifications/initialized"))
        )

        return result

    async def send_ping(self) -> types.EmptyResult:
        """发送 ping 请求以检查连接"""
        return await self.send_request(
            types.ClientRequest(
                types.PingRequest(
                    method="ping",
                )
            ),
            types.EmptyResult,
        )

    async def send_progress_notification(
        self,
        progress_token: str | int,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """发送进度更新通知，可用于指示当前处理进度等"""
        await self.send_notification(
            types.ClientNotification(
                types.ProgressNotification(
                    method="notifications/progress",
                    params=types.ProgressNotificationParams(
                        progressToken=progress_token,
                        progress=progress,
                        total=total,
                        message=message,
                    ),
                ),
            )
        )

    async def set_logging_level(self, level: types.LoggingLevel) -> types.EmptyResult:
        """发送设置日志级别请求。"""
        return await self.send_request(
            types.ClientRequest(
                types.SetLevelRequest(
                    method="logging/setLevel",
                    params=types.SetLevelRequestParams(level=level),
                )
            ),
            types.EmptyResult,
        )

    async def list_resources(self, cursor: str | None = None) -> types.ListResourcesResult:
        """发送请求以列出所有资源。支持分页（通过 cursor 指定起始位置）。"""
        return await self.send_request(
            types.ClientRequest(
                types.ListResourcesRequest(
                    method="resources/list",
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None,
                )
            ),
            types.ListResourcesResult,
        )

    async def list_resource_templates(self, cursor: str | None = None) -> types.ListResourceTemplatesResult:
        """发送请求以列出资源模板。支持分页。"""
        return await self.send_request(
            types.ClientRequest(
                types.ListResourceTemplatesRequest(
                    method="resources/templates/list",
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None,
                )
            ),
            types.ListResourceTemplatesResult,
        )

    async def read_resource(self, uri: AnyUrl) -> types.ReadResourceResult:
        """发送请求以读取指定 URI 的资源内容。"""
        return await self.send_request(
            types.ClientRequest(
                types.ReadResourceRequest(
                    method="resources/read",
                    params=types.ReadResourceRequestParams(uri=uri),
                )
            ),
            types.ReadResourceResult,
        )

    async def subscribe_resource(self, uri: AnyUrl) -> types.EmptyResult:
        """订阅指定资源的变化通知。"""
        return await self.send_request(
            types.ClientRequest(
                types.SubscribeRequest(
                    method="resources/subscribe",
                    params=types.SubscribeRequestParams(uri=uri),
                )
            ),
            types.EmptyResult,
        )

    async def unsubscribe_resource(self, uri: AnyUrl) -> types.EmptyResult:
        """取消订阅指定资源的变化通知。"""
        return await self.send_request(
            types.ClientRequest(
                types.UnsubscribeRequest(
                    method="resources/unsubscribe",
                    params=types.UnsubscribeRequestParams(uri=uri),
                )
            ),
            types.EmptyResult,
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: ProgressFnT | None = None,
    ) -> types.CallToolResult:
        """调用指定名称的工具，可传递参数与进度回调函数。"""

        return await self.send_request(
            types.ClientRequest(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(
                        name=name,
                        arguments=arguments,
                    ),
                )
            ),
            types.CallToolResult,
            request_read_timeout_seconds=read_timeout_seconds,
            progress_callback=progress_callback,
        )

    async def list_prompts(self, cursor: str | None = None) -> types.ListPromptsResult:
        """列出可用提示模板（prompts），支持分页。"""
        return await self.send_request(
            types.ClientRequest(
                types.ListPromptsRequest(
                    method="prompts/list",
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None,
                )
            ),
            types.ListPromptsResult,
        )

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
        """获取指定名称的提示模板，支持传入参数。"""
        return await self.send_request(
            types.ClientRequest(
                types.GetPromptRequest(
                    method="prompts/get",
                    params=types.GetPromptRequestParams(name=name, arguments=arguments),
                )
            ),
            types.GetPromptResult,
        )

    async def complete(
        self,
        ref: types.ResourceTemplateReference | types.PromptReference,
        argument: dict[str, str],
        context_arguments: dict[str, str] | None = None,
    ) -> types.CompleteResult:
        """执行补全请求（completion），基于提示模板或资源模板，并附带参数上下文。"""
        context = None
        if context_arguments is not None:
            context = types.CompletionContext(arguments=context_arguments)

        return await self.send_request(
            types.ClientRequest(
                types.CompleteRequest(
                    method="completion/complete",
                    params=types.CompleteRequestParams(
                        ref=ref,
                        argument=types.CompletionArgument(**argument),
                        context=context,
                    ),
                )
            ),
            types.CompleteResult,
        )

    async def list_tools(self, cursor: str | None = None) -> types.ListToolsResult:
        """列出可用工具。支持分页。"""
        return await self.send_request(
            types.ClientRequest(
                types.ListToolsRequest(
                    method="tools/list",
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None,
                )
            ),
            types.ListToolsResult,
        )

    async def send_roots_list_changed(self) -> None:
        """通知服务端“根节点列表”已发生变化。"""
        await self.send_notification(
            types.ClientNotification(
                types.RootsListChangedNotification(
                    method="notifications/roots/list_changed",
                )
            )
        )

    async def _received_request(self, responder: RequestResponder[types.ServerRequest, types.ClientResult]) -> None:
        # 创建请求上下文对象，用于封装当前请求的信息，方便后续回调使用
        ctx = RequestContext[ClientSession, Any](
            request_id=responder.request_id,  # 请求的唯一ID
            meta=responder.request_meta,  # 请求的元信息（如头信息、附加参数等）
            session=self,  # 当前会话对象
            lifespan_context=None,  # 生命周期上下文，暂时未使用
        )

        # 根据请求的具体类型进行匹配处理
        match responder.request.root:
            # 如果是创建消息请求(CreateMessageRequest)
            case types.CreateMessageRequest(params=params):
                with responder:
                    # 调用采样回调异步处理请求参数
                    response = await self._sampling_callback(ctx, params)
                    # 对回调结果进行验证转换成客户端响应格式
                    client_response = ClientResponse.validate_python(response)
                    # 发送响应给客户端
                    await responder.respond(client_response)

            # 如果是推理请求(ElicitRequest)
            case types.ElicitRequest(params=params):
                with responder:
                    # 调用推理回调异步处理请求参数
                    response = await self._elicitation_callback(ctx, params)
                    # 验证并转换响应格式
                    client_response = ClientResponse.validate_python(response)
                    # 发送响应
                    await responder.respond(client_response)

            # 如果是请求根列表(ListRootsRequest)
            case types.ListRootsRequest():
                with responder:
                    # 调用根列表回调获取数据
                    response = await self._list_roots_callback(ctx)
                    # 验证并转换响应格式
                    client_response = ClientResponse.validate_python(response)
                    # 发送响应
                    await responder.respond(client_response)

            # 如果是心跳请求(PingRequest)
            case types.PingRequest():
                with responder:
                    # 直接返回空结果作为响应，保持连接活跃
                    return await responder.respond(types.ClientResult(root=types.EmptyResult()))

    async def _handle_incoming(
        self,
        req: RequestResponder[types.ServerRequest, types.ClientResult] | types.ServerNotification | Exception,
    ) -> None:
        """
        处理所有传入的消息请求，将其转发给消息处理器统一处理。
        """
        await self._message_handler(req)

    async def _received_notification(self, notification: types.ServerNotification) -> None:
        """
        处理服务器发来的通知消息，根据通知类型调用对应的回调函数。
        """
        match notification.root:
            # 如果是日志消息通知
            case types.LoggingMessageNotification(params=params):
                # 调用日志回调处理日志内容
                await self._logging_callback(params)
            case _:
                # 其他未处理的通知类型，忽略
                pass
