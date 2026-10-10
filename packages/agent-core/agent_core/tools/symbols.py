"""符号抽取后端（C3）：正则（默认）与 tree-sitter（可选增强）。

为什么要有两个后端：
    核心链路要求「零强制第三方依赖」——内网、离线、无 pip 源也必须能跑。
    因此正则抽取是**永久保留**的默认路径，tree-sitter 只在装了可选依赖
    （`pip install -e ".[precise]"`）时被探测启用，缺失或解析失败都自动回退，
    绝不因为少装一个包就让 repo_map 失效。

两个后端对外的产物结构完全一致（Symbol 列表 + import 列表），
差异只在召回与信息量：tree-sitter 版额外给出函数签名、类继承与 import 引用，
且是严格的超集（`tree-sitter ⊇ 正则`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

KIND_TYPE = "type"
KIND_FUNC = "func"

import_statement_types = frozenset({
    "import_statement",          # python / js / ts
    "import_from_statement",     # python
    "import_declaration",        # go
    "use_declaration",           # rust
})


@dataclass(frozen=True)
class Symbol:
    """一个被抽取出来的符号。

    line   —— 1 基行号，供后续 read_file 精确切片定位；
    indent —— 层级（0 起），用于渲染成符号树；
    signature / inherits —— 仅 tree-sitter 后端填充，正则为空。
    """

    name: str
    kind: str
    line: int
    indent: int = 0
    signature: str = ""
    inherits: str = ""


@dataclass
class ScanResult:
    """单个文件的抽取结果。imports 为文件级引用（仅 tree-sitter 提供）。"""

    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


# =========================================================================
# 后端一：正则（现逻辑原样保留，抽取行为不变）
# =========================================================================
# 每种语言一组 (正则, 是否为类型定义) —— 正则第 1 组是缩进，第 2 组是符号名
_RULES: dict[str, tuple[tuple[re.Pattern[str], bool], ...]] = {
    ".py": (
        (re.compile(r"^(\s*)class\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)"), False),
    ),
    ".js": (
        (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?class\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"), False),
        (re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), False),
    ),
    ".go": (
        (re.compile(r"^(\s*)type\s+(\w+)\s+(?:struct|interface)"), True),
        (re.compile(r"^(\s*)func\s+(?:\([^)]*\)\s*)?(\w+)"), False),
    ),
    ".rs": (
        (re.compile(r"^(\s*)(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), False),
    ),
    ".java": (
        (re.compile(r"^(\s*)(?:public\s+|private\s+|protected\s+|final\s+|abstract\s+|sealed\s+)*(?:class|interface|enum|record)\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:public\s+|private\s+|protected\s+|static\s+|final\s+)+\S+\s+(\w+)\s*\("), False),
    ),
    ".cs": (
        (re.compile(r"^(\s*)(?:public\s+|internal\s+|private\s+|abstract\s+|sealed\s+|static\s+)*(?:class|interface|struct|enum|record)\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:public\s+|private\s+|protected\s+|internal\s+|static\s+)+\S+\s+(\w+)\s*\("), False),
    ),
    ".rb": (
        (re.compile(r"^(\s*)(?:class|module)\s+([A-Z]\w*)"), True),
        (re.compile(r"^(\s*)def\s+(\w+[?!=]?)"), False),
    ),
    ".php": (
        (re.compile(r"^(\s*)(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)"), False),
    ),
    ".kt": (
        (re.compile(r"^(\s*)(?:public\s+|private\s+|internal\s+|open\s+|sealed\s+|data\s+)*(?:class|interface|object|enum\s+class)\s+(\w+)"), True),
        (re.compile(r"^(\s*)(?:public\s+|private\s+|internal\s+|suspend\s+)*fun\s+(\w+)"), False),
    ),
}

_EXT_ALIASES = {
    ".jsx": ".js", ".mjs": ".js", ".cjs": ".js", ".ts": ".js", ".tsx": ".js",
    ".mts": ".js", ".cts": ".js", ".vue": ".js", ".svelte": ".js",
    ".h": ".java", ".hpp": ".java", ".cc": ".java", ".cpp": ".java",
    ".c": ".java", ".hh": ".java", ".cxx": ".java",
    ".pyi": ".py",
}


def regex_rules_for(suffix: str):
    return _RULES.get(_EXT_ALIASES.get(suffix.lower(), suffix.lower()))


def _depth(indent: str) -> int:
    """把缩进折算成层级。制表符按 1 层算，空格按 4 个一层。"""
    if "\t" in indent:
        return min(indent.count("\t"), 3)
    return min(len(indent) // 4, 3)


def regex_scan(path: Path, text: str) -> ScanResult:
    """纯正则抽取：逐年逐行匹配，行为与 C3 之前完全一致。"""
    rules = regex_rules_for(path.suffix)
    if not rules:
        return ScanResult()
    symbols: list[Symbol] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        if len(line) > 400:
            continue
        for pattern, is_type in rules:
            match = pattern.match(line)
            if match:
                indent, name = match.group(1), match.group(2)
                if name.startswith("__") and name.endswith("__") and not is_type:
                    break  # 跳过 __init__ 之类噪音不大的魔法方法
                symbols.append(
                    Symbol(
                        name=name,
                        kind=KIND_TYPE if is_type else KIND_FUNC,
                        line=lineno,
                        indent=_depth(indent),
                    )
                )
                break
    return ScanResult(symbols=symbols)


# =========================================================================
# 后端二：tree-sitter（可选依赖，运行时探测，失败即回退）
# =========================================================================
# 只映射我们真正实现了 walker 的语言；其余扩展名（如 .java/.rb/.php/.kt/.cs）
# 一律回退正则。这样即使 provider 支持某语言，也不会因为缺 walker 而漏符号。
LANG_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}

# provider 探测顺序：先 language-pack（维护中），再 languages（已废弃但兼容旧环境）
_PROVIDERS = ("tree_sitter_language_pack", "tree_sitter_languages")

_PROBE_DONE = False
_GET_PARSER = None
_PARSERS: dict[str, object] = {}
_PARSER_MISSING: set[str] = set()


def _load_get_parser():
    """按顺序探测可用的 tree-sitter provider，返回 get_parser 或 None。"""
    global _PROBE_DONE, _GET_PARSER
    if _PROBE_DONE:
        return _GET_PARSER
    _PROBE_DONE = True
    for module_name in _PROVIDERS:
        try:
            module = __import__(module_name, fromlist=["get_parser"])
            getter = getattr(module, "get_parser")
        except Exception:  # noqa: BLE001 —— 缺依赖是预期路径，不该抛
            continue
        if callable(getter):
            _GET_PARSER = getter
            break
    return _GET_PARSER


def language_for(suffix: str) -> str | None:
    return LANG_BY_EXT.get(suffix.lower())


def _parser_for(lang: str):
    """按语言缓存 parser（每文件新建 parser 开销大）。不可用则记入 _PARSER_MISSING。"""
    if lang in _PARSER_MISSING:
        return None
    cached = _PARSERS.get(lang)
    if cached is not None:
        return cached
    getter = _load_get_parser()
    if getter is None:
        _PARSER_MISSING.add(lang)
        return None
    try:
        parser = getter(lang)
    except Exception:  # noqa: BLE001 —— 该语言 grammar 缺失（如 c_sharp），回退正则
        parser = None
    if parser is None:
        _PARSER_MISSING.add(lang)
        return None
    _PARSERS[lang] = parser
    return parser


def treesitter_available(lang: str) -> bool:
    return _parser_for(lang) is not None


def _line(node) -> int:
    return int(node.start_point[0]) + 1


def _txt(node) -> str:
    if node is None:
        return ""
    raw = node.text
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _clean(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _name_of(node) -> str:
    return _clean(_txt(node.child_by_field_name("name")), 60)


def _sig(node) -> str:
    params = node.child_by_field_name("parameters")
    if params is None:
        params = node.child_by_field_name("parameter")
    if params is None:
        return ""
    return _clean(_txt(params))


def _inherits(node) -> str:
    sup = node.child_by_field_name("superclasses")
    if sup is not None:
        text = _txt(sup).strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        return _clean(text, 60)
    for child in node.children:
        if child.type in ("class_heritage", "extends_clause", "implements_clause", "base_class_clause"):
            return _clean(_txt(child), 60)
    return ""


def _walk_python(node, depth: int, out: list[Symbol], imports: list[str]) -> None:
    for child in node.children:
        kind = child.type
        if kind == "class_definition":
            out.append(Symbol(_name_of(child), KIND_TYPE, _line(child), min(depth, 3),
                              inherits=_inherits(child)))
            _walk_python(child, depth + 1, out, imports)
        elif kind == "function_definition":
            out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                              signature=_sig(child)))
            _walk_python(child, depth + 1, out, imports)
        elif kind in import_statement_types:
            imports.append(_clean(_txt(child), 100))
        else:
            _walk_python(child, depth, out, imports)


def _walk_ecma(node, depth: int, out: list[Symbol], imports: list[str]) -> None:
    """javascript / typescript / tsx 共用（节点命名一致）。"""
    for child in node.children:
        kind = child.type
        if kind == "class_declaration":
            out.append(Symbol(_name_of(child), KIND_TYPE, _line(child), min(depth, 3),
                              inherits=_inherits(child)))
            _walk_ecma(child, depth + 1, out, imports)
        elif kind == "interface_declaration":  # typescript
            out.append(Symbol(_name_of(child), KIND_TYPE, _line(child), min(depth, 3)))
            _walk_ecma(child, depth + 1, out, imports)
        elif kind == "method_definition":
            out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                              signature=_sig(child)))
            _walk_ecma(child, depth + 1, out, imports)
        elif kind == "function_declaration":
            out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                              signature=_sig(child)))
            _walk_ecma(child, depth + 1, out, imports)
        elif kind == "variable_declarator":
            value = child.child_by_field_name("value")
            if value is not None and value.type in (
                "arrow_function", "function_expression", "function", "generator_function"
            ):
                out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                                  signature=_sig(value)))
                _walk_ecma(child, depth + 1, out, imports)
            else:
                _walk_ecma(child, depth, out, imports)
        elif kind == "import_statement":
            imports.append(_clean(_txt(child), 100))
        else:
            _walk_ecma(child, depth, out, imports)


def _walk_go(node, depth: int, out: list[Symbol], imports: list[str]) -> None:
    for child in node.children:
        kind = child.type
        if kind == "type_declaration":
            for spec in child.children:
                if spec.type == "type_spec":
                    out.append(Symbol(_name_of(spec), KIND_TYPE, _line(spec), min(depth, 3)))
        elif kind == "function_declaration":
            out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                              signature=_sig(child)))
            _walk_go(child, depth + 1, out, imports)
        elif kind == "method_declaration":
            out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                              signature=_sig(child)))
            _walk_go(child, depth + 1, out, imports)
        elif kind == "import_declaration":
            imports.append(_clean(_txt(child), 100))
        else:
            _walk_go(child, depth, out, imports)


def _walk_rust(node, depth: int, out: list[Symbol], imports: list[str]) -> None:
    for child in node.children:
        kind = child.type
        if kind in ("struct_item", "enum_item", "union_item"):
            out.append(Symbol(_name_of(child), KIND_TYPE, _line(child), min(depth, 3)))
        elif kind == "trait_item":
            out.append(Symbol(_name_of(child), KIND_TYPE, _line(child), min(depth, 3)))
            _walk_rust(child, depth + 1, out, imports)
        elif kind == "impl_item":
            target = _clean(_txt(child.child_by_field_name("type")), 60)
            out.append(Symbol(f"impl {target}".strip(), KIND_TYPE, _line(child), min(depth, 3)))
            _walk_rust(child, depth + 1, out, imports)
        elif kind in ("function_item", "function_signature_item"):
            out.append(Symbol(_name_of(child), KIND_FUNC, _line(child), min(depth, 3),
                              signature=_sig(child)))
            _walk_rust(child, depth + 1, out, imports)
        elif kind == "use_declaration":
            imports.append(_clean(_txt(child), 100))
        else:
            _walk_rust(child, depth, out, imports)


_WALKERS = {
    "python": _walk_python,
    "javascript": _walk_ecma,
    "typescript": _walk_ecma,
    "tsx": _walk_ecma,
    "go": _walk_go,
    "rust": _walk_rust,
}


def treesitter_scan(text: str, lang: str) -> ScanResult | None:
    """用 tree-sitter 抽取。返回 None 表示不可用（调用方回退正则）。"""
    walker = _WALKERS.get(lang)
    if walker is None:
        return None
    parser = _parser_for(lang)
    if parser is None:
        return None
    try:
        tree = parser.parse(text.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 —— 解析异常一律回退
        return None
    result = ScanResult()
    walker(tree.root_node, 0, result.symbols, result.imports)
    return result


# =========================================================================
# 统一入口：优先 tree-sitter，不可用回退正则
# =========================================================================
# 这两个函数刻意放在 symbols.py 而不是各调用点：repo_map（符号地图）、
# @symbol 引用解析、依赖图（M5-B）都要「按扩展名挑后端」，各写一遍迟早漂移。
def supports(suffix: str) -> bool:
    """该扩展名是否在解析范围内（正则或 tree-sitter 任一覆盖即可）。"""
    return bool(regex_rules_for(suffix)) or language_for(suffix) is not None


def scan_file(path: Path, text: str) -> ScanResult:
    """按扩展名选择后端抽取符号与 import。两个后端的产物结构完全一致。"""
    lang = language_for(path.suffix)
    if lang is not None:
        result = treesitter_scan(text, lang)
        if result is not None:
            return result
    return regex_scan(path, text)