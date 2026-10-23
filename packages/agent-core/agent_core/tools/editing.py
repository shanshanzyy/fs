"""编辑工具：edit_file —— 落地 ADR-004 的唯一默认编辑入口。"""

from __future__ import annotations

from ..audit import digest
from ..edit import commit_plan, plan_patch
from ..errors import AgentError, SandboxViolation
from ..preview_media import is_draftable
from ..sandbox import Risk
from .base import DraftFile, ToolContext, ToolPreview, ToolResult, ToolSpec

_NEVER_READ_HINT = (
    "\n（提示：本次会话还没有读过这个文件。edit_file 的 search 片段必须与文件当前内容"
    "逐字符一致，凭记忆书写极易失配。建议先 read_file 确认原文。）"
)


def _edit_file(ctx: ToolContext, args: dict) -> ToolResult:
    if not ctx.config.allow_write:
        raise SandboxViolation("当前会话已关闭写权限（allow_write=false）")

    path = ctx.paths.check_write(args["path"])
    rel = ctx.paths.relative(path)

    # plan_patch 只计算不落盘，因此这里抛出的定位失败不会污染工作区
    plan = plan_patch(
        path,
        args["search"],
        args["replace"],
        rel_path=rel,
        replace_all=bool(args.get("replace_all")),
    )
    # 检查点（B1）：真正落盘前收旧内容，保证这笔改动可回滚
    ctx.snapshot_before(path, rel)
    commit_plan(plan)

    ctx.record_change(
        path=rel, diff=plan.diff, added=plan.added, removed=plan.removed
    )
    ctx.audit.record(
        "tool.edit_file",
        path=rel,
        strategy=plan.strategy,
        added=plan.added,
        removed=plan.removed,
        # 只落指纹，不落代码原文（ADR-006）
        search_digest=digest(args["search"]),
        replace_digest=digest(args["replace"]),
        search_lines=args["search"].count("\n") + 1,
    )

    content = f"[edit_file] {plan.summary()}\n{plan.diff}"
    if rel not in ctx.read_files:
        content += _NEVER_READ_HINT
    return ToolResult(
        ok=True,
        content=content,
        meta={
            "path": rel,
            "strategy": plan.strategy,
            "added": plan.added,
            "removed": plan.removed,
        },
        diff=plan.diff,
    )


def _preview_edit(ctx: ToolContext, args: dict) -> ToolPreview:
    """审批预演：算出真实的 patch diff，让人审的是改动本身而不是 search/replace 参数。

    这里与 _edit_file 走的是同一个 plan_patch，区别只在最后不调 commit_plan。
    因此「审批时看到的 diff」与「放行后落盘的内容」必然一致 —— 不会出现
    审批人以为改 A、实际改了 B 的情况。

    定位失败能在这一步就查出来：这是 edit_file 最常见的失败原因（search 片段
    与文件当前内容对不上）。既已确定失败，就没必要再弹窗打扰用户，
    直接标记 blocking 让循环把诊断信息回灌给模型。
    """
    if not ctx.config.allow_write:
        return ToolPreview(kind="error", error="当前会话已关闭写权限", blocking=True)

    try:
        path = ctx.paths.check_write(args.get("path") or "")
    except SandboxViolation as exc:
        return ToolPreview(kind="error", error=str(exc), blocking=True)

    rel = ctx.paths.relative(path)
    if not path.is_file():
        return ToolPreview(kind="error", error=f"{rel} 不存在或不是普通文件", blocking=True)

    try:
        plan = plan_patch(
            path,
            args.get("search") or "",
            args.get("replace") or "",
            rel_path=rel,
            replace_all=bool(args.get("replace_all")),
        )
    except AgentError as exc:
        return ToolPreview(
            kind="error",
            error=str(exc),
            summary=f"{rel}：补丁无法应用",
            blocking=True,
        )

    summary = plan.summary()
    return ToolPreview(kind="diff", body=plan.diff, summary=summary)


def _draft_edit(ctx: ToolContext, args: dict) -> list[DraftFile]:
    """草稿：把这次补丁应用到原文件后的**全文**（只算不写）。

    早退的理由不是省事：`.py` 之类的文件走的是「看 diff」，草稿正文既不能渲染、
    也让额外一份模型产出的内容在审批期被复制一遍。只有浏览器能渲染的文本资源
    才值得。注意这只是优化 —— 真正的过滤在 `ToolSpec.draft` 里再做一次。
    """
    raw = str(args.get("path") or "")
    if not is_draftable(raw):
        return []

    path = ctx.paths.check_read(raw)
    rel = ctx.paths.relative(path)
    if not path.is_file() or not is_draftable(rel):
        return []

    plan = plan_patch(
        path,
        args.get("search") or "",
        args.get("replace") or "",
        rel_path=rel,
        replace_all=bool(args.get("replace_all")),
    )
    if not plan.changed:
        return []
    return [DraftFile(path=rel, text=plan.new_text, created=False)]


def _assess_edit(ctx: ToolContext, args: dict) -> tuple[Risk, str]:
    """写操作一律 REVIEW；命中敏感文件时提前拦下。"""
    if not ctx.config.allow_write:
        return Risk.DENY, "当前会话已关闭写权限"
    try:
        path = ctx.paths.check_write(args.get("path") or "")
    except SandboxViolation as exc:
        return Risk.DENY, str(exc)
    return Risk.REVIEW, f"修改 {ctx.paths.relative(path)}"


EDIT_FILE_SPEC = ToolSpec(
    name="edit_file",
    description=(
        "对已有文件做**局部修改**：给出原样片段 search 与替换片段 replace，引擎精确定位替换。\n"
        "规则：\n"
        "  1. search 必须逐字符复制自 read_file 返回的内容（不要凭记忆改写，不要带行号前缀）；\n"
        "  2. search 必须能在文件中唯一命中，必要时带上前后各 2-3 行上下文；\n"
        "  3. 只给需要改动的片段，不要给整个文件；\n"
        "  4. 若要全部替换（如批量改名），显式传 replace_all=true。\n"
        "引擎支持三级容错匹配（精确 / 忽略行尾空白 / 忽略整体缩进），命中后会回报实际使用的策略。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "search": {"type": "string", "description": "要被替换的原文片段，必须与文件内容逐字符一致"},
            "replace": {"type": "string", "description": "替换后的新片段"},
            "replace_all": {"type": "boolean", "description": "是否替换全部匹配处，默认 false"},
        },
        "required": ["path", "search", "replace"],
    },
    handler=_edit_file,
    risk=Risk.REVIEW,
    assessor=_assess_edit,
    previewer=_preview_edit,
    drafter=_draft_edit,
)
