"""HTTP 层：极简路由 + 统一错误映射 + 本机安全头。

为什么还是手写：这一层需要的能力是「几十个 JSON 接口 + 统一鉴权 + 统一错误」，
标准库完全够。换成 FastAPI 会同时带来「要 pip 装」和「离线装不上就起不来」，
而 M2 的部署环境正是内网离线。真正的收益（OpenAPI 文档、依赖注入）
在这个规模上体现不出来。

三个刻意的设计：

1. **错误一律映射成结构化 JSON，且不泄漏内部细节。** 未预期异常只回
   `requestId`，堆栈进服务端日志 —— 排查靠 requestId 关联，而不是把
   内部结构暴露给调用方。
2. **不发任何 CORS 头。** 治理面只有客户端程序调用，浏览器不该能读它。
   默认拒绝跨域读取，比维护一份白名单更安全。
3. **`X-Request-Id` 贯穿始终。** 审计与排查都靠它把「客户端看到的那次失败」
   与「服务端日志里的那条记录」对上，而不用把请求内容记下来。
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import ApiError

log = logging.getLogger("vibe.governance")

MAX_BODY_BYTES = 8 << 20      # 8 MiB：审计批次可能一次带上千条
MAX_QUERY_KEYS = 32

# 认证模式
AUTH_PUBLIC = "public"        # 无需认证
AUTH_OPTIONAL = "optional"    # 有则解析，无则放行
AUTH_REQUIRED = "required"    # 必须认证


# --------------------------------------------------------------------------- 请求
@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]        # 键名已归一为小写
    body: bytes
    remote: str
    app: Any = None
    request_id: str = ""
    actor: Any = None
    route_permissions: tuple[str, ...] = ()
    params: dict[str, str] = field(default_factory=dict)

    # --- 便捷读取 ---
    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "bad_json", f"请求体不是合法 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "请求体必须是 JSON 对象")
        return data

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return values[0] if values else default

    def q_int(self, name: str, default: int = 0) -> int:
        raw = self.q(name, "")
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "bad_param", f"参数 {name} 需要整数，收到 {raw!r}"
            ) from exc

    def bearer(self) -> str:
        raw = self.headers.get("authorization") or ""
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return ""


@dataclass
class Response:
    status: int = HTTPStatus.OK
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None
    content_type: str = "application/json; charset=utf-8"
    # 流式响应（M3）：给出「字节块迭代器」时按 chunked 传输逐块写出，
    # payload/raw 被忽略。生成器耗尽前连接保持打开。
    stream: Any = None
    # 流没被消费时的兜底回调（见 StreamSession.abort）。
    #
    # 为什么必须有它：生成器只有「被迭代」或「被 close」才会走到 finally。
    # 若客户端在响应头发出前就断开，生成器根本没启动过 —— 对它调 close()
    # 只是把它标记为结束，**不会执行 finally**。预留就永远挂在账上，
    # 表现为「配额莫名少了但没有任何请求记录」。所以这里给一个
    # 不依赖生成器生命周期的兜底出口。
    stream_abort: Any = None

    @classmethod
    def json(cls, payload: Any, status: int = HTTPStatus.OK, **headers: str) -> "Response":
        return cls(status=status, payload=payload, headers=headers)

    @classmethod
    def text(cls, body: str, status: int = HTTPStatus.OK, **headers: str) -> "Response":
        return cls(
            status=status,
            raw=body.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            headers=headers,
        )


HandlerFn = Callable[..., Response]
Authenticator = Callable[[str, Request], Any]


# --------------------------------------------------------------------------- 路由
_PARAM_RE = re.compile(r"^\{(\w+)\}$")


@dataclass
class Route:
    method: str
    template: str
    handler: HandlerFn
    auth: str = AUTH_REQUIRED
    permissions: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()

    def matches(self, method: str, segments: list[str]) -> dict[str, str] | None:
        if self.method != method or len(self.segments) != len(segments):
            return None
        params: dict[str, str] = {}
        for template_seg, actual in zip(self.segments, segments):
            m = _PARAM_RE.match(template_seg)
            if m:
                params[m.group(1)] = urllib.parse.unquote(actual)
            elif template_seg != actual:
                return None
        return params


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(
        self,
        method: str,
        template: str,
        handler: HandlerFn,
        *,
        auth: str = AUTH_REQUIRED,
        permissions: tuple[str, ...] = (),
    ) -> None:
        route = Route(
            method=method.upper(),
            template=template,
            handler=handler,
            auth=auth,
            permissions=permissions,
            segments=tuple(s for s in template.strip("/").split("/") if s),
        )
        self._routes.append(route)

    def get(self, template: str, **kw: Any) -> Callable[[HandlerFn], HandlerFn]:
        return self._decorator("GET", template, kw)

    def post(self, template: str, **kw: Any) -> Callable[[HandlerFn], HandlerFn]:
        return self._decorator("POST", template, kw)

    def _decorator(self, method: str, template: str, kw: dict[str, Any]) -> Callable:
        def wrapper(fn: HandlerFn) -> HandlerFn:
            self.add(method, template, fn, **kw)
            return fn

        return wrapper

    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        segments = [s for s in path.strip("/").split("/") if s]
        for route in self._routes:
            params = route.matches(method, segments)
            if params is not None:
                return route, params
        return None

    def allow_header(self, path: str) -> str:
        segments = [s for s in path.strip("/").split("/") if s]
        methods = sorted(
            {r.method for r in self._routes if r.matches(r.method, segments) is not None}
        )
        return ", ".join(methods + ["OPTIONS"]) if methods else ""

    @property
    def routes(self) -> list[Route]:
        return list(self._routes)


# --------------------------------------------------------------------------- 服务
class AppContext:
    """路由处理函数能拿到的一切。

    刻意做成「一个朴素对象」而不是依赖注入容器：这一层的组件数量是十来级，
    注册表式的 DI 只会让「谁依赖谁」变模糊。
    """

    def __init__(
        self,
        *,
        config: Any,
        store: Any,
        clock: Any,
        router: Router,
        authenticator: Authenticator,
    ) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.router = router
        self.authenticate = authenticator
        # 以下由 app.py 装配后赋值。显式声明成 None 而不是靠 setattr 挂上，
        # 是为了「漏装配」能在第一次访问时以 AttributeError 立刻暴露，
        # 而不是变成某个接口悄悄返回空。
        self.auth_service: Any = None
        self.gateway: Any = None
        self.audit_service: Any = None
        self.policy_service: Any = None
        self.quota_service: Any = None
        self.secrets: Any = None
        self.oidc: Any = None
        self.oidc_flows: Any = None
        self.audit_recorder: Any = None
        self.secret_rotation_report: Any = None
        self.version: str = __import__("vibe_server", fromlist=["__version__"]).__version__
        # 用注入的时钟而不是 time.time()：`/healthz` 与 `/metrics` 都用
        # `clock.now() - started_at` 算 uptime，起点若来自另一个时间源，
        # 换了时钟（测试、或运维注入的时间服务）就会算出负数。
        self.started_at = int(clock.now())
        # 进程内指标（M4）：/metrics 的数据源。没有它就只能靠翻日志回答
        # 「错误率多少、慢在哪个接口」，而那正是监控系统要问的问题。
        from .metrics import Metrics

        self.metrics = Metrics()

    def require(self, actor: Any, permission: str) -> None:
        from .rbac import require_permission

        require_permission(actor, permission)


class VibeHandler(BaseHTTPRequestHandler):
    server_version = "VibeGovernance"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> AppContext:
        return self.server.app  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ 分发
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def handle_one_request(self) -> None:
        # 客户端提前断开是常态（超时、取消、批量上传中途退出），不该刷 traceback
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 自行记录结构化访问日志，见 _access_log

    def _dispatch(self, method: str) -> None:
        app = self.app
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        try:
            if len(query) > MAX_QUERY_KEYS:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "too_many_params", "查询参数过多"
                )
            response = self._route_request(app, method, path, query, request_id)
        except ApiError as exc:
            response = Response.json(exc.to_body(), status=exc.status, **exc.headers)
        except Exception:  # noqa: BLE001 — 服务端不能因单个请求崩掉
            # 故意不回堆栈：内部结构不该出现在客户端
            log.error(
                "unhandled error requestId=%s path=%s\n%s",
                request_id,
                path,
                traceback.format_exc(),
            )
            response = Response.json(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "服务端内部错误",
                        "requestId": request_id,
                    }
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        self._send(app, response, request_id)
        self._access_log(app, method, path, response.status, elapsed_ms, request_id)
        # 指标在响应发出后记：这一步不该影响请求本身的成败，
        # 所以放在最后，且它自己不抛异常（纯内存计数）。
        app.metrics.observe(
            method=method,
            template=self._route_template(app, method, path),
            status=response.status,
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _route_template(app: AppContext, method: str, path: str) -> str:
        """把实际路径映射回路由模板，避免标签基数被路径参数撑爆。

        解析失败的（404/405）统一归到空串，由 metrics 归到 `(unmatched)`。
        """
        matched = app.router.resolve(method, path)
        if matched is not None:
            return matched[0].template
        return ""

    def _route_request(
        self,
        app: AppContext,
        method: str,
        path: str,
        query: dict[str, list[str]],
        request_id: str,
    ) -> Response:
        if method == "OPTIONS":
            allow = app.router.allow_header(path)
            return Response.json({}, status=HTTPStatus.NO_CONTENT, Allow=allow)

        matched = app.router.resolve(method, path)
        if matched is None:
            matched_any = app.router.resolve("GET", path) or app.router.resolve("POST", path)
            if matched_any is not None:
                raise ApiError(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    f"{method} 不适用于该路径",
                    headers={"Allow": app.router.allow_header(path)},
                )
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", f"未知接口 {path}")

        route, params = matched

        request = Request(
            method=method,
            path=path,
            query=query,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=self._read_body(),
            remote=self._client_ip(app),
            app=app,
            request_id=request_id,
            params=params,
            route_permissions=route.permissions,
        )

        # 强制 TLS：健康检查（探针无法配 TLS/令牌）豁免，其余接口一律拒绝明文。
        # 代码与注释必须一致 —— 之前注释宣称豁免而实现没有，
        # 导致 require_tls=1 时 K8s readiness（明文 http /healthz）被 403。
        if (
            app.config.require_tls
            and route.template != "/healthz"
            and not self._is_secure(app)
        ):
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "tls_required",
                "本服务要求 TLS 连接。请通过 https 访问，"
                "或在前面架一个做 TLS 终止的反向代理（并设置 TRUST_FORWARDED_FOR=1）。",
            )

        if route.auth != AUTH_PUBLIC:
            token = request.bearer()
            if token:
                request.actor = app.authenticate(token, request)
            elif route.auth == AUTH_REQUIRED:
                from .errors import Unauthenticated

                raise Unauthenticated()

        # 路由级权限在这里集中校验，而不是交给每个 handler。
        # 理由很实际：handler 有几十个，靠"记得写上"一定会漏一个，
        # 而漏掉的那个就是越权入口。声明在路由上，忘记的概率降到零。
        if route.permissions:
            if request.actor is None:
                from .errors import Unauthenticated

                raise Unauthenticated()
            for permission in route.permissions:
                if not request.actor.can(permission):
                    from .errors import Forbidden

                    raise Forbidden(f"缺少权限 {permission}")

        return route.handler(request, **params)

    # ------------------------------------------------------------------ 工具
    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            # 不支持 chunked 请求体：客户端明确声明长度，避免「读不到头」的隐患
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "chunked_unsupported", "不支持分块请求体"
                )
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "bad_length", "Content-Length 不是整数"
            ) from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_length", "Content-Length 为负")
        if length > MAX_BODY_BYTES:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                f"请求体超过上限 {MAX_BODY_BYTES // (1 << 20)} MiB",
            )
        return self.rfile.read(length) if length else b""

    def _client_ip(self, app: AppContext) -> str:
        if app.config.trust_forwarded_for:
            forwarded = self.headers.get("X-Forwarded-For") or ""
            if forwarded:
                # 只取第一段（最靠近客户端的那个），且限制长度，避免被塞长串
                return forwarded.split(",")[0].strip()[:64]
        return (self.client_address[0] if self.client_address else "")[:64]

    def _is_secure(self, app: AppContext) -> bool:
        # 只有在明确信任前置代理（TRUST_FORWARDED_FOR=1）时才采信该头。
        # 否则任何直连客户端都能自带 X-Forwarded-Proto: https，
        # 绕过 require_tls 的明文拒绝。
        if app.config.trust_forwarded_for:
            if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
                return True
        return isinstance(self.connection, __import__("ssl").SSLSocket)

    # ------------------------------------------------------------------ 响应
    def _send(self, app: AppContext, response: Response, request_id: str) -> None:
        if response.stream is not None:
            self._send_stream(app, response, request_id)
            return
        if response.raw is not None:
            body = response.raw
        elif response.status == HTTPStatus.NO_CONTENT:
            body = b""
        else:
            body = json.dumps(
                response.payload, ensure_ascii=False, default=str
            ).encode("utf-8")

        try:
            self.send_response(response.status)
            if body:
                self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Request-Id", request_id)
            if app.config.require_tls:
                self.send_header(
                    "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                )
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_stream(self, app: AppContext, response: Response, request_id: str) -> None:
        """chunked 逐块写出。

        用 chunked 而不是「无 Content-Length + 连接关闭」分界：中间若挂了反代，
        关闭分界会被当成网关崩溃，chunked 不会。
        生成器中途抛异常时无法再补终止块 —— 只能断开连接，
        客户端把「流被截断」当作错误处理（这正是想要的语义）。
        """
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Transfer-Encoding", "chunked")
            # 头一旦发出，连接就交给了 stream；之后的失败由下面的 finally 收尾
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-Id", request_id)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            for chunk in response.stream:
                if not chunk:
                    continue
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()  # SSE 要立刻推给客户端，攒批次就失去流式意义
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            # 无论「头都没发出去」还是「写到一半断了」，都要给上游一个
            # 确定的收尾：没结算就释放预留（abort 幂等，已结算则不动）。
            abort = getattr(response, "stream_abort", None)
            if callable(abort):
                try:
                    abort()
                except Exception:  # noqa: BLE001 —— 收尾失败不该掩盖原始异常
                    pass

    def _access_log(
        self,
        app: AppContext,
        method: str,
        path: str,
        status: int,
        elapsed_ms: float,
        request_id: str,
    ) -> None:
        """结构化访问日志。

        **刻意不带查询串与请求体** —— 治理面的请求里会出现邮箱等标识信息，
        日志是最容易泄漏的一环。要做合规排查请走审计接口，那里有 RBAC。
        """
        if not getattr(app.config, "access_log", True):
            return
        log.info(
            "%s %s -> %s %.0fms rid=%s ip=%s",
            method,
            path,
            status,
            elapsed_ms,
            request_id,
            self._client_ip(app),
        )


class VibeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: AppContext) -> None:
        self.app = app
        super().__init__(address, VibeHandler)


class GovernanceServer:
    """生命周期封装，便于 CLI 与测试复用。"""

    def __init__(self, app: AppContext, *, host: str, port: int) -> None:
        self.app = app
        self._server = VibeHTTPServer((host, port), app)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def address(self) -> str:
        return f"{self._server.server_address[0]}:{self.port}"

    @property
    def base_url(self) -> str:
        host = self._server.server_address[0]
        scheme = "https" if self.app.config.require_tls else "http"
        return f"{scheme}://{host}:{self.port}"

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.3)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def start_background(self) -> Any:
        import threading

        thread = threading.Thread(target=self.serve_forever, name="vibe-governance", daemon=True)
        thread.start()
        return thread


__all__ = [
    "AUTH_OPTIONAL",
    "AUTH_PUBLIC",
    "AUTH_REQUIRED",
    "AppContext",
    "GovernanceServer",
    "Request",
    "Response",
    "Route",
    "Router",
]
