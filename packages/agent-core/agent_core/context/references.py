"""上下文引用：把对话框里的 `@file` / `@symbol` / `@folder` 解析成模型上下文。

## 为什么解析放后端

前端只传「引用意图」（type + value），正文由后端经 `PathGuard` 读取。这样：

1. 路径校验只有**一处** —— 前端拿不到越权路径，也不可能把狱外文件塞进上下文；
2. CLI 与桌面端共用同一解析器，行为不会漂移；
3. 前端不必把文件正文搬来搬去，代码不出本机这条红线不受影响。

## 四种引用

    file   —— 读该文件正文（复用 read_file 的 `---8<---` 包裹与二进制判断）
    folder —— 列该目录一层（跳过 .git / node_modules 等忽略目录）
    symbol —— 在工作区里定位定义行，读该行前后 ±SYMBOL_CONTEXT_LINES 行
    image  —— 读一张图片，产出一个 OpenAI 兼容的 image part（N / P1-10）。
              它不是文本正文，**不进**下方拼接的提示块，而是作为 parts 随
              user 消息一并发往模型；且默认关闭（见 `allow_images`）。

## 预算与降级

所有引用展开后的正文总量不超过 `MAX_REF_CHARS`：一个 `@folder` 就能带上几万字符，
不设上限会把模型上下文挤爆 —— 超限**按序截断**并置 `truncated`。
解析失败（越权、敏感文件、符号找不到）**不阻断整条任务**，降级为一行提示，
由模型自行决定是否用 grep/find_files 重新定位。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AgentError, SandboxViolation, ToolError
from ..sandbox import PathGuard
from ..tools.base import DEFAULT_IGNORED_DIRS, MAX_SCAN_BYTES, wrap_untrusted
from ..tools.fs import _FILE_FOOTER, _FILE_HEADER, _is_probably_binary, render_dir_listing
from ..tools.search import _iter_files
from ..tools.symbols import KIND_TYPE, Symbol, scan_file, supports
from .images import DEFAULT_MAX_IMAGE_BYTES, load_image

#: 所有引用展开后的正文总量上限（字符）。
MAX_REF_CHARS = 24_000
#: @symbol 命中定义行后，向前后各读多少行作为上下文。
SYMBOL_CONTEXT_LINES = 30
#: @symbol 在工作区内的扫描文件数上限（防止在巨型仓库里遍历太久）。
MAX_SYMBOL_SCAN_FILES = 400
#: /api/refs 候选列表的硬上限。
MAX_REF_ITEMS = 50

# 引用类型。`image`（N / P1-10）与其它三种的**产物形态不同**：它不产出文本块，
# 而是产出一个 OpenAI 兼容的 image part，随消息 content 的 parts 列表一起发。
_TYPES = ("file", "folder", "symbol", "image")


@dataclass(frozen=True)
class ResolvedRef:
    """一条成功解析的引用。`body` 是展开后的正文（已含包裹标记）。"""

    type: str
    value: str
    label: str
    body: str
    truncated: bool = False


@dataclass
class RefResolution:
    """一次引用的解析结果。

    `refs` 为成功项，`errors` 为逐条降级提示 —— 两者可以同时非空：
    用户引用了三项，其中一项越权，另两项照常展开。
    `images`（N）是与 `refs` 并列的**另一类产物**：图片不是文本正文，不进
    `format_refs_block` 拼出的提示块，而是作为 parts 直接随消息发往模型。
    """

    refs: list[ResolvedRef] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def chars(self) -> int:
        return sum(len(ref.body) for ref in self.refs)

    @property
    def ok(self) -> bool:
        return bool(self.refs or self.images)


# --------------------------------------------------------------------------- 扫描
def iter_workspace_files(paths: PathGuard):
    """遍历工作区文件，产出**相对路径**。供 `@file` 候选列表使用。

    与 `iter_workspace_symbols` 共用同一套口径（跳过忽略目录、二进制与敏感文件），
    保证「候选里能看到」与「引用时能读到」是同一个集合。
    """
    for path in _iter_files(paths.root_resolved):
        try:
            checked = paths.check_read(path)
        except AgentError:
            continue
        yield paths.relative(checked)


def iter_workspace_dirs(paths: PathGuard, *, max_dirs: int = 3000):
    """遍历工作区目录，产出**相对路径**。供 `@folder` 候选列表使用。"""
    stack = [paths.root_resolved]
    found = 0
    while stack and found < max_dirs:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        children: list[Path] = []
        for entry in entries:
            if entry.name in DEFAULT_IGNORED_DIRS or entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    children.append(Path(entry.path))
            except OSError:
                continue
        children.sort()
        for child in children:
            yield paths.relative(child)
            found += 1
            if found >= max_dirs:
                return
        stack.extend(reversed(children))


def iter_workspace_symbols(
    paths: PathGuard, *, max_files: int = MAX_SYMBOL_SCAN_FILES
):
    """遍历工作区，产出 `(相对路径, Symbol)`。

    只读、走 PathGuard：越权与敏感文件在 `check_read` 处被跳过（不抛）。
    供「@symbol 引用解析」与「/api/refs 候选列表」共用，保证两边看到同一批符号。
    """
    remaining = max(0, int(max_files))
    for path in _iter_files(paths.root_resolved):
        if remaining <= 0:
            return
        if not supports(path.suffix):
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            checked = paths.check_read(path)
        except (OSError, AgentError):
            continue
        try:
            text = checked.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining -= 1
        result = scan_file(checked, text)
        rel = paths.relative(checked)
        for symbol in result.symbols:
            yield rel, symbol


# --------------------------------------------------------------------------- 单条
def _render_file(paths: PathGuard, value: str) -> tuple[str, str]:
    path = paths.check_read(value)
    if path.is_dir():
        raise ToolError(f"{paths.relative(path)} 是目录，引用目录请用 @folder")
    if not path.exists():
        raise ToolError(f"文件不存在：{paths.relative(path)}")
    if _is_probably_binary(path):
        raise ToolError(f"{paths.relative(path)} 看起来是二进制文件，无法作为文本引用")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > MAX_SCAN_BYTES:
        raise ToolError(
            f"{paths.relative(path)} 过大（{size} 字节，上限 {MAX_SCAN_BYTES}），"
            "请改用 read_file 分段读取"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"读取失败：{exc}") from exc

    rel = paths.relative(path)
    body = "\n".join([_FILE_HEADER, text, _FILE_FOOTER])
    return rel, body


def _render_folder(paths: PathGuard, value: str) -> tuple[str, str]:
    root = paths.check_list(value or ".")
    if root.is_file():
        raise ToolError(f"{paths.relative(root)} 是文件，引用文件请用 @file")
    if not root.exists():
        raise ToolError(f"目录不存在：{paths.relative(root)}")
    listing, _count = render_dir_listing(paths, root, depth=1)
    return paths.relative(root) or ".", listing


def _find_symbol(paths: PathGuard, value: str, name: str) -> tuple[str, Symbol] | None:
    """在指定文件或整个工作区里找到第一个匹配的定义。

    匹配优先级：精确 → 忽略大小写 → 子串。子串兜底是为了让
    「记不太清全名」的场景也能引用上，代价是可能命中更泛的符号。
    """
    if value:
        path = paths.check_read(value)
        if not path.is_file():
            raise ToolError(f"文件不存在：{paths.relative(path)}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc
        symbol = _match_symbol(scan_file(path, text).symbols, name)
        return (paths.relative(path), symbol) if symbol is not None else None

    candidates = list(iter_workspace_symbols(paths))
    exact = [(rel, sym) for rel, sym in candidates if sym.name == name]
    if exact:
        return exact[0]
    lowered = name.lower()
    folded = [(rel, sym) for rel, sym in candidates if sym.name.lower() == lowered]
    if folded:
        return folded[0]
    partial = [(rel, sym) for rel, sym in candidates if lowered in sym.name.lower()]
    return partial[0] if partial else None


def _match_symbol(symbols: list[Symbol], name: str) -> Symbol | None:
    for symbol in symbols:
        if symbol.name == name:
            return symbol
    lowered = name.lower()
    for symbol in symbols:
        if symbol.name.lower() == lowered:
            return symbol
    for symbol in symbols:
        if lowered in symbol.name.lower():
            return symbol
    return None


def _render_symbol(paths: PathGuard, value: str) -> tuple[str, str]:
    name = value
    path_part = ""
    if "::" in value:
        path_part, _, name = value.partition("::")
        name = name.strip()
    name = name.strip()
    if not name:
        raise ToolError("符号名为空，格式形如 ClassName 或 src/app.py::ClassName")

    found = _find_symbol(paths, path_part.strip(), name)
    if found is None:
        scope = path_part.strip() or "整个工作区"
        raise ToolError(f"在 {scope} 未找到符号 {name!r}（可先用 grep / repo_map 定位）")

    rel, symbol = found
    path = paths.check_read(rel)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as exc:
        raise ToolError(f"读取失败：{exc}") from exc

    total = len(lines)
    start = max(1, symbol.line - SYMBOL_CONTEXT_LINES)
    end = min(total, symbol.line + SYMBOL_CONTEXT_LINES)
    marker = "◆ 类型" if symbol.kind == KIND_TYPE else "· 函数"
    head = (
        f"[符号 {marker}] {symbol.name}  定义于 {rel}:{symbol.line}"
        f"（本次给出第 {start}-{end} 行，共 {total} 行）"
    )
    body = "\n".join(
        [head, _FILE_HEADER, "\n".join(lines[start - 1 : end]), _FILE_FOOTER]
    )
    return f"{rel}:{symbol.line} {symbol.name}", body


def _render_ref(paths: PathGuard, rtype: str, value: str) -> tuple[str, str]:
    if rtype == "file":
        return _render_file(paths, value)
    if rtype == "folder":
        return _render_folder(paths, value)
    return _render_symbol(paths, value)


# --------------------------------------------------------------------------- 批量
def resolve_refs(
    paths: PathGuard,
    refs: list[dict] | None,
    *,
    max_chars: int = MAX_REF_CHARS,
    allow_images: bool = False,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> RefResolution:
    """把引用意图列表解析成上下文正文（与图片 parts）。永不抛异常。

    `allow_images`（N / P1-10）默认 **False**：图片是会外发的能力，未显式开启
    （`vision_mode=off`）时 `image` 引用逐条降级为提示，绝不进 parts。
    """
    result = RefResolution()
    remaining = max(0, int(max_chars))
    seen: set[tuple[str, str]] = set()

    for raw in refs or []:
        item = raw if isinstance(raw, dict) else {}
        rtype = str(item.get("type") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if not rtype or not value:
            result.errors.append("引用缺少 type 或 value，已跳过")
            continue
        if rtype not in _TYPES:
            result.errors.append(
                f"不支持的引用类型 {rtype!r}（仅 {'/'.join(_TYPES)}），已跳过"
            )
            continue
        key = (rtype, value)
        if key in seen:
            continue
        seen.add(key)

        # 图片与文本引用走两条不同的产物路径，且**不占**字符预算 ——
        # 一张图的 base64 有几十万字符，按文本口径算会把预算瞬间吃光。
        if rtype == "image":
            if not allow_images:
                result.errors.append(
                    f"image:{value} 已跳过 —— 图片输入未开启（VIBE_VISION=off）；"
                    "如需据图改代码请显式开启后再试"
                )
                continue
            try:
                part = load_image(paths, value, max_bytes=max_image_bytes)
            except SandboxViolation as exc:
                result.errors.append(f"image:{value} 被拒绝 —— {exc}")
            except AgentError as exc:
                result.errors.append(f"image:{value} 解析失败 —— {exc}")
            except Exception as exc:  # noqa: BLE001 —— 单张图片出错不能弄死整条任务
                result.errors.append(
                    f"image:{value} 解析失败 —— {type(exc).__name__}: {exc}"
                )
            else:
                result.images.append(part)
            continue

        if remaining <= 0:
            result.truncated = True
            result.errors.append(
                f"引用上下文预算（{max_chars} 字符）已用尽，未展开 {rtype}:{value}"
            )
            continue

        try:
            label, body = _render_ref(paths, rtype, value)
        except SandboxViolation as exc:
            result.errors.append(f"{rtype}:{value} 被拒绝 —— {exc}")
            continue
        except AgentError as exc:
            result.errors.append(f"{rtype}:{value} 解析失败 —— {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 —— 单条引用出错绝不能弄死整条任务
            result.errors.append(
                f"{rtype}:{value} 解析失败 —— {type(exc).__name__}: {exc}"
            )
            continue

        truncated = False
        if len(body) > remaining:
            body = body[:remaining]
            truncated = True
            result.truncated = True
        remaining -= len(body)
        result.refs.append(
            ResolvedRef(type=rtype, value=value, label=label, body=body, truncated=truncated)
        )

    return result


def format_refs_block(resolution: RefResolution) -> str:
    """把解析结果渲染成一段注入模型上下文的「引用上下文」块。

    整块用 `wrap_untrusted` 包裹：引用正文来自工作区文件，属不可信内容，
    不能被当成指令执行。

    **没有任何成功引用时返回空串** —— 引用失败已经通过 `run.notice` 告诉用户，
    再把一段只有报错、没有正文的块塞进提示，只会白占上下文。
    这也让「没有可注入内容 ⇒ prompt 原样不变」成为一条可断言的不变式。
    """
    if not resolution.refs:
        return ""

    parts = [
        f"[引用上下文] 用户在本次消息中显式引用了 {len(resolution.refs)} 项内容，"
        "以下正文供本轮任务参考。",
    ]
    for index, ref in enumerate(resolution.refs, start=1):
        head = f"--- 引用 {index}/{len(resolution.refs)} [{ref.type}] {ref.label}"
        if ref.truncated:
            head += "（超出预算已截断）"
        parts.append(head)
        parts.append(ref.body)

    if resolution.errors:
        parts.append("--- 未能解析的引用（如需使用请自行用 grep / find_files 重新定位）：")
        parts.extend(f"  · {line}" for line in resolution.errors)

    parts.append("（以上引用由用户显式指定，是本轮任务的上下文重点。）")
    return wrap_untrusted("references", "\n".join(parts))


def build_refs_prompt(
    paths: PathGuard,
    text: str,
    refs: list[dict] | None,
    *,
    allow_images: bool = False,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> tuple[str, RefResolution]:
    """把用户正文与引用块拼成真正提交给 Agent 的 prompt。

    返回 `(prompt, resolution)`；无**文本**引用时 prompt 与 text **逐字节相同** ——
    这是「不传 refs 时行为一行不变」的保证。图片（resolution.images）不进 prompt
    文本，而是由调用方作为 parts 随消息附带；因此「只带图片、不带文本引用」时
    prompt 里不会多出任何注入块。
    """
    resolution = resolve_refs(
        paths, refs, allow_images=allow_images, max_image_bytes=max_image_bytes
    )
    block = format_refs_block(resolution)
    if not block:
        return text, resolution
    return f"{block}\n\n{text}", resolution


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