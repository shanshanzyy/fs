"""统一异常。

所有可预期的失败都走 ApiError，由 HTTP 层统一映射成 JSON 响应。
未预期的异常一律 500 且**不把堆栈暴露给客户端**（只写服务端日志）。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from . import __version__


class ApiError(Exception):
    """可预期的接口错误。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return body


# --------------------------------------------------------------------------- 认证
class Unauthenticated(ApiError):
    def __init__(self, message: str = "未认证或凭证已失效") -> None:
        super().__init__(
            HTTPStatus.UNAUTHORIZED,
            "unauthenticated",
            message,
            # 明确告知这是 Bearer 认证，避免客户端去猜
            headers={"WWW-Authenticate": 'Bearer realm="vibe-governance"'},
        )


class Forbidden(ApiError):
    def __init__(self, message: str = "无权执行该操作", *, details: Any = None) -> None:
        super().__init__(HTTPStatus.FORBIDDEN, "forbidden", message, details=details)


class NotFound(ApiError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(HTTPStatus.NOT_FOUND, "not_found", message)


class Conflict(ApiError):
    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(HTTPStatus.CONFLICT, "conflict", message, details=details)


class BadRequest(ApiError):
    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(HTTPStatus.BAD_REQUEST, "bad_request", message, details=details)


class QuotaExceeded(ApiError):
    """配额用尽。

    刻意带上 429 而不是 403 —— 客户端要能区分「不许你做」和「今天额度没了」，
    后者的正确处理是降级到本地模型继续干活，而不是把任务判死。
    """

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(
            HTTPStatus.TOO_MANY_REQUESTS,
            "quota_exceeded",
            message,
            details=details,
            headers={"Retry-After": "60"},
        )


# --------------------------------------------------------------------------- 配置
class ConfigError(Exception):
    """服务端启动配置有问题，直接退出，不带着错误配置继续跑。"""


# --------------------------------------------------------------------------- 存储
class StoreError(Exception):
    """存储层不可用。"""


class UnsupportedStore(ApiError):
    """存储驱动未实现（例如配了 postgres:// 但没装驱动）。"""

    def __init__(self, message: str) -> None:
        super().__init__(HTTPStatus.SERVICE_UNAVAILABLE, "store_unavailable", message)


__all__ = [
    "ApiError",
    "BadRequest",
    "ConfigError",
    "Conflict",
    "Forbidden",
    "NotFound",
    "QuotaExceeded",
    "StoreError",
    "Unauthenticated",
    "UnsupportedStore",
    "__version__",
]
