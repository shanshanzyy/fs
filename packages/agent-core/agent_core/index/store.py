"""检索索引门面（B3）：一次构建、进程内复用、**不落盘**。

为什么不落盘：能写的地方只有 `.vibe/`（PathGuard 保护），而 `.vibe/` 里的陈旧
索引比「慢一点」危险得多 —— 用户改了代码却搜到旧结果，比搜不到更误导人，
而且这种错很难被察觉。用「(工作区, 最大 mtime, 文件数) 一变就重建」来代替持久化，
是更保守的取舍：重建的代价是读一遍源码，语义检索本来就是低频动作。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .depgraph import DepGraph
from .lexical import (
    OVERLAP_LINES,
    WINDOW_LINES,
    LexicalIndex,
    chunk_text,
    chunk_text_by_symbols,
)

_MAX_CACHE_ENTRIES = 4


@dataclass(frozen=True)
class Chunk:
    path: str
    start: int
    end: int
    text: str

    def snippet(self, limit: int = 240) -> str:
        """压缩成一行可读片段：去掉空行、合并空白，超长截断。"""
        lines = [line.strip() for line in self.text.split("\n") if line.strip()]
        body = " ⏎ ".join(lines)
        return body if len(body) <= limit else body[: limit - 1] + "…"


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float

    @property
    def path(self) -> str:
        return self.chunk.path

    @property
    def start(self) -> int:
        return self.chunk.start

    @property
    def end(self) -> int:
        return self.chunk.end

    def snippet(self, limit: int = 240) -> str:
        return self.chunk.snippet(limit)


@dataclass
class SearchIndex:
    """一个工作区的检索索引：块 + 词法倒排 + 文件级依赖图。

    块表用 `dict[doc_id, Chunk]` 而不是 `list[Chunk]`：增量更新要能摘掉
    某个文件的块（J1）。若用 list，删除就得重排下标，而块下标记在倒排的
    postings 里 —— 重排等于把整个倒排索引作废。dict 允许空洞，代价只是
    `len()` 之外不能再靠下标遍历（外部本来也没这么用）。
    """

    root: Path
    chunks: dict[int, Chunk] = field(default_factory=dict)
    lexical: LexicalIndex = field(default_factory=LexicalIndex)
    graph: DepGraph = field(default_factory=DepGraph)
    files: list[str] = field(default_factory=list)
    parsed_files: int = 0
    skipped_binary: int = 0
    skipped_big: int = 0
    # 切块参数：`update()` 复用同一套参数重切「改过的文件」，与 build 保持一致，
    # 否则同一个文件在增量前后会被切成不同的块，等价性无从谈起。
    window: int = WINDOW_LINES
    overlap: int = OVERLAP_LINES
    # 增量所需的「按文件」账本：file -> 它当前占用的块 id、它解析出的 import。
    imports_by_file: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _file_chunks: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _parsed: set[str] = field(default_factory=set, repr=False)
    _next_id: int = 0
    # 被 watcher 持有的索引不参与 LRU 淘汰（J6）：否则淘汰后 `get_index` 会
    # 另建一个实例，watcher 更新的那个就再也没人读了 —— 表现是「搜到旧内容」。
    pinned: bool = False
    # 检索增强（M3+）：分词子词化 + 符号感知分块 + 路径加权。
    # 默认 False → 与改造前逐字节一致；它与 root/window 一样是索引的**形状**，
    # 因此必须进缓存键（同一个 root 在增强/非增强下是两个不同的索引）。
    enhanced: bool = False

    @classmethod
    def build(
        cls,
        root: Path,
        rel_of: Callable[[Path], str] | None = None,
        max_files: int = 800,
        window: int = WINDOW_LINES,
        overlap: int = OVERLAP_LINES,
        enhanced: bool = False,
    ) -> "SearchIndex":
        # 延迟导入：既避免与 tools 包初始化形成环，也让「只想用词法索引」的
        # 调用方不必连带加载整个工具系统。
        from ..tools.search import _iter_files

        index = cls(
            root=root,
            window=window,
            overlap=overlap,
            enhanced=enhanced,
            lexical=LexicalIndex(enhanced=enhanced),
        )
        rel_of = rel_of or (lambda path: path.relative_to(root).as_posix())
        max_files = max(1, max_files)

        for path in _iter_files(root, max_files=max_files):
            index._ingest(rel_of(path), path)

        index.graph = DepGraph.build(index.files, index.imports_by_file)
        return index

    # -------------------------------------------------------------- 增量更新
    def update(
        self,
        added: list[str] | tuple[str, ...] = (),
        changed: list[str] | tuple[str, ...] = (),
        removed: list[str] | tuple[str, ...] = (),
    ) -> dict[str, int]:
        """把磁盘上的变化合进索引，返回本次实际生效的计数。

        只碰 `added/changed/removed` 里的那几个文件：万级文件仓库里，
        「改一个文件就全量重扫」是纯粹的浪费。依赖边是**文件级**的，
        因此只在最后重算一次图（它本来就只依赖各文件的 import 列表）。

        与全量重建的等价性来自两点：doc_id 单调分配、只摘掉死文档的
        postings —— 存活文档集的分词、df、avgdl 与重建逐项相同。
        """
        # changed 与 removed 都要先摘掉旧块；同一个 rel 同时出现在两边时只摘一次。
        # 计数必须**在摘除前**取：摘完再问「在不在索引里」永远是 False。
        removed_rels = list(dict.fromkeys(removed))
        dropped = sum(1 for rel in removed_rels if rel in self._file_chunks)
        for rel in dict.fromkeys([*removed_rels, *changed]):
            self._drop_file(rel)

        applied = {"added": 0, "changed": 0, "removed": dropped}
        for key, rels in (("added", added), ("changed", changed)):
            for rel in dict.fromkeys(rels):
                path = self.root / rel
                if path.is_file() and self._ingest(rel, path):
                    applied[key] += 1

        self.graph = DepGraph.build(self.files, self.imports_by_file)
        return {
            **applied,
            "files": len(self.files),
            "chunks": len(self.chunks),
            "edges": self.graph.edge_count(),
        }

    def _ingest(self, rel: str, path: Path) -> bool:
        """把一个文件读进来切块 + 建倒排 + 记依赖。返回是否真的入了索引。

        build 与 update 共用它，保证「全量」与「增量」走的是同一条切块/解析
        路径 —— 这是两者结果等价的前提。
        """
        from ..tools.search import _looks_binary
        from ..tools.symbols import scan_file, supports

        # 幂等：同一个 rel 已经在索引里就先摘掉再收。
        # 少了这一步，调用方把「已在索引里的文件」报成 added（或漏报一次删除后
        # 又报 added）时，files 会多出一条、块会留下一份旧的 —— 检索时同一个
        # 文件出现两次，其中一份还是过期内容。
        if rel in self._file_chunks:
            self._drop_file(rel)
        if _looks_binary(path):
            self.skipped_binary += 1
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

        self.files.append(rel)
        ids = self._file_chunks.setdefault(rel, [])

        # 解析先行：增强路径的符号感知切块要用符号定义行号当块边界，
        # 因此 scan 必须在切块之前。默认路径不依赖它，但提前算也无副作用
        # —— import 账本本来就要在这里填，全量/增量共用同一条路径不变。
        symbol_lines: list[int] = []
        if supports(path.suffix.lower()):
            self.parsed_files += 1
            self._parsed.add(rel)
            result = scan_file(path, text)
            if result.imports:
                self.imports_by_file[rel] = result.imports
            if self.enhanced:
                symbol_lines = [symbol.line for symbol in result.symbols]

        if self.enhanced:
            blocks = chunk_text_by_symbols(
                text, symbol_lines, window=self.window, overlap=self.overlap
            )
        else:
            blocks = chunk_text(text, window=self.window, overlap=self.overlap)

        for start, end, body in blocks:
            doc_id = self._next_id
            self._next_id += 1
            self.chunks[doc_id] = Chunk(path=rel, start=start, end=end, text=body)
            # 增强路径把路径串再计入一次（字段加权）：命中文件名的块排更前。
            self.lexical.add(
                doc_id, f"{rel}\n{body}", boost=rel if self.enhanced else ""
            )
            ids.append(doc_id)
        return True

    def _drop_file(self, rel: str) -> bool:
        """摘掉一个文件：块、倒排项、依赖、以及它在 files 里的位置。"""
        present = rel in self._file_chunks
        for doc_id in self._file_chunks.pop(rel, []):
            self.chunks.pop(doc_id, None)
            self.lexical.remove(doc_id)
        if rel in self._parsed:
            self._parsed.discard(rel)
            self.parsed_files = max(0, self.parsed_files - 1)
        self.imports_by_file.pop(rel, None)
        try:
            self.files.remove(rel)
        except ValueError:
            pass
        return present

    # ------------------------------------------------------------------ 查询
    def search(self, query: str, k: int = 8) -> list[SearchHit]:
        return [
            SearchHit(chunk=self.chunks[doc_id], score=score)
            for doc_id, score in self.lexical.search(query, k=k)
        ]

    def related(self, rel: str, limit: int = 3) -> tuple[list[str], list[str]]:
        """返回 (依赖它的文件, 它依赖的文件)。空图时两边都是空列表。"""
        return (
            self.graph.dependents(rel, limit),
            self.graph.neighbors(rel, limit),
        )

    def stats(self) -> dict[str, int]:
        return {
            "files": len(self.files),
            "chunks": len(self.chunks),
            "edges": self.graph.edge_count(),
        }


# ---- 进程内单例 -----------------------------------------------------------
_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[tuple, SearchIndex]" = OrderedDict()
# 被 watcher 持有的索引：<root, max_files, window, overlap, enhanced> -> 实例。
# 它比 _CACHE 多一层语义 —— 「签名再变也返回这个实例」，因为 watcher 会把它
# 保持最新。少了这条规则就会出现两个实例：watcher 更新 A，检索读 B（旧），
# 而且 B 永远不会被更新。见 J6。
_PINNED: "dict[tuple, SearchIndex]" = {}


def _signature(root: Path, max_files: int) -> tuple[int, int]:
    """廉价的「变没变」指纹：文件数 + 最新 mtime。

    只 stat 不读内容 —— 命中缓存时省掉的正是「读 + 切块 + 建倒排 + 建图」，
    而这几步才是耗时的部分。用 mtime 而不是内容哈希，是因为后者必须读全文，
    那就等于没缓存。
    """
    from ..tools.search import _iter_files

    count = 0
    newest = 0
    for path in _iter_files(root, max_files=max_files):
        count += 1
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if mtime > newest:
            newest = mtime
    return count, newest


def get_index(
    root: Path,
    rel_of: Callable[[Path], str] | None = None,
    max_files: int = 800,
    window: int = WINDOW_LINES,
    overlap: int = OVERLAP_LINES,
    pin: bool = False,
    enhanced: bool = False,
) -> SearchIndex:
    """取索引：签名一致则复用进程内缓存，否则重建并替换。

    `pin=True`（watcher 用）把返回的实例**钉住**：此后同一 root 的调用恒定
    拿到它、且它不被 LRU 淘汰。绑定同一个实例是 watcher 能生效的前提。
    """
    base = (str(root), max_files, window, overlap, enhanced)
    with _CACHE_LOCK:
        pinned = _PINNED.get(base)
        if pinned is not None:
            return pinned  # watcher 在维护它，签名是否「变过」由它自己吸收

    key = (*base, *_signature(root, max_files))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            if pin and not hit.pinned:
                hit.pinned = True
                _PINNED[base] = hit
            return hit

    index = SearchIndex.build(root, rel_of, max_files, window, overlap, enhanced)

    with _CACHE_LOCK:
        if pin:
            index.pinned = True
            _PINNED[base] = index
        _CACHE[key] = index
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_CACHE_ENTRIES:
            for stale in list(_CACHE):
                if not _CACHE[stale].pinned:
                    _CACHE.pop(stale)
                    break
            else:
                break  # 全是钉住的：宁可超一点，也不能淘汰掉正在被写的那一个
    return index


def peek_index(
    root: Path,
    max_files: int = 800,
    window: int = WINDOW_LINES,
    overlap: int = OVERLAP_LINES,
    enhanced: bool = False,
) -> SearchIndex | None:
    """只看缓存里有没有，**不建索引**。

    `/api/index/status` 要用它：那是个只读查询，不该因为一次 GET 就把整个
    工作区读一遍（万级文件下这是秒级动作）。没有索引就如实回 0。
    """
    base = (str(root), max_files, window, overlap, enhanced)
    with _CACHE_LOCK:
        pinned = _PINNED.get(base)
        if pinned is not None:
            return pinned
        for key, index in _CACHE.items():
            if key[:5] == base:
                return index
    return None


def release_index(index: SearchIndex) -> None:
    """解钉并移出缓存（watcher 停止时调）。之后它可以被 LRU 正常淘汰。"""
    with _CACHE_LOCK:
        index.pinned = False
        for base, candidate in list(_PINNED.items()):
            if candidate is index:
                _PINNED.pop(base, None)
        for key, candidate in list(_CACHE.items()):
            if candidate is index:
                _CACHE.pop(key, None)


def clear_cache() -> None:
    """清空缓存。测试用（也让「重建」路径可被显式验证）。"""
    with _CACHE_LOCK:
        for index in _CACHE.values():
            index.pinned = False
        _CACHE.clear()
        _PINNED.clear()