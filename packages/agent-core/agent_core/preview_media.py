"""预览面共享的媒体口径：MIME 白名单、大小上限、可草稿的扩展名。

为什么单独一份：这份清单至少有**两个**消费方 —— `host/server.py` 的
`GET /preview/…` 与 `GET /preview-draft/…`，以及工具层的草稿预演
（判断「这份改动值不值得生成草稿」）。按项目约定，跨模块共用的清单必须
**导入同一份**，两处各写一遍必然漂移：某天给预览加了 `.mjs`，草稿却仍旧
拒绝它，用户看到的现象是「HTML 里引的模块加载不出来」，排查成本极高。

放在包顶层而不是 `host/`：`host/__init__.py` 会拉起 `server` → `session`
→ `tools`，工具层若反向导入 `host.preview_media` 就成环了。本模块**不导入
任何项目内模块**，两边都能安全地静态导入。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

# 预览（O1）允许的 MIME 白名单 —— 只放**文本/web 资源**。
# 刻意不含 .exe/.dll/.zip 等可执行或打包格式：预览是「读渲染」，不是分发二进制。
PREVIEW_MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".xhtml": "application/xhtml+xml; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".cjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
}

# 单次预览响应的字节上限（8 MiB）。草稿与静态预览共用同一个上限：
# 两者走的是同一个 iframe，不该出现「原文件能看、草稿打不开」的落差。
PREVIEW_MAX_BYTES = 8 << 20

# 「值得为它生成草稿」的扩展名：浏览器能直接渲染的文档，加上文档会去取的
# 文本类子资源（CSS / JS / JSON / SVG）。二进制（png/woff）不在此列 ——
# 它们的「落盘后内容」无法用文本表达，草稿只会是一堆乱码。
DRAFT_SUFFIXES = frozenset(
    {
        ".html",
        ".htm",
        ".xhtml",
        ".css",
        ".js",
        ".mjs",
        ".cjs",
        ".json",
        ".map",
        ".xml",
        ".svg",
        ".txt",
        ".md",
    }
)

# 草稿主文档的优先级：同一个事务改了多个文件时（如 index.html + app.css），
# 预览标签该落到哪一份。HTML 在前 —— 用户要「看效果」，不是「看 CSS 源码」。
DRAFT_PRIMARY_ORDER = (".html", ".htm", ".xhtml", ".svg", ".md", ".txt")


def mime_for(path: str) -> str | None:
    """按扩展名取预览 MIME；不在白名单内返回 None（调用方据此 403）。"""
    lowered = str(path).lower()
    dot = lowered.rfind(".")
    if dot < 0:
        return None
    return PREVIEW_MIME.get(lowered[dot:])


def is_previewable(path: str) -> bool:
    """这份文件能否被 `GET /preview/…` 服务（白名单口径）。"""
    return mime_for(path) is not None


def is_draftable(path: str) -> bool:
    """这份改动值不值得生成草稿（只认浏览器的文本类资源）。"""
    lowered = str(path).lower()
    dot = lowered.rfind(".")
    if dot < 0:
        return False
    return lowered[dot:] in DRAFT_SUFFIXES


def draft_rank(path: str) -> tuple[int, str]:
    """草稿主文档的排序键：HTML 优先，其次按扩展名固定次序。"""
    lowered = str(path).lower()
    dot = lowered.rfind(".")
    suffix = lowered[dot:] if dot >= 0 else ""
    try:
        index = DRAFT_PRIMARY_ORDER.index(suffix)
    except ValueError:
        index = len(DRAFT_PRIMARY_ORDER)
    return (index, lowered)


# ---------------------------------------------------------------------------
# 真实应用预览（P2 / ADR-039）：允许内嵌的**本机应用地址**
#
# 放在这里而不是 `host/server.py`：消费方是 server（HTTP 层）与 session（审计），
# 而 server 反过来要导入 session —— 校验若留在 server 里，session 就得延迟导入，
# 白白多一层环。本模块不依赖任何项目内模块，两边都能静态导入。
#
# 这条白名单刻意窄到能一眼枚举完：它决定的是**界面里唯一一处非同源内容**。
# 与 `server.CSP` 的关系是「校验放行的集合 ⊆ CSP 放行的集合」，两边必须同时是
# 只有 127.0.0.1 —— 否则会出现「填得进、渲染不出来」的白屏。
# ---------------------------------------------------------------------------
EXTERNAL_PREVIEW_HOST = "127.0.0.1"
EXTERNAL_PREVIEW_MAX_URL = 300


def validate_external_preview_url(raw: Any, *, host_port: int) -> tuple[str, int]:
    """校验「用户要内嵌的本机应用地址」，返回 `(规范化 URL, 端口)`。

    只接受 `http://127.0.0.1:<port>[/path][?query]`：

    · **只 `http`**：dev server 基本都是 http；https 自签证书在 iframe 里会直接
      白屏报错，放行它只会制造「填得进、看不见」的困惑。
    · **只 `127.0.0.1` 字面量**：`localhost` 取决于 hosts 文件，`[::1]` 与 CSP 里的
      source 写法不同；字面量才能让「校验放行」与「CSP 放行」是同一个集合。
    · **必须显式端口**：没有端口就没法在审计里说清「连了哪个服务」。
    · **拒绝 userinfo**：`http://127.0.0.1:5173@evil.com/` 的真实主机是 evil.com，
      虽然 `hostname` 检查已经拦住它，但这类混淆不该只靠单点兜底。
    · **拒绝本应用自身的端口**：那会变成**同源** iframe，隔离的前提当场失效 ——
      跨源才是这功能的安全依据，这一条不能省。
    · **拒绝 `..` 路径段**：浏览器不归一化这种地址，用户以为打的是 A、实际打的
      可能是 B。宁可报错让人自己改。

    失败一律 `ValueError`（server 层映射 400），消息直接给人看。
    """
    if not isinstance(raw, str):
        raise ValueError("地址必须是字符串")
    text = raw.strip()
    if not text:
        raise ValueError("地址不能为空")
    if len(text) > EXTERNAL_PREVIEW_MAX_URL:
        raise ValueError(f"地址过长（上限 {EXTERNAL_PREVIEW_MAX_URL} 字符）")
    # 反斜杠与空白在浏览器解析里会被「顺手修正」成别的东西，本函数不模拟那套规则。
    if "\\" in text or any(ch.isspace() for ch in text):
        raise ValueError("地址不能包含空白或反斜杠")

    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() != "http":
        raise ValueError("只支持 http:// 地址（https 自签证书在预览里会白屏）")
    if parts.username is not None or parts.password is not None:
        raise ValueError("地址不能包含用户名 / 密码")
    if parts.hostname != EXTERNAL_PREVIEW_HOST:
        raise ValueError("只支持本机 127.0.0.1 地址")
    try:
        port = parts.port
    except ValueError as exc:  # 端口不是数字 / 超出 0-65535
        raise ValueError("端口不合法") from exc
    if not port:
        raise ValueError("地址必须带端口，例如 http://127.0.0.1:5173/")
    if port == host_port:
        raise ValueError(
            f"不能预览本应用自己的端口（{host_port}）——那就不是跨源隔离的预览了"
        )
    if parts.fragment:
        raise ValueError("地址不能带 #片段（它不会发给服务端）")

    path = parts.path or "/"
    if not path.startswith("/"):
        raise ValueError("路径必须以 / 开头")
    if ".." in path.split("/"):
        raise ValueError("路径不能包含 .. 段")

    query = f"?{parts.query}" if parts.query else ""
    return f"http://{EXTERNAL_PREVIEW_HOST}:{port}{path}{query}", port


__all__ = [
    "DRAFT_PRIMARY_ORDER",
    "DRAFT_SUFFIXES",
    "EXTERNAL_PREVIEW_HOST",
    "EXTERNAL_PREVIEW_MAX_URL",
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MIME",
    "draft_rank",
    "is_draftable",
    "is_previewable",
    "mime_for",
    "validate_external_preview_url",
]
