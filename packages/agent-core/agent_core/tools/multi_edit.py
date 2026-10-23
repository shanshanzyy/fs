"""多文件原子编辑工具：edit_files（P1-8）。

一次改多个文件时，逐个调用 `edit_file` 会在第 k 个失败时把前 k-1 个留在盘上 ——
「改了一半」。本工具把这一组改动收敛成一个事务：**要么全落盘，要么全不落盘**，
中途任一文件失败则把已写文件回滚为原内容（见 [transaction.py](file:///e:/VibeCoding/packages/agent-core/agent_core/edit/transaction.py)）。

与 `edit_file` 的关系：`edit_file` 仍是**单文件**场景的默认入口（语义更清晰、
审批更聚焦）；本工具只在「一处逻辑耦合到多个文件、必须同生共死」时使用。
"""

from __future__ import annotations

from ..edit import MAX_EDIT_FILES, EditTransaction, TransactionError, TransactionReport
from ..errors import AgentError, SandboxViolation, ToolError
from ..preview_media import is_draftable
from ..sandbox import Risk
from .base import DraftFile, ToolContext, ToolPreview, ToolResult, ToolSpec

_NEVER_READ_HINT = (
    "\n（提示：其中部分文件本次会话还没读过。search 片段必须与文件当前内容"
    "逐字符一致，建议先 read_file 确认原文。）"
)


def _normalize(raw: object) -> list[dict]:
    """校验并归一化 `edits` 数组。**任一非法即整批拒绝**（同 update_plan 的口径）。

    为什么整批拒绝而不是跳过坏项：一次事务的语义就是「这组改动一起生效」，
    悄悄丢掉一项会让落盘结果与模型心里的意图不一致，而这正是本工具要消灭的问题。
    """
    if not isinstance(raw, list) or not raw:
        raise ToolError("edits 必须是一个非空的数组")
    if len(raw) > MAX_EDIT_FILES:
        raise ToolError(
            f"一次最多改 {MAX_EDIT_FILES} 个文件（收到 {len(raw)} 个），请拆成多次提交"
        )

    ops: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"edits 第 {index} 项不是对象")
        path = str(item.get("path") or "").strip()
        if not path:
            raise ToolError(f"edits 第 {index} 项缺少 path")
        if path in seen:
            raise ToolError(f"edits 里出现重复路径: {path}（同一文件请合并成一项）")
        seen.add(path)

        has_patch = "search" in item or "replace" in item
        has_content = "content" in item
        if has_patch and has_content:
            raise ToolError(
                f"{path}: 不能同时给 search/replace 与 content —— 局部改走 search+replace，"
                "整文件写走 content，二者择一"
            )
        if not has_patch and not has_content:
            raise ToolError(
                f"{path}: 需要 search+replace（局部改）或 content（整文件写），二者之一"
            )

        ops.append(
            {
                "path": path,
                "mode": "patch" if has_patch else "overwrite",
                "search": str(item.get("search") or ""),
                "replace": str(item.get("replace") or ""),
                "content": str(item.get("content") or ""),
                "overwrite": bool(item.get("overwrite")),
                "replace_all": bool(item.get("replace_all")),
                "expected_digest": str(item.get("expected_digest") or ""),
            }
        )
    return ops


def _build(ctx: ToolContext, ops: list[dict], *, snapshot) -> EditTransaction:
    """把归一化后的 ops 登记进事务（**只算不写**，可能抛错）。"""
    txn = EditTransaction(paths=ctx.paths, snapshot=snapshot)
    for op in ops:
        if op["mode"] == "patch":
            txn.add_patch(
                op["path"],
                op["search"],
                op["replace"],
                replace_all=op["replace_all"],
                expected_digest=op["expected_digest"],
            )
        else:
            txn.add_overwrite(
                op["path"],
                op["content"],
                overwrite=op["overwrite"],
                expected_digest=op["expected_digest"],
            )
    return txn


def _render(report: TransactionReport) -> tuple[str, str, bool]:
    """渲染工具回话与合并 diff。返回 (content, diff, has_patch_plan)。"""
    header = f"[edit_files] {report.summary()}"
    chunks = [header]
    diffs = [header]
    for plan in report.changed:
        chunks.append(plan.summary())
        chunks.append(plan.diff)
        diffs.append(plan.summary())
        diffs.append(plan.diff)
    if not report.changed:
        chunks.append("（没有产生实际改动）")
    return "\n".join(chunks), "\n".join(diffs), any(
        plan.strategy not in {"create", "overwrite"} for plan in report.plans
    )


def _edit_files(ctx: ToolContext, args: dict) -> ToolResult:
    if not ctx.config.allow_write:
        raise SandboxViolation("当前会话已关闭写权限（allow_write=false）")

    ops = _normalize(args.get("edits"))
    # 阶段一：整批校验（只算不写）。任一失败在这里抛出，盘上不留痕。
    txn = _build(ctx, ops, snapshot=ctx.snapshot_before)

    # 阶段二：整批落盘，失败回滚。
    try:
        report = txn.commit()
    except TransactionError as exc:
        ctx.audit.record(
            "edit.transaction",
            files_count=len(ops),
            changed_count=0,
            added=0,
            removed=0,
            strategy="atomic",
            rolled_back=exc.rolled_back,
        )
        raise

    for plan in report.changed:
        ctx.record_change(
            path=plan.rel_path,
            diff=plan.diff,
            added=plan.added,
            removed=plan.removed,
            created=plan.is_create,
        )

    ctx.audit.record(
        "edit.transaction",
        files_count=len(ops),
        changed_count=report.changed_count,
        added=report.added,
        removed=report.removed,
        strategy="atomic",
        rolled_back=False,
    )

    content, diff, has_patch_plan = _render(report)
    if has_patch_plan:
        unread = [p.rel_path for p in report.plans if p.rel_path not in ctx.read_files]
        if unread:
            content += _NEVER_READ_HINT
    return ToolResult(
        ok=True,
        content=content,
        meta={
            "files": [p.rel_path for p in report.changed],
            "changed": report.changed_count,
            "added": report.added,
            "removed": report.removed,
        },
        diff=diff,
    )


def _assess_edit_files(ctx: ToolContext, args: dict) -> tuple[Risk, str]:
    """写操作一律 REVIEW；敏感路径由 check_write 在构建阶段拦下。"""
    if not ctx.config.allow_write:
        return Risk.DENY, "当前会话已关闭写权限"
    edits = args.get("edits")
    count = len(edits) if isinstance(edits, list) else 0
    return Risk.REVIEW, f"一次修改 {count} 个文件（原子事务）"


def _draft_edit_files(ctx: ToolContext, args: dict) -> list[DraftFile]:
    """草稿：一次事务里各文件落盘后的正文（只算不写）。

    先按扩展名筛掉「不需要草稿」的项再建事务：`EditTransaction` 会把每项都真正
    计算一遍（含读盘与定位），为一份 `.py` 白算没有意义。
    筛完为空就直接返回 —— 这时连事务都不必建。
    """
    ops = [op for op in _normalize(args.get("edits")) if is_draftable(op["path"])]
    if not ops:
        return []

    txn = _build(ctx, ops, snapshot=None)
    return [
        DraftFile(path=plan.rel_path, text=plan.new_text, created=plan.is_create)
        for plan in txn.plans
        if plan.changed or plan.is_create
    ]


def _preview_edit_files(ctx: ToolContext, args: dict) -> ToolPreview:
    """审批预演：汇总各文件的 unified diff，一次审批看全部（不落盘）。"""
    if not ctx.config.allow_write:
        return ToolPreview(kind="error", error="当前会话已关闭写权限", blocking=True)

    try:
        ops = _normalize(args.get("edits"))
        txn = _build(ctx, ops, snapshot=None)  # 预演绝不收检查点
    except AgentError as exc:
        return ToolPreview(
            kind="error", error=str(exc), summary="多文件改动无法应用", blocking=True
        )

    pending = [p for p in txn.plans if p.changed or p.is_create]
    if not pending:
        return ToolPreview(
            kind="text", body="（这些改动不会产生任何内容变化）",
            summary=f"一次改 {len(txn.plans)} 个文件（无实际改动）",
        )

    body = "\n".join(
        line
        for plan in pending
        for line in (f"### {plan.summary()}", plan.diff)
    )
    added = sum(p.added for p in pending)
    removed = sum(p.removed for p in pending)
    return ToolPreview(
        kind="diff",
        body=body,
        summary=f"一次改 {len(pending)} 个文件（+{added} / -{removed}，原子事务）",
    )


EDIT_FILES_SPEC = ToolSpec(
    name="edit_files",
    description=(
        "**一次原子地修改多个文件**：要么全部落盘，要么全部不落盘；"
        "中途任一文件失败，已写文件会被回滚为原内容。\n"
        "每项二选一：\n"
        "  · 局部改：{path, search, replace}（同 edit_file，search 逐字符复制自 read_file）；\n"
        "  · 整文件写：{path, content, overwrite?}（新建或整体覆盖）。\n"
        "可选 replace_all / expected_digest（乐观锁，磁盘被改则整批放弃）。\n"
        "单文件改动仍请用 edit_file —— 审批更聚焦。仅在「一处逻辑耦合到多个文件、"
        "必须同生共死」时使用本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "description": f"要一起落盘的改动列表（最多 {MAX_EDIT_FILES} 项，路径不可重复）",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对工作区的文件路径"},
                        "search": {"type": "string", "description": "局部改：被替换的原文片段"},
                        "replace": {"type": "string", "description": "局部改：替换后的新片段"},
                        "content": {"type": "string", "description": "整文件写：完整内容"},
                        "overwrite": {
                            "type": "boolean",
                            "description": "整文件写且文件已存在时必须显式置 true",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "局部改：是否替换全部匹配处，默认 false",
                        },
                        "expected_digest": {
                            "type": "string",
                            "description": "可选乐观锁：文件当前指纹，不符则整批放弃",
                        },
                    },
                    "required": ["path"],
                },
            }
        },
        "required": ["edits"],
    },
    handler=_edit_files,
    risk=Risk.REVIEW,
    assessor=_assess_edit_files,
    previewer=_preview_edit_files,
    drafter=_draft_edit_files,
)


__all__ = ["EDIT_FILES_SPEC"]