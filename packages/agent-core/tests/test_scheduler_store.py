"""定时任务库（M10 Part E）：stdlib sqlite3 落盘 + 坏行 fail-closed。

验收要点（对齐 M10-PLAN B / E）：
1. 写 → 关 → 重开 round-trip 一致（含 `enabled=0` 的定义）；
2. 父目录不存在自动创建；库确实落在指定路径；
3. 坏行（调度表达式非法 / 关键字段缺失）**跳过并计数**，不让单条坏行弄挂调度器；
4. 库文件损坏 → 挪走重建，服务保命；
5. `delete` 返回值正确；`get` 按 id 命中。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.scheduler import JobSpec, JobStore  # noqa: E402


def _job(job_id: str = "job-1", schedule: str = "@every 30m", **kw) -> JobSpec:
    base = dict(
        job_id=job_id,
        name="测试任务",
        schedule=schedule,
        prompt="跑一遍测试并汇报",
        enabled=True,
        created_at=1000.0,
        next_run_at=2000.0,
        last_run_at=0.0,
        last_task_id="",
        last_state="",
        run_count=0,
    )
    base.update(kw)
    return JobSpec(**base)


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.db = self.ws / ".vibe" / "scheduler.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_parent_dir_and_file(self) -> None:
        """父目录不存在自动创建（对齐 checkpoint.py 的 _ensure_dir）。"""
        self.assertFalse((self.ws / ".vibe").exists())
        store = JobStore(self.db)
        try:
            self.assertTrue(self.db.parent.is_dir())
            self.assertTrue(self.db.exists())
        finally:
            store.close()

    def test_round_trip_preserves_all_fields(self) -> None:
        job = _job(schedule="0 9 * * 1-5", run_count=3, last_state="triggered",
                   last_task_id="abc123abc123", last_run_at=1500.0)
        store = JobStore(self.db)
        try:
            store.upsert(job)
        finally:
            store.close()

        store2 = JobStore(self.db)
        try:
            rows = store2.load_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0], job)
        finally:
            store2.close()

    def test_disabled_job_definition_retained(self) -> None:
        """`enabled=0` 的 job 保留定义（重启后仍能列出来）。"""
        store = JobStore(self.db)
        try:
            store.upsert(_job(job_id="off-1", enabled=False))
        finally:
            store.close()
        store2 = JobStore(self.db)
        try:
            rows = store2.load_all()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].enabled)
        finally:
            store2.close()

    def test_upsert_updates_existing(self) -> None:
        store = JobStore(self.db)
        try:
            store.upsert(_job())
            store.upsert(replace(_job(), name="改过的", run_count=9))
            rows = store.load_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].name, "改过的")
            self.assertEqual(rows[0].run_count, 9)
        finally:
            store.close()

    def test_get_and_delete(self) -> None:
        store = JobStore(self.db)
        try:
            store.upsert(_job())
            self.assertIsNotNone(store.get("job-1"))
            self.assertIsNone(store.get("nope"))
            self.assertTrue(store.delete("job-1"))
            self.assertFalse(store.delete("job-1"))  # 再次删返回 False
            self.assertEqual(store.count(), 0)
        finally:
            store.close()

    def test_bad_schedule_row_skipped_and_counted(self) -> None:
        """手工塞一行非法 schedule → load_all 不抛异常、跳过计数 +1。"""
        store = JobStore(self.db)
        try:
            store.upsert(_job(job_id="good"))
            with store._lock:  # noqa: SLF001 - 测试直接写坏行
                store._conn.execute(  # noqa: SLF001
                    "INSERT INTO jobs (job_id, name, schedule, prompt) "
                    "VALUES (?, ?, ?, ?)",
                    ("bad-sched", "坏行", "99 9 * * *", "内容"),
                )
                store._conn.commit()  # noqa: SLF001
            rows = store.load_all()
            self.assertEqual([r.job_id for r in rows], ["good"])
            self.assertEqual(store.skipped_rows, 1)
        finally:
            store.close()

    def test_missing_required_field_row_skipped(self) -> None:
        """关键字段（prompt）缺失 → 跳过并计数。"""
        store = JobStore(self.db)
        try:
            with store._lock:  # noqa: SLF001
                store._conn.execute(  # noqa: SLF001
                    "INSERT INTO jobs (job_id, name, schedule, prompt) "
                    "VALUES (?, ?, ?, ?)",
                    ("no-prompt", "缺正文", "@every 1h", ""),
                )
                store._conn.commit()  # noqa: SLF001
            self.assertEqual(store.load_all(), [])
            self.assertEqual(store.skipped_rows, 1)
        finally:
            store.close()

    def test_corrupt_db_is_backed_up_and_rebuilt(self) -> None:
        """库文件损坏 → 挪走重建，服务保命（同 memory/store.py）。"""
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.db.write_bytes(b"this is not a sqlite database at all")
        store = JobStore(self.db)
        try:
            # 新库可用：能写能读
            store.upsert(_job())
            self.assertEqual(len(store.load_all()), 1)
        finally:
            store.close()
        backups = list(self.db.parent.glob("scheduler.db.corrupt-*"))
        self.assertTrue(backups, "损坏库应被挪走备份")

    def test_skipped_rows_zero_on_clean_load(self) -> None:
        store = JobStore(self.db)
        try:
            store.upsert(_job())
            store.load_all()
            self.assertEqual(store.skipped_rows, 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
