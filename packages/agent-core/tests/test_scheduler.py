"""调度器（M10 Part E）：常驻线程语义 + 到点投池 + 限流 + 重启补跑 + 崩溃安全。

验收要点（对齐 M10-PLAN C / E）：
1. `add_job` 落库 + 审计 + 只回元数据（不含 prompt 正文）；
2. **到点才触发**、触发后重排到未来、`run_count`/`last_task_id` 记账；
3. **单轮限流** `max_per_tick` 生效（余下留到下一轮，不并发风暴）；job 数上限生效；
4. **重启补跑只补一次**（catch-up once），关掉补跑则记 `skipped`；
5. `run_now` 立即投且**不改动排期**；`update_job` 改表达式**强重排**；
6. 单个 job 投递失败**绝不弄挂调度线程**；
7. `schedule.job` 审计**只记元数据**。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.scheduler import (  # noqa: E402
    MIN_TICK_SECONDS,
    JobSpec,
    JobStore,
    ScheduleError,
    Scheduler,
)


class _Clock:
    """可手动推进的时钟（注入 `Scheduler(now=...)`）。"""

    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class _FakePool:
    """记录被投递的 prompt；可切换成「永远失败」以测崩溃安全。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.submitted: list[str] = []
        self.fail = fail
        self._n = 0

    def submit(self, prompt: str) -> str:
        if self.fail:
            raise RuntimeError("pool boom")
        self._n += 1
        task_id = f"task{self._n:08d}"
        self.submitted.append(prompt)
        return task_id


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def actions(self) -> list[str]:
        return [f["action"] for e, f in self.events if e == "schedule.job"]


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.db = self.ws / ".vibe" / "scheduler.db"
        self.clock = _Clock(1_700_000_000.0)
        self.pool = _FakePool()
        self.audit = _FakeAudit()
        self._stores: list[JobStore] = []

    def tearDown(self) -> None:
        for store in self._stores:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()

    def _store(self) -> JobStore:
        store = JobStore(self.db)
        self._stores.append(store)
        return store

    def _sched(self, **kw) -> Scheduler:
        opts = dict(
            pool=self.pool,
            store=self._store(),
            audit=self.audit,
            tick=30,
            max_per_tick=3,
            max_jobs=50,
            catch_up=True,
            now=self.clock,
        )
        opts.update(kw)
        return Scheduler(**opts)

    def _due(self, scheduler: Scheduler, job_id: str) -> None:
        """把某 job 的 next_run_at 挪到过去（模拟到期 / 逾期）。"""
        job = scheduler._jobs[job_id]  # noqa: SLF001
        scheduler._jobs[job_id] = replace(  # noqa: SLF001
            job, next_run_at=self.clock.t - 600
        )


class AddJobTests(_Harness):
    def test_add_and_list_metadata_only(self) -> None:
        s = self._sched()
        job = s.add_job("每晚测试", "@every 1h", "跑一遍测试并汇报")
        self.assertEqual(len(job.job_id), 12)
        self.assertTrue(job.enabled)
        self.assertGreater(job.next_run_at, self.clock.t)

        jobs = s.list_jobs()
        self.assertEqual(len(jobs), 1)
        blob = json.dumps(jobs, ensure_ascii=False)
        self.assertNotIn("跑一遍测试", blob)
        self.assertNotIn("prompt", jobs[0])

        # 落库了
        self.assertEqual(len(self._stores[-1].load_all()), 1)
        # 审计只记元数据
        events = [f for e, f in self.audit.events if e == "schedule.job"]
        self.assertEqual(events[0]["action"], "created")
        self.assertEqual(events[0]["job_id"], job.job_id)
        self.assertEqual(events[0]["chars"], len("跑一遍测试并汇报"))
        self.assertNotIn("跑一遍测试", json.dumps(events, ensure_ascii=False))

    def test_add_invalid_schedule_raises(self) -> None:
        s = self._sched()
        with self.assertRaises(ScheduleError):
            s.add_job("x", "99 9 * * *", "p")
        with self.assertRaises(ValueError):
            s.add_job("   ", "@every 1h", "p")

    def test_add_empty_prompt_raises(self) -> None:
        s = self._sched()
        with self.assertRaises(ValueError):
            s.add_job("x", "@every 1h", "   ")

    def test_max_jobs_limit(self) -> None:
        s = self._sched(max_jobs=2)
        s.add_job("a", "@every 1h", "pa")
        s.add_job("b", "@every 1h", "pb")
        with self.assertRaises(ValueError):
            s.add_job("c", "@every 1h", "pc")

    def test_tick_floor(self) -> None:
        s = self._sched(tick=1)
        self.assertEqual(s.status()["tick"], MIN_TICK_SECONDS)


class TickTests(_Harness):
    def test_only_due_jobs_fire(self) -> None:
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa").job_id
        # 未到点 → 不触发
        self.assertEqual(s.tick_once(), 0)
        self.assertEqual(self.pool.submitted, [])
        # 推进时钟越过 next_run_at → 触发
        self.clock.t = s._jobs[jid].next_run_at + 1  # noqa: SLF001
        self.assertEqual(s.tick_once(), 1)
        self.assertEqual(self.pool.submitted, ["pa"])
        after = s._jobs[jid]  # noqa: SLF001
        self.assertEqual(after.run_count, 1)
        self.assertEqual(len(after.last_task_id), 12)
        self.assertEqual(after.last_state, "triggered")
        self.assertGreater(after.next_run_at, self.clock.t)  # 重排到未来

    def test_disabled_job_never_fires(self) -> None:
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa", enabled=False).job_id
        self._due(s, jid)
        self.assertEqual(s.tick_once(), 0)
        self.assertEqual(self.pool.submitted, [])

    def test_max_per_tick_throttles(self) -> None:
        """单轮最多 max_per_tick 个 —— 余下留到下一轮。"""
        s = self._sched(max_per_tick=3)
        ids = [s.add_job(f"j{i}", "@every 1h", f"p{i}").job_id for i in range(5)]
        for jid in ids:
            self._due(s, jid)
        self.assertEqual(s.tick_once(), 3)
        self.assertEqual(s.tick_once(), 2)
        self.assertEqual(s.tick_once(), 0)
        self.assertEqual(len(self.pool.submitted), 5)

    def test_fire_failure_does_not_kill_scheduler(self) -> None:
        """投递失败只把该 job 记 failed，不影响后续触发。"""
        self.pool.fail = True
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa").job_id
        self._due(s, jid)
        self.assertEqual(s.tick_once(), 1)  # 仍算「尝试触发过」
        self.assertTrue(s._jobs[jid].last_state.startswith("failed"))  # noqa: SLF001
        self.assertEqual(s._jobs[jid].run_count, 0)  # noqa: SLF001
        self.assertEqual(self.audit.actions()[-1], "failed")

    def test_audit_has_no_prompt(self) -> None:
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "秘密-SECRET-正文").job_id
        self._due(s, jid)
        s.tick_once()
        blob = json.dumps([f for _, f in self.audit.events], ensure_ascii=False)
        self.assertNotIn("SECRET", blob)


class CatchUpTests(_Harness):
    def _seed_overdue(self, *, run_count: int = 0, last_task_id: str = "") -> str:
        store = self._store()
        job = JobSpec(
            job_id="overdue-job1",
            name="逾期任务",
            schedule="@every 1h",
            prompt="补跑内容",
            enabled=True,
            created_at=self.clock.t - 7200,
            next_run_at=self.clock.t - 600,  # 已逾期
            last_run_at=0.0,
            last_task_id=last_task_id,
            last_state="",
            run_count=run_count,
        )
        store.upsert(job)
        return job.job_id

    def test_catch_up_once_on_start(self) -> None:
        """逾期 job 各补跑一次，随后重排到未来（不连补多次）。"""
        self._seed_overdue()
        s = self._sched(catch_up=True)
        s.start()
        try:
            jobs = s.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["runCount"], 1)  # 恰好一次
            self.assertGreater(jobs[0]["nextRunAt"], self.clock.t)
            self.assertEqual(len(self.pool.submitted), 1)
            self.assertEqual(self.audit.actions(), ["triggered"])
        finally:
            s.stop()

    def test_catch_up_disabled_records_skipped(self) -> None:
        """关掉补跑 → 不触发、记账 skipped、run_count 不变。"""
        self._seed_overdue(run_count=7)
        s = self._sched(catch_up=False)
        s.start()
        try:
            jobs = s.list_jobs()
            self.assertEqual(jobs[0]["runCount"], 7)
            self.assertEqual(jobs[0]["lastState"], "skipped")
            self.assertEqual(self.pool.submitted, [])
            self.assertEqual(self.audit.actions(), ["skipped"])
        finally:
            s.stop()

    def test_start_is_idempotent_and_stop_clean(self) -> None:
        self._seed_overdue()
        s = self._sched()
        s.start()
        s.start()  # 幂等：不重复补跑
        try:
            self.assertEqual(len(self.pool.submitted), 1)
            self.assertTrue(s.status()["threadAlive"])
        finally:
            s.stop()
        self.assertFalse(s.status()["threadAlive"])


class RunNowTests(_Harness):
    def test_run_now_submits_without_touching_schedule(self) -> None:
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa").job_id
        nxt = s._jobs[jid].next_run_at  # noqa: SLF001
        tid = s.run_now(jid)
        self.assertEqual(len(tid), 12)
        job = s._jobs[jid]  # noqa: SLF001
        self.assertEqual(job.next_run_at, nxt)  # 排期没动
        self.assertEqual(job.last_task_id, tid)
        self.assertEqual(job.run_count, 1)
        self.assertEqual(job.last_state, "triggered")

    def test_run_now_missing_returns_none(self) -> None:
        s = self._sched()
        self.assertIsNone(s.run_now("nope"))


class UpdateToggleDeleteTests(_Harness):
    def test_update_reschedules_when_expression_changes(self) -> None:
        """改表达式 → 必强重排（否则会沿用旧表达式的下次触发点跑错时间）。"""
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa").job_id
        before = s._jobs[jid].next_run_at  # noqa: SLF001
        updated = s.update_job(jid, schedule="0 3 * * *")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.schedule, "0 3 * * *")
        self.assertNotEqual(updated.next_run_at, before)
        self.assertGreater(updated.next_run_at, self.clock.t)

    def test_update_missing_and_invalid(self) -> None:
        s = self._sched()
        self.assertIsNone(s.update_job("nope", name="x"))
        jid = s.add_job("a", "@every 1h", "pa").job_id
        with self.assertRaises(ScheduleError):
            s.update_job(jid, schedule="99 9 * * *")

    def test_toggle_enabled(self) -> None:
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa").job_id
        off = s.set_enabled(jid, False)
        self.assertFalse(off.enabled)
        on = s.set_enabled(jid, True)
        self.assertTrue(on.enabled)
        self.assertIsNone(s.set_enabled("nope", True))
        self.assertEqual(self.audit.actions(), ["created", "toggled", "toggled"])

    def test_delete_removes_from_store(self) -> None:
        s = self._sched()
        jid = s.add_job("a", "@every 1h", "pa").job_id
        self.assertTrue(s.remove_job(jid))
        self.assertFalse(s.remove_job(jid))
        self.assertEqual(s.list_jobs(), [])
        self.assertEqual(self._stores[-1].count(), 0)


class StatusTests(_Harness):
    def test_status_fields(self) -> None:
        s = self._sched(tick=30, max_per_tick=3, max_jobs=10)
        st = s.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["tick"], 30)
        self.assertEqual(st["maxPerTick"], 3)
        self.assertEqual(st["maxJobs"], 10)
        self.assertTrue(st["catchUp"])
        self.assertEqual(st["jobCount"], 0)
        self.assertEqual(st["skippedRows"], 0)
        self.assertFalse(st["threadAlive"])  # 未 start

    def test_status_reports_skipped_rows(self) -> None:
        store = self._store()
        with store._lock:  # noqa: SLF001
            store._conn.execute(  # noqa: SLF001
                "INSERT INTO jobs (job_id, name, schedule, prompt) VALUES (?,?,?,?)",
                ("bad", "坏", "99 9 * * *", "内容"),
            )
            store._conn.commit()  # noqa: SLF001
        s = self._sched(store=store)
        s._jobs.update({j.job_id: j for j in store.load_all()})  # noqa: SLF001
        self.assertEqual(s.status()["skippedRows"], 1)


if __name__ == "__main__":
    unittest.main()
