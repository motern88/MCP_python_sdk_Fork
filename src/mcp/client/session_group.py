"""
SessionGroup 并发管理多个 MCP 会话连接。

工具、资源和提示在各个服务器之间进行汇总。服务器可以在初始化后随时连接或断开连接。

该抽象层可以通过用户自定义的钩子函数来处理命名冲突。
"""

import contextlib
import logging
from collections.abc import Callable
from datetime import timedelta
from types import TracebackType
from typing import Any, TypeAlias

import anyio
from pydantic import BaseModel
from typing_extensions import Self

import mcp
from mcp import types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError


class SseServerParameters(BaseModel):
    """初始化 sse_client 所需的参数。"""

    # 服务器端点的 URL 地址。
    url: str

    # 请求时可选的请求头，字典格式，键为字符串，值任意类型。
    headers: dict[str, Any] | None = None

    # 常规 HTTP 请求的超时时间，单位秒。
    timeout: float = 5

    # SSE（服务器发送事件）读取操作的超时时间，默认 5 分钟。
    sse_read_timeout: float = 60 * 5


class StreamableHttpParameters(BaseModel):
    """初始化 streamablehttp_client 所需的参数。"""

    # 服务器端点 URL。
    url: str

    # 请求时可选的请求头，字典格式。
    headers: dict[str, Any] | None = None

    # 常规 HTTP 请求的超时时间，使用 timedelta 类型，默认 30 秒。
    timeout: timedelta = timedelta(seconds=30)

    # SSE 读取操作的超时时间，默认 5 分钟。
    sse_read_timeout: timedelta = timedelta(seconds=60 * 5)

    # 当传输关闭时，是否关闭客户端会话。
    terminate_on_close: bool = True

# 定义 ServerParameters 类型别名，可以是 StdioServerParameters、SseServerParameters 或 StreamableHttpParameters 三种之一。
ServerParameters: TypeAlias = StdioServerParameters | SseServerParameters | StreamableHttpParameters


class ClientSessionGroup:
    """用于管理多个 MCP 服务器连接的客户端。

    该类负责封装服务器连接的管理工作，
    汇总所有已连接服务器的工具、资源和提示信息。

    对于辅助的处理器，例如资源订阅，委托给客户端，可通过 session 访问。

    示例用法：
        name_fn = lambda name, server_info: f"{(server_info.name)}_{name}"
        async with ClientSessionGroup(component_name_hook=name_fn) as group:
            for server_params in server_params:
                await group.connect_to_server(server_param)
            ...
    """

    class _ComponentNames(BaseModel):
        """用于反向索引查找组件名称的结构体。"""

        prompts: set[str] = set()  # 该 session 管理的所有提示名称集合
        resources: set[str] = set()  # 该 session 管理的所有资源名称集合
        tools: set[str] = set()  # 该 session 管理的所有工具名称集合

    # MCP 标准组件字典，key 为名称，value 为对应类型
    _prompts: dict[str, types.Prompt]
    _resources: dict[str, types.Resource]
    _tools: dict[str, types.Tool]

    # 客户端-服务器连接管理
    _sessions: dict[mcp.ClientSession, _ComponentNames]  # 每个 session 对应的组件名称集合
    _tool_to_session: dict[str, mcp.ClientSession]  # 工具名称到 session 的映射
    _exit_stack: contextlib.AsyncExitStack  # 管理异步上下文资源退出的栈
    _session_exit_stacks: dict[mcp.ClientSession, contextlib.AsyncExitStack]  # 每个 session 专属的退出栈

    # Optional fn consuming (component_name, serverInfo) for custom names.
    # This is provide a means to mitigate naming conflicts across servers.
    # Example: (tool_name, serverInfo) => "{result.serverInfo.name}.{tool_name}"
    # 用于自定义组件命名的函数类型别名，接收组件名和服务器信息，返回自定义名称
    _ComponentNameHook: TypeAlias = Callable[[str, types.Implementation], str]
    _component_name_hook: _ComponentNameHook | None  # 可选的自定义命名钩子函数

    def __init__(
        self,
        exit_stack: contextlib.AsyncExitStack | None = None,
        component_name_hook: _ComponentNameHook | None = None,
    ) -> None:
        """初始化 MCP 客户端。"""

        self._tools = {}  # 初始化工具字典
        self._resources = {}  # 初始化资源字典
        self._prompts = {}  # 初始化提示字典

        self._sessions = {}  # 初始化会话-组件映射
        self._tool_to_session = {}  # 初始化工具名到会话映射

        if exit_stack is None:
            # 如果外部未传入退出栈，则自己创建一个，并标记拥有它的权限
            self._exit_stack = contextlib.AsyncExitStack()
            self._owns_exit_stack = True
        else:
            # 使用外部传入的退出栈，不拥有其关闭权限
            self._exit_stack = exit_stack
            self._owns_exit_stack = False

        self._session_exit_stacks = {}  # 初始化每个会话对应的退出栈字典
        self._component_name_hook = component_name_hook  # 保存自定义组件名称钩子

    async def __aenter__(self) -> Self:
        # 进入异步上下文管理，如果拥有退出栈权限则进入退出栈管理
        if self._owns_exit_stack:
            await self._exit_stack.__aenter__()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> bool | None:
        """退出异步上下文时关闭所有会话退出栈和主退出栈。"""

        # 只有拥有主退出栈权限时才关闭主退出栈
        if self._owns_exit_stack:
            await self._exit_stack.aclose()

        # 并发关闭所有会话的退出栈
        async with anyio.create_task_group() as tg:
            for exit_stack in self._session_exit_stacks.values():
                tg.start_soon(exit_stack.aclose)

    @property
    def sessions(self) -> list[mcp.ClientSession]:
        """返回当前管理的所有会话列表。"""
        return list(self._sessions.keys())

    @property
    def prompts(self) -> dict[str, types.Prompt]:
        """返回所有提示名称到提示对象的字典。"""
        return self._prompts

    @property
    def resources(self) -> dict[str, types.Resource]:
        """返回所有资源名称到资源对象的字典。"""
        return self._resources

    @property
    def tools(self) -> dict[str, types.Tool]:
        """返回所有工具名称到工具对象的字典。"""
        return self._tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        """根据工具名称和参数执行对应的工具调用。"""

        # 通过工具名找到对应的会话
        session = self._tool_to_session[name]
        # 获取该工具的会话内名称（可能与外部名称不同）
        session_tool_name = self.tools[name].name
        # 通过会话执行工具调用，传入参数
        return await session.call_tool(session_tool_name, args)

    async def disconnect_from_server(self, session: mcp.ClientSession) -> None:
        """断开与单个 MCP 服务器的连接。"""

        # 判断该 session 是否在组件管理中
        session_known_for_components = session in self._sessions
        # 判断该 session 是否在退出栈管理中
        session_known_for_stack = session in self._session_exit_stacks

        if not session_known_for_components and not session_known_for_stack:
            # 如果两边都没有该 session，说明该 session 未被管理或已断开，抛出异常
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message="Provided session is not managed or already disconnected.",
                )
            )

        if session_known_for_components:
            # 从组件管理中移除该 session 并获取其管理的组件名称集合
            component_names = self._sessions.pop(session)  # Pop from _sessions tracking

            # 删除该 session 关联的所有提示
            for name in component_names.prompts:
                if name in self._prompts:
                    del self._prompts[name]
            # 删除该 session 关联的所有资源
            for name in component_names.resources:
                if name in self._resources:
                    del self._resources[name]
            # 删除该 session 关联的所有工具，并清除工具到 session 的映射
            for name in component_names.tools:
                if name in self._tools:
                    del self._tools[name]
                if name in self._tool_to_session:
                    del self._tool_to_session[name]

        # 关闭该 session 对应的退出栈，释放相关资源
        if session_known_for_stack:
            session_stack_to_close = self._session_exit_stacks.pop(session)
            await session_stack_to_close.aclose()

    async def connect_with_session(
        self, server_info: types.Implementation, session: mcp.ClientSession
    ) -> mcp.ClientSession:
        """连接到单个 MCP 服务器。

        该方法将给定的服务器信息和客户端会话进行组件汇总。
        """
        # 聚合该 session 上的组件（工具、资源、提示）
        await self._aggregate_components(server_info, session)
        # 返回该会话对象
        return session

    async def connect_to_server(
        self,
        server_params: ServerParameters,
    ) -> mcp.ClientSession:
        """通过服务器参数连接到单个 MCP 服务器。

        该方法先建立会话，然后调用 connect_with_session 完成组件聚合。
        """
        # 建立客户端会话，返回服务器信息和会话对象
        server_info, session = await self._establish_session(server_params)
        # 使用会话进行组件聚合并返回会话
        return await self.connect_with_session(server_info, session)

    async def _establish_session(
        self, server_params: ServerParameters
    ) -> tuple[types.Implementation, mcp.ClientSession]:
        """建立到 MCP 服务器的客户端会话。

        根据传入的服务器参数类型，创建对应的客户端连接。
        """

        # 创建一个异步退出栈，用于管理资源的正确释放
        session_stack = contextlib.AsyncExitStack()
        try:
            # 判断服务器参数类型，创建对应客户端实例并进入其上下文管理
            if isinstance(server_params, StdioServerParameters):
                client = mcp.stdio_client(server_params)
                read, write = await session_stack.enter_async_context(client)
            elif isinstance(server_params, SseServerParameters):
                client = sse_client(
                    url=server_params.url,
                    headers=server_params.headers,
                    timeout=server_params.timeout,
                    sse_read_timeout=server_params.sse_read_timeout,
                )
                read, write = await session_stack.enter_async_context(client)
            else:
                # 默认使用 streamablehttp_client
                client = streamablehttp_client(
                    url=server_params.url,
                    headers=server_params.headers,
                    timeout=server_params.timeout,
                    sse_read_timeout=server_params.sse_read_timeout,
                    terminate_on_close=server_params.terminate_on_close,
                )
                read, write, _ = await session_stack.enter_async_context(client)

            # 创建 MCP 客户端会话，并进入其异步上下文管理
            session = await session_stack.enter_async_context(mcp.ClientSession(read, write))
            # 初始化 session，获取服务器信息
            result = await session.initialize()

            # 会话初始化成功，将该 session 的退出栈保存，并将其注册到主退出栈管理中
            self._session_exit_stacks[session] = session_stack
            # session_stack itself becomes a resource managed by the
            # main _exit_stack.
            await self._exit_stack.enter_async_context(session_stack)

            # 返回服务器信息和会话对象
            return result.serverInfo, session
        except Exception:
            # If anything during this setup fails, ensure the session-specific
            # stack is closed.
            # 如果建立连接过程中发生异常，关闭刚才创建的退出栈释放资源
            await session_stack.aclose()
            # 继续抛出异常
            raise

    async def _aggregate_components(self, server_info: types.Implementation, session: mcp.ClientSession) -> None:
        """汇总指定会话中的提示、资源和工具组件。

    将从服务器查询到的组件信息加入到 ClientSessionGroup 的管理中。
    """

        # Create a reverse index so we can find all prompts, resources, and
        # tools belonging to this session. Used for removing components from
        # the session group via self.disconnect_from_server.
        # 创建一个组件名称容器，用于反向索引当前 session 管理的组件
        component_names = self._ComponentNames()

        # Temporary components dicts. We do not want to modify the aggregate
        # lists in case of an intermediate failure.
        # 创建临时字典，用于缓存本次查询到的组件，避免中途失败影响已有组件
        prompts_temp: dict[str, types.Prompt] = {}
        resources_temp: dict[str, types.Resource] = {}
        tools_temp: dict[str, types.Tool] = {}
        tool_to_session_temp: dict[str, mcp.ClientSession] = {}

        # Query the server for its prompts and aggregate to list.
        # 查询服务器的提示列表，并进行名称转换和缓存
        try:
            prompts = (await session.list_prompts()).prompts
            for prompt in prompts:
                # 自定义组件名称（防止冲突）
                name = self._component_name(prompt.name, server_info)
                prompts_temp[name] = prompt
                component_names.prompts.add(name)
        except McpError as err:
            logging.warning(f"Could not fetch prompts: {err}")

        # Query the server for its resources and aggregate to list.
        # 查询服务器的资源列表，进行同样处理
        try:
            resources = (await session.list_resources()).resources
            for resource in resources:
                name = self._component_name(resource.name, server_info)
                resources_temp[name] = resource
                component_names.resources.add(name)
        except McpError as err:
            logging.warning(f"Could not fetch resources: {err}")

        # Query the server for its tools and aggregate to list.
        # 查询服务器的工具列表，进行同样处理
        try:
            tools = (await session.list_tools()).tools
            for tool in tools:
                name = self._component_name(tool.name, server_info)
                tools_temp[name] = tool
                tool_to_session_temp[name] = session
                component_names.tools.add(name)
        except McpError as err:
            logging.warning(f"Could not fetch tools: {err}")

        # Clean up exit stack for session if we couldn't retrieve anything
        # from the server.
        # 如果三类组件都没有获取到，说明服务器未返回有效组件，移除该 session 的退出栈
        if not any((prompts_temp, resources_temp, tools_temp)):
            del self._session_exit_stacks[session]

        # 检查是否有组件名称重复（与已有组件冲突）
        matching_prompts = prompts_temp.keys() & self._prompts.keys()
        if matching_prompts:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"{matching_prompts} already exist in group prompts.",
                )
            )
        matching_resources = resources_temp.keys() & self._resources.keys()
        if matching_resources:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"{matching_resources} already exist in group resources.",
                )
            )
        matching_tools = tools_temp.keys() & self._tools.keys()
        if matching_tools:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"{matching_tools} already exist in group tools.",
                )
            )

        # 合并临时组件到主字典
        self._sessions[session] = component_names
        self._prompts.update(prompts_temp)
        self._resources.update(resources_temp)
        self._tools.update(tools_temp)
        self._tool_to_session.update(tool_to_session_temp)

    def _component_name(self, name: str, server_info: types.Implementation) -> str:
        # 如果用户提供了自定义命名钩子，则调用它生成组件名
        if self._component_name_hook:
            return self._component_name_hook(name, server_info)
        # 否则直接返回原名
        return name
