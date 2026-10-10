"""上下文引用（P0-A）：`@file` / `@symbol` / `@folder` 的后端解析。

正文永远由后端经 `PathGuard` 读取，前端只传「引用意图」——
这样路径校验只有一处，CLI 与桌面端共用同一实现。
详见 `references.py` 的模块说明。
"""

from .references import (
    MAX_REF_CHARS,
    MAX_REF_ITEMS,
    SYMBOL_CONTEXT_LINES,
    RefResolution,
    ResolvedRef,
    build_refs_prompt,
    format_refs_block,
    iter_workspace_dirs,
    iter_workspace_files,
    iter_workspace_symbols,
    resolve_refs,
)

__all__ = [
    "MAX_REF_CHARS",
    "MAX_REF_ITEMS",
    "SYMBOL_CONTEXT_LINES",
    "RefResolution",
    "ResolvedRef",
    "build_refs_prompt",
    "format_refs_block",
    "iter_workspace_dirs",
    "iter_workspace_files",
    "iter_workspace_symbols",
    "resolve_refs",
]