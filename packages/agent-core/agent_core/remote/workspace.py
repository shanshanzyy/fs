"""远端工作区：把「连接 + 路径 Jail + 传输」组装成可用的操作面。

本模块是**唯一**允许把这三者拼起来的地方 —— 工具层只调这里的方法，
不直接碰 `SSHTransport`。这样「所有远端文件访问都必须先过 jail」这条不变式
有一个明确的收口，而不是散落在每个工具处理器里靠自觉。

## 远端结构化编辑（对齐 ADR-004）

ADR-004 的红线是「编辑必须走结构化补丁，禁止整文件重写」。这条红线在远端
**完全保留**，而且实现方式比想象中省事：

    远端内容 → 写入本地临时文件 → 复用本地 plan_patch（纯计算，不落盘）
             → plan.doc.render() → 原子写回远端

也就是说，三级匹配（exact / normalized / flexible）、命中多处拦截、
编码与 BOM/CRLF/末尾换行保真，这些已经有测试锁住的能力**原样复用**，
而不是在远端再造一份简化版。参数是分离的、不落盘的，临时文件在
`finally` 里删掉，远端和本机都不会留下中间产物。
"""

from __future__ import annotations

import posixpath
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from ..edit.patcher import PatchPlan, TextDoc, plan_patch
from ..errors import SandboxViolation
from .connection import RemoteConnection
from .pathguard import RemotePathGuard
from .transport import Capabilities, RemoteResult, SSHTransport


def _default_max_bytes() -> int:
    """单文件读入上限定为与本地扫描同源的那个值。

    延迟导入而不是模块级 import：`..tools.base` 会拉起整个 `tools` 包，
    而 `tools` 包又反过来导入 `remote`（见 tools/__init__.py 的远程分支），
    模块级 import 必然成环。这里也刻意**不**另抄一个 2 MiB 常量 ——
    阈值分散成两份，迟早会有一边被改而另一边没改。
    """
    from ..tools.base import MAX_SCAN_BYTES

    return MAX_SCAN_BYTES

# 传输实例缓存：同一进程内同一条连接复用同一个传输，
# 从而复用它的 ControlMaster 套接字与能力探测结果。
# 键用 `conn.key`（含 host/port/root/密钥/跳板），配置变了自然换新实例。
_TRANSPORTS: dict[str, SSHTransport] = {}
_TRANSPORTS_LOCK = threading.Lock()


def transport_for(conn: RemoteConnection) -> SSHTransport:
    with _TRANSPORTS_LOCK:
        existing = _TRANSPORTS.get(conn.key)
        if existing is not None:
            return existing
        created = SSHTransport(conn)
        _TRANSPORTS[conn.key] = created
        return created


def close_all_transports() -> None:
    """关闭全部复用连接并清空缓存。会话收尾时调用。"""
    with _TRANSPORTS_LOCK:
        transports = list(_TRANSPORTS.values())
        _TRANSPORTS.clear()
    for transport in transports:
        transport.close()


def close_transport(conn: RemoteConnection) -> bool:
    """关闭并驱逐**某一条**连接的在位传输，返回是否确实关掉了一个实例。

    与 `close_all_transports()` 的区别是精度：远程工作面**换连接**时只该关掉
    正在离开的那条，不能顺手把进程内其他连接的传输一并关掉（那等于误伤）。
    会话收尾仍然用 `close_all_transports()` —— 那时确实该全关。
    """
    with _TRANSPORTS_LOCK:
        transport = _TRANSPORTS.pop(conn.key, None)
    if transport is None:
        return False
    transport.close()
    return True


@dataclass(frozen=True)
class RemoteReadResult:
    """一次远端读取的结果。"""

    abs_path: str
    rel_path: str
    text: str
    size: int
    encoding: str
    newline: str
    had_bom: bool
    trailing_newline: bool
    # 原始字节。为什么连正文一起留着：用户面的「保存」要两样东西都靠它算 ——
    # 乐观锁指纹（sha256 原始字节，与本地 `file_digest` 同一口径）与改动前留底。
    # 让调用方为了这两样再单独拉一次 `transport.read_bytes` 会多打一趟 SSH，
    # 而且两次读之间文件可能已被别处改动，留底与指纹就不再是同一份快照。
    data: bytes = b""

    @property
    def newline_label(self) -> str:
        return "CRLF" if self.newline == "\r\n" else "LF"


@dataclass
class RemoteEditResult:
    """一次远端结构化编辑的结果。"""

    abs_path: str
    rel_path: str
    diff: str
    strategy: str
    added: int
    removed: int
    changed: bool
    written: bool

    def summary(self) -> str:
        if not self.changed:
            return f"{self.rel_path} 无需改动（search 与现有内容等价）"
        verb = "已写回" if self.written else "未写回"
        return (
            f"{verb} {self.rel_path} "
            f"(+{self.added} / -{self.removed}, 匹配策略: {self.strategy})"
        )


class RemoteWorkspace:
    """一条远端连接上的工作区。所有路径参数都接受**相对 root** 或**绝对**写法。"""

    def __init__(
        self,
        conn: RemoteConnection,
        *,
        allow_sensitive: bool = False,
        denied_patterns: tuple[str, ...] = (),
        transport: SSHTransport | None = None,
    ) -> None:
        self.conn = conn
        self.guard = RemotePathGuard(
            root=conn.root_normalized,
            allow_sensitive=allow_sensitive,
            denied_patterns=denied_patterns,
        )
        self.transport = transport or transport_for(conn)

    # ------------------------------------------------------------------ 元信息
    def capabilities(self) -> Capabilities:
        return self.transport.capabilities()

    def label(self) -> str:
        return f"{self.conn.name} ({self.conn.display})"

    def report(self) -> str:
        """连通性与环境报告。CLI 的 `test` 与 `remote_probe` 工具共用。"""
        caps = self.capabilities()
        lines = [
            f"连接      : {self.conn.name}",
            f"目标      : {self.conn.display}",
            f"工作区根  : {self.guard.root_resolved}",
            f"可写      : {'是' if self.conn.writable else '否（allow_write=off）'}",
            f"可执行命令: {'是' if self.conn.executable else '否（allow_shell=off）'}",
            f"指纹策略  : {self.conn.host_key_policy}",
            "",
            caps.describe(),
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ 读取
    def read(self, rel: str, *, max_bytes: int | None = None) -> RemoteReadResult:
        abs_path = self.guard.check_read(rel)
        self._assert_realpath_inside(abs_path, for_write=False)
        limit = _default_max_bytes() if max_bytes is None else max_bytes
        data = self.transport.read_bytes(abs_path, max_bytes=limit)
        doc = _load_doc(data)
        return RemoteReadResult(
            abs_path=abs_path,
            rel_path=self.guard.relative(abs_path),
            text=doc.text,
            size=len(data),
            encoding=doc.encoding,
            newline=doc.newline,
            had_bom=doc.had_bom,
            trailing_newline=doc.trailing_newline,
            data=data,
        )

    # ------------------------------------------------------------------ 写入
    def write(self, rel: str, content: str, *, newline: str = "\n") -> dict[str, object]:
        """整文件写入（远端新建或覆盖）。

        与本地 `write_file` 同性质：是**例外路径**，调用方必须先拿到用户确认
        （工具层把它放在 REVIEW 门后面，见 tools/remote.py）。
        """
        abs_path = self.guard.check_write(rel)
        self._assert_realpath_inside(abs_path, for_write=True)
        doc = TextDoc.from_text(content, newline=newline)
        payload = doc.render(content)
        info = self.transport.write_bytes(abs_path, payload)
        info["rel_path"] = self.guard.relative(abs_path)
        return info

    def edit(
        self,
        rel: str,
        search: str,
        replace: str,
        *,
        replace_all: bool = False,
    ) -> RemoteEditResult:
        """结构化编辑：复用本地三级匹配引擎（见模块头）。"""
        abs_path = self.guard.check_write(rel)
        self._assert_realpath_inside(abs_path, for_write=True)
        data = self.transport.read_bytes(abs_path, max_bytes=_default_max_bytes())
        rel_path = self.guard.relative(abs_path)

        with tempfile.TemporaryDirectory(prefix="vibe-remote-edit-") as tmp_dir:
            scratch = Path(tmp_dir) / (posixpath.basename(abs_path) or "content")
            scratch.write_bytes(data)
            plan: PatchPlan = plan_patch(
                scratch,
                search,
                replace,
                rel_path=rel_path,
                replace_all=replace_all,
            )
            changed = plan.changed
            payload = (plan.doc or TextDoc.from_text(plan.new_text)).render(
                plan.new_text
            )

        written = False
        if changed:
            # 只有真的变了才写回。原因不是省流量：TextDoc 的换行是「按多数判定」，
            # 混合换行的文件在 load → render 往返后字节可能不同 —— 无改动却写回
            # 会把整份文件的换行统一掉，属于静默的破坏性副作用。
            self.transport.write_bytes(abs_path, payload)
            written = True

        return RemoteEditResult(
            abs_path=abs_path,
            rel_path=rel_path,
            diff=plan.diff,
            strategy=plan.strategy,
            added=plan.added,
            removed=plan.removed,
            changed=changed,
            written=written,
        )

    # ------------------------------------------------------------------ 浏览
    def list_dir(self, rel: str | None = None) -> list[dict[str, object]]:
        abs_path = self.guard.check_list(rel)
        return self.transport.list_dir(abs_path)

    def exists(self, rel: str) -> bool:
        return self.transport.exists(self.guard.normalize(rel))

    def grep(
        self, pattern: str, *, rel: str | None = None, ignore_case: bool = False
    ) -> RemoteResult:
        abs_path = self.guard.check_list(rel)
        return self.transport.grep(pattern, path=abs_path, ignore_case=ignore_case)

    def find_files(
        self, pattern: str, *, rel: str | None = None, max_entries: int = 2000
    ) -> tuple[list[str], bool]:
        """按文件名模式递归查找，返回 `(相对路径清单, 是否被截断)`。

        为什么走 `list_dir` 逐层展开而不是调远端 `find`：那要 `allow_shell`，
        而「找文件」是**只读浏览**，不该因为某条连接关了 shell 就不能用。
        代价是比一条 `find` 多打若干次目录列举，但只依赖最基础的目录能力，
        行为在各版本远端上也更一致（`find` 的输出格式与选项差异很大）。

        `max_entries` 溢出即停并如实回报截断 —— 与本地 `find_files` 同一纪律：
        宁可少给几条并说清「已截断」，也不把远端整棵树拉进上下文。
        """
        import fnmatch

        from ..tools.base import DEFAULT_IGNORED_DIRS

        root_abs = self.guard.check_list(rel)
        found: list[str] = []
        truncated = False
        stack = [root_abs]
        while stack:
            if len(found) >= max_entries:
                truncated = True
                break
            current = stack.pop()
            entries = self.transport.list_dir(current)
            dirs: list[str] = []
            for entry in entries:
                name = str(entry.get("name") or "")
                if not name or name in (".", "..") or name in DEFAULT_IGNORED_DIRS:
                    continue
                kind = str(entry.get("type") or "")
                child = posixpath.join(current, name)
                if kind == "d":
                    dirs.append(child)
                elif kind in ("f", "l") and fnmatch.fnmatch(name, pattern):
                    found.append(self.guard.relative(child))
                    if len(found) >= max_entries:
                        break
            stack.extend(reversed(dirs))
        return found, truncated

    # ------------------------------------------------------------------ 执行
    def run_argv(self, argv: list[str], *, timeout: int | None = None) -> RemoteResult:
        """在远端工作目录执行 argv。**参数已由上层沙箱分析过**（见 tools/remote.py）。"""
        return self.transport.run(argv, cwd=self.guard.root_resolved, timeout=timeout)

    # ------------------------------------------------------------------ 校验
    def _assert_realpath_inside(self, abs_path: str, *, for_write: bool) -> None:
        """远端 jail 的第二道防线：用 realpath 揭穿符号链接逃逸。

        文本级校验（`RemotePathGuard`）挡不住「工作区内一个指向 /etc 的软链」。
        这里在远端能力允许时拿真实路径再确认一次。

        能力不可用时**不阻断**，但也不假装安全 —— 调用方可以通过
        `capabilities().degraded` 看到这一项，审计里同样有记录。
        """
        target = posixpath.dirname(abs_path) if for_write else abs_path
        if not target:
            return
        real = self.transport.realpath(target)
        if real is None:
            return  # 远端没有 python3/realpath，无法二次确认（已在能力报告里标出）
        if not self.guard.is_inside(real):
            raise SandboxViolation(
                "远端路径经符号链接解析后越出工作区，已拒绝：\n"
                f"  请求路径 : {self.guard.relative(abs_path)}\n"
                f"  真实路径 : {real}\n"
                f"  工作区根 : {self.guard.root_resolved}",
                kind="escape",
                path=self.guard.relative(abs_path),
            )


def _load_doc(data: bytes) -> TextDoc:
    """借道本地临时文件复用 `TextDoc` 的多编码探测。

    为什么不自己 decode：TextDoc 会依次尝试 UTF-8/BOM/若干候选编码并记录
    换行与末尾换行状态，这套逻辑已有测试覆盖。复制一份到远端分支，
    等于把「编码探测」这条容易出错的逻辑变成两份会漂移的实现。
    """
    with tempfile.TemporaryDirectory(prefix="vibe-remote-read-") as tmp_dir:
        scratch = Path(tmp_dir) / "content"
        scratch.write_bytes(data)
        return TextDoc.load(scratch)


def open_workspace(
    conn: RemoteConnection,
    *,
    allow_sensitive: bool = False,
    denied_patterns: tuple[str, ...] = (),
) -> RemoteWorkspace:
    return RemoteWorkspace(
        conn,
        allow_sensitive=allow_sensitive,
        denied_patterns=denied_patterns,
    )


__all__ = [
    "RemoteEditResult",
    "RemoteReadResult",
    "RemoteWorkspace",
    "close_all_transports",
    "close_transport",
    "open_workspace",
    "transport_for",
]
