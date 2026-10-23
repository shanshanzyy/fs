"""命名空间透明路由：把 `@remote/<连接>/<路径>` 形式的文件工具调用分流到远端链路。

## 分流点在哪

**不在 `ToolRegistry.execute()`，也不在 `Agent` 的运行期**。做法是把 7 个文件工具的
`ToolSpec` 换成**同名路由壳**（`handler` / `assessor` / `previewer` / `drafter`
四个成员全是「按 `args["path"]` 选臂」的闭包）。于是：

  - 模型的工具清单、描述、参数 schema **一字不改** —— 换的是实现，不是工具；
  - 审批预演、审批草稿、真正执行**共用同一份选臂逻辑**，结构上排除
    「审批时按本地预演、执行时却打到远端」这种最危险的错配；
  - `loop/agent.py` 的审批漏斗与审计一行都不用动。

## 四条纪律

1. **前缀判定在两道 Jail 之前**：远端臂交 `RemotePathGuard`，本地臂交 `PathGuard`。
   非路由工具（`repo_map` / `semantic_search` / `git_diff` / `edit_files` /
   `run_command` …）收到命名空间路径时，由 `PathGuard.reserved_prefixes` 明确报错
   （见 sandbox/paths.py），**不静默**当成本地文件去解析。
2. **只认已登记的根**：路径落在某条连接的 root 里、但那个目录没被「加入工作区」
   → 人话报错并列出可加入的位置，而不是替用户猜。
3. **不猜连接**：第二段不是清单里的连接名 → 报错并列出可用连接名。
4. **门控沿用 `VIBE_REMOTE`**：路由壳只在 `remote=True` 时装入
   （见 `tools/__init__.py::build_default_registry`）；未启用时工具清单逐字节不变。
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any, Callable

from ..errors import ToolError
from .errors import RemoteConfigError
from .roots import (
    PREFIX,
    RemoteRoot,
    RouteHit,
    default_root_rel,
    is_ns_path,
    parse_ns_root,
)
from .store import RemoteStore, store_path_for

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查用，避免 tools ↔ remote 模块级成环
    from ..tools.base import ToolSpec

# 参与路由的工具名。**顺序即使用顺序**，与本地注册表一致。
ROUTED_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "find_files",
    "grep",
    "edit_file",
    "write_file",
)


def _norm(path: object) -> str:
    """把模型可能写出的反斜杠 / 尾斜杠写法收敛成正斜杠无尾斜杠形式。"""
    return str(path).strip().replace("\\", "/")


class RemoteRouter:
    """按前缀把文件工具调用分流到远端链路。持清单快照，可用 `invalidate()` 刷新。

    为什么不每次调用都读盘：工具调用可能很密集，而清单只有几十行；持一份快照 +
    在根/连接变更时显式失效（`host/session.py` 的根端点会调 `invalidate()`），
    比每次 `open()`/`read()` 一遍更省，也不会让「并发读到半个文件」变成可能。
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self._store: RemoteStore | None = None
        self._lock = threading.RLock()
        # 远程工作区（M13 / ADR-042）：`remote_workspace` 非空时它成为**默认根**，
        # 裸路径也走远端臂。解析放在构造期而不是每次调用：写法非法要在**会话装配时**
        # 就炸出来，而不是等到模型第一次调工具才发现自己在一个坏工作区里。
        self.default_conn, self.default_sub = parse_ns_root(
            str(getattr(config, "remote_workspace", "") or "")
        )

    @property
    def default_ns(self) -> str:
        """默认根的命名空间写法（未设远程工作区时为空串）。"""
        if not self.default_conn:
            return ""
        base = f"{PREFIX}{self.default_conn}"
        return f"{base}/{self.default_sub}" if self.default_sub else base

    # ------------------------------------------------------------------ 快照
    def invalidate(self) -> None:
        """丢弃快照，下次 `store()` 重新读盘。根 / 连接变更后由 host 侧调用。"""
        with self._lock:
            self._store = None

    def store(self) -> RemoteStore:
        with self._lock:
            if self._store is None:
                self._store = RemoteStore.load(store_path_for(self.config))
            return self._store

    def roots(self) -> list[RemoteRoot]:
        return list(self.store().roots)

    def roots_for(self, connection: str) -> list[RemoteRoot]:
        return self.store().roots_for(connection)

    # ------------------------------------------------------------------ 分流
    def match(self, path: object) -> RouteHit | None:
        """判定一次调用该走哪条臂。

        三条互斥的出口，**只在本机模式（未设远程工作区）时与 M12 完全同构**：

        - 写 `@remote/...` → `_match_ns()`（显式前缀，ADR-041 原逻辑）；
        - 远程工作区模式下的**裸路径** → `_match_default()`（ADR-042 新增的默认根）；
        - 其余 → `None`（本机臂）。

        解析不了的 `@remote/...` 一律抛 `ToolError`（**不回退本地**：把它当本地路径
        解析，最坏情况是改动写到了错的机器上）。
        """
        if is_ns_path(path):
            return self._match_ns(path)
        if self.default_conn:
            return self._match_default(path)
        return None

    def _match_ns(self, path: object) -> RouteHit | None:
        """显式 `@remote/<连接>/<路径>` 写法（ADR-041 的原逻辑，逐行保留）。"""
        store = self.store()
        text = _norm(path)
        if text.rstrip("/") == PREFIX.rstrip("/"):
            raise ToolError(
                f"{PREFIX} 后面还要跟连接名与目录，例如 "
                f"`{PREFIX}<连接名>/<目录>`。"
            )
        rest = text[len(PREFIX) :].strip("/")
        connection, _, rel = rest.partition("/")
        rel = rel.strip("/")
        if not connection:
            raise ToolError(f"命名空间路径缺少连接名：{text}")
        if connection not in store.names():
            available = "、".join(f"`{name}`" for name in store.names()) or "（无）"
            raise ToolError(
                f"未找到名为 {connection!r} 的远程连接，已拒绝猜测。\n"
                f"  可用连接：{available}\n"
                f"  写法示例：{PREFIX}devbox/项目/smart-writer/src/app.py"
            )
        # 远程工作区模式：显式写法落在默认根之内时**不必**已登记 —— 否则「显式写」
        # 与「裸写」两套行为，反而更难解释（ADR-042 决策 14：在内存里合成，不写回清单）。
        hit = self._default_hit(connection, rel)
        if hit is not None:
            return hit
        root = store.match_root(connection, rel)
        if root is None:
            raise ToolError(
                f"该远端目录还没有加入工作区：{text}\n"
                "  请在「远程设备」面板的远端文件树里，右键该目录 → 「加入工作区」，\n"
                "  之后就能用本地同名工具（read_file / edit_file 等）直接操作它。\n"
                f"  当前已加入工作区的位置：{self.describe()}"
            )
        ns_path = f"{PREFIX}{connection}" + (f"/{rel}" if rel else "")
        return RouteHit(
            connection=connection,
            sub=root.sub,
            rel_to_root=rel,
            ns_path=ns_path,
        )

    def _match_default(self, path: object) -> RouteHit | None:
        """远程工作区模式：**裸路径**的默认落点（ADR-042 决策 2）。

        模型写 `src/app.py` 就等于写 `@remote/<连接>/<子目录>/src/app.py`。
        `default_root_rel()` 负责把「相对写法 / 远端绝对写法」收敛成相对连接 root 的
        rel，并在 `../` 越界时明确报错（拼完再归一化，见 roots.py 的说明）。
        """
        store = self.store()
        if self.default_conn not in store.names():
            available = "、".join(f"`{name}`" for name in store.names()) or "（无）"
            raise ToolError(
                f"当前远程工作区指向的连接 {self.default_conn!r} 已不在清单里。\n"
                f"  可用连接：{available}\n"
                "  请在「切换工作区 → 远程目录」里另选一个远端目录。"
            )
        conn = store.get(self.default_conn)
        try:
            rel = default_root_rel(
                self.default_sub,
                path,
                root=getattr(conn, "root_normalized", "/") or "/",
            )
        except RemoteConfigError as exc:
            raise ToolError(str(exc)) from exc
        return self._make_hit(rel)

    def _default_hit(self, connection: str, rel: str) -> RouteHit | None:
        """显式前缀写法是否落在默认根之内（是 → 构造 RouteHit，不要求已登记）。"""
        if not self.default_conn or connection != self.default_conn:
            return None
        if not self.default_sub:
            return self._make_hit(rel)
        if rel == self.default_sub or rel.startswith(self.default_sub + "/"):
            return self._make_hit(rel)
        return None

    def _make_hit(self, rel: str) -> RouteHit:
        ns_path = f"{PREFIX}{self.default_conn}" + (f"/{rel}" if rel else "")
        return RouteHit(
            connection=self.default_conn,
            sub=self.default_sub,
            rel_to_root=rel,
            ns_path=ns_path,
        )

    def describe(self) -> str:
        """当前可用的命名空间根清单（提示词与错误消息共用同一口径）。

        远程工作区模式下默认根**排在最前** —— 它是本次会话的主落点，
        错误消息里列位置时先看它最省事（ADR-042 决策 14：它是内存合成的，
        不在 `roots()` 里，所以要显式补上）。
        """
        items = [root.ns_path() for root in self.roots()]
        default = self.default_ns
        if default and default not in items:
            items.insert(0, default)
        return "、".join(items) if items else "（无）"

    def hint_roots(self) -> tuple[dict[str, str], ...]:
        """给提示词用的根清单（只挑每条连接「最能代表它」的那个根）。"""
        store = self.store()
        out: list[dict[str, str]] = []
        for name in store.names():
            root = store.best_root(name)
            if root is None:
                continue
            conn = store.get(name)
            out.append(
                {
                    "ns_path": root.ns_path(),
                    "connection": name,
                    "target": getattr(conn, "target", "") if conn else "",
                }
            )
        return tuple(out)


# --------------------------------------------------------------------------- #
# 路由壳
# --------------------------------------------------------------------------- #
def routed_specs(
    specs: list["ToolSpec"],
    *,
    arms: dict[str, Any] | None = None,
) -> list["ToolSpec"]:
    """把可路由的同名 spec 换成路由壳，其余原样返回（**顺序不变**）。

    `arms` 缺省取 `tools/remote.py::ROUTE_ARMS`（远端臂的实现在那里，本模块
    只负责选臂 —— 这样「留底 / 补丁引擎 / 写回」的实现只有一份）。
    """
    if arms is None:
        from ..tools.remote import ROUTE_ARMS  # 延迟导入：避免 tools ↔ remote 模块级成环

        arms = ROUTE_ARMS
    out: list["ToolSpec"] = []
    for spec in specs:
        arm = arms.get(spec.name)
        out.append(_shell(spec, arm) if arm is not None else spec)
    return out


def _shell(spec: "ToolSpec", arm: Any) -> "ToolSpec":
    """同名路由壳：name / description / parameters / risk 原样，四个成员选臂。"""
    from ..tools.base import ToolPreview, ToolSpec  # 延迟导入（同上）
    from ..sandbox import Risk

    def choose(ctx: Any, args: dict) -> RouteHit | None:
        router = getattr(ctx, "remote_router", None)
        if router is None:
            # 没有路由器（如直接调 registry 的测试场景）：命名空间路径无法处理，
            # 但普通本地路径必须照旧可用 —— 因此只在真的写了前缀时报错。
            if is_ns_path(args.get("path")):
                raise ToolError(
                    "本会话未启用远端路由（VIBE_REMOTE 未开启），"
                    f"无法解析命名空间路径：{args.get('path')}"
                )
            return None
        return router.match(args.get("path"))

    def handler(ctx: Any, args: dict):
        hit = choose(ctx, args)
        if hit is None:
            return spec.handler(ctx, args)
        return arm.handler(ctx, args, hit)

    def assessor(ctx: Any, args: dict):
        try:
            hit = choose(ctx, args)
        except ToolError as exc:
            # 解析不了 → 拒绝，而不是回退本地（见 match 的说明）。
            return Risk.DENY, str(exc)
        if hit is None:
            return spec.assess(ctx, args)
        return arm.assessor(ctx, args, hit)

    def previewer(ctx: Any, args: dict):
        hit = choose(ctx, args)
        if hit is None:
            return spec.preview(ctx, args)
        if arm.previewer is None:
            return ToolPreview(
                kind="json",
                body=json.dumps(args, ensure_ascii=False, indent=2),
                summary=f"远端 {hit.connection}: {hit.ns_path}",
            )
        return arm.previewer(ctx, args, hit)

    def drafter(ctx: Any, args: dict):
        try:
            hit = choose(ctx, args)
        except ToolError:
            return ()
        if hit is None:
            return spec.draft(ctx, args)
        if arm.drafter is None:
            return ()
        return arm.drafter(ctx, args, hit)

    return ToolSpec(
        name=spec.name,
        description=spec.description,
        parameters=spec.parameters,
        handler=handler,
        risk=spec.risk,
        assessor=assessor,
        previewer=previewer,
        drafter=drafter,
    )


__all__ = ["ROUTED_TOOLS", "RemoteRouter", "routed_specs"]
