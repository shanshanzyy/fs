"""检索工具：grep（内容检索）与 find_files（路径检索）。

刻意用纯标准库实现，不依赖 ripgrep：
    - 内网环境不一定有 rg，装了也未必在 PATH 里；
    - 检索结果是喂给模型的，可预测性比极致速度更重要。
代价是超大仓库会慢，因此加了文件数与结果数上限（见 limit/max_results）。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from ..errors import ToolError
from ..sandbox import Risk
from .base import (
    DEFAULT_IGNORED_DIRS,
    MAX_SCAN_BYTES,
    ToolContext,
    ToolResult,
    ToolSpec,
    wrap_untrusted,
)


def _iter_files(root: Path, max_files: int = 20000):
    """深度优先遍历工作区，跳过忽略目录与超限文件。"""
    seen = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        dirs, files = [], []
        for entry in entries:
            if entry.name in DEFAULT_IGNORED_DIRS:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
            except OSError:
                continue
        for path in files:
            seen += 1
            if seen > max_files:
                return
            yield path
        stack.extend(sorted(dirs, reverse=True))


def _looks_binary(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return True
        with path.open("rb") as fh:
            return b"\x00" in fh.read(2048)
    except OSError:
        return True


# --------------------------------------------------------------------------- grep
def _grep(ctx: ToolContext, args: dict) -> ToolResult:
    pattern = args["pattern"]
    regex = None
    try:
        regex = re.compile(pattern, 0 if args.get("case_sensitive") else re.IGNORECASE)
    except re.error as exc:
        # 常见情况：模型想搜字面量 "a.b" 却写成了正则
        try:
            regex = re.compile(re.escape(pattern), 0 if args.get("case_sensitive") else re.IGNORECASE)
            note = f"（{pattern!r} 不是合法正则，已按字面量处理）"
        except re.error:
            raise ToolError(
                f"检索表达式非法: {exc}\n"
                "  如需搜索字面量，请直接写纯文本；如需正则，请检查括号与转义。"
            ) from exc
    else:
        note = ""

    root = ctx.paths.check_list(args.get("path") or ".")
    if not root.exists():
        raise ToolError(f"路径不存在: {ctx.paths.relative(root)}")

    glob_filter = args.get("glob") or ""
    max_results = max(1, min(int(args.get("max_results") or 50), 400))
    before = max(0, min(int(args.get("context_lines") or 0), 5))

    targets = [root] if root.is_file() else list(_iter_files(root))
    hits: list[str] = []
    files_scanned = 0
    truncated = False

    for path in targets:
        if len(hits) >= max_results:
            truncated = True
            break
        rel = ctx.paths.relative(path)
        if glob_filter and not fnmatch.fnmatch(rel, glob_filter) and not fnmatch.fnmatch(path.name, glob_filter):
            continue
        if _looks_binary(path):
            continue
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if regex.search(line):
                if before:
                    lo = max(0, index - before)
                    for context_index in range(lo, index):
                        hits.append(f"{rel}:{context_index + 1}-: {lines[context_index][:300]}")
                hits.append(f"{rel}:{index + 1}: {line[:300]}")
                if len(hits) >= max_results:
                    truncated = True
                    break

    header = (
        f"[grep] 表达式 {pattern!r} 于 {ctx.paths.relative(root) or '.'}"
        f"（匹配模式 {glob_filter or '*'}）{note}\n"
        f"扫描 {files_scanned} 个文件，命中 {len(hits)} 条"
        + ("，结果已截断" if truncated else "")
    )
    if not hits:
        body = header + "\n（无匹配。可放宽条件：去掉 glob 过滤、改用更短的子串，或换用 list_dir 看目录结构）"
    else:
        body = header + "\n\n" + "\n".join(hits)
    if truncated:
        body += "\n（结果已达上限，请缩小 path 范围或把 pattern 写得更具体）"

    ctx.audit.record(
        "tool.grep", pattern=pattern, path=ctx.paths.relative(root), hits=len(hits)
    )
    return ToolResult(
        ok=True,
        content=wrap_untrusted("grep", body),
        meta={"hits": len(hits), "files_scanned": files_scanned},
    )


# --------------------------------------------------------------------------- find
def _find_files(ctx: ToolContext, args: dict) -> ToolResult:
    root = ctx.paths.check_list(args.get("path") or ".")
    if not root.exists():
        raise ToolError(f"路径不存在: {ctx.paths.relative(root)}")

    pattern = args.get("pattern") or "*"
    max_results = max(1, min(int(args.get("max_results") or 100), 500))
    if not root.is_dir():
        matches = [root] if fnmatch.fnmatch(root.name, pattern) else []
        files_scanned = 1
    else:
        found: list[Path] = []
        files_scanned = 0
        for path in _iter_files(root):
            files_scanned += 1
            rel = ctx.paths.relative(path)
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(rel, pattern):
                found.append(path)
                if len(found) >= max_results:
                    break
        matches = found

    lines = [ctx.paths.relative(p) for p in matches]
    header = (
        f"[find_files] 模式 {pattern!r} 于 {ctx.paths.relative(root) or '.'}\n"
        f"扫描 {files_scanned} 个文件，命中 {len(lines)} 个路径"
    )
    body = header + "\n\n" + ("\n".join(lines) if lines else "（无匹配）")
    ctx.audit.record("tool.find_files", pattern=pattern, hits=len(lines))
    return ToolResult(
        ok=True,
        content=wrap_untrusted("find_files", body),
        meta={"hits": len(lines)},
    )


GREP_SPEC = ToolSpec(
    name="grep",
    description=(
        "在工作区内按正则检索文件内容，返回 `路径:行号: 内容`。\n"
        "用途：定位符号定义、查找调用点、搜索 TODO、确认某段代码在哪些文件出现。\n"
        "改动代码前应先用它确认目标位置，再用 read_file 读取原文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式；纯文本也能用"},
            "path": {"type": "string", "description": "检索范围目录或文件，默认工作区根目录"},
            "glob": {"type": "string", "description": "文件名过滤，如 '*.py' 或 'src/**/*.ts'"},
            "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 false"},
            "context_lines": {"type": "integer", "description": "每条命中前附带的上下文行数 0-5，默认 0"},
            "max_results": {"type": "integer", "description": "最多返回多少条，默认 50，上限 400"},
        },
        "required": ["pattern"],
    },
    handler=_grep,
    risk=Risk.SAFE,
)

FIND_FILES_SPEC = ToolSpec(
    name="find_files",
    description="按文件名通配符查找工作区内的文件路径，如 '*.py'、'test_*.ts'、'**/config/*.yaml'。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "通配符模式，默认 '*'"},
            "path": {"type": "string", "description": "查找范围，默认工作区根目录"},
            "max_results": {"type": "integer", "description": "最多返回多少条，默认 100"},
        },
        "required": ["pattern"],
    },
    handler=_find_files,
    risk=Risk.SAFE,
)
