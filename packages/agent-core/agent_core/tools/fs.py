"""文件系统工具：read_file / list_dir / write_file。

一个刻意的设计取舍：**read_file 默认不返回行号**。
很多 Agent 会给每行加 "123|" 前缀，方便模型引用行号，但代价是弱模型会把这些
前缀一起复制进 edit_file 的 search 片段，导致匹配全挂。
这里把「定位」交给 grep（它返回 path:line:content），把「原样复制」交给 read_file，
职责分开后，search 片段的保真度显著提升。
"""

from __future__ import annotations

from pathlib import Path

from ..audit import digest
from ..edit import commit_plan, plan_overwrite
from ..errors import SandboxViolation, ToolError
from ..preview_media import is_draftable
from ..sandbox import PathGuard, Risk
from .base import (
    DEFAULT_IGNORED_DIRS,
    DraftFile,
    ToolContext,
    ToolPreview,
    ToolResult,
    ToolSpec,
    wrap_untrusted,
)

_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".exe", ".dll", ".so",
    ".dylib", ".bin", ".pyc", ".pyo", ".class", ".jar", ".woff", ".woff2",
    ".ttf", ".eot", ".mp3", ".mp4", ".avi", ".mov", ".sqlite", ".db",
}

_FILE_HEADER = "---8<--- 文件内容开始"
_FILE_FOOTER = "---8<--- 文件内容结束"


def _is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    return b"\x00" in chunk


# --------------------------------------------------------------------------- read
def _read_file(ctx: ToolContext, args: dict) -> ToolResult:
    path = ctx.paths.check_read(args["path"])
    if not path.exists():
        raise ToolError(f"文件不存在: {ctx.paths.relative(path)}")
    if path.is_dir():
        raise ToolError(
            f"{ctx.paths.relative(path)} 是目录不是文件。列目录请用 list_dir。"
        )
    if _is_probably_binary(path):
        raise ToolError(f"{ctx.paths.relative(path)} 看起来是二进制文件，无法作为文本读取")

    rel = ctx.paths.relative(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"读取失败: {exc}") from exc

    lines = raw.split("\n")
    total = len(lines)
    start = max(1, int(args.get("start_line") or 1))
    end = int(args.get("end_line") or total)
    end = max(start, min(end, total))

    selected = "\n".join(lines[start - 1 : end])
    body = "\n".join(
        [f"[read_file] {rel} — 共 {total} 行，本次返回第 {start}-{end} 行", ""]
        + [_FILE_HEADER]
        + [selected]
        + [_FILE_FOOTER]
        + ["", "（这两行标记之间才是文件原文，edit_file 的 search 片段请从其中原样复制）"]
    )

    ctx.mark_read(rel)
    ctx.audit.record(
        "tool.read_file", path=rel, lines=f"{start}-{end}", chars=len(selected)
    )

    meta = {"path": rel, "total_lines": total, "start": start, "end": end}
    if end < total:
        body += f"\n（文件未读完，剩余 {total - end} 行，可继续用 start_line={end + 1} 读取）"
    return ToolResult(ok=True, content=wrap_untrusted(rel, body), meta=meta)


# --------------------------------------------------------------------------- list
def render_dir_listing(
    paths: PathGuard,
    root: Path,
    *,
    depth: int = 1,
    include_hidden: bool = False,
    limit: int = 400,
) -> tuple[str, int]:
    """渲染一棵有层级缩进的目录清单。

    抽成纯函数是为了让「list_dir 工具」与「@folder 上下文引用」共用同一份渲染 ——
    两处各写一遍，迟早会出现「工具里跳过 .git、引用里却列出来」这种不一致。
    返回 (文本, 条目数)；文本含相对路径首行。
    """
    depth = max(1, min(int(depth), 4))
    lines: list[str] = []
    count = 0

    def walk(directory: Path, current_depth: int) -> None:
        nonlocal count
        if count >= limit:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if count >= limit:
                lines.append("  ... （条目过多，已截断）")
                return
            if entry.name in DEFAULT_IGNORED_DIRS:
                continue
            if entry.name.startswith(".") and not include_hidden:
                continue
            indent = "  " * (current_depth + 1)
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                count += 1
                if current_depth + 1 < depth:
                    walk(entry, current_depth + 1)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{indent}{entry.name}  ({size} B)")
                count += 1

    relative_root = paths.relative(root) or "."
    lines.append(f"{relative_root}/")
    walk(root, 0)
    if not lines[1:]:
        lines.append("  （空目录）")
    return "\n".join(lines), count


def _list_dir(ctx: ToolContext, args: dict) -> ToolResult:
    root = ctx.paths.check_list(args.get("path") or ".")
    if root.is_file():
        raise ToolError(f"{ctx.paths.relative(root)} 是文件不是目录")
    if not root.exists():
        raise ToolError(f"目录不存在: {ctx.paths.relative(root)}")

    depth = max(1, min(int(args.get("depth") or 1), 4))
    include_hidden = bool(args.get("include_hidden"))
    body, count = render_dir_listing(
        ctx.paths, root, depth=depth, include_hidden=include_hidden
    )
    relative_root = ctx.paths.relative(root) or "."

    ctx.audit.record("tool.list_dir", path=relative_root, depth=depth, entries=count)
    return ToolResult(
        ok=True,
        content=wrap_untrusted(relative_root, body),
        meta={"path": relative_root, "entries": count},
    )


# --------------------------------------------------------------------------- write
def _write_file(ctx: ToolContext, args: dict) -> ToolResult:
    """整文件写入。只在新建文件或用户明确要求整体覆盖时使用。

    对应 ADR-004：默认编辑路径是 edit_file 的结构化 patch，
    这里要求 overwrite=true 才能覆盖已有文件，避免模型图省事整文件重写。
    """
    if not ctx.config.allow_write:
        raise SandboxViolation("当前会话已关闭写权限（allow_write=false）")

    path = ctx.paths.check_write(args["path"])
    rel = ctx.paths.relative(path)
    if path.is_dir():
        raise ToolError(f"{rel} 是目录，不能写入")

    exists = path.exists()
    if exists and not args.get("overwrite"):
        raise ToolError(
            f"{rel} 已存在。修改已有文件请用 edit_file 提交结构化补丁"
            "（只改需要改的部分）；确有整体重写必要时，请显式设置 overwrite=true。"
        )

    # 检查点（B1）：真正落盘前收旧内容（新建文件记 existed=false）
    ctx.snapshot_before(path, rel)
    plan = plan_overwrite(path, args["content"], rel_path=rel)
    commit_plan(plan)

    ctx.record_change(
        path=rel,
        diff=plan.diff,
        added=plan.added,
        removed=plan.removed,
        created=not exists,
    )
    ctx.audit.record(
        "tool.write_file",
        path=rel,
        mode="overwrite" if exists else "create",
        added=plan.added,
        removed=plan.removed,
        bytes=len(args["content"].encode("utf-8")),
        # 只记录指纹与长度，正文不出本机（ADR-006）
        content_digest=digest(args["content"]),
    )

    verb = "覆盖" if exists else "新建"
    return ToolResult(
        ok=True,
        content=f"[write_file] {verb} {rel} (+{plan.added} / -{plan.removed})\n{plan.diff}",
        meta={"path": rel, "created": not exists},
        diff=plan.diff,
    )


def _draft_write(ctx: ToolContext, args: dict) -> list[DraftFile]:
    """草稿：整文件写入后的正文（只算不写）。

    覆盖已有文件时走 `plan_overwrite` 而不是直接回传 `content`：落盘要经过
    TextDoc 处理（编码 / BOM / 换行风格），草稿必须展示**同一份**结果 ——
    否则「预览里的样子」和「写下去的样子」会在换行符这种细节上分叉。
    """
    raw = str(args.get("path") or "")
    if not is_draftable(raw):
        return []

    path = ctx.paths.check_read(raw)
    rel = ctx.paths.relative(path)
    if not is_draftable(rel) or path.is_dir():
        return []

    content = args.get("content") or ""
    if path.is_file():
        plan = plan_overwrite(path, content, rel_path=rel)
        return [DraftFile(path=rel, text=plan.new_text, created=False)]
    return [DraftFile(path=rel, text=content, created=True)]


def _preview_write(ctx: ToolContext, args: dict) -> ToolPreview:
    """审批预演：展示整文件写入会产生什么。

    新建文件时给出完整内容；覆盖已有文件时给出 diff —— 覆盖是最危险的一类写操作
    （ADR-004 之所以要求显式 overwrite=true 就是这个原因），
    审批人必须能一眼看清「原有内容里哪些行要没了」。
    """
    if not ctx.config.allow_write:
        return ToolPreview(kind="error", error="当前会话已关闭写权限", blocking=True)

    try:
        path = ctx.paths.check_write(args.get("path") or "")
    except SandboxViolation as exc:
        return ToolPreview(kind="error", error=str(exc), blocking=True)

    rel = ctx.paths.relative(path)
    if path.is_dir():
        return ToolPreview(kind="error", error=f"{rel} 是目录，不能写入", blocking=True)

    content = args.get("content") or ""
    exists = path.is_file()
    plan = plan_overwrite(path, content, rel_path=rel)

    if not exists:
        return ToolPreview(
            kind="text",
            body=content,
            summary=f"新建 {rel}（{len(content.splitlines())} 行）",
        )
    if not args.get("overwrite"):
        return ToolPreview(
            kind="error",
            error=(
                f"{rel} 已存在。修改已有文件请用 edit_file 提交结构化补丁；"
                "确有整体重写必要时，请显式设置 overwrite=true。"
            ),
            blocking=True,
        )
    return ToolPreview(kind="diff", body=plan.diff, summary=f"覆盖 {rel} (+{plan.added} / -{plan.removed})")


# --------------------------------------------------------------------------- spec
READ_FILE_SPEC = ToolSpec(
    name="read_file",
    description=(
        "读取工作区内某个文本文件的原文。返回内容被 ---8<--- 标记包裹，"
        "标记之间的部分才是原样文本，edit_file 的 search 片段必须从这里复制。\n"
        "大文件可用 start_line / end_line 分段读取。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径，如 src/app.py"},
            "start_line": {"type": "integer", "description": "起始行号（从 1 开始），可选"},
            "end_line": {"type": "integer", "description": "结束行号（含），可选"},
        },
        "required": ["path"],
    },
    handler=_read_file,
    risk=Risk.SAFE,
)

LIST_DIR_SPEC = ToolSpec(
    name="list_dir",
    description="列出工作区内某个目录的结构与文件大小。默认忽略 .git / node_modules 等目录。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对目录路径，默认工作区根目录"},
            "depth": {"type": "integer", "description": "递归深度 1-4，默认 1"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件，默认 false"},
        },
        "required": [],
    },
    handler=_list_dir,
    risk=Risk.SAFE,
)

WRITE_FILE_SPEC = ToolSpec(
    name="write_file",
    description=(
        "整文件写入。仅用于新建文件，或用户明确要求整体覆盖。\n"
        "修改已有文件请优先使用 edit_file —— 整文件重写会丢失未提及的改动。\n"
        "覆盖已有文件必须显式传 overwrite=true。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "content": {"type": "string", "description": "完整的文件内容（含末尾换行）"},
            "overwrite": {
                "type": "boolean",
                "description": "文件已存在时必须显式置为 true 才允许覆盖，默认 false",
            },
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
    risk=Risk.REVIEW,
    previewer=_preview_write,
    drafter=_draft_write,
)
