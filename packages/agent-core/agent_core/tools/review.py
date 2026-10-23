"""review_changes（M9-H）：提交前的只读自审工作流。

## 为什么恒 SAFE

`review_changes` 只做一件事：把「当前工作区相对 HEAD 的改动」组装成一份
**审阅材料包**（unified diff + 相关文件上下文 + 审阅模板），交给模型在下一轮
产出**结构化意见**。它**不改盘、不执行命令、不派生子 Agent** —— 审阅是「自审」，
产出的是「意见」而非「补丁」，真正要修复时仍由模型走既有 `edit_file` /
`edit_files` / `run_command`，**恒过审批漏斗**（ADR-008）。

它与「人审 Agent 补丁」（审批浮层 + diff 审查，ADR-008 的最后防线）是**两层**：
本工具是 Agent 提交补丁**之前**的自审，给人审提供**前置信息量**，绝不替代人审。

## 注入纪律

diff 与文件内容是工作区里读到的自由文本，天然是 Prompt Injection 面，
组装结果一律 `wrap_untrusted` 包裹（与记忆/工具结果同一纪律，ADR-021）。

## 审计

只记 `files / issues / severity_high / ms` 四字段（ADR-006）：审阅意见是模型
输出、属「正文类」，一个字符都不进审计；这里只记「审了多少文件、提了几条、
其中几条是严重级」，足够回答「自审到底做了没、有没有抓到大问题」。
"""

from __future__ import annotations

from .. import gitinfo
from ..errors import ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec, wrap_untrusted

# 单文件送给模型的内容上限（字节）。diff 可能很大，这里只给「改了什么 + 有限上下文」，
# 细节让模型按需用 read_file 精读。
_PER_FILE_LIMIT = 40_000
# 整份审阅材料包的总上限（字节），防止改动集过大时把上下文冲爆。
_TOTAL_LIMIT = 200_000
# 每个文件最多回看多少行「改动附近」的原文上下文。
_CONTEXT_LINES = 20


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（内容过长已截断，可用 read_file 精读原文）"


def _review_changes(ctx: ToolContext, args: dict) -> ToolResult:
    root = ctx.config.workspace
    max_files = max(1, int(ctx.config.review_max_files))

    # 取「工作区相对 HEAD」的改动（缺省审全部，可选 paths 收窄）。
    paths_arg = args.get("paths")
    rel_paths: list[str] = []
    if paths_arg:
        if isinstance(paths_arg, str):
            paths_arg = [p for p in paths_arg.split(",") if p.strip()]
        for raw in paths_arg:
            target = ctx.paths.check_list(str(raw))
            rel_paths.append(ctx.paths.relative(target))

    data = gitinfo.diff(root, ref="HEAD")
    if not data.get("available"):
        return ToolResult(
            ok=False,
            content=(
                f"[review_changes] 当前工作区不是 git 仓库：{data.get('reason') or '未知原因'}。\n"
                "无法做相对 HEAD 的自审。可改用 read_file / grep 直接看文件内容。"
            ),
            meta={"available": False},
        )

    diff_body = data.get("diff") or ""
    files = int(data.get("files", 0))
    if not diff_body.strip():
        # 干净工作区：没有可审的改动。
        ctx.audit.record("review.run", files=0, issues=0, severity_high=0, ms=0)
        return ToolResult(
            ok=True,
            content=(
                "[review_changes] 工作区相对 HEAD 没有未提交的改动，无需自审。"
            ),
            meta={"available": True, "files": 0, "issues": 0, "severity_high": 0},
        )

    # 超上限时如实告知，而不是静默截断到「好像只改了这几个文件」。
    truncated_files = False
    if files > max_files:
        truncated_files = True

    body_parts: list[str] = [
        "[review_changes] 以下为「工作区相对 HEAD」的改动材料，请据此做提交前自审。",
        "",
        f"改动 {files} 个文件（审阅上限 {max_files} 个）"
        + ("，已超出上限，请用 paths 参数收窄后再审。" if truncated_files else ""),
        "",
        "===== 改动 diff =====",
        diff_body,
    ]

    # 逐文件补一段「改动附近」的原文上下文，帮助模型判断影响面。
    if not truncated_files:
        ctx_lines: list[str] = []
        for rel in _changed_paths(diff_body):
            try:
                target = ctx.paths.check_read(rel)
                if not target.is_file():
                    continue
                raw = target.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — 单个文件读不到不影响整体审阅
                continue
            lines = raw.split("\n")
            ctx_lines.append(f"===== 文件上下文：{rel}（前 {_CONTEXT_LINES} 行）=====")
            ctx_lines.append("\n".join(lines[:_CONTEXT_LINES]))
            ctx_lines.append("")
            ctx.mark_read(rel)
        body_parts.append("\n".join(ctx_lines))

    body_parts.append(
        "===== 审阅模板 ====="
        "\n请按以下结构输出审阅意见（只给意见，不要改代码、不要生成补丁）："
        "\n- 严重级别：critical / major / minor / nit"
        "\n- 类别：bug / api / 安全 / 性能 / 可维护"
        "\n- 定位：具体文件与行号/片段"
        "\n- 说明：问题是什么、为什么是问题"
        "\n- 建议：如何修（修复仍需走 edit_file / edit_files 并接受审批）"
        "\n若无问题，明确回复「未发现问题」即可。"
    )

    full = "\n".join(body_parts)
    full = _truncate(full, _TOTAL_LIMIT)

    # 审计只记计数与耗时，不含意见正文（ADR-006）。
    # 注意：意见是模型在**下一轮**才产出的，本工具此刻只能记「审了多少文件」。
    # `issues` / `severity_high` 在工具侧无法预知，记 0 作「本轮发起」标记；
    # 真正的意见计数由上层（Agent/界面）在模型产出后回填（若接入）。
    ctx.audit.record(
        "review.run",
        files=files if not truncated_files else max_files,
        issues=0,
        severity_high=0,
        ms=0,
    )

    return ToolResult(
        ok=True,
        content=wrap_untrusted("review_changes", full),
        meta={
            "available": True,
            "files": files,
            "max_files": max_files,
            "truncated_files": truncated_files,
            "diff": diff_body,
        },
    )


def _changed_paths(diff_body: str) -> list[str]:
    """从 unified diff 里抽「改动的文件路径」，供补上下文用。

    只认 `diff --git a/<path> b/<path>` 与 `+++ b/<path>` 两类行，解析失败就返回空
    （不阻塞审阅主流程）。
    """
    seen: list[str] = []
    for line in diff_body.split("\n"):
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :]
            # 形如 a/src/app.py b/src/app.py
            b_part = rest.split(" b/", 1)[-1] if " b/" in rest else rest.split(" ", 1)[0]
            if b_part:
                seen.append(b_part.lstrip("a/"))
        elif line.startswith("+++ b/"):
            seen.append(line[len("+++ b/") :])
    # 去重保序
    out: list[str] = []
    for p in seen:
        if p and p not in out and p != "/dev/null":
            out.append(p)
    return out


REVIEW_CHANGES_SPEC = ToolSpec(
    name="review_changes",
    description=(
        "对「当前工作区相对 HEAD 的改动」做提交前的只读自审：把 diff 与相关文件上下文"
        "组装成材料，由你据此产出结构化意见（严重级别 / 类别 / 定位 / 说明 / 建议）。\n"
        "用途：在提交补丁前先自查「是否引入 bug / 是否破坏 API / 影响面多大」，"
        "给人审提供更多信息量。\n"
        "约束：\n"
        "  - 恒只读 —— 不改盘、不执行、不派子 Agent；\n"
        "  - 只给「意见」，不生成补丁；真要修复仍走 edit_file / edit_files 并接受审批；\n"
        "  - 自审不替代人审（高危改动仍须人工审批）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "paths": {
                "type": "string",
                "description": (
                    "可选，只审这些路径（逗号分隔的相对路径）。缺省审全部未提交改动。"
                ),
            },
        },
        "required": [],
    },
    handler=_review_changes,
    risk=Risk.SAFE,
)


__all__ = ["REVIEW_CHANGES_SPEC"]
