"""git_diff（C2）：把「相对 git 的改动」直接给模型，不必让它拼命令。

与 run_command 跑 `git diff` 的分工：
    run_command  —— 通用逃生口，但每次都要过审批闸门（`git diff` 虽在只读白名单，仍会
                    在日志里留下一条命令原文，且模型很容易把参数拼错）；
    git_diff     —— 固定动作、固定输出形状，SAFE 级，不打扰用户。

刻意**只读**：不 commit、不 add、不 stash。改动落盘的唯一入口仍是补丁引擎，
把「提交」这类动作收进来等于给红线开一个新口子（见 P0 计划 §5「明确不做」）。
"""

from __future__ import annotations

from .. import gitinfo
from ..errors import ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec, wrap_untrusted

_STATUS_LABEL = {
    "M": "已修改",
    "A": "新增（已暂存）",
    "D": "已删除",
    "R": "重命名",
    "C": "复制",
    "??": "未跟踪",
}

# 送给模型的 diff 上限。gitinfo 已经截断到 400k，但那个量级会直接把上下文冲掉，
# 所以再收一道：这里只回答「改了什么」，细节让模型用 read_file 精读。
_BODY_LIMIT = 20_000


def _render_status(data: dict) -> str:
    head = f"[git_diff] 分支 {data.get('branch') or '(未知)'}"
    if data.get("detached"):
        head += "（分离头指针）"
    if data.get("upstream"):
        head += f" → {data['upstream']}"
        counts = []
        if data.get("ahead"):
            counts.append(f"领先 {data['ahead']}")
        if data.get("behind"):
            counts.append(f"落后 {data['behind']}")
        if counts:
            head += f"（{'，'.join(counts)}）"
    if data.get("unborn"):
        head += " —— 仓库还没有任何提交"

    entries = data.get("entries") or []
    if not entries:
        return head + "\n工作区干净：没有未提交的改动。"

    counts = data.get("counts") or {}
    lines = [
        head,
        f"改动 {counts.get('changed', len(entries))} 个文件"
        f"（未跟踪 {counts.get('untracked', 0)}，冲突 {counts.get('conflicts', 0)}）：",
    ]
    for item in entries:
        label = _STATUS_LABEL.get(item["status"], item["status"])
        line = f"  {item['status']:<2} {item['path']}  {label}"
        if item.get("orig"):
            line += f"（原 {item['orig']}）"
        if item.get("conflicted"):
            line += "  ← 合并冲突未解决"
        lines.append(line)
    return "\n".join(lines)


def _render_diff(data: dict) -> str:
    if not data.get("diff"):
        return (
            f"[git_diff] 相对 {data.get('ref')} 没有任何改动"
            "（暂存区与工作区都与该基准一致）。"
        )
    scope = "暂存区" if data.get("staged") else "工作区"
    target = f"，路径 {data['path']}" if data.get("path") else ""
    lines = [
        f"[git_diff] {scope}相对 {data.get('ref')} 的改动"
        f"{target}：{data.get('files', 0)} 个文件，+{data.get('added', 0)} -{data.get('removed', 0)}",
    ]
    body = data["diff"]
    if len(body) > _BODY_LIMIT:
        body = body[:_BODY_LIMIT] + "\n…（diff 过长已截断，用 path 参数缩小范围，或直接用 read_file 看文件）"
    lines.append(body)
    if data.get("truncated"):
        lines.append("（注：gitinfo 侧也已截断到 400000 字符）")
    return "\n".join(lines)


def _git_diff(ctx: ToolContext, args: dict) -> ToolResult:
    mode = str(args.get("mode") or "diff").strip().lower()
    if mode not in ("diff", "status"):
        raise ToolError(f"mode 只能是 diff 或 status，收到 {mode!r}")

    root = ctx.config.workspace

    if mode == "status":
        data = gitinfo.status(root)
        if not data.get("available"):
            return _not_a_repo(data.get("reason") or "")
        body = _render_status(data)
        ctx.audit.record(
            "tool.git_diff",
            mode="status",
            path="",
            ref="",
            staged=False,
            files=len(data.get("entries") or []),
            added=0,
            removed=0,
            diff=None,
        )
        return ToolResult(
            ok=True,
            content=wrap_untrusted("git_diff", body),
            meta={
                "available": True,
                "mode": "status",
                "branch": data.get("branch") or "",
                "files": len(data.get("entries") or []),
                "conflicts": len(data.get("conflicts") or []),
            },
        )

    ref = str(args.get("ref") or "HEAD").strip() or "HEAD"
    staged = bool(args.get("staged", False))
    rel = ""
    if args.get("path"):
        # 路径也要过 PathGuard：模型给的 path 同样是不可信输入
        target = ctx.paths.check_list(str(args["path"]))
        rel = ctx.paths.relative(target)

    data = gitinfo.diff(root, ref=ref, path=rel or None, staged=staged)
    if not data.get("available"):
        return _not_a_repo(data.get("reason") or "")

    body = _render_diff(data)
    ctx.audit.record(
        "tool.git_diff",
        mode="diff",
        path=rel,
        ref=data.get("ref") or ref,
        staged=staged,
        files=data.get("files", 0),
        added=data.get("added", 0),
        removed=data.get("removed", 0),
        # 与 tool.run_command 的 command 同一约定：原文只留本机，
        # 上传时由 governance/vocabulary.py 换成 digest + 长度（ADR-006）
        diff=data.get("diff") or None,
    )
    return ToolResult(
        ok=True,
        content=wrap_untrusted("git_diff", body),
        meta={
            "available": True,
            "mode": "diff",
            "ref": data.get("ref") or ref,
            "staged": staged,
            "files": data.get("files", 0),
            "added": data.get("added", 0),
            "removed": data.get("removed", 0),
        },
        diff=data.get("diff") or "",
    )


def _not_a_repo(reason: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=(
            f"[git_diff] 当前工作区不是 git 仓库：{reason}。\n"
            "改用 read_file / grep / list_dir 直接看文件内容。"
        ),
        meta={"available": False},
    )


GIT_DIFF_SPEC = ToolSpec(
    name="git_diff",
    description=(
        "查看工作区**相对 git 的**改动：默认给出「工作区 vs HEAD」的 unified diff，"
        "可只看暂存区（staged=true）或限定某个 path。\n"
        "mode=status 时给分支、上游领先/落后、改动文件清单与未解决的合并冲突。\n"
        "用途：判断「这些改动相对上次提交是什么样」「有哪些文件被改/被删/未跟踪」。\n"
        "想看**本次会话里**改了什么（不依赖 git），用改动面板的「本会话」视图。\n"
        "只读工具：不会 commit、add 或 stash。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["diff", "status"],
                "description": "diff（默认）看改动内容；status 看分支与文件清单",
            },
            "ref": {"type": "string", "description": "对比基准，默认 HEAD"},
            "path": {"type": "string", "description": "只比较该路径（工作区相对路径）"},
            "staged": {"type": "boolean", "description": "true = 比较暂存区（git diff --cached）"},
        },
    },
    handler=_git_diff,
    risk=Risk.SAFE,
)


__all__ = ["GIT_DIFF_SPEC"]