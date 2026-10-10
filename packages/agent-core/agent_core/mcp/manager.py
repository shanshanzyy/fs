"""MCP server 生命周期（L5）—— 按需启动、崩溃重启一次、退出兜底清理。

## 为什么要有这一层

MCP server 是**外部常驻进程**，我们无法保证它写得对：可能压根启动不了、
可能握完手就崩、可能返回畸形 JSON。会话不能因此受影响，所以这里的策略是：

- 按 server 名缓存实例，同一个 server 只起一个进程；
- **惰性启动**：只有真的要列工具 / 调工具时才 spawn；
- 崩溃后**自动重启一次**，再失败就把这个 server 标记为不可用
  （避免每次工具调用都去 spawn 一个必然失败的二进制，把界面卡住）；
- 空闲 `idle_seconds`（默认 0 = 不回收）自动 shutdown；
- `atexit` 兜底清理全部子进程 —— 界面上不能留下一串孤儿。

每一层失败都**只影响它自己**：`status()` 如实回 `available:false + reason`，
会话其余部分一行不变（见 tools/registry.merge_mcp）。
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .client import McpClient, McpError, McpHttpClient
from .registry import NAME_PREFIX, McpServerConfig

DEFAULT_TIMEOUT = 30.0
# 同一个 server 连续失败多少次后就标记为不可用。1 = 初启 + 重启一次。
_MAX_FAILURES = 2


class McpManager:
    """MCP server 进程池。线程安全：Agent 工作线程与 HTTP 线程可能并发调用。"""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        idle_seconds: int = 0,
        workspace: str | Path | None = None,
        client_factory: Callable[[McpServerConfig, str, float], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = float(timeout)
        self._idle_seconds = max(0, int(idle_seconds))
        self._workspace = str(workspace) if workspace else str(Path.cwd())
        self._factory = client_factory or self._default_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._servers: dict[str, McpServerConfig] = {}
        self._clients: dict[str, Any] = {}
        self._tools: dict[str, list[dict[str, Any]]] = {}
        self._last_used: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._refused: dict[str, str] = {}
        self._config_error = ""
        self._audit: Any = None
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ 接线
    def bind_audit(self, audit: Any) -> None:
        self._audit = audit

    def bind_workspace(self, workspace: str | Path | None) -> None:
        if workspace:
            self._workspace = str(workspace)

    def set_timeout(self, timeout: float) -> None:
        self._timeout = float(timeout)

    def set_config_error(self, message: str) -> None:
        """记下「配置读不出来」这类全局错误，供 status 显示。"""
        self._config_error = str(message)[:200]

    def attach(self, server: McpServerConfig) -> None:
        with self._lock:
            self._servers[server.name] = server

    def detach_all(self) -> None:
        """清掉全部 server 与实例（reload 前调用）。"""
        with self._lock:
            servers = list(self._servers)
        for name in servers:
            self._drop(name)
        with self._lock:
            self._servers.clear()
            self._tools.clear()
            self._failures.clear()
            self._refused.clear()
            self._config_error = ""

    def _default_factory(
        self, server: McpServerConfig, workspace: str, timeout: float
    ) -> Any:
        if server.transport == "http":
            return McpHttpClient(server.url, name=server.name, timeout=timeout)
        return McpClient(
            [server.command, *server.args],
            cwd=workspace,
            name=server.name,
            timeout=timeout,
            env=dict(server.env),
        )

    # ------------------------------------------------------------------ 生命周期
    def _start(self, server: McpServerConfig) -> Any:
        """spawn 一个实例并握手。失败抛 McpError。"""
        client = self._factory(server, self._workspace, self._timeout)
        client.start()
        return client

    def _ensure_client(self, name: str) -> Any:
        """取一个可用实例；没有就启动。失败重试一次后标记不可用。"""
        with self._lock:
            refused = self._refused.get(name)
            if refused:
                raise McpError(f"MCP server {name} 不可用：{refused}")
            server = self._servers.get(name)
            if server is None:
                raise McpError(f"未知 MCP server：{name}")
            self._reap()
            client = self._clients.get(name)
            if client is not None and not client.closed:
                self._last_used[name] = self._clock()
                return client
            if client is not None:
                self._clients.pop(name, None)

        last = ""
        for _ in range(_MAX_FAILURES):
            started = self._clock()
            try:
                client = self._start(server)
                tools = client.list_tools()
            except McpError as exc:
                last = str(exc)
                self._register_failure(name, last)
                with self._lock:
                    if name in self._refused:
                        break
                continue
            with self._lock:
                self._clients[name] = client
                self._tools[name] = tools
                self._last_used[name] = self._clock()
                self._failures.pop(name, None)
            self._record_start(
                server.name,
                server.transport,
                ok=True,
                tools=len(tools),
                ms=int((self._clock() - started) * 1000),
            )
            return client

        self._record_start(server.name, server.transport, ok=False, tools=0, ms=0)
        raise McpError(f"MCP server {name} 不可用：{last}")

    def _register_failure(self, name: str, reason: str) -> None:
        with self._lock:
            self._clients.pop(name, None)
            count = self._failures.get(name, 0) + 1
            self._failures[name] = count
            if count >= _MAX_FAILURES:
                self._refused[name] = reason[:200]

    def _drop(self, name: str) -> None:
        with self._lock:
            client = self._clients.pop(name, None)
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 — 收尾失败不该影响调用方
                pass

    def _reap(self) -> None:
        """空闲回收。调用方必须已持有 `self._lock`。"""
        if self._idle_seconds <= 0:
            return
        now = self._clock()
        expired = [
            name
            for name, used in self._last_used.items()
            if now - used >= self._idle_seconds and name in self._clients
        ]
        for name in expired:
            client = self._clients.pop(name)
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 调用
    def list_tools(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            cached = self._tools.get(name)
        if cached is not None:
            return cached
        self._ensure_client(name)
        with self._lock:
            return self._tools.get(name) or []

    def call(self, name: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client(name)
        try:
            return client.call_tool(tool, arguments)
        except McpError:
            # 调用中途进程死了：丢掉实例，下一次调用会重启一次。
            self._drop(name)
            raise

    def refuse(self, name: str, reason: str) -> None:
        with self._lock:
            self._refused[name] = str(reason)[:200]

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        """`GET /api/mcp/status` 的载荷。只读，永不抛。"""
        with self._lock:
            servers = list(self._servers.values())
            refused = dict(self._refused)
            tools = {name: list(items) for name, items in self._tools.items()}
            clients = dict(self._clients)
            config_error = self._config_error
        entries: list[dict[str, Any]] = []
        for server in servers:
            client = clients.get(server.name)
            running = client is not None and not client.closed
            reason = refused.get(server.name, "")
            if not reason and not running and server.name not in tools:
                reason = "尚未启动（首次调用时惰性启动）"
            entries.append(
                {
                    **server.to_api(),
                    "available": not refused.get(server.name),
                    "reason": reason,
                    "running": running,
                    "tools": [
                        f"{NAME_PREFIX}{server.name}__{str(item.get('name') or '')}"
                        for item in tools.get(server.name, [])
                    ],
                }
            )
        return {"servers": entries, "configError": config_error}

    def _record_start(
        self, server: str, transport: str, *, ok: bool, tools: int, ms: int
    ) -> None:
        """记一条 `mcp.server_start`。审计绝不阻断主流程，异常一律吞掉。"""
        audit = self._audit
        if audit is None:
            return
        try:
            audit.record(
                "mcp.server_start",
                server=server,
                transport=transport,
                ok=ok,
                tools=tools,
                ms=ms,
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 收尾
    def shutdown(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- 单例
_DEFAULT: McpManager | None = None
_DEFAULT_LOCK = threading.Lock()


def get_manager() -> McpManager:
    """进程内单例。MCP server 进程是稀缺资源，不该每个端点各起一套。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = McpManager()
        return _DEFAULT


def set_manager(manager: McpManager | None) -> None:
    """替换单例（测试用：注入假 server）。`None` = 恢复默认。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        previous = _DEFAULT
        _DEFAULT = manager
    if previous is not None and previous is not manager:
        previous.shutdown()


__all__ = ["DEFAULT_TIMEOUT", "McpManager", "get_manager", "set_manager"]