"""repo_map —— 用最小成本让模型「看懂整个项目」。

对标 Aider 的 repo map 思路：不做向量检索、不做语义分析，只用轻量的符号抽取
生成带层级的符号树与行号。它比全量丢文件便宜一两个数量级，
是长上下文场景下的第一优先级上下文来源。

抽取由两个后端协作（见 tools/symbols.py）：
    默认走正则（永久保留、零强制依赖）；装了可选依赖 `tree-sitter` 时，
    按扩展名优先走 tree-sitter —— 它能给出函数签名参数、类继承与 import 引用。
    未覆盖的扩展名或解析失败一律回退正则，不因缺包失效。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ToolError
from ..sandbox import Risk
from .base import (
    MAX_SCAN_BYTES,
    ToolContext,
    ToolResult,
    ToolSpec,
    wrap_untrusted,
)
from .search import _iter_files
from .symbols import (
    KIND_FUNC,
    KIND_TYPE,
    Symbol,
    scan_file,
    supports,
)


def _render_symbol(symbol: Symbol) -> str:
    """渲染单个符号。两个后端共用，保证格式严格一致。"""
    marker = "◆" if symbol.kind == KIND_TYPE else "·"
    detail = ""
    if symbol.kind == KIND_TYPE and symbol.inherits:
        detail = f"({symbol.inherits})"
    elif symbol.kind == KIND_FUNC and symbol.signature:
        detail = symbol.signature if symbol.signature.startswith("(") else f"({symbol.signature})"
    indent = "  " * min(symbol.indent, 3)
    return f"    {indent}{marker} {symbol.name}{detail}  L{symbol.line}"


def _repo_map(ctx: ToolContext, args: dict) -> ToolResult:
    root = ctx.paths.check_list(args.get("path") or ".")
    if not root.exists():
        raise ToolError(f"路径不存在: {ctx.paths.relative(root)}")

    max_files = max(1, min(int(args.get("max_files") or 200), 1000))
    max_symbols = max(10, min(int(args.get("max_symbols") or 2500), 8000))

    scanned = 0
    skipped_big = 0
    symbol_count = 0
    by_dir: dict[str, list[str]] = {}

    for path in _iter_files(root):
        if scanned >= max_files or symbol_count >= max_symbols:
            break
        if not supports(path.suffix):
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                skipped_big += 1
                continue
        except OSError:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        result = scan_file(path, text)
        scanned += 1
        if not result.symbols and not result.imports:
            continue

        rel = ctx.paths.relative(path)
        parent = str(Path(rel).parent).replace("\\", "/")
        parent = "" if parent == "." else parent
        line_count = len(text.split("\n"))

        block = [f"  {Path(rel).name}  (约 {line_count} 行)"]
        for ref in result.imports:
            block.append(f"    ⇢ {ref}")
        for symbol in result.symbols:
            if symbol_count >= max_symbols:
                block.append("    ... （符号过多已截断）")
                break
            block.append(_render_symbol(symbol))
            symbol_count += 1
        by_dir.setdefault(parent, []).extend(block)

    header = (
        f"[repo_map] 符号地图 —— 扫描 {scanned} 个源文件，提取 {symbol_count} 个符号\n"
        "图例：◆ 类型定义（class/struct/interface）  · 函数或方法  "
        "⇢ import 引用（需可选依赖 tree-sitter）  L数字 为定义所在行号\n"
        "（这是结构索引，不含函数体。需要看实现请用 read_file，需要定位引用请用 grep）"
    )
    if skipped_big:
        header += f"\n（另有 {skipped_big} 个文件超过 2MB 已跳过）"

    if not by_dir:
        body = header + "\n（未识别到可解析的源文件，可能是语言不在支持范围内）"
    else:
        chunks = []
        for directory in sorted(by_dir):
            chunks.append(f"{directory or '.'}/")
            chunks.extend(by_dir[directory])
        body = header + "\n\n" + "\n".join(chunks)

    ctx.audit.record(
        "tool.repo_map", path=ctx.paths.relative(root),
        files=scanned, symbols=symbol_count, truncated=symbol_count >= max_symbols,
    )
    return ToolResult(
        ok=True,
        content=wrap_untrusted("repo_map", body),
        meta={"files": scanned, "symbols": symbol_count},
    )


REPO_MAP_SPEC = ToolSpec(
    name="repo_map",
    description=(
        "生成整个项目（或指定子目录）的**符号地图**：按目录列出每个源文件及其类/函数签名与行号。\n"
        "这是最省 token 的项目全貌获取方式 —— 在动手改代码前先看它，"
        "能快速判断该改哪个文件，避免盲目 grep 和通读大文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要分析的子目录，默认整个工作区"},
            "max_files": {"type": "integer", "description": "最多解析多少个源文件，默认 200"},
            "max_symbols": {"type": "integer", "description": "最多列出多少个符号，默认 2500"},
        },
        "required": [],
    },
    handler=_repo_map,
    risk=Risk.SAFE,
)