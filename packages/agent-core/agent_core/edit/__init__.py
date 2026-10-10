"""编辑引擎：结构化补丁的计划与应用。"""

from .hunks import Hunk, apply_hunks, split_hunks
from .patcher import (
    PatchPlan,
    TextDoc,
    apply_patch,
    commit_plan,
    plan_create,
    plan_overwrite,
    plan_patch,
)
from .transaction import (
    MAX_EDIT_FILES,
    EditTransaction,
    TransactionError,
    TransactionReport,
)

__all__ = [
    "Hunk",
    "MAX_EDIT_FILES",
    "PatchPlan",
    "TextDoc",
    "EditTransaction",
    "TransactionError",
    "TransactionReport",
    "apply_hunks",
    "apply_patch",
    "commit_plan",
    "plan_create",
    "plan_overwrite",
    "plan_patch",
    "split_hunks",
]
