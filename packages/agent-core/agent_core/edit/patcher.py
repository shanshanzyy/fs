"""结构化补丁引擎 —— 本产品的效果上限所在。

对应 docs/ARCHITECTURE.md 的 ADR-004：**编辑必须走结构化 patch，禁止整文件重写。**
理由：整文件重写会丢掉模型没提及的改动、容易引入幻觉代码、Diff 不可读、无法回滚。

本模块解决三个工程难题：

1. **匹配容错**。小模型输出的 search 块经常有细微偏差（行尾空格、缩进差一两格，
   或外层多包了一层缩进）。这里用三级策略逐级放宽：
       exact      —— 逐字节完全一致（最可靠）
       normalized —— 忽略每行行尾空白
       flexible   —— 忽略整体缩进差异，并把替换内容自动缩进对齐到目标位置
   每次命中都会在结果里回报用的是哪一级，便于观察模型质量。

2. **文件保真**。改一个文件不能顺手把 CRLF 换成 LF、把 BOM 抹掉、
   或把末尾换行吞掉。TextDoc 负责编码 / 换行风格 / BOM / 末尾换行的往返保真。

3. **原子落盘**。先写同目录临时文件再 os.replace，避免写一半崩溃导致文件损坏。
"""

from __future__ import annotations

import codecs
import difflib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..errors import PatchError

MAX_MATCHES = 200


# ===========================================================================
# 文件保真层
# ===========================================================================
@dataclass
class TextDoc:
    """一个文本文件的全部格式信息 + 归一化后的正文（统一 \\n）。

    text 始终是 \\n 归一化后的内容，所有匹配与替换都在这个域里做；
    render() 负责还原成磁盘上的原始风格。
    """

    text: str
    encoding: str = "utf-8"
    newline: str = "\n"
    had_bom: bool = False
    trailing_newline: bool = True

    @classmethod
    def load(cls, path: Path) -> "TextDoc":
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PatchError(f"读取文件失败: {path} ({exc})") from exc

        had_bom = data.startswith(codecs.BOM_UTF8)
        if had_bom:
            data = data[len(codecs.BOM_UTF8) :]

        encoding = "utf-8"
        text: str | None = None
        for candidate in ("utf-8", "gbk", "latin-1"):
            try:
                text = data.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if text is None:  # 理论上到不了这里（latin-1 不会失败）
            raise PatchError(f"文件无法解码为文本: {path}")

        crlf = text.count("\r\n")
        lf_total = text.count("\n")
        newline = "\r\n" if crlf > (lf_total - crlf) else "\n"
        trailing = text.endswith("\n") or text.endswith("\r")

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return cls(
            text=normalized,
            encoding=encoding,
            newline=newline,
            had_bom=had_bom,
            trailing_newline=trailing,
        )

    @classmethod
    def from_text(cls, text: str, newline: str = "\n") -> "TextDoc":
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return cls(text=normalized, newline=newline, trailing_newline=text.endswith("\n"))

    def render(self, text: str) -> bytes:
        """把归一化文本还原为磁盘字节。"""
        out = text
        if self.trailing_newline and not out.endswith("\n"):
            out += "\n"
        elif not self.trailing_newline and out.endswith("\n"):
            out = out[:-1]
        if self.newline != "\n":
            out = out.replace("\n", self.newline)
        try:
            data = out.encode(self.encoding)
        except UnicodeEncodeError as exc:
            raise PatchError(
                f"改写后的内容无法用原编码 {self.encoding} 保存：{exc}"
            ) from exc
        return (codecs.BOM_UTF8 + data) if self.had_bom else data


# ===========================================================================
# 补丁计划
# ===========================================================================
@dataclass
class PatchPlan:
    """一个待执行的改动。plan 阶段只计算，commit 阶段才落盘。"""

    path: Path
    rel_path: str
    old_text: str
    new_text: str
    diff: str
    strategy: str
    added: int
    removed: int
    is_create: bool = False
    doc: TextDoc | None = field(default=None, repr=False)

    @property
    def changed(self) -> bool:
        return self.old_text != self.new_text

    def summary(self) -> str:
        verb = "新建" if self.is_create else "修改"
        return (
            f"{verb} {self.rel_path} "
            f"(+{self.added} / -{self.removed}, 匹配策略: {self.strategy})"
        )


# ===========================================================================
# 匹配策略
# ===========================================================================
@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    replace_text: str


def _find_all_plain(haystack: str, needle: str, limit: int = MAX_MATCHES) -> list[int]:
    """朴素全量查找（不用正则，避免 needle 里的特殊字符引发意外）。"""
    if not needle:
        return []
    hits: list[int] = []
    cursor = 0
    while len(hits) < limit:
        idx = haystack.find(needle, cursor)
        if idx < 0:
            break
        hits.append(idx)
        cursor = idx + 1
    return hits


def _line_offsets(text: str) -> tuple[list[str], list[int]]:
    """返回 (lines, prefix)，其中 text[prefix[i]:prefix[i+1]-1] == lines[i]。"""
    lines = text.split("\n")
    prefix = [0]
    for line in lines:
        prefix.append(prefix[-1] + len(line) + 1)
    return lines, prefix


def _slice(lines: list[str], prefix: list[int], start: int, count: int, total: int) -> tuple[int, int]:
    lo = prefix[start]
    hi = min(prefix[start + count] - 1, total)
    return lo, max(lo, hi)


def _strip_edge_blanks(lines: list[str]) -> tuple[list[str], int, int]:
    """去掉首尾空行，返回 (核心行, 前导空行数, 尾部空行数)。"""
    lo = 0
    hi = len(lines)
    while lo < hi and not lines[lo].strip():
        lo += 1
    while hi > lo and not lines[hi - 1].strip():
        hi -= 1
    return lines[lo:hi], lo, len(lines) - hi


def _leading_ws(line: str) -> str:
    stripped = line.lstrip()
    return line[: len(line) - len(stripped)]


def _block_indent(lines: Iterable[str]) -> str:
    """一组行的共同缩进前缀（忽略空行）。

    必须按「当前比较的那几行」来算，不能用整个文件的缩进 ——
    否则文件里存在顶层代码时公共缩进恒为空串，相对缩进信息就丢了。
    """
    indents = [_leading_ws(line) for line in lines if line.strip()]
    if not indents:
        return ""
    candidate = min(indents, key=len)
    return candidate if all(i.startswith(candidate) for i in indents) else ""


def _dedent_line(line: str, base: str) -> str:
    if not line.strip():
        return ""
    body = line[len(base) :] if base and line.startswith(base) else line
    return body.rstrip()


def _shift_indent(text: str, delta: int) -> str:
    """整体平移缩进：delta > 0 加空格，delta < 0 去前缀。"""
    if delta == 0:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            out.append("")
            continue
        if delta > 0:
            out.append(" " * delta + line)
        else:
            remove = -delta
            prefix = line[:remove]
            out.append(line[remove:] if prefix.strip() == "" else line)
    return "\n".join(out)


def _match_exact(haystack: str, needle: str, replace: str) -> list[_Hit]:
    return [
        _Hit(idx, idx + len(needle), replace)
        for idx in _find_all_plain(haystack, needle)
    ]


def _match_normalized(haystack: str, needle: str, replace: str) -> list[_Hit]:
    """忽略行尾空白与首尾空行的匹配。"""
    hay_lines, prefix = _line_offsets(haystack)
    needle_core, _, _ = _strip_edge_blanks(needle.split("\n"))
    if not needle_core:
        return []

    hay_norm = [line.rstrip() for line in hay_lines]
    needle_norm = [line.rstrip() for line in needle_core]
    count = len(needle_norm)
    total = len(haystack)

    hits: list[_Hit] = []
    for start in range(0, len(hay_lines) - count + 1):
        if hay_norm[start : start + count] != needle_norm:
            continue
        lo, hi = _slice(hay_lines, prefix, start, count, total)
        hits.append(_Hit(lo, hi, replace))
        if len(hits) >= MAX_MATCHES:
            break
    return hits


def _match_flexible(haystack: str, needle: str, replace: str) -> list[_Hit]:
    """忽略整体缩进差异的匹配，并把替换内容缩进对齐到命中位置。

    对本地小模型极其有效 —— 它们经常把整块代码多缩进或少缩进两格。
    做法：对每个等长候选窗口单独算其公共缩进，再与 needle 去缩进后的形式比较；
    命中后把 replace 平移 len(窗口缩进) - len(needle 缩进) 个空格。
    """
    hay_lines, prefix = _line_offsets(haystack)
    needle_core, _, _ = _strip_edge_blanks(needle.split("\n"))
    if not needle_core:
        return []

    needle_base = _block_indent(needle_core)
    needle_norm = [_dedent_line(line, needle_base) for line in needle_core]
    if not any(needle_norm):
        return []

    count = len(needle_norm)
    if count > len(hay_lines):
        return []
    total = len(haystack)

    hits: list[_Hit] = []
    for start in range(0, len(hay_lines) - count + 1):
        window = hay_lines[start : start + count]
        base = _block_indent(window)
        if [_dedent_line(line, base) for line in window] != needle_norm:
            continue
        lo, hi = _slice(hay_lines, prefix, start, count, total)
        shifted = _shift_indent(replace, len(base) - len(needle_base))
        hits.append(_Hit(lo, hi, shifted))
        if len(hits) >= MAX_MATCHES:
            break
    return hits


_STRATEGIES: tuple[tuple[str, object], ...] = (
    ("exact", _match_exact),
    ("normalized", _match_normalized),
    ("flexible", _match_flexible),
)


def _locate(haystack: str, needle: str, replace: str) -> tuple[str, list[_Hit]]:
    """按可靠度顺序尝试各级策略，返回第一个命中的策略名及其全部匹配。"""
    for name, finder in _STRATEGIES:
        hits = finder(haystack, needle, replace)  # type: ignore[operator]
        if hits:
            return name, hits
    return "", []


# ===========================================================================
# 失败诊断
# ===========================================================================
def _line_number_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _diagnose(haystack: str, needle: str) -> str:
    """匹配失败时，给出能直接指导下一步排查的信息。"""
    lines, _ = _line_offsets(haystack)
    needle_core, _, _ = _strip_edge_blanks(needle.split("\n"))
    if not needle_core:
        return "search 内容为空。"

    first = needle_core[0].strip()
    best_line, best_score = 0, 0.0
    for idx, line in enumerate(lines):
        score = difflib.SequenceMatcher(None, first, line.strip()).ratio()
        if score > best_score:
            best_line, best_score = idx, score

    parts = [
        "search 片段在当前文件中找不到。",
        f"  文件共 {len(lines)} 行。",
    ]
    if best_score >= 0.5:
        parts.append(
            f"  第 {best_line + 1} 行最接近你的 search 首行"
            f"（相似度 {best_score:.0%}）：{lines[best_line].rstrip()[:120]}"
        )
    parts.append("  search 首行：" + first[:120])
    parts.append(
        "  建议：先用 read_file 重新读取该文件确认当前内容，"
        "并确保 search 片段原样复制自文件（不要凭记忆改写）。"
    )
    return "\n".join(parts)


def _ambiguity_message(hits: list[_Hit], haystack: str) -> str:
    positions = ", ".join(str(_line_number_of(haystack, h.start)) for h in hits[:8])
    more = f" 等 {len(hits)} 处" if len(hits) > 8 else ""
    return (
        f"search 片段在文件中命中多处（第 {positions} 行{more}），无法确定改哪一处。\n"
        "  建议：在 search 片段里补充前后各 2-3 行上下文使其唯一；"
        "若确实要全部替换，请显式设置 replace_all=true。"
    )


def _diff(old: str, new: str, rel_path: str) -> tuple[str, int, int]:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}",
            lineterm="", n=3,
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(diff_lines), added, removed


# ===========================================================================
# 对外接口
# ===========================================================================
def plan_patch(
    path: Path,
    search: str,
    replace: str,
    *,
    rel_path: str | None = None,
    replace_all: bool = False,
    create_if_missing: bool = False,
) -> PatchPlan:
    """计算一个改动计划，**不落盘**。供审批环节先展示 diff。"""
    rel = rel_path or path.name

    if not search.strip() and not replace.strip():
        raise PatchError("search 与 replace 都为空，没有可执行的操作")

    if not path.exists():
        if create_if_missing and not search.strip():
            return plan_create(path, replace, rel_path=rel)
        raise PatchError(
            f"文件不存在: {rel}。若确实要新建文件，请用 write_file 工具；"
            "本工具只改已有文件。"
        )
    if path.is_dir():
        raise PatchError(f"目标是一个目录而非文件: {rel}")

    if not search.strip():
        raise PatchError(
            f"search 片段为空。修改已有文件必须提供 search 片段定位改动位置（{rel}）。\n"
            "  整文件覆盖请显式使用 write_file，不要用 edit_file 变相实现。"
        )

    doc = TextDoc.load(path)
    if search == replace:
        raise PatchError("search 与 replace 完全相同，改动无意义，已跳过")

    strategy, hits = _locate(doc.text, search, replace)
    if not hits:
        raise PatchError(f"{rel}: " + _diagnose(doc.text, search))
    if len(hits) > 1 and not replace_all:
        raise PatchError(f"{rel}: " + _ambiguity_message(hits, doc.text))

    selected = hits if replace_all else hits[:1]
    # 从后往前替换，避免前面的替换影响后面的偏移
    new_text = doc.text
    for hit in sorted(selected, key=lambda h: h.start, reverse=True):
        new_text = new_text[: hit.start] + hit.replace_text + new_text[hit.end :]

    diff, added, removed = _diff(doc.text, new_text, rel)
    return PatchPlan(
        path=path,
        rel_path=rel,
        old_text=doc.text,
        new_text=new_text,
        diff=diff,
        strategy=strategy if not replace_all else f"{strategy}+all",
        added=added,
        removed=removed,
        is_create=False,
        doc=doc,
    )


def plan_create(path: Path, content: str, *, rel_path: str | None = None) -> PatchPlan:
    """新建文件的计划。文件已存在则报错（避免误覆盖）。"""
    rel = rel_path or path.name
    if path.exists():
        raise PatchError(
            f"文件已存在: {rel}。新建操作用于创建新文件；"
            "修改已有文件请用 edit_file，整体覆盖请显式用 write_file。"
        )
    doc = TextDoc.from_text(content)
    diff, added, removed = _diff("", doc.text, rel)
    return PatchPlan(
        path=path,
        rel_path=rel,
        old_text="",
        new_text=doc.text,
        diff=diff,
        strategy="create",
        added=added,
        removed=0,
        is_create=True,
        doc=doc,
    )


def plan_overwrite(path: Path, content: str, *, rel_path: str | None = None) -> PatchPlan:
    """整文件覆盖的计划。

    这是**例外路径**，不是默认编辑方式（见 ADR-004）。调用方（write_file 工具）
    必须先拿到用户的显式确认，否则应改走 plan_patch。
    """
    rel = rel_path or path.name
    if not path.exists():
        return plan_create(path, content, rel_path=rel)
    doc = TextDoc.load(path)
    diff, added, removed = _diff(doc.text, content, rel)
    return PatchPlan(
        path=path,
        rel_path=rel,
        old_text=doc.text,
        new_text=doc.text if content == doc.text else content,
        diff=diff,
        strategy="overwrite",
        added=added,
        removed=removed,
        is_create=False,
        doc=doc,
    )


def commit_plan(plan: PatchPlan) -> None:
    """原子落盘：先写同目录临时文件，再 os.replace 顶替，避免写坏原文件。"""
    if not plan.changed and not plan.is_create:
        return
    doc = plan.doc or TextDoc.from_text(plan.new_text)
    payload = doc.render(plan.new_text)

    plan.path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{plan.path.name}.", suffix=".vibetmp", dir=str(plan.path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if plan.path.exists():
            try:
                os.chmod(tmp_path, plan.path.stat().st_mode)
            except OSError:
                pass
        os.replace(tmp_path, plan.path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def apply_patch(
    path: Path,
    search: str,
    replace: str,
    *,
    rel_path: str | None = None,
    replace_all: bool = False,
    create_if_missing: bool = False,
) -> PatchPlan:
    """计划 + 落盘一步到位（审批已在更上层完成时使用）。"""
    plan = plan_patch(
        path, search, replace, rel_path=rel_path,
        replace_all=replace_all, create_if_missing=create_if_missing,
    )
    commit_plan(plan)
    return plan
