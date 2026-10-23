"""SSH 本地端口转发：把远端设备上的环回端口搬到本机环回（P3 / ADR-040）。

## 这个文件解决的是「预览的第四个来源」

预览已经有三档来源：工作区文件（`/preview/`）、审批期草稿（`/preview-draft/`）、
本机 dev server（P2 的跨源帧）。第四档是**远端设备上跑着的服务** —— 代码在远端，
`npm run dev` 也在远端，但人坐在这台机器前面。缺了这一段，「Agent 在远端改完 →
人看效果」的闭环就断了。

## 三条硬约束

1. **只碰环回。** `-L` 的规格在 `transport.forward_argv()` 里由代码拼死：
   本地监听 `127.0.0.1`、远端目标 `127.0.0.1`。用户能给的只有两个**整数端口**。
   否则 `-L 8080:10.0.0.5:6379` 这种输入就等于开了一条没有审批的隧道，
   把远端所在内网的任意服务拽到本机。
2. **本地端口由我们分配**（bind 0 → 取端口 → 放开 → 交给 ssh），不提供
   「指定本地端口」选项。理由同上：本地端口一旦可指定，它就成了另一个
   可以打穿环回假设的自由度。
3. **不新增凭据通道。** 复用 `RemoteConnection` 与 `SSHTransport.base_argv()`
   的全部加固（`BatchMode`、指纹策略、`IdentitiesOnly`），本文件不碰密钥、
   不读密码、不解析 `~/.ssh/config` 里我们没校验过的东西。

## 孤儿进程：不是「事后清理」，而是「不可能成为孤儿」

`-N`（不建会话）是端口转发的常见写法，但它有一个致命细节：**ssh 不看 stdin**。
于是父进程无论怎么死（崩溃、被 SIGKILL），那条 ssh 都还活着、本地监听也还在，
下次启动就会撞上「端口被占」而查不出是谁占的。

这里改用「建会话 + 远端跑 `cat >/dev/null`」：本地 stdin 是父进程持有的管道，
父进程一死，内核关掉写端 → ssh 读到 EOF → 远端 `cat` 退出 → 会话关闭 → ssh 退出
→ 本地监听消失。**这条链由内核保证，与父进程是正常退出还是被 kill -9 无关**，
也不需要落地 pid 再回来逐个甄别（pid 会重用，靠 pid 杀进程本身是危险的）。

## 与 ADR-006 的关系

转发的是 TCP 字节，不经过我们的内核：没有一条远端响应会进模型上下文，
也不会有任何正文进审计。审计只记「连接名 / 两个端口 / 动作 / 耗时」。
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .connection import RemoteConnection
from .errors import RemoteError
from .transport import FORWARD_HOST, SSHTransport

# 一条转发 = 一个本地监听口 + 一个 ssh 进程。上限刻意小：
# 这是「看一个 dev server」的功能，不是通用端口映射工具。
MAX_TUNNELS = 4

# 从 spawn 到本地监听可接受连接。含 SSH 握手（首次可能几百毫秒），
# 但不该等一个永远不来的东西 —— 超时后由调用方拿到明确失败。
READY_TIMEOUT = 15.0
POLL_INTERVAL = 0.05

# 正常路径下 `stdin.close()` 就够了（见模块头）；terminate / kill 只是兜底。
STOP_GRACE = 3.0

# 意外断开后的重建：次数与退避都有限。无限重连会让一个已经消失的远端主机
# 变成后台永动机 —— 每次失败都要起一个 ssh 进程、打一次握手。
RECONNECT_ATTEMPTS = 3
RECONNECT_BACKOFF = (0.5, 1.0, 2.0)

# 状态机取值。`starting` 只在首次建立时出现；重连期间是 `reconnecting`。
TUNNEL_STATES = ("starting", "up", "reconnecting", "down", "closed")

# 重连退避的等待上限（测试里会把它调小，避免真等 3.5 秒）。
_BACKOFF_FACTOR = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_remote_port(raw: Any) -> int:
    """校验「远端设备上的端口」。返回 int，失败 `ValueError`（server 层映射 400）。

    刻意接受数字字符串：界面输入框拿到的天然是字符串，而 `"5173"` 与 `5173`
    表达的是同一个端口 —— 在这里拒绝只会逼调用方各自 `int()` 一遍，
    反而更容易在别处漏掉校验。布尔值必须挡住（`True == 1`）。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("远端端口必须是整数")
    text = str(raw).strip()
    if not text:
        raise ValueError("远端端口不能为空")
    if not text.isdigit():
        raise ValueError(f"远端端口必须是正整数（实际: {raw!r}）")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"远端端口超出范围 1-65535（实际: {port}）")
    return port


def _free_loopback_port() -> int:
    """向内核要一个空闲的环回端口。**只绑 127.0.0.1**，不碰 0.0.0.0。

    先绑再放开的竞态是存在的（另一进程可能抢走），所以 `_launch` 里失败会重试；
    这里不做「扫描端口区间」那种自作聪明的事。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((FORWARD_HOST, 0))
        return int(sock.getsockname()[1])


def _accepts_connection(port: int, timeout: float = 0.3) -> bool:
    """本地端口是否已能建立 TCP 连接。

    **注意它能证明什么**：ssh 已经把本地监听建起来了。它**不能**证明远端那个
    端口有服务在跑 —— `-L` 的本地监听是先建的，远端拒绝只在真正连上去时才暴露。
    界面文案必须与这个口径一致，不能把「已建立」写成「远端可用」。
    """
    try:
        with socket.create_connection((FORWARD_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


class _StderrDrain:
    """把 ssh 的 stderr 持续读走，只保留末尾一段。

    必须**不停地读**：管道缓冲区填满后 ssh 会阻塞在写 stderr 上，
    现象是「转发还能用，但进程卡死、退出也退不掉」。只在退出时读是行不通的。
    """

    def __init__(self, stream: Any, *, limit: int = 4096) -> None:
        self._stream = stream
        self._limit = limit
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(1024)
                if not chunk:
                    break
                with self._lock:
                    self._buf.extend(chunk)
                    if len(self._buf) > self._limit:
                        del self._buf[: len(self._buf) - self._limit]
        except (OSError, ValueError):
            pass
        finally:
            # 进程退出后把管道关掉：否则 `Popen` 持有的文件对象会活到 GC，
            # 在 POSIX 上是 fd 泄漏，在 Windows 上是测试里成片的 ResourceWarning。
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass

    def text(self) -> str:
        with self._lock:
            return self._buf.decode("utf-8", errors="replace").strip()


class SSHTunnel:
    """一条转发通道：一个本地监听口 + 一个 ssh 子进程 + 一个监工线程。"""

    def __init__(
        self,
        conn: RemoteConnection,
        remote_port: int,
        *,
        on_state: Callable[[dict[str, Any]], None] | None = None,
        transport: SSHTransport | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.conn = conn
        self.remote_port = int(remote_port)
        self.local_port = 0
        # 引用计数由 `TunnelManager` 维护（两个标签看同一个远端服务时共用一条转发）。
        self.refs = 0
        self.created_at = _now()
        self.restarts = 0
        self.last_error = ""

        self._on_state = on_state
        self._transport = transport if transport is not None else SSHTransport(conn)
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._drain: _StderrDrain | None = None
        self._supervisor: threading.Thread | None = None
        self._state = "closed"
        self._stopping = False

    # ------------------------------------------------------------------ 查询
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "connection": self.conn.name,
                "display": self.conn.display,
                "remotePort": self.remote_port,
                "localPort": self.local_port,
                "url": f"http://{FORWARD_HOST}:{self.local_port}/" if self.local_port else "",
                "state": self._state,
                "refs": self.refs,
                "restarts": self.restarts,
                "error": self.last_error,
                "createdAt": self.created_at,
            }

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> dict[str, Any]:
        """建立转发并等它真的能连上。失败抛 `RemoteError`（不会留半开的进程）。"""
        with self._lock:
            if self._proc is not None:
                return self.snapshot()
            self._stopping = False
            if not self.local_port:
                self.local_port = _free_loopback_port()
        self._launch(initial=True)
        thread = threading.Thread(
            target=self._watch, name=f"vibe-tunnel-{self.id}", daemon=True
        )
        with self._lock:
            self._supervisor = thread
        thread.start()
        return self.snapshot()

    def stop(self) -> None:
        """关闭转发。幂等：重复调用不会抛，也不会误杀别的进程。"""
        with self._lock:
            self._stopping = True
            proc, self._proc = self._proc, None
            self._state = "closed"
            self.last_error = ""
            thread = self._supervisor
            self._supervisor = None
        self._emit()
        if proc is not None:
            self._terminate(proc)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_GRACE + 1)

    # ------------------------------------------------------------------ 内部
    def _launch(self, *, initial: bool) -> None:
        """起一条 ssh 进程并等它可连。失败时**不留进程**，状态落到 `down`。"""
        with self._lock:
            if self._stopping:
                return
            self._state = "starting" if initial else "reconnecting"
            local_port = self.local_port
        self._emit()

        last: RemoteError | None = None
        for attempt in range(3):
            try:
                proc = self._transport.spawn_forward(
                    local_port=local_port, remote_port=self.remote_port
                )
            except RemoteError as exc:
                last = exc
                # 端口被别人抢走的竞态：换一个端口再试。只在首次建立时换 ——
                # 重连期间换端口会让已经打开的 iframe 指向一个没人监听的地址。
                if initial and attempt < 2:
                    local_port = _free_loopback_port()
                    with self._lock:
                        self.local_port = local_port
                    continue
                with self._lock:
                    self.last_error = str(exc)
                    self._state = "down"
                self._emit()
                raise

            drain = _StderrDrain(proc.stderr)
            if self._await_ready(proc, local_port):
                with self._lock:
                    self._proc = proc
                    self._drain = drain
                    self._state = "up"
                    self.last_error = ""
                self._emit()
                return

            # 起不来：收尸并带上 stderr（"Address already in use" /
            # "remote port forwarding failed" 都在这里显形）。
            code = proc.poll()
            detail = drain.text()
            self._terminate(proc)
            message = detail or (
                f"ssh 转发进程已退出（退出码 {code}）"
                if code is not None
                else f"转发通道未在 {READY_TIMEOUT:.0f} 秒内就绪"
            )
            last = RemoteError(
                f"无法在本地建立到远端 127.0.0.1:{self.remote_port} 的转发：{message}",
                kind="transport",
                detail=detail,
            )
            if initial and attempt < 2 and "already in use" in detail.lower():
                local_port = _free_loopback_port()
                with self._lock:
                    self.local_port = local_port
                continue
            break

        assert last is not None
        with self._lock:
            self.last_error = str(last)
            self._state = "down"
            self._proc = None
        self._emit()
        raise last

    def _await_ready(self, proc: subprocess.Popen[bytes], local_port: int) -> bool:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            if _accepts_connection(local_port):
                return True
            if self._stopping:
                return False
            time.sleep(POLL_INTERVAL)
        return False

    def _watch(self) -> None:
        """唯一一条监工线程：等当前进程 → 需要时重建 → 继续等。"""
        while True:
            with self._lock:
                proc = self._proc
                stopping = self._stopping
            if stopping or proc is None:
                return

            proc.wait()
            # 进程已退出，管道留着只会变成 fd 泄漏（长会话里反复重连会积累）。
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            with self._lock:
                drain = self._drain
            detail = drain.text() if drain is not None else ""

            with self._lock:
                if self._stopping:
                    self._proc = None
                    return
                self._proc = None
                self._state = "reconnecting"
                self.last_error = detail or (
                    f"ssh 转发进程意外退出（退出码 {proc.returncode}）"
                )
            self._emit()

            rebuilt = False
            for delay in RECONNECT_BACKOFF[:RECONNECT_ATTEMPTS]:
                if self._is_stopping():
                    return
                time.sleep(delay * _BACKOFF_FACTOR)
                if self._is_stopping():
                    return
                try:
                    self._launch(initial=False)
                except RemoteError:
                    continue
                rebuilt = True
                break

            if not rebuilt:
                with self._lock:
                    self._state = "down"
                    self._supervisor = None
                self._emit()
                return

            with self._lock:
                self.restarts += 1
            # 循环继续：下一轮对新进程 proc.wait()

    def _is_stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def _terminate(self, proc: subprocess.Popen[bytes]) -> None:
        """先走「关 stdin 让远端 cat 退出」的正常路径，再 terminate / kill。"""
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=STOP_GRACE)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=STOP_GRACE)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _emit(self) -> None:
        """状态变了就通知外部。**回调异常一律吞掉** —— 一个画不出来的界面
        不该把转发或监工线程一起带走。"""
        callback = self._on_state
        if callback is None:
            return
        try:
            callback(self.snapshot())
        except Exception:  # noqa: BLE001 — 观察者故障不参与主链路
            pass


class TunnelManager:
    """会话级的转发名单：按 `(连接名, 远端端口)` 去重，引用计数管生命周期。"""

    def __init__(
        self,
        *,
        on_state: Callable[[dict[str, Any]], None] | None = None,
        max_tunnels: int = MAX_TUNNELS,
        transport_factory: Callable[[RemoteConnection], SSHTransport] | None = None,
    ) -> None:
        self._on_state = on_state
        self._max = max(1, int(max_tunnels))
        self._make_transport = transport_factory or (lambda conn: SSHTransport(conn))
        self._tunnels: dict[str, SSHTunnel] = {}
        self._by_key: dict[tuple[str, int], str] = {}
        self._lock = threading.RLock()

    def open(self, conn: RemoteConnection, remote_port: Any) -> dict[str, Any]:
        port = validate_remote_port(remote_port)
        created: SSHTunnel | None = None

        with self._lock:
            key = (conn.name, port)
            existing_id = self._by_key.get(key)
            tunnel = self._tunnels.get(existing_id) if existing_id else None
            stale: SSHTunnel | None = None

            # 连接被改过（换主机 / 换密钥 / 换 root）→ 老通道指向的是旧目标，
            # 复用它会让人以为「连的是当前这台」，实际打到上一台。
            if tunnel is not None and tunnel.conn.key != conn.key:
                self._forget(tunnel)
                stale, tunnel = tunnel, None

            # 已经不健康的那条不再复用：用户再点一次就是要一条能用的。
            if tunnel is not None and tunnel.state in ("down", "closed"):
                self._forget(tunnel)
                stale, tunnel = tunnel, None

            if tunnel is None:
                if len(self._tunnels) >= self._max:
                    raise ValueError(
                        f"同时最多 {self._max} 条远端转发。"
                        "关掉对应的预览标签即可释放；刷新工作台页面会一次性释放本页开的所有转发。"
                    )
                tunnel = SSHTunnel(
                    conn,
                    port,
                    on_state=self._relay,
                    transport=self._make_transport(conn),
                )
                self._tunnels[tunnel.id] = tunnel
                self._by_key[key] = tunnel.id
                created = tunnel

            tunnel.refs += 1

        if stale is not None:
            stale.stop()
        if created is not None:
            try:
                created.start()
            except BaseException:
                with self._lock:
                    created.refs = 0
                    self._forget(created)
                raise
        return tunnel.snapshot()

    def close(self, tunnel_id: str) -> dict[str, Any]:
        """引用计数减一；减到 0 才真的收转发。未知 id 报错（不静默成功）。"""
        with self._lock:
            tunnel = self._tunnels.get(tunnel_id or "")
            if tunnel is None:
                raise ValueError(f"没有这条远端转发：{tunnel_id!r}")
            tunnel.refs = max(0, tunnel.refs - 1)
            remaining = tunnel.refs
            last = remaining == 0
            if last:
                self._forget(tunnel)
        if last:
            tunnel.stop()
        return {"id": tunnel_id, "closed": last, "refs": remaining}

    def get(self, tunnel_id: str) -> SSHTunnel | None:
        with self._lock:
            return self._tunnels.get(tunnel_id or "")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            tunnels = list(self._tunnels.values())
        return [tunnel.snapshot() for tunnel in tunnels]

    def close_all(self) -> None:
        with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
            self._by_key.clear()
        for tunnel in tunnels:
            tunnel.refs = 0
            try:
                tunnel.stop()
            except Exception:  # noqa: BLE001 — 收尾阶段不允许互相拖累
                pass

    # ------------------------------------------------------------------ 内部
    def _forget(self, tunnel: SSHTunnel) -> None:
        with self._lock:
            self._tunnels.pop(tunnel.id, None)
            self._by_key.pop((tunnel.conn.name, tunnel.remote_port), None)

    def _relay(self, snapshot: dict[str, Any]) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(snapshot)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "MAX_TUNNELS",
    "READY_TIMEOUT",
    "RECONNECT_ATTEMPTS",
    "RECONNECT_BACKOFF",
    "SSHTunnel",
    "TUNNEL_STATES",
    "TunnelManager",
    "validate_remote_port",
]
