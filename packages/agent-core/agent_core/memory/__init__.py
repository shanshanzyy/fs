"""跨会话记忆（P2-15）。

纯标准库：本地 SQLite + 词法检索，零第三方依赖。见 ``store.py`` 的模块头。
"""

from .store import (
    MAX_TAG_CHARS,
    MAX_TAGS,
    MAX_TEXT_CHARS,
    MemoryLimitExceeded,
    MemoryRecord,
    MemoryStore,
    default_memory_db_path,
)

__all__ = [
    "MAX_TAG_CHARS",
    "MAX_TAGS",
    "MAX_TEXT_CHARS",
    "MemoryLimitExceeded",
    "MemoryRecord",
    "MemoryStore",
    "default_memory_db_path",
]