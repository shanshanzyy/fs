"""多文件原子编辑（事务，P1-8）。

## 为什么需要这一层

`PatchPlan` 与 `commit_plan` 只保证**单文件**原子（先写同目录临时文件再
`os.replace`，见 [patcher.py](file:///e:/VibeCoding/packages/agent-core/agent_core/edit/patcher.py)）。
一次要改多个文件时，若逐个独立提交，第 k 个失败时前 k-1 个**已经写进去了**——
「改了一半」是最难收拾的中间态：仓库既不是改动前、也不是改动后，还得靠人去
分辨哪些该留哪些该退。

本模块把「一次涉及多个文件的改动」收敛成**一个事务**：

    阶段一（准备 + 整批校验）：对每个文件算好 `PatchPlan`（**只算不写**），
        并在这一步做完所有校验（路径沙箱 / search 命中 / 乐观锁）。
        任一失败 → 抛错，一个文件都不落盘。
    阶段二（提交 + 失败回滚）：先对每个文件收检查点快照，再逐个 `commit_plan`；
        任一写失败 → 用计划里记下的旧内容，把已写文件**回滚为原内容**。

## 三条设计约定

1. **不新增写盘路径**（ADR-004）：落盘仍走 `commit_plan`，回滚仍走
   `plan_overwrite` + `commit_plan` —— 与单文件编辑是同一套引擎。
2. **不新增快照实现**：检查点复用 `CheckpointManager.snapshot_before_write`
   （经构造时传入的 `snapshot` 回调），回滚素材直接用 `PatchPlan.old_text`。
   因为阶段一已把全部计划算好、阶段二中途不会有别的写者，
   计划里的 `old_text` 就是**提交那一刻**的磁盘内容。
3. **审计只记计数**（ADR-006）：本模块不写审计，计数由调用方（工具 / 会话层）
   从 `TransactionReport` 与各 `PatchPlan` 取。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..errors import PatchError, ToolError
from ..sandbox import PathGuard
from .patcher import PatchPlan, commit_plan, plan_overwrite, plan_patch

# 一次事务最多涉及多少个文件。多到这个量级的改动本就不该一口气落盘 ——
# 审批人也审不过来，分批更安全。
MAX_EDIT_FILES = 20


def _file_digest(path: Path) -> str:
    """文件内容指纹（sha256 前 16 位）—— 与编辑面乐观锁同一算法（D11）。

    取磁盘**原始字节**：乐观锁比的是「文件有没有被别处改过」，字节级最严格，
    也绕开编码/换行归一带来的歧义。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


class TransactionError(PatchError):
    """整个事务失败。`rolled_back` 表示已写文件是否**已全部回滚**。"""

    def __init__(self, message: str, *, rolled_back: bool = False) -> None:
        super().__init__(message)
        self.rolled_back = rolled_back


@dataclass
class TransactionReport:
    """一次事务的结果。"""

    plans: list[PatchPlan] = field(default_factory=list)
    # 实际落盘并保留在磁盘上的文件（回滚时为空）
    changed: list[PatchPlan] = field(default_factory=list)
    added: int = 0
    removed: int = 0

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    def summary(self) -> str:
        return (
            f"一次改 {len(self.plans)} 个文件（实际改动 {self.changed_count} 个，"
            f"+{self.added} / -{self.removed}）"
        )


class EditTransaction:
    """多文件原子编辑事务。用法：先 `add_*` 若干（阶段一），再 `commit()`（阶段二）。"""

    def __init__(
        self,
        *,
        paths: PathGuard,
        snapshot: Callable[[Path, str], None] | None = None,
    ) -> None:
        self.paths = paths
        # snapshot(abs_path, rel)：落盘前收检查点。None = 未挂载（跳过快照）。
        self._snapshot = snapshot
        self.plans: list[PatchPlan] = []

    # ---------------------------------------------------------------- 阶段一
    def _resolve(self, raw_path: str | Path) -> tuple[Path, str]:
        path = self.paths.check_write(raw_path)
        return path, self.paths.relative(path)

    def _check_fresh(self, path: Path, rel: str, expected_digest: str) -> None:
        """乐观锁：磁盘指纹与调用方给的指纹不符则整批不落盘。"""
        if not expected_digest:
            return
        if _file_digest(path) != expected_digest:
            raise PatchError(
                f"{rel} 已被改动（可能来自外部编辑器或另一次写入），"
                "为免覆盖他人改动，本次事务未落盘，请重新读取后再试"
            )

    def _add(self, plan: PatchPlan) -> PatchPlan:
        if len(self.plans) >= MAX_EDIT_FILES:
            raise PatchError(
                f"一次事务最多改动 {MAX_EDIT_FILES} 个文件，"
                f"请拆成多次提交（当前已登记 {len(self.plans)} 个）"
            )
        self.plans.append(plan)
        return plan

    def add_patch(
        self,
        raw_path: str | Path,
        search: str,
        replace: str,
        *,
        replace_all: bool = False,
        expected_digest: str = "",
    ) -> PatchPlan:
        """登记一个结构化补丁（走 `plan_patch`）。**只算不写**，失败即抛错。"""
        path, rel = self._resolve(raw_path)
        self._check_fresh(path, rel, expected_digest)
        plan = plan_patch(
            path, search, replace, rel_path=rel, replace_all=replace_all
        )
        return self._add(plan)

    def add_overwrite(
        self,
        raw_path: str | Path,
        content: str,
        *,
        overwrite: bool = True,
        expected_digest: str = "",
    ) -> PatchPlan:
        """登记一次整文件写入/新建（走 `plan_overwrite`）。**只算不写**。"""
        path, rel = self._resolve(raw_path)
        if path.is_dir():
            raise ToolError(f"{rel} 是目录，不能写入")
        exists = path.exists()
        if exists and not overwrite:
            raise ToolError(
                f"{rel} 已存在。修改已有文件请提供 search/replace；"
                "确有整体重写必要时，请显式设置 overwrite=true。"
            )
        self._check_fresh(path, rel, expected_digest)
        plan = plan_overwrite(path, content, rel_path=rel)
        return self._add(plan)

    # ---------------------------------------------------------------- 阶段二
    def commit(self) -> TransactionReport:
        """整批落盘。任一文件写失败 → 回滚已写文件，并抛 `TransactionError`。"""
        pending = [p for p in self.plans if p.changed or p.is_create]
        if not pending:
            return TransactionReport(plans=list(self.plans))

        written: list[PatchPlan] = []
        for plan in pending:
            if self._snapshot is not None:
                self._snapshot(plan.path, plan.rel_path)
            try:
                commit_plan(plan)
            except BaseException as exc:  # noqa: BLE001 — 写盘失败必须回滚后再报
                rolled = self._rollback(written)
                tail = (
                    "已回滚此前写入的文件。"
                    if rolled
                    else "且**回滚未能完成**，请检查工作区。"
                )
                raise TransactionError(
                    f"多文件事务失败：{plan.rel_path} 落盘出错（{exc}）。{tail}",
                    rolled_back=rolled,
                ) from exc
            written.append(plan)

        return TransactionReport(
            plans=list(self.plans),
            changed=list(written),
            added=sum(p.added for p in written),
            removed=sum(p.removed for p in written),
        )

    def _rollback(self, written: list[PatchPlan]) -> bool:
        """把已写文件恢复为原内容。逆序回滚；任一失败都如实返回 False。"""
        ok = True
        for plan in reversed(written):
            try:
                if plan.is_create:
                    plan.path.unlink(missing_ok=True)
                else:
                    restore = plan_overwrite(
                        plan.path, plan.old_text, rel_path=plan.rel_path
                    )
                    commit_plan(restore)
            except BaseException:  # noqa: BLE001 — 回滚尽力而为，失败要报出来
                ok = False
        return ok


__all__ = [
    "MAX_EDIT_FILES",
    "EditTransaction",
    "TransactionError",
    "TransactionReport",
]