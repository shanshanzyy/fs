"""词法检索（B2）：零依赖的 BM25-lite。

为什么必须有一套词法打底：核心链路零强制依赖，且索引要「随时可建」——
不能因为没配 embedding 端点就让 `semantic_search` 直接不可用。
所以路径是「词法召回（保底） + 可选向量重排（提精度）」，
而不是「没有向量就没有语义检索」。

切块策略：定长行窗口 + 段落边界优先 + 1 行重叠。
行窗口保证块大小可控（喂给模型时预算可预测）；段落边界优先让块尽量落在
一个函数 / 一段注释里；1 行重叠是为了避免「恰好跨窗口的那句话」两个块都只匹配一半。
"""

from __future__ import annotations

import math
import re

WINDOW_LINES = 40
OVERLAP_LINES = 1

# BM25 的两个经典常数，不打算做成配置项 —— 在代码检索这种短文本场景里，
# 调它们带来的收益远小于「参数面变大」的成本。
_K1 = 1.2
_B = 0.75

_ASCII_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

# 增强路径用的子词拆分（M3+）：把标识符拆成词边界。
#   get_user_name → get / user / name     （snake_case）
#   getUserName   → get / user / name     （camelCase）
#   HTTPClient    → http / client         （连续大写视作一个词）
#   版本号/数字单独成词。刻意不引分词库 —— 核心链路零强制依赖（ADR-003）。
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_DIGIT_SPLIT_RE = re.compile(r"(\d+)")


def chunk_text(
    text: str, window: int = WINDOW_LINES, overlap: int = OVERLAP_LINES
) -> list[tuple[int, int, str]]:
    """把文件切成块，返回 [(起始行, 结束行, 正文)]，行号 1 基。

    段落边界优先：在窗口内从后往前找第一个空行，找到就把断点收在那里。
    找不到（比如一个长函数刚好占满窗口）就按窗口硬切，不会退化成空块。
    """
    lines = text.split("\n")
    total = len(lines)
    chunks: list[tuple[int, int, str]] = []
    start = 0
    floor = max(1, window // 2)

    while start < total:
        while start < total and not lines[start].strip():
            start += 1  # 跳过前导空行，避免块以空行开头
        if start >= total:
            break

        end = min(start + window, total)
        if end < total:
            for probe in range(end, start + floor, -1):
                if not lines[probe - 1].strip():
                    end = probe
                    break

        chunks.append((start + 1, end, "\n".join(lines[start:end])))
        if end >= total:
            break
        start = max(end - overlap, start + 1)

    return chunks


def chunk_text_by_symbols(
    text: str,
    symbol_lines: list[int],
    window: int = WINDOW_LINES,
    overlap: int = OVERLAP_LINES,
) -> list[tuple[int, int, str]]:
    """符号感知切块（M3+ 检索增强）。

    与 `chunk_text` 的唯一区别：窗口内**从后往前**找一个符号定义起点
    （函数/类所在行）当切点，让一个块尽量对应一个完整定义。命中片段因此更「整」，
    读者不用再从块外补上下文。

    退化保证：窗口里没有可用符号边界（或本就不是可解析的源文件）时，
    行为与 `chunk_text` 一致 —— 仍是「窗口 + 段落边界优先」。
    仅在 `Config.retrieval_enhanced` 为真时使用；默认路径仍走 `chunk_text`。
    """
    lines = text.split("\n")
    total = len(lines)
    if total == 0:
        return []

    # 归一化为 0 基行号，丢掉文件头（第 1 行）与越界项 —— 切在文件首行无意义。
    marks = sorted({line - 1 for line in symbol_lines if 2 <= line <= total})
    floor = max(1, window // 2)
    chunks: list[tuple[int, int, str]] = []
    start = 0

    while start < total:
        while start < total and not lines[start].strip():
            start += 1  # 跳过前导空行，与 chunk_text 一致
        if start >= total:
            break

        hard_end = min(start + window, total)
        end = hard_end
        if hard_end < total:
            for mark in reversed(marks):
                if mark <= start:
                    break
                if mark < hard_end and mark >= start + floor:
                    end = mark
                    break

        chunks.append((start + 1, end, "\n".join(lines[start:end])))
        if end >= total:
            break
        start = max(end - overlap, start + 1)

    return chunks


def tokenize(text: str) -> list[str]:
    """分词：ASCII 按 `[A-Za-z0-9_]+`，中文取二字组。

    中文不做分词器（那是可选依赖）。二字组是标准库里性价比最高的近似：
    「用户登录」→ 用户 / 户登 / 登录，查「登录」就能命中。
    单字也一并保留，否则「改」这类单词查询会全军覆没。
    """
    tokens = [match.group(0).lower() for match in _ASCII_RE.finditer(text)]
    for run in _CJK_RE.findall(text):
        tokens.extend(run)
        for index in range(len(run) - 1):
            tokens.append(run[index : index + 2])
    return tokens


def split_identifier(token: str) -> list[str]:
    """把单个标识符拆成子词（全部小写）。不改变语义，只增加可命中面。

    单看 `tokenize` 是「整词匹配」：`getUserName` 只会产生 `getusername`，
    用户查 `user` 命中不了 —— 而查代码时人们几乎总是按**子词**回忆
    （「那个处理用户名的函数」）。子词化补的正是这一刀。
    """
    parts: list[str] = []
    for piece in token.split("_"):
        for chunk in _DIGIT_SPLIT_RE.split(piece):
            if not chunk:
                continue
            if chunk.isdigit():
                parts.append(chunk)
                continue
            parts.extend(sub.lower() for sub in _CAMEL_RE.findall(chunk))
    return [part for part in parts if part]


def tokenize_enhanced(text: str) -> list[str]:
    """增强分词（M3+）：在 `tokenize` 基础上追加**标识符子词**。

    保留原词（`getusername`）与 CJK 二字组不动，只**追加**子词 ——
    这样既不会丢掉整词精确匹配，又能让 `user` 命中 `getUserName`。
    仅当 `Config.retrieval_enhanced` 为真时使用；默认路径仍是 `tokenize`，
    保证未配置时 `semantic_search` 结果与改造前逐字节一致。
    """
    tokens: list[str] = []
    for match in _ASCII_RE.finditer(text):
        raw = match.group(0)
        tokens.append(raw.lower())
        subwords = split_identifier(raw)
        if len(subwords) > 1:
            tokens.extend(subwords)
    for run in _CJK_RE.findall(text):
        tokens.extend(run)
        for index in range(len(run) - 1):
            tokens.append(run[index : index + 2])
    return tokens


class LexicalIndex:
    """纯内存倒排索引。doc_id 由调用方给定，只要唯一即可。

    doc_id 可以是**稀疏的**（块被增量删除后留下的空洞不回收）：
    索引改成增量更新后，块 id 由 `SearchIndex` 单调递增地分配，
    删文件只摘掉对应的 id、不重排其余 id —— 重排会让所有 postings 全部作废，
    那就等于全量重建了。
    """

    def __init__(self, enhanced: bool = False) -> None:
        self._postings: dict[str, dict[int, int]] = {}
        self._lengths: dict[int, int] = {}
        # 反查「这个文档贡献了哪些词」，删除时才知道该去哪些桶里摘（见 remove）。
        self._doc_terms: dict[int, set[str]] = {}
        # 增强路径（M3+）：分词改用 tokenize_enhanced（追加标识符子词）。
        # 默认 False → 与改造前逐字节一致。
        self.enhanced = enhanced

    def _tokenize(self, text: str) -> list[str]:
        return tokenize_enhanced(text) if self.enhanced else tokenize(text)

    def add(self, doc_id: int, text: str, boost: str = "") -> None:
        if doc_id in self._lengths:
            self.remove(doc_id)  # 用同一个 id 重加：先清旧 postings，避免残留死词
        tokens = self._tokenize(text)
        if boost:
            # 字段加权（M3+）：把路径/符号名再算一遍 —— 等价于给它们更高的 tf，
            # 让「命中文件名」的块排在「只在正文里提了一句」的块之前。
            tokens = tokens + self._tokenize(boost)
        self._lengths[doc_id] = len(tokens)
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for token, tf in counts.items():
            self._postings.setdefault(token, {})[doc_id] = tf
        self._doc_terms[doc_id] = set(counts)

    def remove(self, doc_id: int) -> None:
        """摘掉一个文档：长度、它在各词桶里的项，以及因此变空的桶。

        空桶必须删掉 —— `df` 与 `term_count` 都看它，留着会让「已经消失的词」
        继续参与 idf 计算：df 偏大 → idf 偏小 → 真正命中的词被稀释。
        """
        if doc_id not in self._lengths:
            return
        del self._lengths[doc_id]
        for token in self._doc_terms.pop(doc_id, ()):
            posting = self._postings.get(token)
            if posting is None:
                continue
            posting.pop(doc_id, None)
            if not posting:
                del self._postings[token]

    @property
    def doc_count(self) -> int:
        return len(self._lengths)

    @property
    def term_count(self) -> int:
        return len(self._postings)

    def search(self, query: str, k: int = 8) -> list[tuple[int, float]]:
        """返回 [(doc_id, 分数)]，按分数降序；同分按 doc_id 升序，保证可复现。"""
        terms = set(self._tokenize(query))
        if not terms or not self._lengths:
            return []

        total = self.doc_count
        avgdl = sum(self._lengths.values()) / total if total else 0.0
        scores: dict[int, float] = {}

        for term in terms:
            posting = self._postings.get(term)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            for doc_id, tf in posting.items():
                doc_len = self._lengths[doc_id]
                norm = (doc_len / avgdl) if avgdl else 0.0
                denom = tf + _K1 * (1 - _B + _B * norm)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (_K1 + 1)) / denom

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:k]