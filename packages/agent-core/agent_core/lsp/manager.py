"""语言服务器生命周期（E3）—— 按需启停、空闲回收、崩一次就标记不可用。

## 为什么要有这一层

语言服务器是**常驻进程**，而用户可能开着界面几个小时只看几个文件。
「进程常驻」与「用得少」放在一起就是资源泄漏：内存、文件句柄、以及
排障时一串看不出用途的孤儿进程。因此这里的策略是：

- 按 `(工作区, 语言)` 缓存实例，同一种语言只起一个进程；
- **空闲 `lsp_idle_seconds`（默认 300 秒）自动 shutdown** —— 不常驻；
- 进程退出时 `atexit` 兜底清理，Windows 上钩子失效也不会留下孤儿；
- 崩溃后**自动重启一次**，再失败就把这门语言标记为不可用（避免每次请求
  都去 spawn 一个必然失败的二进制，把界面卡住）。

## 与前端的边界

这一层只产出**数据**（诊断、定义位置、重命名的逐文件编辑），
绝不落盘。落盘在 `host/session.py` 里走补丁引擎 + 检查点 + 审计（ADR-004/011）。

语言服务器只在本机跑，不含远端服务器模式（ADR-014）。
"""

from __future__ import annotations

import atexit
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import servers
from .client import LspClient, LspError

DEFAULT_IDLE_SECONDS = 300
# 单个请求的等待上限。语言服务器首次索引一个项目可能要几秒，
# 但超过这个数通常意味着它卡住了（或根本没实现该方法）。
_REQUEST_TIMEOUT = 20.0
# 诊断是「服务器主动推」的，但我们不能无限等：没装好/不支持的文件
# 可能永远不推。等不到就给空结果（界面显示「暂无诊断」而不是转圈）。
_DIAGNOSTICS_TIMEOUT = 8.0

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _uri_to_path(uri: str) -> Path | None:
    """`file:///C:/x/y.py` → `C:\\x\\y.py`；非 file 协议返回 None。

    语言服务器有时会返回 `untitled:` 或 `jdt:` 之类我们自己不认识的 scheme，
    直接判 None 比构造出一个离谱路径要好。
    """
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme and parts.scheme != "file":
        return None
    raw = urllib.parse.unquote(parts.path or "")
    if not raw:
        return None
    # Windows 上 url path 形如 /C:/dir/file，去掉前导斜杠
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    try:
        return Path(raw)
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    """按发送给语言服务器的口径读文件：统一 \\n，容错解码。"""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class _Entry:
    """一个 `(工作区, 语言)` 的活实例。"""

    client: LspClient
    language: str
    workspace: str
    last_used: float
    # 已同步给服务器的文档：uri -> 内容指纹（bool 简化成「内容是否已同步」）
    synced: dict[str, str] = field(default_factory=dict)
    restarts: int = 0
    request_count: int = 0

    def to_api(self, now: float) -> dict[str, Any]:
        return {
            "language": self.language,
            "command": Path(self.client.argv[0]).name,
            "workspace": self.workspace,
            "idleSeconds": round(max(0.0, now - self.last_used), 1),
            "requests": self.request_count,
            "openDocuments": len(self.synced),
        }


class LspManager:
    """语言服务器池。线程安全：HTTP 线程可能并发问同一个实例。"""

    def __init__(
        self,
        *,
        idle_seconds: int = DEFAULT_IDLE_SECONDS,
        resolver: Callable[..., servers.ServerInfo] = servers.resolve,
        clock: Callable[[], float] = time.monotonic,
        request_timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        self._idle_seconds = max(0, int(idle_seconds))
        self._resolver = resolver
        self._clock = clock
        self._request_timeout = float(request_timeout)
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._broken: dict[str, str] = {}  # language -> 原因（重启一次仍失败）
        self._closed = False
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ 状态
    def status(self, config: Any) -> dict[str, Any]:
        """`/api/lsp/status` 的载荷：模式 + 全量探测清单 + 当前活实例。"""
        mode = str(getattr(config, "lsp_mode", "auto") or "auto")
        listing = [info.to_api() for info in servers.detect_all()]
        available = [item for item in listing if item["available"]]
        with self._lock:
            now = self._clock()
            running = [entry.to_api(now) for entry in self._entries.values()]
            broken = dict(self._broken)
        reason = ""
        if mode == "off":
            reason = "LSP 已被配置关闭（lsp_mode=off）"
        elif not available:
            reason = "本机没有安装任何受支持的语言服务器"
        elif broken:
            reason = "以下语言服务器启动/运行失败：" + "；".join(
                f"{lang}（{text}）" for lang, text in sorted(broken.items())
            )
        return {
            "available": bool(mode != "off" and available and not broken),
            "mode": mode,
            "idleSeconds": self._idle_seconds,
            "reason": reason,
            "servers": listing,
            "running": running,
        }

    def available(self, config: Any) -> bool:
        """能力探测（廉价，只查 PATH）：决定诊断工具要不要进工具清单。"""
        if str(getattr(config, "lsp_mode", "auto") or "auto") == "off":
            return False
        with self._lock:
            if self._broken:
                return False
        return servers.any_available()

    # ------------------------------------------------------------------ 三个查询
    def diagnostics(self, config: Any, abs_path: Path, rel: str) -> dict[str, Any]:
        return self._query(
            config,
            abs_path,
            rel,
            lambda client, uri, lang: self._diagnostics(client, uri),
        )

    def definition(
        self, config: Any, abs_path: Path, rel: str, line: int, char: int
    ) -> dict[str, Any]:
        position = {"line": max(0, int(line)), "character": max(0, int(char))}
        return self._query(
            config,
            abs_path,
            rel,
            lambda client, uri, lang: self._definition(client, uri, position, abs_path),
        )

    def rename(
        self,
        config: Any,
        abs_path: Path,
        rel: str,
        line: int,
        char: int,
        new_name: str,
    ) -> dict[str, Any]:
        position = {"line": max(0, int(line)), "character": max(0, int(char))}
        return self._query(
            config,
            abs_path,
            rel,
            lambda client, uri, lang: self._rename(client, uri, position, new_name),
        )

    # ------------------------------------------------------------------ 内部
    def _unavailable(self, reason: str, **extra: Any) -> dict[str, Any]:
        return {"available": False, "reason": reason, **extra}

    def _query(
        self,
        config: Any,
        abs_path: Path,
        rel: str,
        action: Callable[[LspClient, str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        """起/取一个实例、同步文档、执行动作；崩溃自动重启一次。"""
        mode = str(getattr(config, "lsp_mode", "auto") or "auto")
        if mode == "off":
            return self._unavailable("LSP 已被配置关闭（lsp_mode=off）")

        info = self._resolver(abs_path)
        if not info.available:
            return self._unavailable(info.reason, language=info.language)
        with self._lock:
            if self._broken.get(info.language):
                return self._unavailable(
                    f"语言服务器 {info.language} 已标记不可用：{self._broken[info.language]}",
                    language=info.language,
                )

        workspace = str(Path(abs_path).resolve().parent)
        key = (self._workspace_key(config, abs_path), info.language)
        for attempt in (0, 1):
            try:
                client = self._acquire(config, key, info)
                uri = Path(abs_path).resolve().as_uri()
                self._sync(client, uri, info.language, _read_text(abs_path))
                payload = action(client, uri, info.language)
            except LspError as exc:
                self._forget(key, reason=str(exc))
                if attempt == 0 and info.available:
                    # 崩溃后重启一次：多数情况是首次索引把进程搞挂了
                    with self._lock:
                        self._broken.pop(info.language, None)
                    continue
                self._mark_broken(info.language, str(exc))
                return self._unavailable(f"语言服务器调用失败：{exc}", language=info.language)
            except OSError as exc:
                return self._unavailable(f"读取文件失败：{exc}", language=info.language)
            else:
                return {
                    "available": True,
                    "language": info.language,
                    "server": info.command,
                    **payload,
                }
        return self._unavailable("语言服务器不可用", language=info.language)

    def _diagnostics(self, client: LspClient, uri: str) -> dict[str, Any]:
        raw = client.wait_diagnostics(uri, timeout=_DIAGNOSTICS_TIMEOUT)
        items: list[dict[str, Any]] = []
        counts = {"error": 0, "warning": 0, "info": 0, "hint": 0}
        for item in raw:
            if not isinstance(item, dict):
                continue
            severity = _SEVERITY.get(int(item.get("severity") or 0), "info")
            counts[severity] = counts.get(severity, 0) + 1
            start = (item.get("range") or {}).get("start") or {}
            end = (item.get("range") or {}).get("end") or {}
            code = item.get("code")
            items.append(
                {
                    "line": int(start.get("line") or 0),
                    "char": int(start.get("character") or 0),
                    "endLine": int(end.get("line") or 0),
                    "endChar": int(end.get("character") or 0),
                    "severity": severity,
                    "message": str(item.get("message") or ""),
                    "source": str(item.get("source") or ""),
                    "code": str(code) if code not in (None, "") else "",
                }
            )
        items.sort(key=lambda d: (d["line"], d["char"], d["severity"]))
        return {"diagnostics": items, "counts": counts}

    def _definition(
        self, client: LspClient, uri: str, position: dict[str, int], abs_path: Path
    ) -> dict[str, Any]:
        result = client.request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=self._request_timeout,
        )
        locations: list[dict[str, Any]] = []
        workspace = abs_path.resolve().parent
        for item in _as_list(result):
            target = _location_of(item)
            if target is None:
                continue
            path, start, end = target
            try:
                rel = str(path.resolve().relative_to(workspace))
            except (ValueError, OSError):
                rel = ""
            locations.append(
                {
                    "path": rel.replace("\\", "/"),
                    "outside": not rel,
                    "line": int(start.get("line") or 0),
                    "char": int(start.get("character") or 0),
                    "endLine": int(end.get("line") or 0),
                    "endChar": int(end.get("character") or 0),
                }
            )
        return {"locations": locations}

    def _rename(
        self, client: LspClient, uri: str, position: dict[str, int], new_name: str
    ) -> dict[str, Any]:
        result = client.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": position,
                "newName": new_name,
            },
            timeout=self._request_timeout,
        )
        files = _workspace_edit_files(result)
        return {"edits": files, "nameChars": len(new_name)}

    # ------------------------------------------------------------------ 进程池
    def _workspace_key(self, config: Any, abs_path: Path) -> str:
        workspace = getattr(config, "workspace", None)
        if workspace:
            return str(Path(workspace).resolve())
        return str(abs_path.resolve().parent)

    def _acquire(self, config: Any, key: tuple[str, str], info: servers.ServerInfo) -> LspClient:
        with self._lock:
            self._reap()
            entry = self._entries.get(key)
            if entry is not None and not entry.client.closed:
                entry.last_used = self._clock()
                entry.request_count += 1
                return entry.client
            if entry is not None:
                # 客户端死了但条目还在：清掉，走重建
                self._entries.pop(key, None)

        idle = int(getattr(config, "lsp_idle_seconds", self._idle_seconds) or self._idle_seconds)
        if idle > 0:
            with self._lock:
                self._idle_seconds = idle

        client = LspClient(info.argv, key[0], name=info.command or info.language,
                           timeout=self._request_timeout)
        client.start(key[0])
        with self._lock:
            previous = self._entries.get(key)
            self._entries[key] = _Entry(
                client=client,
                language=info.language,
                workspace=key[0],
                last_used=self._clock(),
                restarts=(previous.restarts if previous else 0),
                request_count=1,
            )
        return client

    def _sync(self, client: LspClient, uri: str, language: str, text: str) -> None:
        entry = self._entry_for(client)
        with self._lock:
            already = entry.synced.get(uri) if entry is not None else None
        if already == text:
            return
        language_id = servers.LANGUAGE_IDS.get(language, language)
        if already is None:
            client.open_document(uri, language_id, text)
        else:
            client.change_document(uri, text)
        with self._lock:
            if entry is not None:
                entry.synced[uri] = text

    def _entry_for(self, client: LspClient) -> _Entry | None:
        with self._lock:
            for entry in self._entries.values():
                if entry.client is client:
                    return entry
        return None

    def _reap(self) -> None:
        """空闲回收。调用方必须已持有 `self._lock`。"""
        if self._idle_seconds <= 0:
            return
        now = self._clock()
        expired = [
            key for key, entry in self._entries.items()
            if now - entry.last_used >= self._idle_seconds
        ]
        for key in expired:
            entry = self._entries.pop(key)
            entry.client.stop()

    def _forget(self, key: tuple[str, str], *, reason: str = "") -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            entry.client.stop()

    def _mark_broken(self, language: str, reason: str) -> None:
        with self._lock:
            self._broken[language] = reason[:200]

    def reset_availability(self) -> None:
        """清掉「不可用」标记（用户装好服务器后不必重启进程）。"""
        with self._lock:
            self._broken.clear()

    # ------------------------------------------------------------------ 收尾
    def running(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self._clock()
            return [entry.to_api(now) for entry in self._entries.values()]

    def shutdown(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._closed = True
        for entry in entries:
            entry.client.stop()


# --------------------------------------------------------------------------- 归一化
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _location_of(item: Any) -> tuple[Path, dict, dict] | None:
    """兼容 `Location` 与 `LocationLink` 两种形态。"""
    if not isinstance(item, dict):
        return None
    if "targetUri" in item:  # LocationLink
        uri = str(item.get("targetUri") or "")
        start = item.get("targetSelectionRange", {}).get("start") or item.get("targetRange", {}).get("start") or {}
        end = item.get("targetSelectionRange", {}).get("end") or item.get("targetRange", {}).get("end") or {}
    else:  # Location
        uri = str(item.get("uri") or "")
        start = (item.get("range") or {}).get("start") or {}
        end = (item.get("range") or {}).get("end") or {}
    path = _uri_to_path(uri)
    if path is None or not path.is_file():
        return None
    return path, start, end


def _workspace_edit_files(edit: Any) -> list[dict[str, Any]]:
    """把 `WorkspaceEdit` 归一成 `[{uri, edits[]}]`。

    两种形态都要认：`changes`（老的、按 uri 分组）与 `documentChanges`
    （新的、带版本与创建/重命名操作）。这里只处理纯文本编辑 ——
    「重命名文件」这类操作对改名功能是意外情况，忽略比误改更安全。
    """
    if not isinstance(edit, dict):
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}

    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            grouped.setdefault(str(uri), []).extend(_normalize_edits(edits))

    for item in _as_list(edit.get("documentChanges")):
        if not isinstance(item, dict):
            continue
        doc = item.get("textDocument") or {}
        uri = str(doc.get("uri") or "")
        if uri:
            grouped.setdefault(uri, []).extend(_normalize_edits(item.get("edits")))

    return [
        {"uri": uri, "edits": edits}
        for uri, edits in grouped.items()
        if edits
    ]


def _normalize_edits(edits: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _as_list(edits):
        if not isinstance(item, dict):
            continue
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        out.append(
            {
                "startLine": int(start.get("line") or 0),
                "startChar": int(start.get("character") or 0),
                "endLine": int(end.get("line") or 0),
                "endChar": int(end.get("character") or 0),
                "newText": str(item.get("newText") or ""),
            }
        )
    # 从后往前应用才不会互相挪位；这里先排好序，落盘侧就不用再想这件事
    out.sort(key=lambda e: (e["startLine"], e["startChar"]), reverse=True)
    return out


# --------------------------------------------------------------------------- 单例
_DEFAULT: LspManager | None = None
_DEFAULT_LOCK = threading.Lock()


def get_manager() -> LspManager:
    """进程内单例。语言服务器进程是稀缺资源，不该每个端点各起一套。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = LspManager()
        return _DEFAULT


def set_manager(manager: LspManager | None) -> None:
    """替换单例（测试用：注入假语言服务器）。`None` = 恢复默认。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        previous = _DEFAULT
        _DEFAULT = manager
    if previous is not None and previous is not manager:
        previous.shutdown()


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "LspManager",
    "get_manager",
    "set_manager",
]