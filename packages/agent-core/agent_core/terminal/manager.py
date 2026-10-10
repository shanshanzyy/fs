"""终端会话管理（阶段 M / P1-7）—— 探测后端、降级、空闲回收、退出兜底。

## 定位

终端是**用户在 IDE 里敲命令**（ADR-011 范式）：不走审批浮层，但记审计、受路径
沙箱约束，且**与 Agent 通道彻底隔离** —— Agent 想交互只能提需求，由用户执行
（Agent 跑命令仍走受审批的 `run_command`）。

## 后端探测与降级

- Unix：用 `pty_unix`（标准库 `os.openpty`）；
- Windows：试 `pty_windows`（`ctypes` ConPTY），**任一 API 不可用即降级为管道模式**
  （`subprocess` + stdin/stdout 管道，无 TTY 语义、交互能力受限，但功能不崩）。

降级是**显式的**：`status()` 如实回 `mode` 与 `fallbackReason`，界面据此给一行说明。

## 生命周期

- 上限 `max_sessions`（默认 3），超了明确拒绝而不是无限起进程；
- 空闲 `idle_seconds` 回收（默认 30 分钟）；
- 每个会话一个读线程把后端输出推给回调；
- `atexit` 兜底 kill —— 界面上绝不能留一串孤儿（Windows 走 `taskkill /T /F`）。
"""

from __future__ import annotations

import atexit
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_MAX_SESSIONS = 3
DEFAULT_IDLE_SECONDS = 1800
READ_TIMEOUT = 0.2
# 单条事件里承载的终端输出上限（防一个狂刷输出的程序把总线撑爆）；超出的分片，
# 读循环天然会切成多条。
READ_CHUNK = 65536


class TerminalError(RuntimeError):
    """终端操作的业务性错误（数量超限、id 不存在等）。"""


class _PipeSession:
    """降级后端：普通管道，无 TTY 语义。

    交互程序（top / vim）在此模式下表现受限（不会重排、没有光标控制），
    但读写、输出捕获照常 —— 「功能不崩」是这条路径的唯一目标。
    """

    mode = "pipe"

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        *,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self.argv = list(argv)
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self._closed = False
        self._eof = False
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._proc = subprocess.Popen(  # noqa: S603 — argv 数组 + shell=False
            self.argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=(os.name != "nt"),
        )
        self._reader = threading.Thread(
            target=self._read_loop, name="pipe-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        stream = self._proc.stdout
        while stream is not None:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                self._eof = True
                self._queue.put(None)
                return
            self._queue.put(chunk)

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def eof(self) -> bool:
        return self._eof

    @property
    def alive(self) -> bool:
        return (not self._closed) and self._proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self._proc.poll()

    def read(self, timeout: float = READ_TIMEOUT) -> bytes:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return b""
        return b"" if item is None else item

    def write(self, data: bytes) -> None:
        if not data or self._closed or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def resize(self, cols: int, rows: int) -> None:
        # 管道没有 TTY 语义，resize 是 no-op —— 但**不能报错**（见计划验收）。
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _kill_tree(self._proc.pid, self._proc)
        try:
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def _kill_tree(pid: int, proc: Any = None) -> None:
    """按进程树收尾。Windows 用 taskkill /T /F；POSIX 用进程组信号。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return
    if proc is not None and pgid == os.getpgid(0):
        # 管道后端没单独开会话时不能按组杀，否则会连自己一起杀。
        proc.terminate()
        return
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.1)


def _default_spawn() -> Callable[..., Any]:
    if os.name == "nt":
        from . import pty_windows

        return pty_windows.spawn
    from . import pty_unix

    return pty_unix.spawn


def default_shell_argv(configured: str = "") -> list[str]:
    """终端默认启动的程序：显式配置优先，否则按平台自动选。

    只接受**单条命令名/路径**（`terminal_shell` 是一个词，不是一整条命令行），
    argv 由这里固定组装 —— 模型与前端都改不了它（受信配置）。
    """
    value = (configured or "").strip()
    if value:
        return [value]
    if os.name == "nt":
        return ["powershell.exe", "-NoLogo"]
    return [os.environ.get("SHELL") or "/bin/sh", "-i"]


@dataclass
class TerminalSession:
    """一个活着的终端会话。`alive` 实时反映后端进程状态。"""

    id: str
    argv: list[str]
    cwd: str
    cols: int
    rows: int
    mode: str
    pid: int
    opened_at: float
    last_used: float
    closed: bool = False
    backend: Any = None
    reader: threading.Thread | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return bool(self.backend is not None and self.backend.alive)

    def to_api(self, now: float) -> dict[str, Any]:
        return {
            "termId": self.id,
            "mode": self.mode,
            "cols": int(self.cols),
            "rows": int(self.rows),
            "pid": int(self.pid),
            "alive": self.alive,
            "idleSeconds": round(max(0.0, now - self.last_used), 1),
        }


class TerminalManager:
    """终端进程池。线程安全：HTTP 线程写、读线程推、可能并发。"""

    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_seconds: int = DEFAULT_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        self._max = max(1, int(max_sessions))
        self._idle = max(0, int(idle_seconds))
        self._clock = clock
        self._spawn = spawn or _default_spawn()
        self._lock = threading.RLock()
        self._sessions: dict[str, TerminalSession] = {}
        self._seq = 0
        self._closed = False
        self.last_mode = ""
        self.fallback_reason = ""
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ 配置
    def set_max_sessions(self, value: Any) -> None:
        try:
            self._max = max(1, int(value))
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------ 打开
    def open(
        self,
        *,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        cols: int = 80,
        rows: int = 24,
        on_data: Callable[[str, bytes], None] | None = None,
        on_exit: Callable[[str, int | None], None] | None = None,
    ) -> TerminalSession:
        argv = [str(item) for item in argv if str(item) != ""]
        if not argv:
            raise TerminalError("终端命令为空")
        cols = max(1, min(int(cols), 1000))
        rows = max(1, min(int(rows), 1000))

        with self._lock:
            if self._closed:
                raise TerminalError("终端管理器已关闭")
            self._reap()
            if len(self._sessions) >= self._max:
                raise TerminalError(f"终端数量已达上限（{self._max} 个）")

        backend, mode = self._create(argv, cwd, env, cols, rows)
        now = self._clock()
        with self._lock:
            self._seq += 1
            term_id = f"term_{self._seq}"
        session = TerminalSession(
            id=term_id,
            argv=argv,
            cwd=str(cwd),
            cols=cols,
            rows=rows,
            mode=mode,
            pid=int(getattr(backend, "pid", 0) or 0),
            opened_at=now,
            last_used=now,
            backend=backend,
        )
        with self._lock:
            self._sessions[term_id] = session
        reader = threading.Thread(
            target=self._pump,
            args=(session, on_data, on_exit),
            name=f"term-{term_id}",
            daemon=True,
        )
        session.reader = reader
        reader.start()
        return session

    def _create(
        self, argv: list[str], cwd: str, env: dict[str, str], cols: int, rows: int
    ) -> tuple[Any, str]:
        """先试伪终端，失败则降级管道，并记下降级原因。"""
        try:
            backend = self._spawn(argv, cwd, env, cols=cols, rows=rows)
            self.last_mode = str(getattr(backend, "mode", "pty"))
            self.fallback_reason = ""
            return backend, self.last_mode
        except Exception as exc:  # noqa: BLE001 — 任何起不来都降级，而不是把界面搞崩
            reason = f"{type(exc).__name__}: {exc}"
        backend = _PipeSession(argv, cwd, env, cols=cols, rows=rows)
        self.last_mode = "pipe"
        self.fallback_reason = reason[:200]
        return backend, "pipe"

    def _pump(
        self,
        session: TerminalSession,
        on_data: Callable[[str, bytes], None] | None,
        on_exit: Callable[[str, int | None], None] | None,
    ) -> None:
        backend = session.backend
        try:
            while True:
                chunk = backend.read(READ_TIMEOUT)
                if chunk:
                    session.last_used = self._clock()
                    if on_data is not None:
                        try:
                            on_data(session.id, chunk)
                        except Exception:  # noqa: BLE001 — 回调异常不能拖垮读线程
                            pass
                    continue
                if getattr(backend, "eof", False) or not backend.alive:
                    break
        finally:
            code = backend.exit_code
            with self._lock:
                was_closed = session.closed
                self._sessions.pop(session.id, None)
            if not was_closed and on_exit is not None:
                try:
                    on_exit(session.id, code)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ 操作
    def get(self, term_id: str) -> TerminalSession | None:
        with self._lock:
            return self._sessions.get(term_id)

    def write(self, term_id: str, data: bytes) -> None:
        session = self._require(term_id)
        session.last_used = self._clock()
        session.backend.write(data)

    def resize(self, term_id: str, cols: int, rows: int) -> None:
        session = self._require(term_id)
        session.last_used = self._clock()
        session.cols = max(1, min(int(cols), 1000))
        session.rows = max(1, min(int(rows), 1000))
        session.backend.resize(session.cols, session.rows)

    def close(self, term_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(term_id, None)
        if session is None:
            raise TerminalError(f"终端不存在或已关闭：{term_id}")
        session.closed = True
        backend = session.backend
        try:
            backend.close()
        finally:
            # 幂等：即便 close 抛错，也要把会话标记为已终止、从池里摘掉。
            with self._lock:
                self._sessions.pop(term_id, None)
        return {
            "termId": term_id,
            "mode": session.mode,
            "exitCode": backend.exit_code,
            "ms": int((self._clock() - session.opened_at) * 1000),
        }

    def _require(self, term_id: str) -> TerminalSession:
        session = self.get(term_id)
        if session is None:
            raise TerminalError(f"终端不存在或已关闭：{term_id}")
        return session

    def _reap(self) -> None:
        """空闲回收。调用方必须已持有 `self._lock`。"""
        if self._idle <= 0:
            return
        now = self._clock()
        expired = [
            tid
            for tid, session in self._sessions.items()
            if now - session.last_used >= self._idle
        ]
        for tid in expired:
            session = self._sessions.pop(tid)
            session.closed = True
            try:
                session.backend.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            sessions = [s.to_api(now) for s in self._sessions.values()]
        return {
            "mode": self.last_mode,
            "fallbackReason": self.fallback_reason,
            "maxSessions": self._max,
            "idleSeconds": self._idle,
            "sessions": sessions,
        }

    # ------------------------------------------------------------------ 收尾
    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._closed = True
        for session in sessions:
            session.closed = True
            try:
                session.backend.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- 单例
_DEFAULT: TerminalManager | None = None
_DEFAULT_LOCK = threading.Lock()


def get_manager() -> TerminalManager:
    """进程内单例。终端进程是稀缺资源，不该每个端点各起一套。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = TerminalManager()
        return _DEFAULT


def set_manager(manager: TerminalManager | None) -> None:
    """替换单例（测试用：注入假后端）。`None` = 恢复默认。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        previous = _DEFAULT
        _DEFAULT = manager
    if previous is not None and previous is not manager:
        previous.shutdown()


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_SESSIONS",
    "TerminalError",
    "TerminalManager",
    "TerminalSession",
    "default_shell_argv",
    "get_manager",
    "set_manager",
]