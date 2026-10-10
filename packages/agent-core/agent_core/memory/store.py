"""跨会话记忆（P2-15）：本地 SQLite，纯标准库，零依赖。

## 定位

记忆是**用户本人**在 `~/.vibe/memories.db` 里维护的一小撮文本（用户级，
跨工作区可见）——「这个仓库用 pytest」「提交信息用中文」这类偏好。
它与会话库（`host/store.py`）分开：会话属于**某次对话**，记忆属于**这台机器上的用户**。

## 三条纪律

1. **写入必须经用户确认或审批**（ADR-008）。模型只能**提议**（`remember` 工具，
   `Risk.REVIEW` 恒人审），真正的落库发生在审批通过之后；界面里的「记住这条」
   则是用户本人的动作。**禁止静默自动学习**（那会把注入面变成持久后门）。
2. **注入一律 `wrap_untrusted`**。记忆是要进提示词的文本，天然是提示词注入面 ——
   检索结果由调用方（Agent）用不可信块包裹，这一层只负责「取出文本」。
3. **不引第三方依赖**。检索用词法打分（token 交集），不引 embedding。

## 损坏容忍

与会话库同范式：库文件被手工截断/损坏时，把它挪到一边重建，绝不让 host 起不来。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 单条记忆上限。记忆是「偏好/约束」而不是知识库，写成小作文就是滥用。
MAX_TEXT_CHARS = 2000
# 标签个数与单个标签长度上限。
MAX_TAGS = 8
MAX_TAG_CHARS = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL DEFAULT 'user',
    text          TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    source_digest TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(id DESC);
"""

# 词法分词：英文/数字词 + 每个 CJK 字符。中文没空格，按字级别取交集足够
# 做「任务里提到 pytest」这类粗召回；不引分词库与 embedding（零依赖硬约束）。
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


class MemoryLimitExceeded(ValueError):
    """记忆条数达到上限。属于可预期的用户错误（页面 400），不是崩溃。"""


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    scope: str
    text: str
    tags: tuple[str, ...]
    created_at: str
    source_digest: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "text": self.text,
            "tags": list(self.tags),
            "createdAt": self.created_at,
            "sourceDigest": self.source_digest,
        }


def default_memory_db_path() -> Path:
    """~/.vibe/memories.db（无库覆盖配置时的默认位置）。"""
    return Path.home() / ".vibe" / "memories.db"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _split_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip() for part in raw]
    else:
        return ()
    tags: list[str] = []
    for item in items:
        if not item:
            continue
        item = item[:MAX_TAG_CHARS]
        if item not in tags:
            tags.append(item)
        if len(tags) >= MAX_TAGS:
            break
    return tuple(tags)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


class MemoryStore:
    """SQLite 记忆库。线程安全（工作线程写、HTTP 线程读）。"""

    def __init__(self, path: Path | str, *, max_items: int = 200) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_items = max(1, int(max_items))
        self._lock = threading.RLock()
        self._recovered = False
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            # 库文件损坏：挪走重建，历史不救但服务保命（同 host/store.py）
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                self.path.rename(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
            except OSError:
                self.path.unlink(missing_ok=True)
            self._recovered = True
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

    @property
    def recovered(self) -> bool:
        return self._recovered

    # ------------------------------------------------------------------ 写
    def add(
        self,
        text: str,
        *,
        tags: object = None,
        scope: str = "user",
        source_digest: str = "",
    ) -> MemoryRecord:
        """新增一条记忆。超过 max_items 抛 MemoryLimitExceeded（明确拒绝而非静默丢）。"""
        body = str(text or "").strip()
        if not body:
            raise ValueError("记忆内容不能为空")
        if len(body) > MAX_TEXT_CHARS:
            raise ValueError(f"记忆过长（{len(body)} 字符，上限 {MAX_TEXT_CHARS}）")
        tag_tuple = _split_tags(tags)
        created = _now()
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if count >= self.max_items:
                raise MemoryLimitExceeded(
                    f"记忆条数已达上限（{self.max_items}）。请先删除一些再添加。"
                )
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO memories (scope, text, tags, created_at, source_digest)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(scope or "user"), body, ",".join(tag_tuple), created, str(source_digest or "")),
            )
            mem_id = int(cur.lastrowid or 0)
            self._conn.commit()
        return MemoryRecord(
            id=mem_id,
            scope=str(scope or "user"),
            text=body,
            tags=tag_tuple,
            created_at=created,
            source_digest=str(source_digest or ""),
        )

    def remove(self, mem_id: int) -> bool:
        """按 id 删除。返回是否真的删掉了一条。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memories WHERE id = ?", (int(mem_id),))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    # ------------------------------------------------------------------ 读
    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def _row(self, row: tuple) -> MemoryRecord:
        mem_id, scope, text, tags, created, source = row
        return MemoryRecord(
            id=int(mem_id),
            scope=str(scope),
            text=str(text),
            tags=tuple(t for t in str(tags).split(",") if t),
            created_at=str(created),
            source_digest=str(source),
        )

    _COLS = "id, scope, text, tags, created_at, source_digest"

    def list(self, *, limit: int = 200) -> list[MemoryRecord]:
        """按最近创建倒序返回。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLS} FROM memories ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def search(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        """词法检索：查询与记忆的 token 交集数排序，取 top-N。

        交集为 0 的不返回（避免「什么任务都注入同一批记忆」）。行数不多
        （默认上限 200），全量打分足够快，不建倒排索引。
        """
        k = max(0, int(limit))
        if k == 0:
            return []
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLS} FROM memories ORDER BY id DESC"
            ).fetchall()
        scored: list[tuple[int, int, MemoryRecord]] = []
        for row in rows:
            record = self._row(row)
            mem_tokens = set(_tokens(record.text)) | {t.lower() for t in record.tags}
            overlap = len(query_tokens & mem_tokens)
            if overlap > 0:
                # 主序：交集数降序；次序：id 降序（新记忆优先）
                scored.append((overlap, record.id, record))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [record for _, _, record in scored[:k]]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


__all__ = [
    "MAX_TAG_CHARS",
    "MAX_TAGS",
    "MAX_TEXT_CHARS",
    "MemoryLimitExceeded",
    "MemoryRecord",
    "MemoryStore",
    "default_memory_db_path",
]