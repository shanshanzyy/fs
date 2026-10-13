"""本机 HTTP 服务：桌面界面与 Agent Core 之间的唯一通道。

用标准库 `http.server` 手写，不引入 FastAPI/uvicorn —— 理由是这层只需要
「几个静态文件 + 一个 SSE 流 + 几个 JSON 接口」，而引入一个 Web 框架会把
M0 好不容易守住的「零依赖、内网离线可跑」优势丢掉。

三个必须做对的细节：

1. **SSE 用分块传输（chunked）而不是靠关闭连接分帧。** 后者在 HTTP/1.1 下虽然
   合法，但每次断流都要靠 EOF 判定，重连语义很差；chunked 才是正确做法。
2. **支持 `since` / `Last-Event-ID` 补齐。** 用户刷新页面时，靠总线的历史回放
   把漏掉的事件补上，而不是从空白开始。
3. **客户端断开必须能感知。** SSE 线程阻塞在队列上等事件，如果不管，
   用户每刷新一次页面就多留一个僵尸线程。写入失败即退出并退订。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import __version__
from .. import gitinfo
from ..config import Config
from ..context import (
    MAX_REF_ITEMS,
    iter_workspace_dirs,
    iter_workspace_files,
    iter_workspace_symbols,
)
from ..errors import ConfigError, LLMError, SandboxViolation
from ..governance.client import GovernanceError
from ..llm.openai_compat import OpenAICompatClient, probe_endpoint
from ..tools import ToolRegistry
from ..tools.symbols import KIND_TYPE
from .bus import EventBus
from .files import (
    FileAccessError,
    build_tree,
    list_dirs,
    read_text_file,
)
from .security import Forbidden, HostGuard
from .session import BusyError, Session, StaleFileError
from .settings import (
    effective_completion_overrides,
    effective_mcp_overrides,
    effective_model_overrides,
    env_locked_fields,
    load_settings,
    save_settings,
    settings_path,
)
from .store import SessionStore, default_session_db_path, validate_message_group
from . import protocol as P

UI_DIR = Path(__file__).parent / "ui"
MAX_BODY_BYTES = 1 << 20  # 1 MiB
KEEPALIVE_SECONDS = 15


def _require_bool(value: Any, field: str) -> bool:
    """设置项里的布尔字段：只收真正的 JSON bool，不吃 "true" / 1 这类松散写法。"""
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是 true 或 false")
    return value


def _require_int_range(value: Any, field: str, low: int, high: int) -> int:
    """设置项里的整数字段：必须是 [low, high] 内的整数（显式拒绝 bool 混入）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数")
    if not (low <= value <= high):
        raise ValueError(f"{field} 需在 {low}~{high} 之间")
    return value


# 界面的内容安全策略。
# 界面里会渲染 Agent 读到的代码、模型输出 —— 这些内容本质上不可信，
# 因此把能加载的资源锁死在同源：即便某个环节被注入，
# 也无法让页面去外部地址取脚本或把数据外发。
# connect-src 'self' 同时覆盖了 fetch 与 EventSource。
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# 预览响应（GET /preview/…）专用的 CSP。
# 预览页是**工作区里的用户文件**（可能被 Agent 改过），与界面同源；因此把网络
# 出口焊死：`connect-src 'none'` 让它无法 fetch/XHR/beacon 到本机 API 或外网，
# `form-action 'none'` 挡表单外发，`frame-ancestors 'self'` 只允许被自家编辑器内嵌。
# 代价是预览页自己发起的前端请求也被拦（本功能定位是「看渲染结果」，不是跑应用），
# 这是刻意取舍。
PREVIEW_CSP = (
    "default-src 'self' data: blob:; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' data:; "
    "connect-src 'none'; "
    "base-uri 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'self'"
)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# 预览（O1）允许的 MIME 白名单 —— 只放**文本/web 资源**。
# 刻意不含 .exe/.dll/.zip 等可执行或打包格式：预览是「读渲染」，不是分发二进制。
_PREVIEW_MIME = {
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
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
}

# 单次预览响应的体积上限。静态资源超过这个量级多半是误点/误配，直接回 413。
_PREVIEW_MAX_BYTES = 8 << 20  # 8 MiB

# `/api/visual/check` 的请求体上限：base64 截图会比原始字节大 4/3，
# 再留一点 JSON 外壳余量（与上传图片同一口径）。
_VISUAL_MAX_BODY_BYTES = (8 << 20) * 4 // 3 + 65536

# 上下文引用（P0-A）支持的意图类型。正文一律由后端解析，这里只认「意图」。
# 这三种同时也是 `/api/refs` 的**候选**类型（能在工作区里扫出来）。
_REF_TYPES = ("file", "folder", "symbol")
# `validate_refs` 额外接受 `image`（N/P1-10）：图片来自 `/api/upload-image` 上传，
# 不是扫描候选，因此不列入上面那份（否则 `/api/refs?type=image` 会是个空接口）。
_REF_VALUE_TYPES = _REF_TYPES + ("image",)
_MAX_REF_VALUE_CHARS = 500


def validate_refs(raw: Any) -> list[dict[str, str]]:
    """校验前端的引用意图列表。形状非法即 `ValueError`（由统一入口回 400）。

    刻意严格：前端 bug 应该当场暴露，而不是被静默丢弃后让人以为
    「我明明 @ 了那个文件」。正文不在这里读 —— 那是 `context.resolve_refs`
    经 `PathGuard` 的事。
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise ValueError("refs 必须是数组")
    if len(raw) > MAX_REF_ITEMS:
        raise ValueError(f"refs 过多（上限 {MAX_REF_ITEMS}）")

    cleaned: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"refs[{index}] 必须是对象")
        rtype = str(item.get("type") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if rtype not in _REF_VALUE_TYPES:
            raise ValueError(
                f"refs[{index}].type 非法：{rtype!r}（仅 {'/'.join(_REF_VALUE_TYPES)}）"
            )
        if not value:
            raise ValueError(f"refs[{index}].value 不能为空")
        if len(value) > _MAX_REF_VALUE_CHARS:
            raise ValueError(f"refs[{index}].value 过长（上限 {_MAX_REF_VALUE_CHARS} 字符）")
        cleaned.append({"type": rtype, "value": value})
    return cleaned


# 生成 commit message 时送给模型的 diff 上限。gitinfo 侧已截断到 400k，
# 但那个量级会直接把上下文冲掉 —— 这里只回答「改了什么」，细节不必进提示。
_COMMIT_DIFF_CHARS = 12_000
_COMMIT_FILE_LIMIT = 40


def build_commit_prompt(
    branch: str, entries: list[dict[str, Any]], recent: list[dict[str, Any]], diff: str
) -> str:
    """拼 commit message 的提示。

    刻意把「最近提交」一起喂进去：仓库既有的写法（`fix:` 前缀、中文还是英文）
    只能从历史里读出来，否则生成的 message 风格与仓库格格不入。
    """
    lines = [
        "你是 git 提交信息助手。根据下面的仓库改动，写一条提交信息。",
        "要求：",
        "1. 第一行是祈使句摘要，不超过 72 个字符，语言与最近提交保持一致；",
        "2. 需要时空一行后写 2-4 条要点，说明「为什么改」而不只是「改了什么」；",
        "3. 只输出提交信息本身：不要解释、不要代码围栏、不要引号包裹。",
        "",
        f"当前分支：{branch or '(未知)'}",
    ]
    if recent:
        lines.append("最近提交（对齐既有风格）：")
        lines.extend(f"  {item.get('short', '')} {item.get('subject', '')}" for item in recent)
    if entries:
        lines.append(f"改动文件（{len(entries)} 个）：")
        lines.extend(f"  {item['status']:<2} {item['path']}" for item in entries[:_COMMIT_FILE_LIMIT])
        if len(entries) > _COMMIT_FILE_LIMIT:
            lines.append(f"  …还有 {len(entries) - _COMMIT_FILE_LIMIT} 个")
    if diff:
        clipped = diff[:_COMMIT_DIFF_CHARS]
        if len(diff) > _COMMIT_DIFF_CHARS:
            clipped += "\n…（diff 已截断）"
        lines.append("改动 diff：")
        lines.append(clipped)
    return "\n".join(lines)


def strip_code_fence(text: str) -> str:
    """去掉模型习惯性加上的 ``` 围栏。"""
    body = (text or "").strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


class ServerContext:
    """一次服务进程的全部共享状态。

    工作区切换（W1）放在这里，因为它是**唯一**同时持有「当前会话 + store +
    工具注册表」的对象 —— 重建一套上下文所需的材料都在手上，界面与桌面壳
    因此可以共用同一个 API（POST /api/workspaces/open）。
    """

    def __init__(
        self,
        session: Session,
        guard: HostGuard,
        *,
        verbose: bool = False,
        store: SessionStore | None = None,
        registry: ToolRegistry | None = None,
        client: Any = None,
    ) -> None:
        self.session = session
        self.guard = guard
        self.verbose = verbose
        self.shutdown = threading.Event()
        self.store = store
        self.registry = registry
        self.client = client
        self._switch_lock = threading.Lock()

    @property
    def bus(self) -> EventBus:
        return self.session.bus

    def workspace_view(self) -> dict[str, Any]:
        """最近工作区列表（W1）：当前工作区恒排第一。

        来源刻意是会话库而不是某个独立配置 —— 浏览器开不了原生目录对话框，
        壳与浏览器的「最近」必须来自同一个真源，否则两边会漂移。
        已不存在的目录直接剔掉（选了也打不开，留着只是噪音）。
        """
        current = str(self.session.config.workspace)
        items: list[dict[str, Any]] = [{"path": current, "current": True, "updatedAt": ""}]
        seen = {current}
        if self.store is not None:
            try:
                rows = self.store.list_workspaces(limit=20)
            except Exception:  # noqa: BLE001 — 列历史失败不该让「切换工作区」整个用不了
                rows = []
            for row in rows:
                path = str(row.get("path") or "")
                if not path or path in seen or not Path(path).is_dir():
                    continue
                seen.add(path)
                items.append(
                    {"path": path, "current": False, "updatedAt": row.get("updatedAt") or ""}
                )
        return {"current": current, "workspaces": items[:12]}

    def switch_workspace(self, path: str) -> dict[str, Any]:
        """切到另一个工作区：优雅重启一套上下文（新会话、新审计链、新 jail）。

        为什么不能原地改 `Config.workspace`：它在启动时就绑定了 PathGuard、
        检查点目录与审计哈希链（`<workspace>/.vibe/audit.jsonl`）。原地改一个
        字段会让「审计链已推进」与「jail 已换根」两个事实互相矛盾 —— 所以这里
        换的是**整个 Session**，旧的那套原样收尾后丢弃。

        总线刻意复用：已经连着的 SSE（浏览器标签页、桌面窗口）不掉线，
        切换完成后由一条 `session.ready` 把新全貌推给所有订阅者。
        """
        raw = str(path or "").strip()
        if not raw:
            raise ValueError("工作区路径为空")
        target = Path(raw).expanduser()
        if not target.is_dir():
            raise ValueError(f"工作区不是目录或不存在：{target}")

        with self._switch_lock:
            current = self.session
            if current.state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法切换工作区，请先中断或等任务结束")
            target = target.resolve()
            if str(target) == str(current.config.workspace):
                return {
                    "ok": True,
                    "switched": False,
                    "workspace": str(target),
                    "sessionId": current.config.session_id,
                    "reason": "已经是当前工作区，未做切换",
                }

            # 旧会话收尾：在**旧**工作区的链上留一条明确的 session.end。
            current.end_session("workspace_switch")
            # 再停掉它的后台资源（索引 watch 线程）：旧 Session 会被丢弃，
            # 但线程不会自己消失 —— 不停就留下一个轮询废弃目录的孤儿线程。
            current.close()

            # Config 是 frozen dataclass，只能 replace 出新实例；会话 ID 一并换掉 ——
            # 新工作区是一段新对话，不该续写旧会话的落盘行。
            new_config = replace(
                current.config, workspace=target, session_id=uuid.uuid4().hex[:12]
            )
            session = Session(
                new_config,
                bus=current.bus,
                registry=self.registry,
                store=self.store,
                client=self.client,  # None = 由 Session 按新配置重新装配治理面
            )
            self.session = session

        self.bus.publish(P.event(P.SESSION_READY, **session.describe()))
        return {
            "ok": True,
            "switched": True,
            "workspace": str(target),
            "sessionId": new_config.session_id,
        }

    def apply_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """界面保存「模型设置」：热替换模型客户端 + 可选切工作区 + 落盘。

        为什么不重建整个 Session：模型只影响 LLM 客户端的装配，而 `Agent` 在每次
        调用时读 `self.client`（见 loop/agent.py），因此**换一个客户端引用**即可生效，
        当前对话（`agent.messages`）与检查点/审计链完全不丢。只有工作区变了才需要
        重建整套上下文（那才是 `switch_workspace` 的职责）。

        校验顺序刻意是「先热替换（忙 / 治理面会在这里抛）→ 再落盘」：忙时或治理面
        接管时直接失败，不会留下「文件改了、运行时没改」这种最容易让人困惑的错位。

        空字符串表示「保持原值」——界面里 apiKey 留空即「不改密钥」。
        """
        model = str(body.get("model") or "").strip()
        base_url = str(body.get("baseUrl") or "").strip().rstrip("/")
        api_key = str(body.get("apiKey") or "").strip()
        workspace = str(body.get("workspace") or "").strip()

        changes: dict[str, Any] = {}
        if model:
            changes["model"] = model
        if base_url:
            changes["base_url"] = base_url
        if api_key:
            changes["api_key"] = api_key
        # 补全参数（D3+）：按**显式存在**判断 —— False / 0 都是合法值，不能用
        # 「空串 = 不改」的规则；非法值直接报错（外层转 400），不做静默钳制。
        if "fimEnabled" in body:
            changes["fim_enabled"] = _require_bool(body["fimEnabled"], "fimEnabled")
        if "completionMaxTokens" in body:
            changes["completion_max_tokens"] = _require_int_range(
                body["completionMaxTokens"], "completionMaxTokens", 16, 4096
            )
        if "completionPrefixChars" in body:
            changes["completion_prefix_chars"] = _require_int_range(
                body["completionPrefixChars"], "completionPrefixChars", 0, 100_000
            )
        if "completionSuffixChars" in body:
            changes["completion_suffix_chars"] = _require_int_range(
                body["completionSuffixChars"], "completionSuffixChars", 0, 100_000
            )
        # MCP 开关（L 管理面）：按显式存在判断 —— false 也必须能落盘（关闭）。
        if "mcpEnabled" in body:
            changes["mcp_enabled"] = _require_bool(body["mcpEnabled"], "mcpEnabled")
        if not changes and not workspace:
            raise ValueError("没有需要修改的配置项")

        with self._switch_lock:
            current = self.session
            if current.state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法修改模型设置，请先中断或等任务结束")
            if current.governance is not None:
                raise ValueError("治理面已接管模型接入，界面不支持修改模型配置")
            # MCP 开关真的变了才重建实例：不变时保持进程原样，避免无谓重启。
            mcp_changed = (
                "mcp_enabled" in changes
                and changes["mcp_enabled"] != current.config.mcp_enabled
            )
            if changes:
                new_config = replace(current.config, **changes)
                client = OpenAICompatClient(
                    base_url=new_config.base_url,
                    model=new_config.model,
                    api_key=new_config.api_key,
                    timeout=new_config.request_timeout,
                    prompt_cache=new_config.prompt_cache,
                    cache_key=new_config.session_id,
                    vision_mode=new_config.vision_mode,
                )
                # 热替换：只换引用，不碰 agent.messages（当前对话原样保留）。
                current.agent.client = client
                current.config = new_config
                current.agent.config = new_config
                current._client = client
                # 补全缓存里存的是「按旧配置算出的结果」，配置一变即失效。
                current.invalidate_completion_cache()
            # MCP 开关切换：清掉全部实例并按新配置重建 `mcp__*` 工具清单
            # （关→摘除、开→并入）。读的是刚更新的 current.agent.config。
            if mcp_changed:
                current.mcp_reload()

        # 落盘：只写显式给出的非空字段（save_settings 本身忽略空值）。
        if changes:
            save_settings(changes)

        # 切工作区放在锁外：switch_workspace 自己要取同一把非重入锁，
        # 在锁内调用会自锁死。它自己会再断言一次空闲态。
        switched = False
        if workspace and workspace != str(self.session.config.workspace):
            self.switch_workspace(workspace)
            switched = True

        session = self.session
        self.bus.publish(P.event(P.SESSION_READY, **session.describe()))
        return {
            "ok": True,
            "switched": switched,
            "settingsPath": str(settings_path()),
            "settings": load_settings(),
            "session": session.describe(),
        }


class VibeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    # ctx 必须等端口绑定后才能构造（HostGuard 要拿实际端口拼 Origin 白名单），
    # 因此这里允许两段式初始化；serve 之前一定会被赋值。
    ctx: ServerContext

    def __init__(self, address: tuple[str, int], ctx: ServerContext | None = None) -> None:
        super().__init__(address, VibeHandler)
        if ctx is not None:
            self.ctx = ctx


class VibeHandler(BaseHTTPRequestHandler):
    server_version = "VibeAgentHost"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    ctx: ServerContext  # 由 VibeServer 注入
    # 本请求尚未被处理器读取的请求体字节数。每个请求在 _dispatch 里重置，
    # _read_json 每读一段就扣减，收尾由 _drain_body 排空剩余部分。
    _body_remaining = 0

    # ------------------------------------------------------------------ 日志
    def handle_one_request(self) -> None:
        """客户端断连是常态，不是错误。

        桌面场景下用户关窗口、切页面、刷新，都会让已建立的连接突然消失。
        socketserver 默认会把这类 OSError 打成一大段 traceback，
        真实使用中会把日志刷得没法看 —— 这里主动吞掉，
        只保留真正异常（那些仍然按原样抛出去）。
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        # 默认实现会往 stderr 刷每一行请求，SSE 长连接下噪音很大
        if self.server.ctx.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def log_error(self, fmt: str, *args: Any) -> None:
        super().log_error(fmt, *args)

    # ------------------------------------------------------------------ 分发
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        headers = {k: v for k, v in self.headers.items()}
        self._body_remaining = int(self.headers.get("Content-Length") or 0)
        try:
            self._route(method, path, query, headers)
        finally:
            # 无论走哪条分支（含 400/403 提前 return）都要排空请求体：keep-alive
            # 下残留字节会在关连接时触发 TCP RST，客户端读响应体即报
            # ConnectionAbortedError；残留字节还会污染同一连接上的下一请求。
            self._drain_body()

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
    ) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]

        try:
            ctx.guard.check(method=method, path=path, headers=headers, query=query)
        except Forbidden as exc:
            self._send_json({"error": exc.reason}, status=exc.status)
            return

        if method == "OPTIONS":
            self._send_empty(HTTPStatus.NO_CONTENT, extra_headers={"Allow": "GET, POST"})
            return

        try:
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            elif method == "GET" and (path == "/preview" or path.startswith("/preview/")):
                self._serve_preview(path)
            elif method == "GET":
                self._serve_static(path)
            else:
                self._send_json({"error": "未知路径"}, status=HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass  # 客户端提前断开，正常现象
        except ValueError as exc:
            # 请求体不合法等客户端错误，不该报 500
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 — 服务端绝不能因单个请求崩掉
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------ API
    def _handle_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]
        session = ctx.session

        if path == "/api/health":
            self._send_json(
                {"ok": True, "ready": True, "version": __version__, "pid": os.getpid()}
            )
            return

        if path == "/api/events":
            self._serve_events(query)
            return

        if path == "/api/state":
            self._send_json(session.describe())
            return

        # ---- 模型设置：读当前生效值 + 哪些字段被环境变量锁定 ---------------
        if path == "/api/settings" and method == "GET":
            self._handle_get_settings(session)
            return

        # ---- 模型列表：从当前（或指定）端点拉取可用模型 -------------------
        if path == "/api/models" and method == "GET":
            self._handle_models_list(session, query)
            return

        if path == "/api/tree":
            rel = (query.get("path") or [""])[0]
            try:
                self._send_json(build_tree(session.agent.paths, rel=rel))
            except FileAccessError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            return

        if path == "/api/file":
            rel = (query.get("path") or [""])[0]
            try:
                self._send_json(read_text_file(session.agent.paths, rel))
            except FileAccessError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            return

        if path == "/api/diffs":
            self._send_json({"changes": session.diffs()})
            return

        # ---- 索引健康度（J/P1-9）：只读；watch 关时不建索引，如实回 0 --------
        if path == "/api/index/status":
            self._send_json(session.index_status())
            return

        # ---- 测试树（K/P2-14）：收集 + 最近结果 ---------------------------
        # 未配置 test_command 时如实回 configured=false，不报错 ——
        # 「本工作区没配测试命令」是正常状态，界面据此显示一行说明。
        if path == "/api/tests" and method == "GET":
            refresh = (query.get("refresh") or ["0"])[0].strip().lower() in ("1", "true", "yes")
            self._send_json(session.tests_tree(refresh=refresh))
            return

        if path == "/api/tests/last" and method == "GET":
            self._send_json(session.tests_last())
            return

        # ---- 上下文用量（P2-17）：预算条的数据源 -------------------------
        # 只读、无副作用。界面首帧直接取自 /api/state 的 `context` 字段，
        # 这里供轮询用 —— 两者是同一个快照（见 session.context_stats）。
        if path == "/api/context/stats" and method == "GET":
            self._send_json(session.context_stats())
            return

        # ---- 治理面策略探针（登录门）：是否开放自助注册 -------------------
        # 只读、零副作用，执行中也照样可用（见 Session.governance_config）。
        if path == "/api/governance/config" and method == "GET":
            self._send_json(
                session.governance_config(server_url=(query.get("serverUrl") or [""])[0])
            )
            return

        # ---- Git 集成（P0-C）：只读的仓库视图 ---------------------------
        # 三个端点都走 gitinfo（固定 argv + shell=False），非 git 仓库时返回
        # available=false 而不是报错 —— 界面据此显示「不是 git 仓库」。
        if path == "/api/git/status" and method == "GET":
            self._send_json(gitinfo.status(session.agent.config.workspace))
            return

        if path == "/api/git/branches" and method == "GET":
            self._send_json(gitinfo.branches(session.agent.config.workspace))
            return

        if path == "/api/git/diff" and method == "GET":
            raw_ref = (query.get("ref") or ["HEAD"])[0]
            raw_staged = (query.get("staged") or ["0"])[0].strip().lower()
            staged = raw_staged in ("1", "true", "yes")
            try:
                rel = self._guard_ref_path((query.get("path") or [""])[0])
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json(
                gitinfo.diff(
                    session.agent.config.workspace, ref=raw_ref, path=rel, staged=staged
                )
            )
            return

        # ---- 上下文引用（P0-A）：`@` 候选列表 ----------------------------
        if path == "/api/refs" and method == "GET":
            self._handle_refs(query)
            return

        if path == "/api/audit":
            self._send_json(session.audit_report())
            return

        # ---- 语言服务器（E）：只读的状态 / 诊断 / 定义 ---------------------
        # 不可用时一律返回 {available:false, reason} 而不是报错 ——
        # 「本机没装语言服务器」是正常状态，不是服务端故障。
        if path == "/api/lsp/status" and method == "GET":
            self._send_json(session.lsp_status())
            return

        if path == "/api/lsp/diagnostics" and method == "GET":
            rel = (query.get("path") or [""])[0]
            if not rel.strip():
                self._send_json({"error": "缺少 path 参数"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.lsp_diagnostics(rel)
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/lsp/definition" and method == "GET":
            rel = (query.get("path") or [""])[0]
            if not rel.strip():
                self._send_json({"error": "缺少 path 参数"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                line = int((query.get("line") or ["0"])[0])
                char = int((query.get("char") or ["0"])[0])
            except ValueError:
                self._send_json({"error": "line/char 必须是整数"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.lsp_definition(rel, line, char)
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- MCP 客户端（L/P1-6）：外部工具来源的状态。只读 ----------------
        # 与 LSP 状态同一范式：未启用 / 起不来一律如实回 enabled 与 available，
        # 绝不 500 —— 「没配 server」是正常状态，不是服务端故障。
        if path == "/api/mcp/status" and method == "GET":
            self._send_json(session.mcp_status())
            return

        # ---- 工作区（W1）：浏览器里「打开其他文件夹作为工作区」--------------
        if path == "/api/workspaces" and method == "GET":
            self._send_json(ctx.workspace_view())
            return

        # 内置目录浏览器：只读列子目录。浏览器开不了原生目录对话框，靠这个接口
        # 也能从盘符逐级点进任意目录 —— 与桌面壳的 pickFolder 是两条平行路径。
        if path == "/api/fs/dirs" and method == "GET":
            raw = (query.get("path") or [""])[0]
            try:
                self._send_json(list_dirs(raw))
            except FileAccessError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            return

        # ---- 会话持久化（A2）：列表 / 消息 / 恢复 -----------------------
        if path == "/api/sessions" or path.startswith("/api/sessions/"):
            self._handle_sessions(method, path, query)
            return

        # ---- 检查点 / 回滚（B1）------------------------------------------
        if path == "/api/checkpoints" or path.startswith("/api/checkpoints/"):
            self._handle_checkpoints(method, path)
            return

        if method != "POST":
            self._send_json({"error": "该接口需要 POST"}, status=HTTPStatus.METHOD_NOT_ALLOWED)
            return

        if path == "/api/run":
            body = self._read_json()
            try:
                refs = validate_refs(body.get("refs"))
                run_id = session.submit(str(body.get("text") or ""), refs)
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json({"runId": run_id})
            return

        if path == "/api/approval":
            body = self._read_json()
            try:
                delivered = session.resolve_approval(
                    str(body.get("approvalId") or ""), str(body.get("decision") or "")
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json({"delivered": delivered})
            return

        if path == "/api/interrupt":
            self._read_json()  # HTTP/1.1 keep-alive：必须排空请求体，否则残留字节污染下一请求
            self._send_json({"interrupted": session.interrupt()})
            return

        # ---- 工作区（W1）：切换 = 优雅重启一套上下文 ------------------------
        if path == "/api/workspaces/open":
            body = self._read_json()
            try:
                result = ctx.switch_workspace(str(body.get("path") or ""))
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 模型设置（保存）：热替换 + 落盘 --------------------------------
        if path == "/api/settings":
            body = self._read_json()
            try:
                result = ctx.apply_settings(body)
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 连通性检测：用**临时**客户端探测，不吞错（避免假阳性）-----------
        if path == "/api/model/test":
            body = self._read_json()
            self._handle_model_test(session, body)
            return

        if path == "/api/reset":
            self._read_json()
            try:
                session.reset()
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            else:
                self._send_json({"ok": True})
            return

        # ---- Git 集成（P0-C）：只生成 message，**不提交** -------------------
        if path == "/api/git/commit-message":
            self._read_json()  # keep-alive：排空请求体（该接口本身无参数）
            self._handle_commit_message(session)
            return

        # ---- 编辑面（D）：补全 / 改写 / 保存 ------------------------------
        # 三条链路都属于「用户本人动作」：不走审批浮层（ADR-008 未削弱 ——
        # Agent 发起的写仍恒走审批），但落盘一律经补丁引擎 + 检查点 + 审计。
        if path == "/api/complete":
            body = self._read_json()
            rel = str(body.get("path") or "")
            if not rel.strip():
                self._send_json({"error": "缺少 path 参数"}, status=HTTPStatus.BAD_REQUEST)
                return
            raw_tokens = body.get("maxTokens")
            try:
                limit = int(raw_tokens) if raw_tokens not in (None, "") else None
                result = session.complete(
                    rel,
                    str(body.get("prefix") or ""),
                    str(body.get("suffix") or ""),
                    limit,
                )
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except LLMError as exc:
                self._send_json(
                    {"error": f"模型调用失败，无法补全：{exc}"},
                    status=HTTPStatus.BAD_GATEWAY,
                )
            else:
                self._send_json(result)
            return

        if path == "/api/rewrite":
            body = self._read_json()
            rel = str(body.get("path") or "")
            if not rel.strip():
                self._send_json({"error": "缺少 path 参数"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.rewrite(
                    rel,
                    int(body.get("startLine") or 0),
                    int(body.get("endLine") or 0),
                    str(body.get("instruction") or ""),
                )
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except LLMError as exc:
                self._send_json(
                    {"error": f"模型调用失败，无法改写：{exc}"},
                    status=HTTPStatus.BAD_GATEWAY,
                )
            else:
                self._send_json(result)
            return

        if path == "/api/rewrite/apply":
            body = self._read_json()
            token = str(body.get("token") or "")
            if not token.strip():
                self._send_json({"error": "缺少 token"}, status=HTTPStatus.BAD_REQUEST)
                return
            accepted = body.get("accepted") or []
            if not isinstance(accepted, list):
                self._send_json({"error": "accepted 必须是数组"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.rewrite_apply(token, accepted)
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except StaleFileError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/edit/save":
            body = self._read_json()
            rel = str(body.get("path") or "")
            if not rel.strip():
                self._send_json({"error": "缺少 path 参数"}, status=HTTPStatus.BAD_REQUEST)
                return
            content = body.get("content")
            if not isinstance(content, str):
                self._send_json({"error": "content 必须是字符串"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.save(rel, content, str(body.get("expectedDigest") or ""))
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except StaleFileError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 语言服务器（E）：重命名预览 / 应用 ----------------------------
        # 与编辑面同一范式：预览不落盘、返回 token；应用按 token 取服务端缓存。
        if path == "/api/lsp/rename/preview":
            body = self._read_json()
            rel = str(body.get("path") or "")
            if not rel.strip():
                self._send_json({"error": "缺少 path 参数"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.lsp_rename_preview(
                    rel,
                    int(body.get("line") or 0),
                    int(body.get("char") or 0),
                    str(body.get("newName") or ""),
                )
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/lsp/rename/apply":
            body = self._read_json()
            token = str(body.get("token") or "")
            if not token.strip():
                self._send_json({"error": "缺少 token"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.lsp_rename_apply(token)
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except StaleFileError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except SandboxViolation as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- MCP 客户端（L/P1-6）：重读 servers 配置 ------------------------
        # 「用户本人动作」（ADR-011 同范式）：不落盘、不 409（不与写盘争用）。
        # 只重起外部进程并重建 `mcp__*` 工具清单，随后照常回一份 status。
        if path == "/api/mcp/reload":
            result = session.mcp_reload()
            self._send_json(result)
            return

        # ---- MCP 管理写入（L 管理面）：增 / 改 / 删 / 测连接 -------------------
        # 「用户本人动作」（ADR-011）：不弹审批浮层，但执行中 409、治理面 400；
        # 只写配置文件、**不拼命令**（argv 仍只来自受信配置，ADR-015）。
        if path == "/api/mcp/servers":
            body = self._read_json()
            try:
                result = session.mcp_upsert_server(body)
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # 删除走 POST：`_dispatch` 只认 GET/POST/OPTIONS，没有 DELETE 分支。
        if path == "/api/mcp/servers/remove":
            body = self._read_json()
            try:
                result = session.mcp_delete_server(str(body.get("name") or ""))
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/mcp/test":
            body = self._read_json()
            try:
                result = session.mcp_test_server(str(body.get("name") or ""))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 治理面登录（M3+）：从界面完成登录 / 邀请码注册 / 登出 ----------
        # 「用户本人动作」（ADR-011 同范式）：不走审批浮层，但执行中 409
        # （登录会换掉模型客户端与策略闸门，见 Session._governance_guard）。
        # 令牌写进用户级凭证文件，响应只回掩码视图，绝不回明文令牌。
        if path in (
            "/api/governance/login",
            "/api/governance/register",
            "/api/governance/logout",
        ):
            body = self._read_json()
            try:
                if path.endswith("/login"):
                    result = session.governance_login(
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                        server_url=str(body.get("serverUrl") or ""),
                        device_name=str(body.get("deviceName") or ""),
                    )
                elif path.endswith("/register"):
                    result = session.governance_register(
                        str(body.get("enrollmentToken") or ""),
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                        display_name=str(body.get("displayName") or ""),
                        server_url=str(body.get("serverUrl") or ""),
                        device_name=str(body.get("deviceName") or ""),
                    )
                else:
                    result = session.governance_logout(
                        all_devices=bool(body.get("allDevices"))
                    )
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except GovernanceError as exc:
                # 4xx（凭证错 / 邀请码无效）如实透传，其余按「治理面不可达」转 502。
                status = exc.status if 400 <= exc.status < 500 else HTTPStatus.BAD_GATEWAY
                self._send_json({"error": str(exc)}, status=status)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 测试树（K/P2-14）：跑指定用例（结果走 SSE 事件）----------------
        # 「用户本人动作」：不走审批浮层、不落盘（ADR-011），但记审计；
        # Agent 跑着时也**不** 409（不与写盘争用）。并发跑第二批才 409 ——
        # 锁保证同一时刻只跑一批（见 session.tests_run 与 protocol.py 的说明）。
        if path == "/api/tests/run":
            body = self._read_json()
            raw_ids = body.get("nodeIds") or []
            if not isinstance(raw_ids, list):
                self._send_json({"error": "nodeIds 必须是数组"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = session.tests_run([str(item) for item in raw_ids])
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 跨会话记忆（P2-15）：用户级记忆库的读 / 增 / 删 ------------------
        # 读写都是「用户本人动作」（ADR-011 同范式）：不走审批浮层、不写工作区源码，
        # 只记审计（只记 scope/长度/指纹，ADR-006）。模型侧写入仍必须走 `remember`
        # 工具的审批闸门（Risk.REVIEW）—— 两条路径分开，界面这里绝不代表模型自动学习。
        if path == "/api/memories" and method == "GET":
            self._send_json(session.list_memories())
            return

        if path == "/api/memories" and method == "POST":
            body = self._read_json()
            try:
                result = session.add_memory(str(body.get("text") or ""), body.get("tags"))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/memories/delete" and method == "POST":
            body = self._read_json()
            try:
                result = session.delete_memory(int(body.get("id") or 0))
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 计划视图（P2-12）：人工勾选一步的状态 --------------------------
        # 「用户本人动作」：不走审批浮层、不写盘，但记审计；运行中也不 409 ——
        # 它只是改内存里的一个状态，不与写盘路径争用（见 protocol.py 的边界说明）。
        if path == "/api/plan/toggle":
            body = self._read_json()
            try:
                result = session.plan_toggle(
                    str(body.get("id") or ""), str(body.get("status") or "")
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 交互式终端（M/P1-7）：独立通道，输出走 TERM_DATA/TERM_EXIT -------
        # 「用户本人动作」（ADR-011 范式）：用户在 IDE 里直接敲命令，**不走审批浮层**，
        # 但记审计（term.open/term.close，只记元数据）并受路径沙箱约束；
        # Agent 想交互仍只能走受审批的 run_command —— 两条通道彻底隔离（见 protocol.py）。
        if path == "/api/term/open":
            body = self._read_json()
            try:
                result = session.term_open(
                    int(body.get("cols") or 80), int(body.get("rows") or 24)
                )
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/term/input":
            body = self._read_json()
            try:
                result = session.term_input(
                    str(body.get("termId") or ""), str(body.get("data") or "")
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/term/resize":
            body = self._read_json()
            try:
                result = session.term_resize(
                    str(body.get("termId") or ""),
                    int(body.get("cols") or 80),
                    int(body.get("rows") or 24),
                )
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        if path == "/api/term/close":
            body = self._read_json()
            try:
                result = session.term_close(str(body.get("termId") or ""))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 多模态图片（N/P1-10）：上传图片 → image 引用 --------------------
        # 「用户本人动作」（ADR-011 同范式）：不走审批浮层、不写工作区源码，只把图片
        # 落到 host 侧 .vibe/images/ 并记审计（只记字节数/扩展名/摘要，ADR-006）。
        # 请求体是 base64，比 JSON 接口大得多，因此这个端点按配置单独放宽上限。
        if path == "/api/upload-image":
            cfg = session.agent.config
            limit = max(1, int(cfg.vision_max_image_bytes)) * 4 // 3 + 65536
            body = self._read_json(limit=limit)
            try:
                result = session.upload_image(
                    str(body.get("name") or ""),
                    str(body.get("data") or ""),
                    str(body.get("mediaType") or ""),
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        # ---- 网页自检（O/P1-11）：截图与基线比对，或用它设基线 -----------------
        # 「用户本人动作」（ADR-011 同范式）：不触发 Agent 写盘路径；截图由**壳**
        # 提供（`vibe:capture-page` 离屏截图）。浏览器直开时前端不带 image 调用，
        # session 层如实回 {available:false, reason}，**不是 500**。基线只落
        # host 侧 .vibe/visual/，审计只记摘要与比率（ADR-006，不记截图内容）。
        # 请求体带 base64 截图，故单独放宽上限。
        if path == "/api/visual/check":
            body = self._read_json(limit=_VISUAL_MAX_BODY_BYTES)
            try:
                result = session.visual_check(
                    str(body.get("url") or ""),
                    int(body.get("width") or 1280),
                    int(body.get("height") or 800),
                    str(body.get("image") or ""),
                    str(body.get("baseline") or ""),
                    bool(body.get("setBaseline")),
                )
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json(result)
            return

        self._send_json({"error": "未知接口"}, status=HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------ 会话持久化
    def _handle_sessions(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        """会话持久化 API（A2）。

        GET  /api/sessions[?workspace=...]   会话列表；缺省按当前工作区过滤，
                                             workspace=all 返回全部（跨项目可见）
        GET  /api/sessions/{id}/messages     消息回放（形态与 Agent.messages 一致）
        POST /api/sessions/{id}/resume       恢复进当前会话，从断点继续对话
        """
        ctx = self.server.ctx  # type: ignore[attr-defined]
        session = ctx.session
        store = session.store
        if store is None:
            self._send_json(
                {"error": "会话持久化未开启（VIBE_SESSION_PERSIST=false）"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        parts = [p for p in path.split("/") if p]  # ["api", "sessions", ...]

        if method == "GET" and len(parts) == 2:
            raw = (query.get("workspace") or [""])[0]
            if raw == "all":
                workspace: str | None = None
            else:
                workspace = raw or str(session.config.workspace)
            self._send_json({"sessions": store.list_sessions(workspace=workspace)})
            return

        if len(parts) < 3 or not parts[2]:
            self._send_json({"error": "缺少会话 ID"}, status=HTTPStatus.BAD_REQUEST)
            return
        sid = parts[2]

        if method == "GET" and len(parts) == 4 and parts[3] == "messages":
            if store.get_session(sid) is None:
                self._send_json({"error": "会话不存在"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"sessionId": sid, "messages": store.get_messages(sid)})
            return

        if method == "POST" and len(parts) == 4 and parts[3] == "resume":
            row = store.get_session(sid)
            if row is None:
                self._send_json({"error": "会话不存在"}, status=HTTPStatus.NOT_FOUND)
                return
            if row["workspace"] != str(session.config.workspace):
                self._send_json(
                    {
                        "error": (
                            f"会话属于其他工作区：{row['workspace']}"
                            f"（当前 {session.config.workspace}）"
                        )
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            messages = store.get_messages(sid)
            error = validate_message_group(messages)
            if error:
                # 服务端 400 防线：孤立 tool 消息会让模型服务直接拒绝整个序列
                self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                session.resume(messages, session_id=sid)
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            self._send_json({"ok": True, "sessionId": sid, "restored": len(messages)})
            return

        self._send_json({"error": "未知接口"}, status=HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------ 检查点（B1）
    def _handle_checkpoints(self, method: str, path: str) -> None:
        """GET  /api/checkpoints                    检查点列表（摘要）
        POST /api/checkpoints/{id}/restore         回滚；运行中 409，失败 400
        """
        session = self.server.ctx.session  # type: ignore[attr-defined]
        parts = [p for p in path.split("/") if p]

        if method == "GET" and len(parts) == 2:
            self._send_json({"checkpoints": session.checkpoints()})
            return

        if method == "POST" and len(parts) == 4 and parts[3] == "restore":
            self._read_json()  # keep-alive：排空请求体（restore 本身无参数）
            try:
                result = session.restore_checkpoint(parts[2])
            except BusyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            # CheckpointError 已在 Session 层转成 ValueError，由统一入口回 400
            self._send_json(result)
            return

        self._send_json({"error": "未知接口"}, status=HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------ 模型设置
    def _handle_get_settings(self, session: Session) -> None:
        """当前生效的模型配置 + 环境变量锁定情况。

        apiKey 只回末 4 位掩码，绝不回明文。`apiKeySet` 供界面判断「留空=不变更」。
        """
        cfg = session.agent.config
        key = cfg.api_key or ""
        masked = ("•" * max(0, len(key) - 4) + key[-4:]) if key else ""
        self._send_json(
            {
                "model": cfg.model,
                "baseUrl": cfg.base_url,
                "apiKeyMasked": masked,
                "apiKeySet": bool(key),
                "workspace": str(cfg.workspace),
                "settingsPath": str(settings_path()),
                # 补全参数（D3+）：面板回填用；envLocked 已自动带上这四个键
                "fimEnabled": cfg.fim_enabled,
                "completionMaxTokens": cfg.completion_max_tokens,
                "completionPrefixChars": cfg.completion_prefix_chars,
                "completionSuffixChars": cfg.completion_suffix_chars,
                # MCP 开关（L 管理面）：面板回填用；envLocked 已自动带上 mcp_enabled
                "mcpEnabled": bool(getattr(cfg, "mcp_enabled", False)),
                "envLocked": env_locked_fields(),
                "governance": session.governance is not None,
            }
        )

    def _handle_models_list(self, session: Session, query: dict[str, list[str]]) -> None:
        """`GET /api/models?baseUrl=&apiKey=`：列出端点可用模型。

        `list_models` 会吞错返回空列表，因此这里只用于「有则展示」；要判断可达性请在
        界面用 POST /api/model/test（那里不吞错，避免假阳性）。
        """
        cfg = session.agent.config
        base_url = (query.get("baseUrl") or [""])[0].strip().rstrip("/") or cfg.base_url
        api_key = (query.get("apiKey") or [""])[0].strip() or cfg.api_key
        client = OpenAICompatClient(
            base_url, model="-", api_key=api_key, timeout=8, max_retries=0
        )
        self._send_json({"baseUrl": base_url, "models": client.list_models()})

    def _handle_model_test(self, session: Session, body: dict[str, Any]) -> None:
        """`POST /api/model/test`：探测可达性 + 顺带带回可用模型列表。

        用模块级 `probe_endpoint`（不吞异常）而不是会话客户端的 `list_models` ——
        后者连接失败会返回空列表，让自检给出「可达但无模型」的假阳性结论。
        """
        cfg = session.agent.config
        base_url = str(body.get("baseUrl") or "").strip().rstrip("/") or cfg.base_url
        api_key = str(body.get("apiKey") or "").strip() or cfg.api_key
        ok, message = probe_endpoint(base_url, api_key, timeout=5)
        models: list[str] = []
        if ok:
            client = OpenAICompatClient(
                base_url, model="-", api_key=api_key, timeout=8, max_retries=0
            )
            models = client.list_models()
        self._send_json({"ok": ok, "message": message, "baseUrl": base_url, "models": models})

    # ------------------------------------------------------------------ 上下文引用（P0-A）
    def _handle_refs(self, query: dict[str, list[str]]) -> None:
        """`@` 引用候选：`GET /api/refs?type=file|symbol|folder&q=&limit=`。

        只读、全部走 PathGuard，结果上限 `MAX_REF_ITEMS`。候选集与「引用时能真读到」
        的集合是同一套遍历口径（见 context/references 的 iter_workspace_*）——
        否则会出现「列表里点得到、真引用却被拒」这种最恼人的不一致。
        """
        paths = self.server.ctx.session.agent.paths  # type: ignore[attr-defined]
        rtype = (query.get("type") or ["file"])[0].strip().lower()
        needle = (query.get("q") or [""])[0].strip().lower()
        try:
            limit = int((query.get("limit") or [str(MAX_REF_ITEMS)])[0])
        except ValueError:
            limit = MAX_REF_ITEMS
        limit = max(1, min(limit, MAX_REF_ITEMS))

        if rtype not in _REF_TYPES:
            self._send_json(
                {"error": f"不支持的 type：{rtype!r}（仅 {'/'.join(_REF_TYPES)}）"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        items: list[dict[str, str]] = []
        if rtype == "symbol":
            for rel, symbol in iter_workspace_symbols(paths):
                if needle and needle not in symbol.name.lower() and needle not in rel.lower():
                    continue
                marker = "◆" if symbol.kind == KIND_TYPE else "·"
                items.append(
                    {
                        "type": "symbol",
                        # 值里带文件路径，解析时能精确定位到那一个符号
                        "value": f"{rel}::{symbol.name}",
                        "label": f"{marker} {symbol.name}",
                        "detail": f"{rel}:{symbol.line}",
                    }
                )
                if len(items) >= limit:
                    break
        elif rtype == "folder":
            for rel in iter_workspace_dirs(paths):
                if needle and needle not in rel.lower():
                    continue
                items.append({"type": "folder", "value": rel, "label": f"{rel}/", "detail": "目录"})
                if len(items) >= limit:
                    break
        else:  # file
            for rel in iter_workspace_files(paths):
                if needle and needle not in rel.lower():
                    continue
                items.append({"type": "file", "value": rel, "label": rel, "detail": "文件"})
                if len(items) >= limit:
                    break

        self._send_json({"type": rtype, "items": items})

    # ------------------------------------------------------------------ Git（P0-C）
    def _guard_ref_path(self, raw: str) -> str | None:
        """把查询参数里的路径过一遍 PathGuard，返回工作区相对路径。

        与 `/api/file` 同一口径：前端传进来的 path 一样是不可信输入 ——
        不能因为「反正 git 只看仓库内的东西」就跳过校验。
        越权时抛 `SandboxViolation`（由调用方转 403）。
        """
        text = (raw or "").strip()
        if not text:
            return None
        paths = self.server.ctx.session.agent.paths  # type: ignore[attr-defined]
        return paths.relative(paths.check_list(text))

    def _handle_commit_message(self, session: Session) -> None:
        """`POST /api/git/commit-message`：生成提交信息文本，**不提交**。

        刻意只返回文本（见 P0 计划 §5「明确不做」）：提交动作仍是用户在终端里
        跑 `git commit`，或让 Agent 走 `run_command`（REVIEW 级审批）——
        把提交收进这个端点等于给「不可配置的红线」开一个新入口。
        """
        root = session.agent.config.workspace
        data = gitinfo.diff(root, ref="HEAD", staged=False)
        if not data.get("available"):
            self._send_json(
                {"error": f"{data.get('reason') or '当前工作区不是 git 仓库'}；无法生成提交信息"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        state = gitinfo.status(root)
        entries = state.get("entries") or []
        diff = data.get("diff") or ""
        if not diff and not entries:
            self._send_json(
                {"error": "工作区相对 HEAD 没有任何改动，无需生成提交信息"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        branch = state.get("branch") or ""
        recent = gitinfo.log(root, 8).get("commits") or []
        prompt = build_commit_prompt(branch, entries, recent, diff)

        try:
            response = session.agent.client.chat(
                [{"role": "user", "content": prompt}], temperature=0.2
            )
        except LLMError as exc:
            self._send_json(
                {"error": f"模型调用失败，无法生成提交信息：{exc}"},
                status=HTTPStatus.BAD_GATEWAY,
            )
            return

        message = strip_code_fence(response.content or "")
        if not message:
            self._send_json(
                {"error": "模型没有返回任何内容，请重试或换一个模型"},
                status=HTTPStatus.BAD_GATEWAY,
            )
            return

        session.agent.audit.record(
            "git.commit_message",
            branch=branch,
            files=len(entries),
            added=data.get("added", 0),
            removed=data.get("removed", 0),
            # diff 与 message 正文都只留本机（ADR-006），上传的是指纹与长度
            diff=diff or None,
            message_chars=len(message),
        )
        self._send_json(
            {
                "message": message,
                "branch": branch,
                "files": len(entries),
                "added": data.get("added", 0),
                "removed": data.get("removed", 0),
            }
        )

    # ------------------------------------------------------------------ SSE
    def _serve_events(self, query: dict[str, list[str]]) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]
        since = 0
        raw_since = (query.get("since") or ["0"])[0]
        if raw_since.isdigit():
            since = int(raw_since)
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id and last_event_id.isdigit():
            since = max(since, int(last_event_id))

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        # 明确告诉任何中间层不要缓冲，否则事件会攒着一起到，实时性全丢
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # 先订阅再发欢迎帧：这样「订阅之后、欢迎帧之前」产生的事件会被排进队列，
        # 按顺序在欢迎帧之后送达。反过来的话会有一个丢事件的窗口。
        subscription = ctx.bus.subscribe(since=since)

        # session.ready 刻意不带 id 行：它不参与 Last-Event-ID 的推进，
        # 只是个「欢迎帧」，把当前会话全貌交给刚连上的界面。
        try:
            self._sse_chunk(
                "event: session.ready\ndata: "
                + json.dumps(
                    {"type": P.SESSION_READY, **ctx.session.describe()},
                    ensure_ascii=False,
                )
                + "\n\n"
            )

            while not ctx.shutdown.is_set():
                event = subscription.get(timeout=KEEPALIVE_SECONDS)
                if event is None:
                    if not self._sse_chunk(": keepalive\n\n"):
                        break  # 客户端已断开
                    continue
                payload = json.dumps(event, ensure_ascii=False, default=str)
                frame = f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
                if not self._sse_chunk(frame):
                    break
        finally:
            subscription.close()
            # 结束分块流
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

    def _sse_chunk(self, text: str) -> bool:
        """写一个分块。返回 False 表示客户端已断开。"""
        data = text.encode("utf-8")
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ------------------------------------------------------------------ 预览（O1）
    def _serve_preview(self, path: str) -> None:
        """`GET /preview/<relpath>`：只读地静态服务工作区里的网页资源（O/P1-11）。

        与静态壳的分工：壳服务的是应用自己的 UI_DIR，这里服务的是**用户工作区**，
        因此三件事必须同时成立：

        1. **只读、且经 PathGuard** —— `check_read` 负责越狱/策略/敏感文件三关；
           再额外拒绝任何落在 `.vibe/` 内的路径（基线/审计/图片目录是 host 私产，
           不该经由预览端口对外暴露）。
        2. **MIME 白名单** —— 只放文本/web 资源，`.exe` 之类一律 403。
        3. **专用 CSP + Referrer-Policy: no-referrer** —— 预览页与界面同源，
           必须焊死它的网络出口（见 PREVIEW_CSP），且不允许把带 token 的 Referer 带出去。

        目录请求回落该目录下的 `index.html`（「打开这个文件夹的首页」）。
        """
        ctx = self.server.ctx  # type: ignore[attr-defined]
        paths = ctx.session.agent.paths
        raw = urllib.parse.unquote(path[len("/preview") :].lstrip("/"))

        try:
            target = paths.check_read(raw)
        except SandboxViolation as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            return

        # `.vibe/` 是 host 私产（审计 / 检查点 / 图片 / 视觉基线）：预览端口不碰它。
        rel = paths.relative(target)
        if ".vibe" in rel.split("/"):
            self._send_json(
                {"error": "拒绝预览 .vibe 内部目录（审计与检查点专用）"},
                status=HTTPStatus.FORBIDDEN,
            )
            return

        # 目录请求 → 回落该目录下的 index.html（仅在确实是目录时才回落）。
        if target.is_dir():
            target = paths.check_read(str(target / "index.html"))

        if not target.is_file():
            self._send_json({"error": "资源不存在"}, status=HTTPStatus.NOT_FOUND)
            return

        content_type = _PREVIEW_MIME.get(target.suffix.lower())
        if content_type is None:
            self._send_json(
                {
                    "error": (
                        f"不支持预览该类型的资源（{target.suffix or '无后缀'}）；"
                        "预览只服务文本 / 网页资源"
                    )
                },
                status=HTTPStatus.FORBIDDEN,
            )
            return

        try:
            size = target.stat().st_size
        except OSError as exc:
            self._send_json({"error": f"读取失败：{exc}"}, status=HTTPStatus.NOT_FOUND)
            return
        if size > _PREVIEW_MAX_BYTES:
            self._send_json(
                {"error": f"资源过大（{size} 字节），超出预览上限"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return

        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", PREVIEW_CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ 静态
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (UI_DIR / rel).resolve()

        # 静态目录下的路径穿越防护：解析后必须仍在 UI_DIR 内
        if UI_DIR.resolve() not in candidate.parents and candidate != UI_DIR.resolve():
            self._send_json({"error": "非法路径"}, status=HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self._send_json({"error": "资源不存在"}, status=HTTPStatus.NOT_FOUND)
            return

        body = candidate.read_bytes()
        content_type = _MIME.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        # 启动页 URL 带 ?token=…，禁止把 Referer 带到第三方，避免令牌外泄
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ 工具
    def _read_json(self, *, limit: int = MAX_BODY_BYTES) -> dict[str, Any]:
        """读取 JSON 请求体。`limit` 供上传图片这类**大请求体**端点放宽上限。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > limit:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        # 记下已消费的字节：收尾 _drain_body 只排空**剩余**部分，避免重复读而阻塞。
        self._body_remaining = max(0, self._body_remaining - len(raw))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _drain_body(self) -> None:
        """排空本请求**未被读取**的请求体，杜绝 keep-alive 下的连接污染。

        HTTP/1.1 keep-alive 下，客户端发来的字节若遗留在接收缓冲区，连接关闭时
        （尤其 Windows）会发出 TCP RST —— 客户端读到响应头、再读响应体时即报
        `ConnectionAbortedError [WinError 10053]`；残留字节还会被当作同一连接上
        下一请求的开头（请求走私式的错位）。

        大多数处理器会经 `_read_json` 主动读完请求体，这里剩下的一般是 0；
        但**直接回 400/403 而不读体的分支**（如跨工作区恢复被拒）必须靠这里兜底。
        """
        remaining = self._body_remaining
        if remaining <= 0:
            return
        self._body_remaining = 0
        if remaining > MAX_BODY_BYTES:
            # 体量超出正常上限：与其阻塞在一段来路不明的巨体上，不如断连 ——
            # 这类请求本就已被拒，不影响正常路径。
            self.close_connection = True
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # 读不动就放弃排空并断连，绝不把不干净的连接留给下一请求。
            self.close_connection = True

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_empty(self, status: int, *, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()


# --------------------------------------------------------------------------- 生命周期
class HostServer:
    """把「构建 → 启动 → 关闭」这几步包起来，便于 CLI 与测试复用。"""

    def __init__(
        self,
        config: Config,
        *,
        port: int = 0,
        client: Any = None,
        verbose: bool = False,
        guard: HostGuard | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self.bus = EventBus()
        # 会话持久化（A2）：store 由调用方按配置构造后传入（见 main()）。
        # 这里不做默认创建 —— 测试与临时进程不该悄悄往 ~/.vibe/sessions.db 写数据。
        self.store: SessionStore | None = store
        self.config = config
        session = Session(config, bus=self.bus, client=client, store=self.store)
        # 先绑定端口再建 guard：guard 需要实际端口才能拼出合法的 Origin 白名单
        self._server = VibeServer(("127.0.0.1", port))
        actual_port = self._server.server_address[1]
        self.guard = guard or HostGuard(port=actual_port)
        # registry / store / client 一并交给 ctx：切换工作区时要用它们原样重建
        # 一套新上下文，而不是从头再猜一遍（见 ServerContext.switch_workspace）。
        self.ctx = ServerContext(
            session,
            self.guard,
            verbose=verbose,
            store=self.store,
            registry=session.registry,
            client=client,
        )
        self._server.ctx = self.ctx

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return self.guard.launch_url

    @property
    def session(self) -> Session:
        """当前会话 —— 始终与 ctx 同源，不存快照。

        W1 切换工作区时 ServerContext 会把整个 Session 对象换掉，若这里存过
        一份引用，host.session 就会滞留在旧工作区（shutdown 也会中断错对象）。
        """
        return self.ctx.session

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.3)

    def shutdown(self) -> None:
        self.ctx.shutdown.set()
        self.session.interrupt()
        self._server.shutdown()
        self._server.server_close()
        if self.store is not None:
            self.store.close()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="vibe-http", daemon=True)
        thread.start()
        return thread


def main(argv: list[str] | None = None) -> int:
    """`python -m agent_core.host` 的入口：起服务并打开界面。"""
    import argparse
    import sys

    from .. import ui

    ui.init_console()
    parser = argparse.ArgumentParser(
        prog="vibe-agent-host",
        description="本地化 Vibe Coding Agent —— 桌面客户端后端服务",
    )
    parser.add_argument("--workspace", "-w", default=".", help="工作区目录")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示自动分配")
    parser.add_argument("--model", help="模型名")
    parser.add_argument("--base-url", help="OpenAI 兼容接入地址")
    parser.add_argument("--api-key", help="API Key")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="把访问地址以 VIBE_URL=... 的形式打到 stdout（供桌面壳解析）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出每个 HTTP 请求")
    args = parser.parse_args(argv)

    cli_overrides: dict[str, object] = {}
    if args.model:
        cli_overrides["model"] = args.model
    if args.base_url:
        cli_overrides["base_url"] = args.base_url.rstrip("/")
    if args.api_key:
        cli_overrides["api_key"] = args.api_key

    # 模型配置优先级：环境变量 > ~/.vibe/settings.json（界面保存）> 命令行 > 内核默认。
    # 注意 Config.load 里显式 overrides 会盖过环境变量，所以「环境变量已设置时不给该
    # 字段传 override」这层判断必须放在这里（见 host/settings.py 的说明）。
    overrides: dict[str, object] = dict(
        effective_model_overrides(os.environ, load_settings(), cli_overrides)
    )
    # 补全参数（D3+）与模型字段同一优先级链：环境变量 > settings.json > 内核默认。
    overrides.update(effective_completion_overrides(os.environ, load_settings()))
    # MCP 开关（L 管理面）：与模型/补全同一优先级链；关闭时不 spawn 任何子进程。
    overrides.update(effective_mcp_overrides(os.environ, load_settings()))

    try:
        config = Config.load(args.workspace, **overrides)
    except ConfigError as exc:
        ui.error(str(exc))
        return 2

    # 会话持久化（A2）：生产入口按配置建库；坏库由 SessionStore 自愈，
    # 构造本身的意外只降级为「不持久化」，绝不拦服务启动。
    store: SessionStore | None = None
    if config.session_persist:
        try:
            db_path = Path(config.session_db) if config.session_db else default_session_db_path()
            store = SessionStore(db_path)
        except Exception as exc:  # noqa: BLE001
            ui.info(f"会话持久化不可用（{type(exc).__name__}），本次不落盘。")
            store = None

    host = HostServer(config, port=args.port, verbose=args.verbose, store=store)
    ui.rule("Vibe Coding Agent · 桌面端")
    ui.info(f"工作区  {config.workspace}")
    ui.info(f"模型    {config.model} @ {config.base_url}")
    if args.print_url:
        # 给壳（Electron 主进程）解析用：单独一行，便于逐行读取
        print(f"VIBE_URL={host.url}", flush=True)
    ui.ok(f"界面地址 {host.url}")
    ui.info("按 Ctrl+C 退出。")

    if not args.no_open:
        threading.Thread(
            target=lambda: (time.sleep(0.4), webbrowser.open(host.url)),
            daemon=True,
        ).start()

    try:
        host.serve_forever()
    except KeyboardInterrupt:
        print()
        ui.info("正在关闭…")
    finally:
        host.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
