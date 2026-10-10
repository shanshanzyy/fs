"""本机 HTTP 服务的安全闸门。

桌面壳把 Core 暴露成了一个本机端口，这引入了 M0 阶段不存在的一类攻击面。
一个「只监听 127.0.0.1」的服务并不天然安全，主要威胁有三：

1. **DNS Rebinding** —— 用户用浏览器打开 evil.com，该页面的 JS 把域名解析到
   127.0.0.1，于是它就成了「同源」的本地页面，可以直接调用我们的接口。
   对策：校验 `Host` 头必须确实是 127.0.0.1/localhost，并拒绝任何带
   外部 `Origin` 的请求。这是本文件里最关键的一条。
2. **其他本机进程** —— 同机上的任意进程都能连 127.0.0.1。
   对策：每次启动生成随机 token，**所有 /api/* 请求**都要带
   （静态壳例外，见 _TOKEN_FREE_PATHS 的注释）。
3. **CSRF** —— 即便有 token，若攻击者页面能猜到或用上它（例如 token 出现在
   地址栏被 Referer 带出去），仍可能被利用。
   对策：不发任何 CORS 头（跨域读取默认被浏览器拦下）+ Origin 白名单双重保险。

设计原则是**默认拒绝**：任何一条校验过不了就直接 403，不区分「哪种」不合格
对外暴露细节。
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from http import HTTPStatus

# 允许的 Host 头取值模板（端口在实例化时填）
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}

# 不需要 token 的路径：健康探测是 Electron 主进程在拿到 token 之前要用的
_TOKEN_FREE_PATHS = frozenset({"/api/health"})


def _needs_token(path: str) -> bool:
    """只有 /api/* 需要令牌；静态壳（/ /styles.css /app.js …）不需要。

    为什么静态壳要放行：`<link>`/`<script>` 子资源请求**带不了令牌** ——
    它们不像 fetch 可以加请求头，HTML 里也没法把 token 写进每个资源引用
    （写进去就等于把令牌扩散到每一行 HTML）。若连样式表都要求令牌，
    浏览器加载 `/?token=…` 后，CSS/JS 全部 403，用户看到的是一页裸 HTML。

    这样做不损失安全性：静态壳里没有任何数据（数据全部经 /api/* 下发），
    而 DNS Rebinding / CSRF 的两道闸（Host + Origin 校验）对**所有**请求
    依然生效。令牌守的是数据，不是壳。
    """
    return path.startswith("/api") and path not in _TOKEN_FREE_PATHS

# 允许的 HTTP 方法。刻意不含 PUT/DELETE/PATCH —— 本契约不需要。
_ALLOWED_METHODS = frozenset({"GET", "POST", "OPTIONS"})


class Forbidden(Exception):
    """请求未通过安全闸门。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class HostGuard:
    """鉴权 + 来源校验。"""

    port: int
    token: str = field(default_factory=lambda: secrets.token_hex(24))
    # Electron 用 file:// 载入本地页面时 Origin 为 "null"；
    # 默认关掉，只有确实走 file:// 方案时才打开。
    allow_null_origin: bool = False

    # ------------------------------------------------------------------ 校验
    def allowed_origins(self) -> set[str]:
        origins = set()
        for host in ("127.0.0.1", "localhost"):
            origins.add(f"http://{host}:{self.port}")
        return origins

    def check(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        """任一校验失败即抛 Forbidden。

        headers 的键名大小写**由本方法自行归一**，调用方不必操心。
        这一点很重要：HTTP 头名按规范是大小写不敏感的，但不同客户端发出来的
        大小写并不一致（Chrome 发 `Host`，Node 的 fetch 发 `host`）。
        曾经因为调用方用普通 dict 装着原始大小写，导致 Node 发出的请求
        全被判成「缺少 Host 头」—— 这类 bug 只在特定客户端上才暴露，
        放在这里归一化可以从根上避免。
        """
        normalized = {str(k).lower(): v for k, v in headers.items()}

        if method.upper() not in _ALLOWED_METHODS:
            raise Forbidden(HTTPStatus.METHOD_NOT_ALLOWED, f"不支持的方法: {method}")

        self._check_host(normalized)
        self._check_origin(normalized)

        if method.upper() == "OPTIONS":
            return  # 预检请求不带 token，来源校验已足够

        if _needs_token(path):
            self._check_token(normalized, query)

    # ------------------------------------------------------------------ 分项
    def _check_host(self, headers: dict[str, str]) -> None:
        """Host 头必须是本机。挡住 DNS Rebinding 的核心一步。"""
        raw = (headers.get("host") or "").strip().lower()
        if not raw:
            raise Forbidden(HTTPStatus.FORBIDDEN, "缺少 Host 头")

        hostname = raw
        if raw.startswith("["):  # IPv6 字面量
            hostname = raw.split("]", 1)[0] + "]"
        elif ":" in raw:
            hostname = raw.rsplit(":", 1)[0]

        if hostname not in _ALLOWED_HOSTNAMES:
            raise Forbidden(
                HTTPStatus.FORBIDDEN,
                f"Host 不是本机地址（{hostname}）。本服务只接受本机访问。",
            )

        # 端口也必须对上，防止把请求转发到别的本机服务造成混淆
        if ":" in raw and not raw.startswith("["):
            port_part = raw.rsplit(":", 1)[1]
            if port_part.isdigit() and int(port_part) != self.port:
                raise Forbidden(HTTPStatus.FORBIDDEN, "Host 端口与监听端口不一致")

    def _check_origin(self, headers: dict[str, str]) -> None:
        """Origin 白名单。缺失 Origin 的同源导航/curl 放行，存在但不认识就拒。"""
        origin = (headers.get("origin") or "").strip()
        if not origin:
            return
        if origin in self.allowed_origins():
            return
        if self.allow_null_origin and origin in ("null", "file://"):
            return
        raise Forbidden(
            HTTPStatus.FORBIDDEN,
            f"来源 {origin!r} 不在白名单内，已拒绝（防 DNS Rebinding / CSRF）。",
        )

    def _check_token(
        self, headers: dict[str, str], query: dict[str, list[str]]
    ) -> None:
        """Token 校验。

        SSE（EventSource）无法自定义请求头，因此 `/api/events` 允许用查询参数携带
        token —— 这是 EventSource 的先天限制，不是疏忽。其余接口一律走请求头，
        避免 token 出现在 URL 里被日志或 Referer 带出去。
        """
        supplied = headers.get("x-vibe-token") or ""
        if not supplied:
            values = query.get("token") or []
            supplied = values[0] if values else ""

        if not supplied or not hmac.compare_digest(supplied, self.token):
            raise Forbidden(HTTPStatus.FORBIDDEN, "缺少或错误的访问令牌")

    # ------------------------------------------------------------------ 展示
    @property
    def launch_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"
