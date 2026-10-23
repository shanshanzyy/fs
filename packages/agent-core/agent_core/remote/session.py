"""会话级远程状态层：把「当前连了哪条、在哪个目录浏览、这次会话改过哪些远端文件」收在一处。

为什么需要这一层：`remote/workspace.py` 只有**传输缓存**（`transport_for(conn)` 按
`conn.key` 复用一条 SSH 通道），它回答的是「怎么打通」，不回答「用户现在盯着哪台机器」。
界面要的面板状态（活动连接 / 浏览位置 / 本会话远端改动台账）不属于任何一次单点操作，
而是**跨操作存活的会话状态** —— 放在这里，而不是散进每个端点里各自维护。

三条纪律：

1. **不新增传输实现** —— 一律经 `open_workspace()`（它内部走 `transport_for()`），
   换连接时把旧传输关掉，不泄漏 ControlMaster 套接字。
2. **一次只活动一条连接** —— `open()` 换连接 = 关旧开新。多连接并发工作面在
   M11 明确不做（见 `.trae/documents/M11-PLAN.md` 的「明确不做」）。
3. **快照不含任何文件正文** —— `snapshot()` 只回连接元数据、浏览位置与能力降级项，
   供端点与 `remote.state` 事件使用（ADR-006：正文只走按需读取端点）。
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .connection import RemoteConnection
from .errors import RemoteConfigError
from .store import RemoteStore, store_path_for
from .workspace import RemoteWorkspace, close_transport, open_workspace

# 远端改动台账里允许的动作枚举。与审计 `remote.file(action=...)` 同一套取值。
CHANGE_ACTIONS = ("read", "save", "write", "edit")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ChangeEntry:
    """本会话内一次远端文件改动的元数据记录。

    **不含正文与 diff** —— diff 按需从端点取（留底 vs 远端现算），台账只记
    「改过哪个文件、加删多少行、改动后指纹、有没有留底」。这样台账可以常驻内存、
    随手快照进事件流，而不必担心把代码正文带出去（ADR-006）。
    """

    rel_path: str
    connection: str = ""
    abs_path: str = ""
    action: str = "edit"
    bytes: int = 0
    sha256: str = ""
    added: int = 0
    removed: int = 0
    backed_up: bool = False
    # 改动前内容在本机的留底绝对路径（`<checkpoints>/remote/<连接>/<会话>/<相对路径>`）。
    # 为什么放进台账而不是等审查时现算：留底路径由 `_backup_before_write` 内部
    # 拼出（含连接名/会话 ID 两处路径段收敛），重算一遍等于把同一段拼接逻辑
    # 复制成第二份，迟早漂移。`backed_up=False`（纯新建、无覆盖）时为空串。
    backup_path: str = ""
    edited_at: str = ""

    @property
    def key(self) -> str:
        """台账主键：连接名 + 相对路径。

        带上连接名是必须的 —— 一次会话可以切换连接，两台机器上的同名文件
        （比如各自的 `src/app.py`）在台账里必须分得开，否则改动列表会串。
        """
        return f"{self.connection}:{self.rel_path}"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class RemoteSessions:
    """一条会话的远程工作面状态。线程安全（端点可能并发进来）。"""

    config: object
    allow_sensitive: bool = False
    denied_patterns: tuple[str, ...] = ()
    connection: RemoteConnection | None = field(init=False, default=None)
    browse_path: str = field(init=False, default="")
    workspace: RemoteWorkspace | None = field(init=False, default=None)
    changes: dict[str, ChangeEntry] = field(init=False, default_factory=dict)
    _lock: threading.RLock = field(init=False, default_factory=threading.RLock, repr=False)

    # ------------------------------------------------------------------ 连接
    def open(self, name: str | None = None) -> RemoteWorkspace:
        """打开 / 切换一条连接，返回可用的远端工作区。**换连接 = 关旧开新。**

        解析优先级沿用 `RemoteStore.resolve`：显式名 > `VIBE_REMOTE_DEFAULT` >
        清单 default > 唯一那条。多条又没指定时它拒绝猜测 —— 把改动写到错的
        机器上比报错严重得多。
        """
        with self._lock:
            store = RemoteStore.load(store_path_for(self.config))
            default = str(getattr(self.config, "remote_default", "") or "").strip()
            conn = store.resolve(name or default or None)
            self._swap(conn)
            assert self.workspace is not None
            return self.workspace

    def _swap(self, conn: RemoteConnection) -> None:
        """把活动连接换成 `conn`（调用方须持锁）。同一条链路则原地保留。"""
        active = self.connection
        if active is not None and active.key == conn.key:
            # 同一条链路（target / root / 密钥 / 指纹策略都没变）：只把连接对象
            # 换新，**不重握手** —— `conn.key` 刻意不含 allow_write / allow_shell，
            # 所以刚改的权限位必须靠换对象来即时可见，否则会静默沿用旧值。
            self.connection = conn
            if self.workspace is not None:
                # 工作区不按权限位做拦截（拦截在端点层），但 `report()` 会展示
                # 权限位，因此同步换掉，避免界面看到过期的「可写/可执行」。
                self.workspace.conn = conn
            return
        self._close_active()
        self.workspace = open_workspace(
            conn,
            allow_sensitive=self.allow_sensitive,
            denied_patterns=self.denied_patterns,
        )
        self.connection = conn
        self.browse_path = ""

    def close(self) -> None:
        """关闭活动连接并清空会话状态（会话收尾时调用）。"""
        with self._lock:
            self._close_active()
            self.changes.clear()

    def disconnect(self, name: str | None = None) -> bool:
        """只关活动连接，**保留**改动台账。返回是否真的关了。

        与 `close()` 的区别在台账：删掉一条连接时，若它正是活动连接，需要断掉
        它的传输，但**不该**顺手清掉本次会话对其它设备的改动记录 —— 那些记录
        仍然是有用的审查线索。`name` 给出时只在同名时断。
        """
        with self._lock:
            conn = self.connection
            if conn is None or (name and conn.name != name):
                return False
            self._close_active()
            return True

    def _close_active(self) -> None:
        conn, self.connection = self.connection, None
        self.workspace = None
        self.browse_path = ""
        if conn is not None:
            # 只关这一条的在位传输，不调 close_all_transports —— 后者会把
            # 进程内**所有**连接的传输一起关掉，误伤别的会话/工作区。
            close_transport(conn)

    def active(self) -> RemoteConnection | None:
        with self._lock:
            return self.connection

    def require_workspace(self) -> RemoteWorkspace:
        """取当前工作区；没打开任何连接时给出**可照着做**的报错。"""
        with self._lock:
            if self.workspace is None:
                raise RemoteConfigError(
                    "尚未打开任何远程连接。\n"
                    "  请先在「远程」面板里打开一条连接，或运行 "
                    "`python -m agent_core.remote add <名称> <用户@主机> --root <远端目录>` 添加连接。"
                )
            return self.workspace

    # ------------------------------------------------------------------ 浏览位置
    def chdir(self, rel: str | None = None) -> str:
        """设置浏览位置（相对 root）。越界由 `RemotePathGuard.check_list` 拒绝。"""
        with self._lock:
            workspace = self.require_workspace()
            abs_path = workspace.guard.check_list(rel)
            self.browse_path = workspace.guard.relative(abs_path)
            return self.browse_path

    # ------------------------------------------------------------------ 改动台账
    def note_change(self, entry: ChangeEntry) -> None:
        """登记一次远端改动（同一文件重复改则覆盖为最新一次）。"""
        if not entry.edited_at:
            entry.edited_at = _now()
        with self._lock:
            self.changes[entry.key] = entry

    def list_changes(self) -> list[dict[str, object]]:
        """台账的只读视图（元数据，按改动时间倒序）。"""
        with self._lock:
            entries = sorted(
                self.changes.values(), key=lambda item: item.edited_at, reverse=True
            )
        return [entry.to_dict() for entry in entries]

    def get_change(self, path: str, connection: str | None = None) -> ChangeEntry | None:
        """按相对路径取一条台账记录（`connection` 省略时取当前连接）。"""
        with self._lock:
            conn = connection or (self.connection.name if self.connection else "")
            return self.changes.get(f"{conn}:{path}")

    # ------------------------------------------------------------------ 快照
    def _roots(self) -> list[str]:
        """已加入工作区的远端根的命名空间路径（M12）。

        每次都读一遍清单：根变更走的是 host 端点的原子写，这里读到的是落盘后的
        权威值。读失败一律当「没有根」—— 状态快照不该因为一份坏清单而炸掉
        整个界面（真正的坏清单原因由 `/api/remote/roots` 的 problems 展示）。
        """
        try:
            store = RemoteStore.load(store_path_for(self.config))
        except Exception:
            return []
        return [root.ns_path() for root in store.roots]

    def snapshot(self) -> dict[str, object]:
        """给端点与 `remote.state` 事件用的元数据快照。**不含任何文件正文。**

        未打开连接时如实回空（`connection: ""`）—— 界面据此显示「未连接」，
        而不是把「没连」渲染成一个坏掉的空面板。
        """
        with self._lock:
            conn = self.connection
            workspace = self.workspace
            changes = len(self.changes)
        roots = self._roots()
        if conn is None or workspace is None:
            return {
                "connection": "",
                "display": "",
                "root": "",
                "browsePath": "",
                "allowWrite": False,
                "allowShell": False,
                "capabilities": {"degraded": []},
                "changes": changes,
                "roots": roots,
            }
        return {
            "connection": conn.name,
            "display": conn.display,
            "root": workspace.guard.root_resolved,
            "browsePath": self.browse_path,
            "allowWrite": conn.writable,
            "allowShell": conn.executable,
            "capabilities": {"degraded": list(workspace.capabilities().degraded)},
            "changes": changes,
            "roots": roots,
        }


__all__ = ["CHANGE_ACTIONS", "ChangeEntry", "RemoteSessions"]
