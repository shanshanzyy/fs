"""会话持久化（A2）：标准库 sqlite3，逐条追加写。

库文件默认放在 ``~/.vibe/sessions.db`` —— 会话属于用户而非工作区，
跨项目可见；工作区内的 ``.vibe/`` 只放审计与检查点（维持既有语义）。

隐私边界（ADR-006 澄清）：本地库存的是**本机**会话正文，与
「服务端不存正文」不冲突；高合规团队可用 ``VIBE_SESSION_PERSIST=false``
整体关闭（Config.session_persist）。

损坏容忍是硬要求：库文件被手工截断/损坏时，构造函数会把坏文件挪到一边
重新建库，绝不让 host 起不来 —— 历史丢了可以接受，服务起不来不行。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    workspace  TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    tool_meta  TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""


def default_session_db_path() -> Path:
    """~/.vibe/sessions.db（无库覆盖配置时的默认位置）。"""
    return Path.home() / ".vibe" / "sessions.db"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionStore:
    """SQLite 会话库。线程安全（工作线程写、HTTP 线程读）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._recovered = False  # 先置默认值，_open() 里可能置 True
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=5
            )
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            # 库文件损坏（截断/非 SQLite 内容）：挪走重建，历史不救但服务保命
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
    def append_message(
        self,
        session_id: str,
        *,
        workspace: str,
        model: str = "",
        role: str,
        content: str = "",
        tool_meta: str | None = None,
        title: str | None = None,
    ) -> None:
        """追加一条消息；会话行不存在时先建（INSERT OR IGNORE）。"""
        now = _now()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO sessions (id, workspace, model, title, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, workspace, model, title or "", now, now),
            )
            # 标题只写一次（A2）：resume 重建 hooks 后 _titled 归零，
            # 恢复会话里的下一条用户消息不能再覆盖原标题。
            if title:
                cur.execute(
                    "UPDATE sessions SET title = CASE WHEN title = '' THEN ? ELSE title END,"
                    " updated_at = ? WHERE id = ?",
                    (title, now, session_id),
                )
            else:
                cur.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
            row = cur.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            cur.execute(
                "INSERT INTO messages (session_id, seq, role, content, tool_meta, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, int(row[0]) + 1, role, content, tool_meta, now),
            )
            self._conn.commit()

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cur.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ 读
    def list_sessions(
        self, workspace: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """按最近活跃倒序。workspace 传 None 时返回全部（跨项目可见）。"""
        query = (
            "SELECT id, workspace, model, title, created_at, updated_at"
            " FROM sessions"
        )
        params: list[Any] = []
        if workspace:
            query += " WHERE workspace = ?"
            params.append(workspace)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        keys = ("id", "workspace", "model", "title", "createdAt", "updatedAt")
        return [dict(zip(keys, row)) for row in rows]

    def list_workspaces(self, limit: int = 20) -> list[dict[str, Any]]:
        """出现过的会话工作区，按最近活跃倒序。

        W1（打开其他文件夹）的「最近工作区」用它作数据源：浏览器开不了原生
        目录对话框，桌面壳与浏览器的「最近」必须来自同一个真源，否则两边漂移。
        """
        query = (
            "SELECT workspace, MAX(updated_at) AS updated_at FROM sessions"
            " GROUP BY workspace ORDER BY updated_at DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(query, [limit]).fetchall()
        return [{"path": row[0], "updatedAt": row[1]} for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, workspace, model, title, created_at, updated_at"
                " FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        keys = ("id", "workspace", "model", "title", "createdAt", "updatedAt")
        return dict(zip(keys, row))

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按序返回消息，形态与 Agent.messages 完全一致（可直接重放）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_meta FROM messages"
                " WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for role, content, meta in rows:
            message: dict[str, Any] = {"role": role, "content": content}
            if meta:
                try:
                    extra = json.loads(meta)
                    if isinstance(extra, dict):
                        message.update(extra)
                except json.JSONDecodeError:
                    pass  # 残缺元数据：丢附加字段，保住正文
            messages.append(message)
        return messages

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def validate_message_group(messages: list[dict[str, Any]]) -> str | None:
    """恢复前的成组校验，与 Agent._trim_context 的约束同源。

    规则：role=tool 的消息必须有 tool_call_id，且它所属的 assistant 消息
    （组头）就在它前面、tool_calls 里确实含有这个 id。多轮中断/截断可能
    产生「组头在、结果缺」或「结果在、组头缺」的残缺序列 —— 前者无害，
    后者会让服务端 400，必须拒绝恢复。

    返回 None 表示合法，否则返回人话错误描述。
    """
    expected: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            expected = {
                str(call.get("id") or "")
                for call in calls
                if isinstance(call, dict) and call.get("id")
            }
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id not in expected:
                return (
                    f"第 {index + 1} 条是孤立的 tool 消息"
                    f"（tool_call_id={call_id or '缺失'}），"
                    "其对应的 assistant 调用不在历史中，恢复后会被服务端拒绝。"
                )
            expected.discard(call_id)
        else:
            expected = set()
    return None
