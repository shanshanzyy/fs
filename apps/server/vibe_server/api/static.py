"""极简静态页服务（Q 阶段）：服务端唯一的 HTML 出口。

这个模块存在的唯一理由：服务端账号 API 早已就绪，但界面用户**没有任何登录入口**
（登录只能靠 CLI）。补一个登录页是最小闭环。

三条刻意约束：

1. **零第三方依赖，不引模板引擎。** `GET /login` 读 `static/login.html` 原样返回，
   `GET /assets/<name>` 读同目录的静态资源。这一层需要的只是「读文件 + 定 MIME + 加安全头」，
   标准库完全够；引模板引擎会把「离线装不上就起不来」这条风险重新引进来（同 httpd.py 的取舍）。
2. **安全头。** 静态页只承载认证 UI，因此把网络出口锁死在同源（见 `CSP`）。
   `Referrer-Policy: no-referrer` 不必在这里再写一遍 —— `httpd._send()` 对**每个**响应
   都强制带上它（连同 `X-Content-Type-Options` / `Cache-Control: no-store`）。
   放在统一出口比放在每个 handler 更可靠：handler 会漏，统一出口不会。这里只补 CSP。
3. **不改变「服务端不存代码」的红线（ADR-006）。** 这里服务的是应用自带的登录页文件，
   与用户代码 / prompt / 模型输出无关；目录固定、只读、经路径校验。

与 host 静态壳的分工：host 静态壳服务的是**应用自己的 UI**（桌面端界面）；
这里服务的是**治理面登录页**，两者口径一致（同源锁死 + 禁表单外发），但目录各自独立。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import Forbidden, NotFound
from ..httpd import AUTH_PUBLIC, Request, Response, Router

#: 登录页与静态资源所在目录（本模块同级的 `static/`）。
STATIC_DIR = Path(__file__).parent / "static"

#: 治理面控制台（M8）资源目录。与登录页同目录下的子目录 ——
#: 分开放是因为 Router 只匹配**定长单段** `{name}`：控制台要按
#: `/console/assets/<name>` 走单段路由，只能把资源收进一个独立子目录。
CONSOLE_DIR = STATIC_DIR / "console"

#: 登录页的内容安全策略。
#: 口径与 host 静态壳（agent_core/host/server.py 的 CSP）一致：默认全部锁回同源，
#: `connect-src 'self'` 覆盖 fetch（登录页正是用 fetch 提交凭据），
#: `form-action 'none'` 让任何原生表单提交都无处可去（真正的提交走 JS + fetch）。
#: 唯一的收紧：登录页不需要嵌任何 frame，`frame-src` 直接 'none'。
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def _read(path: Path, content_type: str) -> Response:
    try:
        body = path.read_bytes()
    except OSError as exc:
        # 打包/部署漏了 static/ 目录会走到这里 —— 报 404 而不是 500，
        # 因为「文件不在这台机器上」对调用方而言等价于「这个页面不存在」。
        raise NotFound(f"静态资源不可用：{exc}") from exc
    return Response(
        status=200,
        raw=body,
        content_type=content_type,
        headers={"Content-Security-Policy": CSP},
    )


def _serve_asset(base_dir: Path, name: str) -> Response:
    """在 `base_dir` 下安全地读一个静态资源。

    路径穿越防护三道一起上（登录页与控制台共用同一份实现，避免各写一遍漏一处）：
      1. 拒绝空名 / 含分隔符 / 含 `..`——Router 匹配 `{name}` 时会 `unquote`，
         所以 `%2e%2e%2f…` 这类编码过的穿越也会在这里现出原形；
      2. `resolve()` 后必须仍在 `base_dir` 内（兜底符号链接与边界情况）；
      3. 必须是文件。
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise Forbidden("非法静态资源路径")
    target = (base_dir / name).resolve()
    base = base_dir.resolve()
    if target != base and base not in target.parents:
        raise Forbidden("非法静态资源路径")
    if not target.is_file():
        raise NotFound("资源不存在")
    content_type = _MIME.get(target.suffix.lower(), "application/octet-stream")
    return _read(target, content_type)


def register_static(router: Router) -> None:
    """注册静态路由。

    **公开、不需 token** —— 登录页的用途正是「还没有 token 的人」打开它。
    与既有 `/api/v1/auth/*` 无冲突：那批在 `/api/v1` 前缀下，这几个是顶层路径。
    控制台外壳同样公开：它只是一个空壳，真正的数据请求都带 Bearer 令牌，
    由服务端强制鉴权（前端隐藏权限面板只是体验，闸门始终在路由上）。
    """

    @router.get("/login", auth=AUTH_PUBLIC)
    def login_page(request: Request) -> Response:
        return _read(STATIC_DIR / "login.html", _MIME[".html"])

    @router.get("/assets/{name}", auth=AUTH_PUBLIC)
    def asset(request: Request, name: str) -> Response:
        return _serve_asset(STATIC_DIR, name)

    @router.get("/console", auth=AUTH_PUBLIC)
    def console_page(request: Request) -> Response:
        return _read(CONSOLE_DIR / "console.html", _MIME[".html"])

    @router.get("/console/assets/{name}", auth=AUTH_PUBLIC)
    def console_asset(request: Request, name: str) -> Response:
        return _serve_asset(CONSOLE_DIR, name)


__all__ = ["CONSOLE_DIR", "CSP", "STATIC_DIR", "register_static"]