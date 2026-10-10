"""Repo Wiki 生成（M3+ / C1）：把仓库结构渲染成可长期保存的 markdown。

为什么单独成篇而不复用 `repo_map`：两者面向的读者不同 —— `repo_map` 是给**模型**
看的符号地图（进上下文、追求省 token），Wiki 是给**人**看的文档（落盘、追求可长期
保存与可审阅）。因此这里不追求最小化，而是把「总览 + 模块页 + 关键符号 + 依赖关系」
组织成一份能直接读的文档。

三条硬边界（见 docs/M3+-PLAN.md）：
    1. **纯本机**：只读工作区磁盘、只产出 markdown，不碰任何模型端点（正文不出机）；
    2. **零依赖**：只用 stdlib 与既有 `symbols` / `depgraph`，不引入新依赖；
    3. **可截断**：受 `max_files` 上限，超出**显式标注**，绝不静默丢。

依赖来源：文件清单与依赖图取自进程内索引（`SearchIndex.files` / `graph`），
只有**符号**需要重新扫一遍（索引不保留符号）。这样昂贵的部分（建图）不重复做。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..index.depgraph import DepGraph
from ..tools.symbols import KIND_FUNC, KIND_TYPE, Symbol, scan_file, supports

# 顶层目录之外的文件（工作区根下的散文件）归到这个名字的「模块」。
_ROOT_MODULE = "(root)"
# 目录树最多渲染多少行，超出截断（完整清单在模块页里）。
_TREE_LIMIT = 200


@dataclass(frozen=True)
class WikiPage:
    """一页 Wiki。`name` 是相对**落点目录**的文件名（由工具与 target 拼成最终路径）。"""

    name: str
    content: str


@dataclass
class WikiBundle:
    """一次生成的完整产物（纯内存，尚未落盘）。"""

    pages: list[WikiPage] = field(default_factory=list)
    files: int = 0        # 纳入本次生成的文件数
    symbols: int = 0
    edges: int = 0
    modules: int = 0
    total_files: int = 0  # 未截断时应有几个文件（用于截断标注）
    truncated: bool = False

    @property
    def bytes(self) -> int:
        return sum(len(page.content.encode("utf-8")) for page in self.pages)


def module_of(rel: str) -> str:
    """文件的归属模块 = 顶层目录名；根目录下的散文件归到 `(root)`。"""
    head = posixpath.dirname(rel)
    return head.split("/")[0] if head else _ROOT_MODULE


def _safe_name(name: str) -> str:
    """把模块名收敛成安全的文件名片段（模块名来自目录名，理论上可能含怪字符）。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("._")
    return cleaned or "module"


def _render_symbol(symbol: Symbol) -> str:
    marker = "◆" if symbol.kind == KIND_TYPE else "·"
    detail = ""
    if symbol.kind == KIND_TYPE and symbol.inherits:
        detail = f" ({symbol.inherits})"
    elif symbol.kind == KIND_FUNC and symbol.signature:
        detail = f"({symbol.signature})"
    indent = "  " * min(symbol.indent, 3)
    return f"{indent}- {marker} `{symbol.name}`{detail}  L{symbol.line}"


def _read_scan(path: Path) -> tuple[list[Symbol], int] | None:
    """读源码并抽符号；非源码 / 读不了返回 None。返回 (符号, 行数)。"""
    if not supports(path.suffix):
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return scan_file(path, text).symbols, len(text.split("\n"))


def _render_tree(rels: Sequence[str]) -> str:
    """把文件清单渲染成缩进目录树。目录在前、文件在后，行数超限即截断。"""
    tree: dict = {}
    for rel in rels:
        node = tree
        parts = rel.split("/")
        for part in parts[:-1]:
            node = node.setdefault(f"{part}/", {})
        node[parts[-1]] = None

    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        if len(lines) >= _TREE_LIMIT:
            return
        for name in sorted(node, key=lambda item: (node[item] is None, item.lower())):
            if len(lines) >= _TREE_LIMIT:
                return
            lines.append("  " * depth + name)
            child = node[name]
            if child is not None:
                walk(child, depth + 1)

    walk(tree, 0)
    if len(lines) >= _TREE_LIMIT:
        lines.append("  ... （目录树过长已截断，完整清单见各模块页）")
    return "\n".join(lines)


def _render_readme(
    bundle_modules: list[tuple[str, str, int]],
    *,
    files: int,
    symbols: int,
    edges: int,
    tree: str,
    truncated: bool,
    total_files: int,
    max_files: int,
) -> str:
    lines = [
        "# 仓库 Wiki",
        "",
        "> 由 `repo_wiki` 工具基于**本机代码结构**（目录 / 符号 / 依赖边）生成，"
        "不含任何模型解说，也不外发任何代码。",
        "",
        "## 统计",
        "",
        f"- 源文件：{files}",
        f"- 符号：{symbols}",
        f"- 依赖边：{edges}",
        f"- 模块：{len(bundle_modules)}",
    ]
    if truncated:
        lines.append("")
        lines.append(
            f"> ⚠ 本次仅覆盖前 {max_files} 个文件（共 {total_files} 个），其余已截断。"
            "如需完整覆盖请调大 `VIBE_WIKI_MAX_FILES`。"
        )
    lines += ["", "## 模块", ""]
    for module, name, count in bundle_modules:
        lines.append(f"- [{module}]({name}) —— {count} 个文件")
    lines += ["", "## 目录", "", "```text", tree or "（空）", "```", ""]
    return "\n".join(lines)


def _render_module(
    module: str,
    rels: Sequence[str],
    scans: dict[str, tuple[list[Symbol], int]],
    graph: DepGraph,
) -> str:
    lines = [
        f"# 模块 `{module}`",
        "",
        "[← 返回总览](README.md)",
        "",
        f"共 {len(rels)} 个文件。",
        "",
    ]
    for rel in rels:
        lines.append(f"### `{rel}`")
        scan = scans.get(rel)
        if scan is None:
            lines.append("- （非源码或无法解析，未提取符号）")
        else:
            symbols, line_count = scan
            lines.append(f"- 行数：{line_count}")
            if symbols:
                lines.append("- 符号：")
                lines.extend("  " + _render_symbol(symbol) for symbol in symbols)
            else:
                lines.append("- 符号：（未识别到）")
        dependents = graph.dependents(rel)
        neighbors = graph.neighbors(rel)
        if dependents:
            lines.append("- 被依赖（改动波及面）：" + "、".join(f"`{x}`" for x in dependents))
        if neighbors:
            lines.append("- 依赖：" + "、".join(f"`{x}`" for x in neighbors))
        lines.append("")
    return "\n".join(lines)


def generate_wiki(
    root: Path,
    rels: Sequence[str],
    graph: DepGraph,
    *,
    max_files: int = 400,
) -> WikiBundle:
    """生成 Wiki 产物（不落盘）。

    `rels` 是候选文件的相对工作区路径（通常来自 `SearchIndex.files`）。
    只取前 `max_files` 个（排序后，保证可复现），超出部分**显式标注**为截断。
    """
    root = Path(root)
    candidates = sorted({rel for rel in rels if rel})
    max_files = max(1, int(max_files))
    truncated = len(candidates) > max_files
    selected = candidates[:max_files]

    modules: dict[str, list[str]] = {}
    for rel in selected:
        modules.setdefault(module_of(rel), []).append(rel)

    scans: dict[str, tuple[list[Symbol], int]] = {}
    symbol_total = 0
    for rel in selected:
        scan = _read_scan(root / rel)
        if scan is not None:
            scans[rel] = scan
            symbol_total += len(scan[0])

    ordered_modules = sorted(modules)
    page_name = {
        module: f"{index:02d}-{_safe_name(module)}.md"
        for index, module in enumerate(ordered_modules, start=1)
    }

    pages: list[WikiPage] = []
    module_rows: list[tuple[str, str, int]] = []
    for module in ordered_modules:
        rels_in_module = sorted(modules[module])
        module_rows.append((module, page_name[module], len(rels_in_module)))
        pages.append(
            WikiPage(
                name=page_name[module],
                content=_render_module(module, rels_in_module, scans, graph),
            )
        )

    readme = _render_readme(
        module_rows,
        files=len(selected),
        symbols=symbol_total,
        edges=graph.edge_count(),
        tree=_render_tree(selected),
        truncated=truncated,
        total_files=len(candidates),
        max_files=max_files,
    )
    pages.insert(0, WikiPage(name="README.md", content=readme))

    return WikiBundle(
        pages=pages,
        files=len(selected),
        symbols=symbol_total,
        edges=graph.edge_count(),
        modules=len(ordered_modules),
        total_files=len(candidates),
        truncated=truncated,
    )