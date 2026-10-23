"""定时任务库（M10）：**纯 stdlib `sqlite3`** 落盘。

落盘纪律：
  - 与 `CheckpointManager` 同范式 —— 宿主用**绝对路径直写**
    `<workspace>/.vibe/scheduler.db`，**不走**模型写盘通道（模型仍不可写
    `.vibe/`）。库文件属**宿主资产**。
  - 线程安全：`check_same_thread=False` + 一把 `threading.RLock`
    （与 `memory/store.py` 同做法）。

**坏行 fail-closed**：`load_all()` 逐行解析，**调度表达式非法 / 关键字段缺失**
的行**跳过并计数**（`skipped_rows`），不静默吞、也不让单条坏行弄挂调度器。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .cron import ScheduleError, parse_schedule
from .model import JobSpec

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    schedule    TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL DEFAULT 0,
    next_run_at REAL NOT NULL DEFAULT 0,
    last_run_at REAL NOT NULL DEFAULT 0,
    last_task_id TEXT NOT NULL DEFAULT '',
    last_state  TEXT NOT NULL DEFAULT '',
    run_count   INTEGER NOT NULL DEFAULT 0
);
"""

_FIELDS = (
    "job_id",
    "name",
    "schedule",
    "prompt",
    "enabled",
    "created_at",
    "next_run_at",
    "last_run_at",
    "last_task_id",
    "last_state",
    "run_count",
)


class JobStore:
    """定时任务库。线程安全（调度线程写、HTTP 线程读）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._skipped = 0
        self._conn = self._open()

    # ------------------------------------------------------------------ 打开
    def _open(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            # 库文件损坏：挪走重建，历史不救但服务保命（同 memory/store.py）
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
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

    @property
    def skipped_rows(self) -> int:
        """最近一次 `load_all()` 里被跳过的坏行数（供 `Scheduler.status()` 提示）。"""
        return self._skipped

    # ------------------------------------------------------------------ 读
    def load_all(self) -> list[JobSpec]:
        """读全部 job（含 `enabled=0` 的）。坏行跳过并计数。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_FIELDS)} FROM jobs"
            ).fetchall()
        jobs: list[JobSpec] = []
        skipped = 0
        for row in rows:
            spec = _row_to_job(row)
            if spec is None:
                skipped += 1
                continue
            try:
                parse_schedule(spec.schedule)
            except ScheduleError:
                skipped += 1
                continue
            jobs.append(spec)
        self._skipped = skipped
        return jobs

    def get(self, job_id: str) -> JobSpec | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_FIELDS)} FROM jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        return _row_to_job(row) if row else None

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    # ------------------------------------------------------------------ 写
    def upsert(self, job: JobSpec) -> None:
        """插入或整体更新一个 job（按 job_id）。"""
        values = (
            job.job_id,
            job.name,
            job.schedule,
            job.prompt,
            1 if job.enabled else 0,
            float(job.created_at),
            float(job.next_run_at),
            float(job.last_run_at),
            job.last_task_id,
            job.last_state,
            int(job.run_count),
        )
        placeholders = ", ".join("?" * len(_FIELDS))
        updates = ", ".join(f"{name} = excluded.{name}" for name in _FIELDS[1:])
        with self._lock:
            self._conn.execute(
                f"INSERT INTO jobs ({', '.join(_FIELDS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(job_id) DO UPDATE SET {updates}",
                values,
            )
            self._conn.commit()

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE job_id = ?", (str(job_id),)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


# --------------------------------------------------------------------------- 行映射


def _row_to_job(row: tuple) -> JobSpec | None:
    """行 → JobSpec。关键字段（job_id / schedule / prompt）缺失即返回 None。"""
    if not row:
        return None
    job_id = str(row[0] or "").strip()
    schedule = str(row[2] or "").strip()
    prompt = str(row[3] or "").strip()
    if not job_id or not schedule or not prompt:
        return None
    return JobSpec(
        job_id=job_id,
        name=str(row[1] or ""),
        schedule=schedule,
        prompt=prompt,
        enabled=bool(row[4]),
        created_at=float(row[5] or 0.0),
        next_run_at=float(row[6] or 0.0),
        last_run_at=float(row[7] or 0.0),
        last_task_id=str(row[8] or ""),
        last_state=str(row[9] or ""),
        run_count=int(row[10] or 0),
    )


__all__ = ["JobStore"]
