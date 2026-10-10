"""repo_wiki —— 生成可长期保存的仓库 Wiki（markdown，落盘）。

与 `repo_map` 的分工：`repo_map` 是给**模型**看的符号地图（进上下文、省 token），
`repo_wiki` 是给**人**看的文档（落盘、可审阅、可提交进仓库）。

落盘恒走 `plan_overwrite` + `commit_plan`（ADR-004：结构化补丁 + 审批），与
`write_file` 是同一条编辑面 —— 因此它**恒为 REVIEW 级**，一次调用产出的所有文件
在**一个**审批浮层里看全（`previewer` 汇总各文件 diff）。

三条红线（见 docs/M3+-PLAN.md）：
    · Wiki 只由**本机代码结构**生成，不调用模型、不外发（正文不出机，ADR-006）；
    · 落点恒过 `check_write`：越界与 `.vibe/` 一律拒绝；
    · 审计只记元数据（目录 / 计数 / 字节 / 是否截断），绝不含 Wiki 正文。
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from ..edit import commit_plan, plan_overwrite
from ..errors import SandboxViolation, ToolError
from ..index import get_index
from ..sandbox import Risk
from ..wiki import WikiBundle, generate_wiki
from .base import ToolContext, ToolPreview, ToolResult, ToolSpec, wrap_untrusted

# 审批预演里 diff 正文的字符上限；超出只截断提示，不新造组件。
_PREVIEW_LIMIT = 40_000


def _collect(ctx: ToolContext, args: dict) -> tuple[WikiBundle, str, list]:
    """读取 + 生成 + 规划落盘（**不落盘、不写审计**）。handler 与 previewer 共用。"""
    src = ctx.paths.check_list(args.get("path") or ".")
    if src.is_file():
        raise ToolError(f"{ctx.paths.relative(src)} 是文件不是目录")

    target_rel = str(args.get("target") or ctx.config.wiki_dir).strip() or ctx.config.wiki_dir
    # 落点校验：越界 / 敏感 / `.vibe/` 在这里被 PathGuard 拒掉（root 也被拒）。
    target = ctx.paths.check_write(target_rel)
    if target.exists() and not target.is_dir():
        raise ToolError(f"落点 {ctx.paths.relative(target)} 已存在且不是目录")

    # 复用进程内索引：文件清单与依赖图都不重复构建。符号索引不保留，
    # 由生成器按需重扫（受 wiki_max_files 上限约束）。
    index = get_index(
        src,
        ctx.paths.relative,
        ctx.config.semantic_max_files,
        enhanced=ctx.config.retrieval_enhanced,
    )
    # `index.files` 是**工作区相对**路径，因此读取根必须是工作区根而不是 `src`：
    # 传 `src` 会让 `src/<工作区相对路径>` 永远读不到文件（符号静默丢失）。
    bundle = generate_wiki(
        ctx.paths.root_resolved,
        index.files,
        index.graph,
        max_files=ctx.config.wiki_max_files,
    )

    plans = []
    for page in bundle.pages:
        rel_path = posixpath.join(target_rel, page.name)
        path = ctx.paths.check_write(rel_path)
        plans.append((rel_path, path, plan_overwrite(path, page.content, rel_path=rel_path)))
    return bundle, target_rel, plans


def _repo_wiki(ctx: ToolContext, args: dict) -> ToolResult:
    if not ctx.config.allow_write:
        raise SandboxViolation("当前会话已关闭写权限（allow_write=false）")

    bundle, target_rel, plans = _collect(ctx, args)

    existing = [rel for rel, _path, plan in plans if not plan.is_create]
    if existing and not args.get("overwrite"):
        shown = "\n".join(f"  - {rel}" for rel in existing[:10])
        more = "" if len(existing) <= 10 else f"\n  … 另有 {len(existing) - 10} 个"
        raise ToolError(
            f"落点已存在 {len(existing)} 个同名文件，拒绝覆盖：\n{shown}{more}\n"
            "确认要整体重建时请显式设置 overwrite=true。"
        )

    created = updated = 0
    for rel, path, plan in plans:
        if not plan.is_create and not plan.changed:
            continue  # 内容一致：不落盘、不计入改动
        # 检查点：真正落盘前收旧内容（新建文件记 existed=false）
        ctx.snapshot_before(path, rel)
        commit_plan(plan)
        ctx.record_change(
            path=rel,
            diff=plan.diff,
            added=plan.added,
            removed=plan.removed,
            created=plan.is_create,
        )
        if plan.is_create:
            created += 1
        else:
            updated += 1

    written = created + updated
    ctx.audit.record(
        "tool.repo_wiki",
        path=target_rel,
        files=written,
        created=created,
        updated=updated,
        bytes=bundle.bytes,
        truncated=bundle.truncated,
    )

    lines = [
        f"[repo_wiki] 生成 {written} 个文件到 {target_rel}/"
        f"（新建 {created} / 覆盖 {updated}）",
        f"本次覆盖 {bundle.files} 个源文件 / {bundle.symbols} 个符号 / "
        f"{bundle.edges} 条依赖边，共 {bundle.modules} 个模块页。",
    ]
    if bundle.truncated:
        lines.append(
            f"（已截断：共 {bundle.total_files} 个文件，仅生成前 {bundle.files} 个）"
        )
    lines.append("")
    lines.extend(f"- {rel}" for rel, _path, plan in plans if plan.is_create or plan.changed)

    return ToolResult(
        ok=True,
        content=wrap_untrusted("repo_wiki", "\n".join(lines)),
        meta={
            "files": written,
            "created": created,
            "updated": updated,
            "truncated": bundle.truncated,
        },
    )


def _preview_repo_wiki(ctx: ToolContext, args: dict) -> ToolPreview:
    """审批预演：汇总各文件的 diff，让审批人**一次**看全将写盘的内容。"""
    if not ctx.config.allow_write:
        return ToolPreview(kind="error", error="当前会话已关闭写权限", blocking=True)
    try:
        bundle, target_rel, plans = _collect(ctx, args)
    except SandboxViolation as exc:
        return ToolPreview(kind="error", error=str(exc), blocking=True)
    except ToolError as exc:
        return ToolPreview(kind="error", error=str(exc), blocking=True)

    existing = [rel for rel, _path, plan in plans if not plan.is_create]
    if existing and not args.get("overwrite"):
        return ToolPreview(
            kind="error",
            error=(
                f"落点已存在 {len(existing)} 个同名文件，拒绝覆盖（示例：{existing[0]}）。"
                "确认要整体重建时请显式设置 overwrite=true。"
            ),
            blocking=True,
        )

    lines = [f"[repo_wiki] 将生成 {len(plans)} 个文件到 {target_rel}/：", ""]
    for rel, _path, plan in plans:
        verb = "新建" if plan.is_create else f"覆盖 (+{plan.added} / -{plan.removed})"
        lines.append(f"- {rel}  （{verb}）")
    lines.append("")
    for rel, _path, plan in plans:
        lines.append(f"=== {rel} ===")
        lines.append(plan.diff or "（内容无变化）")
        lines.append("")
    body = "\n".join(lines)
    if len(body) > _PREVIEW_LIMIT:
        body = body[:_PREVIEW_LIMIT] + "\n…（diff 过大已截断，落盘内容以实际文件为准）"

    if bundle.truncated:
        summary = f"生成 Wiki：{len(plans)} 个文件到 {target_rel}/（已截断）"
    else:
        summary = f"生成 Wiki：{len(plans)} 个文件到 {target_rel}/"
    return ToolPreview(kind="diff", body=body, summary=summary)


REPO_WIKI_SPEC = ToolSpec(
    name="repo_wiki",
    description=(
        "把整个项目（或指定子目录）的结构生成为 markdown Wiki 并落盘："
        "总览（目录树 + 模块清单 + 统计）与每模块一页（文件清单、关键符号与行号、依赖关系）。\n"
        "用途：给仓库留一份**可长期保存、可提交进版本库**的结构文档。\n"
        "Wiki 只由本机代码结构生成，不含模型解说；落盘需经审批。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要生成 Wiki 的源码范围，默认整个工作区"},
            "target": {"type": "string", "description": "落点目录（相对工作区），默认 docs/wiki"},
            "overwrite": {
                "type": "boolean",
                "description": "落点已存在同名文件时必须显式置为 true 才允许覆盖，默认 false",
            },
        },
        "required": [],
    },
    handler=_repo_wiki,
    risk=Risk.REVIEW,
    previewer=_preview_repo_wiki,
)