"""文件级依赖图（B1）：把 import 语句还原成「谁依赖谁」的有向边。

为什么需要它：语义检索只能回答「哪些文件提到了这些词」，回答不了
「改了它还会波及谁」。命中列表 + 反向依赖（dependents）合在一起，
才勉强接近「该改哪个文件」这个问题的答案 —— 漏改调用方是重构最常见的翻车方式。

依赖来源是 `tools/symbols.py` 的 `imports[]`，**只有装了可选依赖 tree-sitter 才有**。
因此这里把「空图」当成一等公民：没有 imports 就退化成空图，
检索照常返回结果，只是少了「相关文件」那一段 —— 不失效，只降精度。
这正是「核心链路零强制依赖」对图这一层的要求。
"""

from __future__ import annotations

import bisect
import posixpath
import re
from dataclasses import dataclass, field

# 每种语言里「一行 import」的最常见形态。这里刻意只做**保守解析**：
# 认不出来的语句直接跳过，而不是猜。猜错会造出一条假边，
# 而假边会让「相关文件」给出误导性建议 —— 比少一条边糟得多。
_PY_FROM_RE = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+(.+)$", re.S)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+(.+)$", re.S)
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+(.+?);?\s*$", re.S)

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
_ALL_EXTS = _JS_EXTS + (".py", ".pyi", ".go", ".rs")

# 单条 import 最多展开成多少个目标文件。`import "pkg"` 在 Go 里指向整个目录，
# 不设限的话一个 import 就能把结果淹掉。
_PER_STATEMENT_CAP = 4

# 非相对 import 的解析根。Python 的 `import app.svc`、Rust 的 `use crate::x`
# 到底是相对仓库根还是相对 src/，取决于项目布局，无法从语句本身判断。
# 于是两个都试 —— 只有**目标文件确实存在**才建边，所以多试一个根不会造假边，
# 顶多多花几次集合查询。这是「保守解析」允许的唯一一种猜测。
_SOURCE_ROOTS = ("", "src")


class _Known:
    """已知文件集合。用排序表 + 二分支持「目录前缀」匹配。

    为什么不用 dict 遍历：Go 的 import 指向的是**目录**，要列出目录下所有文件。
    朴素做法是每次 O(文件数) 扫一遍，在依赖图上累积起来就是平方级。
    """

    def __init__(self, rels: list[str]) -> None:
        self.set = set(rels)
        self.sorted = sorted(rels)
        self._tails: dict[str, list[str]] | None = None

    def has(self, rel: str) -> bool:
        return rel in self.set

    def under(self, prefix: str, limit: int = _PER_STATEMENT_CAP) -> list[str]:
        out: list[str] = []
        index = bisect.bisect_left(self.sorted, prefix)
        while index < len(self.sorted) and self.sorted[index].startswith(prefix):
            out.append(self.sorted[index])
            index += 1
            if len(out) >= limit:
                break
        return out

    def _tail_map(self) -> dict[str, list[str]]:
        """每个目录的「尾部别名」→ 该目录（及其子目录）下的文件。

        为什么需要：Go 的 import 写的是模块路径（`github.com/x/demo/pkg/util`），
        而仓库里的目录只是它的**尾部**（`pkg/util/`）。按前缀匹配永远对不上。
        代价是每个文件产生「目录深度」个键，因此**懒构建** —— 只有真遇到
        需要后缀匹配的语句（Go）才会付这份内存。
        """
        if self._tails is None:
            tails: dict[str, list[str]] = {}
            for rel in self.sorted:
                dirname = posixpath.dirname(rel)
                if not dirname:
                    continue
                parts = dirname.split("/")
                for index in range(len(parts)):
                    tails.setdefault("/".join(parts[index:]), []).append(rel)
            self._tails = tails
        return self._tails

    def dir_suffix(self, suffix: str, limit: int = _PER_STATEMENT_CAP) -> list[str]:
        """按「目录尾部」匹配。从最长的尾部开始试，先精确后宽松。

        例：import 路径 `github.com/x/demo/pkg/util` 会依次试
        `github.com/x/demo/pkg/util` / `x/demo/pkg/util` / `demo/pkg/util` /
        `pkg/util` / `util`，命中即返回 —— 全路径匹配到的目录比只匹配末段更可信。
        """
        tails = self._tail_map()
        parts = [p for p in suffix.strip("/").split("/") if p]
        for index in range(len(parts)):
            bucket = tails.get("/".join(parts[index:]))
            if bucket:
                return bucket[:limit]
        return []


def _clean_rel(text: str) -> str:
    """归一化候选路径；返回 "" 表示这个候选项无效（越界或为空）。"""
    if not text:
        return ""
    normalized = posixpath.normpath(text)
    if normalized in ("", "."):
        return ""
    if normalized == ".." or normalized.startswith("../"):
        return ""  # 依赖到工作区外，图里不建这条边
    return normalized


def _candidate_from_spec(base_rel: str, parts: list[str]) -> str:
    joined = "/".join(parts)
    return _clean_rel(posixpath.join(base_rel, joined) if base_rel else joined)


def _py_targets(importer: str, module: str, names: list[str]) -> list[str]:
    """Python 的两种写法：绝对 `app.svc` 与相对 `.svc` / `..pkg`。"""
    base = posixpath.dirname(importer)
    leading = len(module) - len(module.lstrip("."))
    parts = [p for p in module[leading:].split(".") if p]

    if leading:
        root = base
        for _ in range(max(0, leading - 1)):
            root = posixpath.dirname(root)
        roots: tuple[str, ...] = (root,)
    else:
        roots = _SOURCE_ROOTS

    groups: list[list[str]] = []
    if parts:
        groups.append(parts)
        # `from a.b import c` 里的 c 常常是 a/b.py 里的符号，但也可能自己就是模块。
        if len(parts) > 1:
            groups.append(parts[:-1])
    else:
        # `from . import svc` —— 模块名在 import 列表里
        groups.extend([name] for name in names[:2])

    out: list[str] = []
    for root in roots:
        for group in groups:
            spec = _candidate_from_spec(root, group)
            if not spec:
                continue
            out.append(spec + ".py")
            out.append(spec + "/__init__.py")
            out.append(spec + ".pyi")
    return out


def _ecma_targets(importer: str, spec: str) -> list[str]:
    """调用方只放行 `.` / `..` / `/` 开头的引用（见 statement_targets）。

    裸模块名（"react"、"@/x"）一律不解析：前者必然在 node_modules，
    后者要读打包器配置才知道指向哪里 —— 猜错不如不猜。
    """
    base = posixpath.dirname(importer)
    if spec.startswith("."):
        bases: tuple[str, ...] = (base,)
        raw = spec
    else:
        # 以 "/" 开头：部分打包器约定为「项目根」，同样两种布局都试
        bases = _SOURCE_ROOTS
        raw = spec.lstrip("/")

    out: list[str] = []
    for prefix in bases:
        target = _clean_rel(posixpath.join(prefix, raw) if prefix else raw)
        if not target:
            continue
        if posixpath.splitext(target)[1] in _ALL_EXTS:
            out.append(target)
        out.extend(target + ext for ext in _JS_EXTS)
        out.extend(target + ext for ext in (".py", ".go", ".rs"))
        out.extend(f"{target}/index{ext}" for ext in _JS_EXTS)
    return out


def _go_targets(_importer: str, spec: str) -> list[str]:
    parts = [p for p in spec.strip("/").split("/") if p]
    if not parts:
        return []
    joined = "/".join(parts)
    out: list[str] = []
    for prefix in _SOURCE_ROOTS:
        base = posixpath.join(prefix, joined) if prefix else joined
        # Go 的 import 指向包目录；先试「同名 .go 文件」，再退到目录前缀。
        out.append(base + ".go")
        out.append(base + "/")
    out.append(parts[-1] + ".go")
    # 模块路径（`github.com/x/demo/pkg/util`）通常不等于仓库内目录，
    # 真正的包目录只是它的尾部 —— 用 "**" 前缀标记「目录后缀匹配」。
    out.append("**" + joined + "/")
    return out


def _rust_targets(importer: str, spec: str) -> list[str]:
    spec = spec.split("{")[0]
    parts = [p for p in spec.split("::") if p]
    if not parts:
        return []
    base = posixpath.dirname(importer)

    if parts[0] == "crate":
        roots: tuple[str, ...] = _SOURCE_ROOTS
        rest = parts[1:]
    elif parts[0] == "super":
        supers = 0
        while supers < len(parts) and parts[supers] == "super":
            supers += 1
        root = base
        for _ in range(max(0, supers - 1)):
            root = posixpath.dirname(root)
        roots, rest = (root,), parts[supers:]
    elif parts[0] == "self":
        roots, rest = (base,), parts[1:]
    else:
        # 外部 crate：基本解析不到本地文件，仍试一次，失败了自然丢弃
        roots, rest = _SOURCE_ROOTS, parts

    out: list[str] = []
    for root in roots:
        for candidate_parts in (rest, rest[:-1] if len(rest) > 1 else []):
            if not candidate_parts:
                continue
            spec_path = _candidate_from_spec(root, candidate_parts)
            if not spec_path:
                continue
            out.append(spec_path + ".rs")
            out.append(spec_path + "/mod.rs")
    return out


def _strip_as(token: str) -> str:
    return token.split(" as ")[0].strip().strip("()")


def statement_targets(importer: str, statement: str, suffix: str) -> list[str]:
    """把一条 import 语句展开成候选相对路径（尚未判定是否真实存在）。"""
    suffix = suffix.lower()
    if suffix in (".py", ".pyi"):
        match = _PY_FROM_RE.match(statement)
        if match:
            module, rest = match.group(1), match.group(2)
            names = [_strip_as(t) for t in rest.replace("(", "").split(",")]
            return _py_targets(importer, module, [n for n in names if n])
        match = _PY_IMPORT_RE.match(statement)
        if match:
            out: list[str] = []
            for token in match.group(1).replace("(", "").split(","):
                module = _strip_as(token)
                if module:
                    out.extend(_py_targets(importer, module, []))
            return out
        return []

    if suffix in (".rs",):
        match = _RUST_USE_RE.match(statement)
        if not match:
            # 块状 `use a::{x, y};` 的 _clean 结果是单行，这里兜一层引号兜底
            return []
        return _rust_targets(importer, match.group(1))

    if suffix == ".go":
        # `import "x"` 与 `import ( "a" \n "b" )` 都会带引号；只认引号里的内容，
        # 避免把标准库之外的东西也当成边（解析不到就会自然丢弃）。
        out: list[str] = []
        for quoted in _QUOTED_RE.findall(statement):
            out.extend(_go_targets(importer, quoted))
        return out

    # 其余（js/ts/tsx/vue…）统一按 ECMA 处理
    out = []
    for quoted in _QUOTED_RE.findall(statement):
        if quoted.startswith((".", "/")):
            out.extend(_ecma_targets(importer, quoted))
    return out


@dataclass
class DepGraph:
    """文件级有向依赖图。`edge A -> B` 表示 A 依赖（import）B。"""

    forward: dict[str, list[str]] = field(default_factory=dict)
    reverse: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, known_rels: list[str], imports_by_file: dict[str, list[str]]) -> "DepGraph":
        known = _Known(known_rels)
        forward: dict[str, list[str]] = {}
        for importer, statements in imports_by_file.items():
            suffix = posixpath.splitext(importer)[1]
            targets: list[str] = []
            for statement in statements:
                for candidate in statement_targets(importer, statement, suffix):
                    if candidate.startswith("**"):
                        hits = known.dir_suffix(candidate[2:], _PER_STATEMENT_CAP)
                    elif candidate.endswith("/"):
                        hits = known.under(candidate, _PER_STATEMENT_CAP)
                    elif known.has(candidate):
                        hits = [candidate]
                    else:
                        hits = []
                    for hit in hits:
                        if hit != importer and hit not in targets:
                            targets.append(hit)
                        if len(targets) >= _PER_STATEMENT_CAP:
                            break
                    if len(targets) >= _PER_STATEMENT_CAP:
                        break
                if len(targets) >= _PER_STATEMENT_CAP:
                    break
            if targets:
                forward[importer] = targets

        reverse: dict[str, list[str]] = {}
        for importer, targets in forward.items():
            for target in targets:
                reverse.setdefault(target, []).append(importer)

        return cls(forward=forward, reverse=reverse)

    def neighbors(self, rel: str, limit: int = 0) -> list[str]:
        """正向：这个文件 import 了谁。"""
        out = self.forward.get(rel, [])
        return out[:limit] if limit else list(out)

    def dependents(self, rel: str, limit: int = 0) -> list[str]:
        """反向：谁 import 了这个文件（改动波及面）。"""
        out = self.reverse.get(rel, [])
        return out[:limit] if limit else list(out)

    def is_empty(self) -> bool:
        return not self.forward and not self.reverse

    def edge_count(self) -> int:
        return sum(len(v) for v in self.forward.values())