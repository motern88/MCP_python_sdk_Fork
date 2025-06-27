"""
OAuth2 Authentication implementation for HTTPX.

Implements authorization code flow with PKCE and automatic token refresh.

用于 HTTPX 的 OAuth2 认证实现。
实现了带有 PKCE 的授权码流程以及自动刷新令牌功能。
"""

import base64
import hashlib
import logging
import secrets
import string
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode, urljoin, urlparse

import anyio
import httpx
from pydantic import BaseModel, Field, ValidationError

from mcp.client.streamable_http import MCP_PROTOCOL_VERSION
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION

logger = logging.getLogger(__name__)


# 定义 OAuth 流程的基础异常类
class OAuthFlowError(Exception):
    """OAuth 流程错误的基类异常。"""

# 当处理 token（令牌）相关操作失败时抛出
class OAuthTokenError(OAuthFlowError):
    """当令牌操作失败时引发的异常。"""

# 当客户端注册失败时抛出
class OAuthRegistrationError(OAuthFlowError):
    """当客户端注册失败时引发的异常。"""

# 定义 PKCE（Proof Key for Code Exchange）参数的数据模型
class PKCEParameters(BaseModel):
    """PKCE（授权码校验）参数。"""

    # code_verifier 是客户端生成的原始密钥字符串，长度限制为 43 到 128
    code_verifier: str = Field(..., min_length=43, max_length=128)
    # code_challenge 是由 code_verifier 派生出的 SHA256 哈希，经 base64-url 编码后的字符串
    code_challenge: str = Field(..., min_length=43, max_length=128)

    # 类方法：用于生成新的 PKCE 参数
    @classmethod
    def generate(cls) -> "PKCEParameters":
        """生成新的 PKCE 参数。"""
        # 随机生成一个 128 字符的 code_verifier，字符集包含大小写字母、数字和一些 URL 安全字符
        code_verifier = "".join(secrets.choice(string.ascii_letters + string.digits + "-._~") for _ in range(128))
        # 对 code_verifier 进行 SHA-256 哈希处理
        digest = hashlib.sha256(code_verifier.encode()).digest()
        # 将哈希结果进行 base64-url 编码并去掉末尾的填充等号
        code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        # 返回 PKCEParameters 实例
        return cls(code_verifier=code_verifier, code_challenge=code_challenge)


# 定义一个 TokenStorage 协议类，用于描述令牌存储的接口规范
class TokenStorage(Protocol):
    """令牌存储实现的协议（接口）类。"""

    # 异步方法：获取已存储的访问令牌，返回 OAuthToken 或 None
    async def get_tokens(self) -> OAuthToken | None:
        """获取已存储的访问令牌。"""
        ...

    # 异步方法：保存访问令牌
    async def set_tokens(self, tokens: OAuthToken) -> None:
        """存储访问令牌。"""
        ...

    # 异步方法：获取客户端注册信息，返回 OAuthClientInformationFull 或 None
    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """获取已存储的客户端信息。"""
        ...

    # 异步方法：保存客户端注册信息
    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """存储客户端信息。"""
        ...


# 使用 dataclass 定义 OAuthContext 类，作为 OAuth2 流程的上下文信息容器
@dataclass
class OAuthContext:
    """OAuth 流程上下文对象。"""

    # OAuth 授权服务器的地址
    server_url: str
    # 客户端元数据（包含 client_name、redirect_uri 等）
    client_metadata: OAuthClientMetadata
    # 令牌存储器，遵循 TokenStorage 协议
    storage: TokenStorage
    # 重定向处理函数：接收重定向 URL，返回异步操作
    redirect_handler: Callable[[str], Awaitable[None]]
    # 回调处理函数：等待用户完成授权后获取 code 和 state
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]]
    # 超时时间（单位：秒），默认为 300 秒
    timeout: float = 300.0

    # ---------- 发现过程中的元数据（由 OAuth discovery 获取） ----------

    # 受保护资源的元数据
    protected_resource_metadata: ProtectedResourceMetadata | None = None
    # OAuth 服务端元数据
    oauth_metadata: OAuthMetadata | None = None
    # 授权服务器的 URL
    auth_server_url: str | None = None
    # 协议版本号
    protocol_version: str | None = None

    # ---------- 客户端注册信息（如注册时获取的 client_id 等） ----------

    # 客户端完整注册信息
    client_info: OAuthClientInformationFull | None = None

    # ---------- Token管理 ----------
    # 当前存储的访问Token
    current_tokens: OAuthToken | None = None
    # 当前Token的过期时间（Unix 时间戳）
    token_expiry_time: float | None = None

    # ---------- 状态控制 ----------

    # 用于并发访问控制的锁（例如多个协程同时操作令牌）
    lock: anyio.Lock = field(default_factory=anyio.Lock)

    # ---------- 用于回退机制的发现信息 ----------

    # 基础 URL，用于发现服务元数据的回退路径
    discovery_base_url: str | None = None
    # 路径部分，用于拼接最终元数据发现 URL
    discovery_pathname: str | None = None

    # ---------- 实用方法 ----------

    def get_authorization_base_url(self, server_url: str) -> str:
        """从 server_url 提取基础 URL（去掉路径部分）。"""
        parsed = urlparse(server_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def update_token_expiry(self, token: OAuthToken) -> None:
        """根据 token 中的 expires_in 更新本地的过期时间。"""
        if token.expires_in:
            self.token_expiry_time = time.time() + token.expires_in
        else:
            self.token_expiry_time = None

    def is_token_valid(self) -> bool:
        """判断当前的 token 是否有效。"""
        return bool(
            self.current_tokens
            and self.current_tokens.access_token
            and (not self.token_expiry_time or time.time() <= self.token_expiry_time)
        )

    def can_refresh_token(self) -> bool:
        """判断当前 token 是否支持刷新（refresh_token 存在并且客户端信息可用）。"""
        return bool(self.current_tokens and self.current_tokens.refresh_token and self.client_info)

    def clear_tokens(self) -> None:
        """清除当前存储的 token 和过期时间。"""
        self.current_tokens = None
        self.token_expiry_time = None

    def get_resource_url(self) -> str:
        """根据 RFC 8707 获取资源 URL。

        优先使用受保护资源元数据中的 resource 值，
        否则使用默认的 server_url 派生出来的 resource。
        """
        resource = resource_url_from_server_url(self.server_url)

        # 如果 PRM 中配置了资源，且是当前资源的合法父级，则使用它
        if self.protected_resource_metadata and self.protected_resource_metadata.resource:
            prm_resource = str(self.protected_resource_metadata.resource)
            if check_resource_allowed(requested_resource=resource, configured_resource=prm_resource):
                resource = prm_resource

        return resource

    def should_include_resource_param(self, protocol_version: str | None = None) -> bool:
        """判断是否应在 OAuth 请求中加入 resource 参数。

        返回 True 的条件：
        - 存在受保护资源元数据，或
        - 协议版本为 2025-06-18 或更高（支持 resource 参数）
        """
        # 有 PRM 元数据时，始终包含 resource 参数
        if self.protected_resource_metadata is not None:
            return True

        # 未指定协议版本时，不包含 resource 参数
        if not protocol_version:
            return False

        # 判断协议版本是否为 2025-06-18 或更高
        return protocol_version >= "2025-06-18"


class OAuthClientProvider(httpx.Auth):
    """
    用于 HTTPX 的 OAuth2 认证。
    实现了 OAuth 流程，支持自动客户端注册和令牌存储。
    """

    # httpx 认证要求读取响应体时设置为 True
    requires_response_body = True

    def __init__(
        self,
            server_url: str,  # OAuth 服务端地址
            client_metadata: OAuthClientMetadata,  # 客户端元信息，如 redirect_uri、scope 等
            storage: TokenStorage,  # 用于存储 token 和客户端信息的存储实现
            redirect_handler: Callable[[str], Awaitable[None]],  # 跳转处理器，接收重定向 URL
            callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],  # 授权回调处理器，返回 code 和 state
            timeout: float = 300.0,  # 请求流程的超时时间（默认 300 秒）
    ):
        """初始化 OAuth2 认证。"""
        # 创建上下文对象，存储认证所需的所有状态信息
        self.context = OAuthContext(
            server_url=server_url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=timeout,
        )
        self._initialized = False  # 标记是否初始化完成

    async def _discover_protected_resource(self) -> httpx.Request:
        """构建受保护资源元数据的发现请求。"""
        auth_base_url = self.context.get_authorization_base_url(self.context.server_url)
        url = urljoin(auth_base_url, "/.well-known/oauth-protected-resource")
        # 构造 HTTPX 请求对象，附加协议版本头
        return httpx.Request("GET", url, headers={MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION})

    async def _handle_protected_resource_response(self, response: httpx.Response) -> None:
        """处理受保护资源元数据响应。"""
        if response.status_code == 200:
            try:
                content = await response.aread()  # 异步读取响应体
                metadata = ProtectedResourceMetadata.model_validate_json(content)  # 校验并解析 JSON
                self.context.protected_resource_metadata = metadata  # 保存元数据
                # 如果有授权服务器信息，则保存其中第一个为当前的 auth_server_url
                if metadata.authorization_servers:
                    self.context.auth_server_url = str(metadata.authorization_servers[0])
            except ValidationError:
                pass  # 若解析失败则忽略

    def _build_well_known_path(self, pathname: str) -> str:
        """根据路径构建 OAuth 元数据发现的 well-known 路径。"""
        well_known_path = f"/.well-known/oauth-authorization-server{pathname}"
        if pathname.endswith("/"):
            # 如果路径末尾是斜杠，则去掉以避免 URL 中出现双斜杠
            well_known_path = well_known_path[:-1]
        return well_known_path

    def _should_attempt_fallback(self, response_status: int, pathname: str) -> bool:
        """判断是否应尝试回退到根路径的 metadata 发现方式。"""
        return response_status == 404 and pathname != "/"

    async def _try_metadata_discovery(self, url: str) -> httpx.Request:
        """构建指定 URL 的 OAuth 元数据发现请求。"""
        return httpx.Request("GET", url, headers={MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION})

    async def _discover_oauth_metadata(self) -> httpx.Request:
        """构建 OAuth 元数据发现请求，支持回退机制。"""
        if self.context.auth_server_url:
            auth_server_url = self.context.auth_server_url
        else:
            auth_server_url = self.context.server_url

        # 根据 RFC 8414，优先尝试路径感知的 well-known 发现方式
        parsed = urlparse(auth_server_url)
        well_known_path = self._build_well_known_path(parsed.path)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        url = urljoin(base_url, well_known_path)

        # 记录当前发现路径信息，供后续回退时使用
        self.context.discovery_base_url = base_url
        self.context.discovery_pathname = parsed.path

        return await self._try_metadata_discovery(url)

    async def _discover_oauth_metadata_fallback(self) -> httpx.Request:
        """构建 OAuth 元数据的回退发现请求，用于兼容旧服务器。"""
        base_url = getattr(self.context, "discovery_base_url", "")
        if not base_url:
            raise OAuthFlowError("没有可用的 base_url 进行元数据回退发现")

        # 构造根路径的 fallback 请求
        url = urljoin(base_url, "/.well-known/oauth-authorization-server")
        return await self._try_metadata_discovery(url)

    async def _handle_oauth_metadata_response(self, response: httpx.Response, is_fallback: bool = False) -> bool:
        """处理 OAuth 元数据响应。返回是否成功处理。"""
        if response.status_code == 200:
            try:
                content = await response.aread()  # 异步读取响应体
                metadata = OAuthMetadata.model_validate_json(content)  # 校验并反序列化
                self.context.oauth_metadata = metadata  # 保存 OAuth 元数据

                # 若客户端没有指定 scope，但服务器有提供 scopes_supported，则默认使用所有 scope
                if self.context.client_metadata.scope is None and metadata.scopes_supported is not None:
                    self.context.client_metadata.scope = " ".join(metadata.scopes_supported)
                return True
            except ValidationError:
                pass  # 若 JSON 校验失败，则忽略

        # 若不是回退阶段，且响应为 404，判断是否应尝试回退机制
        if not is_fallback and self._should_attempt_fallback(
            response.status_code, getattr(self.context, "discovery_pathname", "/")
        ):
            return False  # 表示需要回退

        return True  # 表示已成功或不需要回退（非 404）

    async def _register_client(self) -> httpx.Request | None:
        """构建客户端注册请求；如果已注册则跳过。"""
        if self.context.client_info:
            return None  # 已经有 client_info，跳过注册

        # 如果发现元数据中提供了 registration_endpoint，就使用它
        if self.context.oauth_metadata and self.context.oauth_metadata.registration_endpoint:
            registration_url = str(self.context.oauth_metadata.registration_endpoint)
        else:
            # 否则构造默认的 /register URL
            auth_base_url = self.context.get_authorization_base_url(self.context.server_url)
            registration_url = urljoin(auth_base_url, "/register")

        # 构建注册数据，转为 JSON 格式，排除 None
        registration_data = self.context.client_metadata.model_dump(by_alias=True, mode="json", exclude_none=True)

        # 构造 POST 请求
        return httpx.Request(
            "POST", registration_url, json=registration_data, headers={"Content-Type": "application/json"}
        )

    async def _handle_registration_response(self, response: httpx.Response) -> None:
        """处理客户端注册响应。"""
        if response.status_code not in (200, 201):
            raise OAuthRegistrationError(f"注册失败: {response.status_code} {response.text}")

        try:
            content = await response.aread()  # 异步读取响应体
            client_info = OAuthClientInformationFull.model_validate_json(content)  # 校验并反序列化
            self.context.client_info = client_info  # 保存到上下文中
            await self.context.storage.set_client_info(client_info)  # 持久化存储
        except ValidationError as e:
            raise OAuthRegistrationError(f"无效的注册响应: {e}")

    async def _perform_authorization(self) -> tuple[str, str]:
        """执行 OAuth 授权重定向并获取授权码（auth code）。"""
        if self.context.oauth_metadata and self.context.oauth_metadata.authorization_endpoint:
            auth_endpoint = str(self.context.oauth_metadata.authorization_endpoint)
        else:
            auth_base_url = self.context.get_authorization_base_url(self.context.server_url)
            auth_endpoint = urljoin(auth_base_url, "/authorize")

        if not self.context.client_info:
            raise OAuthFlowError("未找到客户端信息")

        # 生成 PKCE 参数（包含 code_verifier 和 code_challenge）
        pkce_params = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)  # 随机生成 state 防止 CSRF 攻击

        # 构造授权请求参数
        auth_params = {
            "response_type": "code",
            "client_id": self.context.client_info.client_id,
            "redirect_uri": str(self.context.client_metadata.redirect_uris[0]),
            "state": state,
            "code_challenge": pkce_params.code_challenge,
            "code_challenge_method": "S256",
        }

        # 如需支持资源参数（RFC 8707），则加入 resource 字段
        if self.context.should_include_resource_param(self.context.protocol_version):
            auth_params["resource"] = self.context.get_resource_url()

        # 如客户端设置了 scope，则添加 scope
        if self.context.client_metadata.scope:
            auth_params["scope"] = self.context.client_metadata.scope

        # 构造最终的授权 URL
        authorization_url = f"{auth_endpoint}?{urlencode(auth_params)}"
        await self.context.redirect_handler(authorization_url)  # 执行跳转（可能打开浏览器）

        # 等待用户回调（例如用户在浏览器中授权后回传 code）
        auth_code, returned_state = await self.context.callback_handler()

        # 检查返回的 state 是否一致（防止攻击）
        if returned_state is None or not secrets.compare_digest(returned_state, state):
            raise OAuthFlowError(f"状态参数不一致: {returned_state} != {state}")

        if not auth_code:
            raise OAuthFlowError("未接收到授权码")

        # 返回授权码和 code_verifier（用于后续换 token）
        return auth_code, pkce_params.code_verifier

    async def _exchange_token(self, auth_code: str, code_verifier: str) -> httpx.Request:
        """构建使用授权码换取访问令牌的请求。"""
        if not self.context.client_info:
            raise OAuthFlowError("缺少客户端信息")

        # 优先使用 metadata 中的 token_endpoint，否则 fallback
        if self.context.oauth_metadata and self.context.oauth_metadata.token_endpoint:
            token_url = str(self.context.oauth_metadata.token_endpoint)
        else:
            auth_base_url = self.context.get_authorization_base_url(self.context.server_url)
            token_url = urljoin(auth_base_url, "/token")

        # 构造表单数据
        token_data = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": str(self.context.client_metadata.redirect_uris[0]),
            "client_id": self.context.client_info.client_id,
            "code_verifier": code_verifier,
        }

        # 如需支持资源参数，则加入
        if self.context.should_include_resource_param(self.context.protocol_version):
            token_data["resource"] = self.context.get_resource_url()

        # 如果有 client_secret，则加入认证信息（机密客户端）
        if self.context.client_info.client_secret:
            token_data["client_secret"] = self.context.client_info.client_secret

        return httpx.Request(
            "POST", token_url, data=token_data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    async def _handle_token_response(self, response: httpx.Response) -> None:
        """处理从授权码交换来的 token 响应。"""
        if response.status_code != 200:
            raise OAuthTokenError(f"Token 交换失败: {response.status_code}")

        try:
            content = await response.aread()
            token_response = OAuthToken.model_validate_json(content)

            # 校验服务端返回的 scope 是否多给了权限
            if token_response.scope and self.context.client_metadata.scope:
                requested_scopes = set(self.context.client_metadata.scope.split())
                returned_scopes = set(token_response.scope.split())
                unauthorized_scopes = returned_scopes - requested_scopes
                if unauthorized_scopes:
                    raise OAuthTokenError(f"服务端返回了未授权的 scopes: {unauthorized_scopes}")

            # 保存 token 并更新过期时间
            self.context.current_tokens = token_response
            self.context.update_token_expiry(token_response)
            await self.context.storage.set_tokens(token_response)
        except ValidationError as e:
            raise OAuthTokenError(f"无效的 token 响应: {e}")

    async def _refresh_token(self) -> httpx.Request:
        """构建刷新访问令牌的请求。"""
        if not self.context.current_tokens or not self.context.current_tokens.refresh_token:
            raise OAuthTokenError("当前没有可用的 refresh token")

        if not self.context.client_info:
            raise OAuthTokenError("缺少客户端信息")

        if self.context.oauth_metadata and self.context.oauth_metadata.token_endpoint:
            token_url = str(self.context.oauth_metadata.token_endpoint)
        else:
            auth_base_url = self.context.get_authorization_base_url(self.context.server_url)
            token_url = urljoin(auth_base_url, "/token")

        refresh_data = {
            "grant_type": "refresh_token",
            "refresh_token": self.context.current_tokens.refresh_token,
            "client_id": self.context.client_info.client_id,
        }

        if self.context.should_include_resource_param(self.context.protocol_version):
            refresh_data["resource"] = self.context.get_resource_url()

        if self.context.client_info.client_secret:
            refresh_data["client_secret"] = self.context.client_info.client_secret

        return httpx.Request(
            "POST", token_url, data=refresh_data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        """处理刷新令牌的响应。如果成功返回 True，否则返回 False。"""
        if response.status_code != 200:
            logger.warning(f"Token refresh failed: {response.status_code}")
            self.context.clear_tokens()
            return False

        try:
            content = await response.aread()  # 异步读取响应体
            # 尝试将响应内容解析为 OAuthToken 对象
            token_response = OAuthToken.model_validate_json(content)

            # 更新上下文中的当前令牌和过期时间
            self.context.current_tokens = token_response
            self.context.update_token_expiry(token_response)
            # 将新的 token 保存到持久化存储中
            await self.context.storage.set_tokens(token_response)

            return True
        except ValidationError as e:
            logger.error(f"Invalid refresh response: {e}")
            self.context.clear_tokens()
            return False

    async def _initialize(self) -> None:
        """加载存储的 token 和 client 信息。"""
        self.context.current_tokens = await self.context.storage.get_tokens()  # 从存储中加载 token
        self.context.client_info = await self.context.storage.get_client_info()  # 从存储中加载 client 信息
        self._initialized = True  # 标记已初始化

    def _add_auth_header(self, request: httpx.Request) -> None:
        """如果当前 token 有效，则为请求添加 Authorization 头。"""
        if self.context.current_tokens and self.context.current_tokens.access_token:
            request.headers["Authorization"] = f"Bearer {self.context.current_tokens.access_token}"

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """HTTPX 的异步认证流程集成入口。"""
        async with self.context.lock:
            if not self._initialized:
                await self._initialize()  # 加载已有 token/client 信息

            # 捕获客户端协议版本信息
            self.context.protocol_version = request.headers.get(MCP_PROTOCOL_VERSION)

            # 如果当前没有有效的 token，则执行完整 OAuth 授权流程
            if not self.context.is_token_valid():
                try:
                    # 注意：由于 httpx 的认证流程是一个生成器，所有操作必须“内联”完成

                    # 第一步：发现受保护资源的元数据（符合 2025-06-18 规范）
                    discovery_request = await self._discover_protected_resource()
                    discovery_response = yield discovery_request
                    await self._handle_protected_resource_response(discovery_response)

                    # 第二步：发现 OAuth 授权服务器的元数据（包含授权端点、token端点等信息）
                    oauth_request = await self._discover_oauth_metadata()
                    oauth_response = yield oauth_request
                    handled = await self._handle_oauth_metadata_response(oauth_response, is_fallback=False)

                    # 如果路径感知发现失败（返回 404），尝试 fallback 到根路径的 well-known 地址
                    if not handled:
                        fallback_request = await self._discover_oauth_metadata_fallback()
                        fallback_response = yield fallback_request
                        await self._handle_oauth_metadata_response(fallback_response, is_fallback=True)

                    # 第三步：如果尚未注册客户端，则注册（动态客户端注册）
                    registration_request = await self._register_client()
                    if registration_request:
                        registration_response = yield registration_request
                        await self._handle_registration_response(registration_response)

                    # 第四步：执行授权流程（用户跳转到登录授权页面，回调拿到 code）
                    auth_code, code_verifier = await self._perform_authorization()

                    # 第五步：使用授权码和 PKCE 验证码换取 token
                    token_request = await self._exchange_token(auth_code, code_verifier)
                    token_response = yield token_request
                    await self._handle_token_response(token_response)
                except Exception as e:
                    logger.error(f"OAuth flow error: {e}")
                    raise

            # 走完授权流程或已有 token 后，给原始请求加上 Authorization 头
            self._add_auth_header(request)
            response = yield request  # 发出请求并接收响应

            # 如果服务器返回 401（未授权），且我们持有 refresh_token，可以尝试刷新 token
            if response.status_code == 401 and self.context.can_refresh_token():
                # 构造刷新请求
                refresh_request = await self._refresh_token()
                refresh_response = yield refresh_request

                if await self._handle_refresh_response(refresh_response):
                    # 使用新 token 重试原始请求
                    self._add_auth_header(request)
                    yield request
                else:
                    # 如果刷新失败，则必须重新执行 OAuth 授权流程
                    self._initialized = False  # 清除初始化标志（强制重载）

                    # 以下重复与上面一致：重新 discovery → register → authorize → exchange token

                    # 第一步：发现资源服务器元数据
                    discovery_request = await self._discover_protected_resource()
                    discovery_response = yield discovery_request
                    await self._handle_protected_resource_response(discovery_response)

                    # 第二步：发现 OAuth 元数据（并处理 fallback）
                    oauth_request = await self._discover_oauth_metadata()
                    oauth_response = yield oauth_request
                    handled = await self._handle_oauth_metadata_response(oauth_response, is_fallback=False)

                    if not handled:
                        fallback_request = await self._discover_oauth_metadata_fallback()
                        fallback_response = yield fallback_request
                        await self._handle_oauth_metadata_response(fallback_response, is_fallback=True)

                    # 第三步：注册客户端（如有必要）
                    registration_request = await self._register_client()
                    if registration_request:
                        registration_response = yield registration_request
                        await self._handle_registration_response(registration_response)

                    # 第四步：重新发起授权（用户再次登录）
                    auth_code, code_verifier = await self._perform_authorization()

                    # 第五步：使用新 code 换取新 token
                    token_request = await self._exchange_token(auth_code, code_verifier)
                    token_response = yield token_request
                    await self._handle_token_response(token_response)

                    # 用新 token 重试原始请求
                    self._add_auth_header(request)
                    yield request
