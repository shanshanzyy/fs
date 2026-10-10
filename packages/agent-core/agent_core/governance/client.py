"""治理面客户端（零第三方依赖）。

**所有请求都用短期访问令牌，且令牌过期时自动刷新一次后重试。**
这个「一次」是刻意的：不自动重试的话，用户会在令牌刚过期时看到一次
莫名其妙的失败；无限重试的话，一个坏掉的刷新令牌会变成死循环。

错误分类是这一层的主要价值：

| 场景 | 客户端该做什么 |
|---|---|
| 401 且能刷新 | 刷新后重试一次 |
| 401 且刷新失败 | 清凭证，提示重新登录 |
| 403 | 权限不足 —— **不要重试**，重试一万次结果一样 |
| 409 冲突（令牌被重用等） | 清凭证并提示，因为这通常意味着会话已被吊销 |
| 429 配额 | **降级到本地模型继续干活**，而不是把任务判死 |
| 连接失败 | 治理面不可达 —— **本地能力照常可用**，这一点必须做到 |

最后两条是设计上的核心：治理面是**增强**，不是单点依赖。
网关不可达时 Agent 必须还能用本地模型跑完任务。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .credentials import CredentialStore, Credentials

DEFAULT_TIMEOUT = 20
USER_AGENT = "vibe-agent-core/0.1"


# --------------------------------------------------------------------------- 错误
class GovernanceError(Exception):
    """治理面交互失败。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def retryable(self) -> bool:
        # 只把「服务端暂时不行」和「网络抖」当可重试。
        # 4xx 一律不可重试 —— 请求本身有问题，重试只是浪费时间并放大问题。
        return self.status == 0 or self.status >= 500 or self.status == 429


class NotEnrolled(GovernanceError):
    """本机还没有凭证。"""


class CredentialsRejected(GovernanceError):
    """凭证被服务端拒绝（已被吊销 / 令牌重用）。此时必须清掉本地凭证。"""


class QuotaExhausted(GovernanceError):
    """配额或限流。**这不是错误，是一种需要降级应对的状态。**"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None, retry_after: int = 0) -> None:
        super().__init__(message, status=429, code="quota_exceeded")
        self.details = details or {}
        self.retry_after = retry_after


class Unreachable(GovernanceError):
    """治理面不可达。调用方应降级到纯本地模式，而不是报错退出。"""


# --------------------------------------------------------------------------- 响应
@dataclass
class ApiResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str]

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


# --------------------------------------------------------------------------- 客户端
class GovernanceClient:
    def __init__(
        self,
        server: str,
        *,
        store: CredentialStore | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        credentials: Credentials | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.store = store or CredentialStore()
        self.credentials = credentials if credentials is not None else self.store.load()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ============================================================== 底层请求
    def _url(self, path: str) -> str:
        return self.server + ("/api/v1" + path if path.startswith("/") else path)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str = "",
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        _retry_auth: bool = True,
    ) -> ApiResponse:
        url = self._url(path)
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if headers:
            request_headers.update(headers)

        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                parsed = json.loads(raw) if raw.strip() else {}
                return ApiResponse(
                    status=int(response.status),
                    body=parsed if isinstance(parsed, dict) else {},
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            parsed = _safe_json(detail)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            message = ""
            code = ""
            details: dict[str, Any] = {}
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                code = str(error.get("code") or "")
                details = error.get("details") if isinstance(error.get("details"), dict) else {}

            if exc.code == 401 and _retry_auth and self.credentials and self.credentials.refresh_token:
                # 令牌过期是最常见的 401。刷新一次再试；刷新失败会在
                # refresh_session 里抛 CredentialsRejected，避免死循环。
                self.refresh_session()
                if self.credentials:
                    return self.request(
                        method,
                        path,
                        body=body,
                        token=self.credentials.access_token,
                        headers=headers,
                        query=query,
                        _retry_auth=False,
                    )

            if exc.code == 429:
                raise QuotaExhausted(
                    message or "配额已用尽",
                    details=details,
                    retry_after=_int_header(exc.headers.get("Retry-After")),
                ) from exc
            if exc.code == 401:
                raise CredentialsRejected(message or "凭证被拒绝", status=401, code=code or "unauthenticated") from exc
            if exc.code == 403:
                raise GovernanceError(message or "权限不足", status=403, code=code or "forbidden") from exc
            if exc.code == 404:
                raise GovernanceError(message or f"接口不存在: {path}", status=404, code=code or "not_found") from exc
            if exc.code == 409:
                raise CredentialsRejected(message or "冲突", status=409, code=code or "conflict") from exc
            raise GovernanceError(
                message or f"HTTP {exc.code}: {detail[:300]}", status=int(exc.code), code=code
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Unreachable(
                f"无法连接治理面 {self.server}：{reason}\n"
                "  本地模型与全部工具不受影响，可继续离线工作。",
                status=0,
                code="unreachable",
            ) from exc

    # ============================================================== 令牌
    def _require_token(self) -> str:
        if self.credentials is None or not self.credentials.access_token:
            raise NotEnrolled("本机尚未接入治理面，请先执行 enroll（注册令牌或账号口令）")
        return self.credentials.access_token

    def ensure_token(self) -> str:
        """返回一个可用令牌，必要时先刷新。"""
        credentials = self.credentials
        if credentials is None:
            raise NotEnrolled("本机尚未接入治理面")
        if credentials.is_expired() and credentials.refresh_token:
            self.refresh_session()
            credentials = self.credentials
        if credentials is None or not credentials.access_token:
            raise NotEnrolled("凭证不可用，请重新接入")
        return credentials.access_token

    def refresh_session(self) -> Credentials:
        if self.credentials is None or not self.credentials.refresh_token:
            raise NotEnrolled("没有刷新令牌，请重新接入治理面")
        try:
            # 这里**不带 Authorization**（访问令牌可能已过期），
            # 也不能走 request() 的自动刷新分支 —— 否则会无限递归。
            response = self.request(
                "POST",
                "/auth/refresh",
                body={"refreshToken": self.credentials.refresh_token},
                _retry_auth=False,
            )
        except CredentialsRejected:
            # 刷新令牌被拒（重用检测 / 已吊销 / 过期）→ 本地凭证已无意义。
            # 必须清掉，否则每次请求都会带着一个坏令牌去撞 401。
            self.store.clear()
            self.credentials = None
            raise

        self._absorb_session(response.body)
        return self.credentials  # type: ignore[return-value]

    def _absorb_session(self, payload: dict[str, Any]) -> None:
        previous = self.credentials or Credentials()
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        credentials = Credentials(
            server=self.server,
            user_id=str(user.get("id") or previous.user_id),
            email=str(user.get("email") or previous.email),
            device_id=str(payload.get("deviceId") or previous.device_id),
            access_token=str(payload.get("accessToken") or ""),
            refresh_token=str(payload.get("refreshToken") or previous.refresh_token),
            expires_at=int(payload.get("expiresAt") or 0),
            fingerprint=previous.fingerprint,
            obtained_at=_now(),
            audit_chain_head=previous.audit_chain_head,
            extra=previous.extra,
        )
        self.credentials = credentials
        self.store.save(credentials)

    # ============================================================== 接入
    def bootstrap_owner(self, *, email: str, password: str, display_name: str = "") -> Credentials:
        """从零建组织所有者并直接登录。

        只在服务端「一个用户都没有」时可用 —— 这是内网自助起步的路径，
        生产环境会改用 SSO（见服务端 README）。
        """
        self.request(
            "POST",
            "/auth/bootstrap",
            body={"email": email, "password": password, "displayName": display_name},
            _retry_auth=False,
        )
        return self.login(email=email, password=password)

    def login(
        self,
        *,
        email: str,
        password: str,
        device_name: str = "",
        platform: str = "",
    ) -> Credentials:
        fingerprint = self.store.fingerprint()
        response = self.request(
            "POST",
            "/auth/login",
            body={
                "email": email,
                "password": password,
                "deviceName": device_name or _platform_name(),
                "platform": platform or _platform(),
                "fingerprint": fingerprint,
            },
            _retry_auth=False,
        )
        self._absorb_session(response.body)
        if self.credentials is not None:
            self.credentials.fingerprint = fingerprint
            self.store.save(self.credentials)
        return self.credentials  # type: ignore[return-value]

    def register(
        self,
        *,
        enrollment_token: str,
        email: str,
        password: str,
        display_name: str = "",
        device_name: str = "",
    ) -> Credentials:
        fingerprint = self.store.fingerprint()
        response = self.request(
            "POST",
            "/auth/register",
            body={
                "enrollmentToken": enrollment_token,
                "email": email,
                "password": password,
                "displayName": display_name,
                "deviceName": device_name or _platform_name(),
                "platform": _platform(),
                "fingerprint": fingerprint,
            },
            _retry_auth=False,
        )
        self._absorb_session(response.body)
        if self.credentials is not None:
            self.credentials.fingerprint = fingerprint
            self.store.save(self.credentials)
        return self.credentials  # type: ignore[return-value]

    def logout(self, *, all_devices: bool = False) -> None:
        try:
            self.request(
                "POST",
                "/auth/logout",
                body={"refreshToken": self.credentials.refresh_token if self.credentials else "",
                      "allDevices": all_devices},
                token=self.credentials.access_token if self.credentials else "",
                _retry_auth=False,
            )
        except GovernanceError:
            pass  # 登出失败不该阻止本地清除凭证
        self.store.clear()
        self.credentials = None

    # ============================================================== 读取
    def me(self) -> dict[str, Any]:
        return self.request("GET", "/auth/me", token=self.ensure_token()).body

    def fetch_policy(self, *, team_id: str = "", etag: str = "") -> ApiResponse:
        """拉取生效策略。带上 ETag 得到 304 时可以省掉一次解析与合并。"""
        headers = {"If-None-Match": etag} if etag else None
        return self.request(
            "GET",
            "/policy",
            token=self.ensure_token(),
            headers=headers,
            query={"teamId": team_id} if team_id else None,
        )

    def fetch_quota(self, **query: str) -> dict[str, Any]:
        return self.request(
            "GET", "/quota", token=self.ensure_token(), query=query or None
        ).body

    def fetch_audit_schema(self) -> dict[str, Any]:
        """拿服务端接受的事件类型清单。

        客户端据此决定「哪些本地事件值得上传」—— 硬编码一份名单在客户端
        必然与服务端漂移，然后表现为「一批上传全被拒」。
        """
        return self.request("GET", "/audit/schema", token=self.ensure_token()).body

    def list_devices(self) -> list[dict[str, Any]]:
        body = self.request("GET", "/devices", token=self.ensure_token()).body
        devices = body.get("devices")
        return devices if isinstance(devices, list) else []

    def gateway_models(self) -> dict[str, Any]:
        return self.request("GET", "/gateway/models", token=self.ensure_token()).body

    # ============================================================== 上传
    def upload_audit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """上传一批审计事件。返回服务端的接收结果。

        调用方必须检查返回里的 `clientHeadMatch`：False 说明
        「客户端以为发了的东西」与「服务端收到的」不一致，
        此时**不应推进本地链头**，否则后续批次会一路错下去。
        """
        response = self.request(
            "POST",
            "/audit/events",
            body=envelope,
            token=self.ensure_token(),
        )
        return response.body

    # ============================================================== 网关
    def gateway_base_url(self) -> str:
        """网关的 OpenAI 兼容基址。

        客户端把模型请求指向这里，由服务端注入密钥并做路由 —— 于是
        客户端**永远不需要知道任何厂商密钥**（ADR-007）。
        """
        return f"{self.server}/api/v1/gateway/v1"

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/healthz", _retry_auth=False).body


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_header(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _now() -> int:
    import time

    return int(time.time())


def _platform() -> str:
    import sys

    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _platform_name() -> str:
    import platform

    return f"{platform.system()} {platform.release()}".strip()


__all__ = [
    "ApiResponse",
    "CredentialsRejected",
    "GovernanceClient",
    "GovernanceError",
    "NotEnrolled",
    "QuotaExhausted",
    "Unreachable",
]
