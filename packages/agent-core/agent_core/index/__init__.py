"""检索索引（B）：词法倒排 + 文件级依赖图 + 可选向量重排。

对外只需要 `get_index()` / `SearchIndex`：调用方不该关心内部用的是
词法还是向量 —— 那正是「探到就用、探不到降级」这条约定的落点。
"""

from .depgraph import DepGraph
from .lexical import (
    LexicalIndex,
    chunk_text,
    chunk_text_by_symbols,
    split_identifier,
    tokenize,
    tokenize_enhanced,
)
from .store import (
    Chunk,
    SearchHit,
    SearchIndex,
    clear_cache,
    get_index,
    peek_index,
    release_index,
)
from .watch import IndexWatcher

__all__ = [
    "Chunk",
    "DepGraph",
    "IndexWatcher",
    "LexicalIndex",
    "SearchHit",
    "SearchIndex",
    "chunk_text",
    "chunk_text_by_symbols",
    "clear_cache",
    "get_index",
    "peek_index",
    "release_index",
    "split_identifier",
    "tokenize",
    "tokenize_enhanced",
]