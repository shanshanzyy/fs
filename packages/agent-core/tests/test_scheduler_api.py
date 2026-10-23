"""定时任务 API / 宿主接线（M10 Part E）：会话门面 + 事件 + 落盘 + 重启补跑。

验收要点（对齐 M10-PLAN D / E）：
1. `VIBE_SCHEDULER=0` 时门面 fail-closed（无调度器、无端点、无库文件）；
2. 打开态门面全链路：create / list / update / toggle / runNow / delete；
3. `schedule.state` 事件广播 + 列表 / 事件**只回元数据**（不含 prompt 正文）；
4. `schedule.job` 审计**只记元数据**；
5. **重启补跑恰好一次**并重排到未来；关掉补跑则记 `skipped`；
6. **依赖 M9-D 池**：只开调度不开后台 → 不起调度器并明说原因（fail-closed）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.errors import AgentError  # noqa: E402
from agent_core.host import protocol as P  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.llm import ChatResponse  # noqa: E402
from agent_core.scheduler import JobStore  # noqa: E402


class _FakeClient:
    supports_native_tools = True

    def chat(self, messages, tools=None, temperature=None):
        return ChatResponse(content="后台任务的结论")


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self._sessions: list[Session] = []

    def tearDown(self) -> None:
        # 先关会话（否则 Windows 下 sqlite 未 close 会锁住 scheduler.db，
        # 临时目录清不掉）。
        for sess in self._sessions:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()

    def _open(self, **overrides) -> Session:
        cfg = Config.load(self.ws, **overrides)
        sess = Session(cfg, client=_FakeClient())
        self._sessions.append(sess)
        return sess

    def _drain(self, sub, timeout: float = 0.4) -> list[dict]:
        out: list[dict] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            ev = sub.get(timeout=0.05)
            if ev is None:
                continue
            out.append(ev)
        return out

    def _wait_task(self, sess: Session, task_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = sess.task_status(task_id)
            if st and st["state"] in ("succeeded", "failed", "cancelled"):
                return st
            time.sleep(0.02)
        return sess.task_status(task_id) or {}


class DisabledTests(_Harness):
    def test_disabled_by_default_is_fail_closed(self) -> None:
        cfg = Config.load(self.ws)
        self.assertFalse(cfg.scheduler_enabled)
        self.assertEqual(cfg.scheduler_tick, 30)
        self.assertEqual(cfg.scheduler_max_jobs, 50)
        self.assertEqual(cfg.scheduler_max_per_tick, 3)
        self.assertTrue(cfg.scheduler_catch_up)
        self.assertEqual(cfg.scheduler_db_path, self.ws / ".vibe" / "scheduler.db")

        sess = self._open()
        self.assertIsNone(sess.scheduler)
        self.assertEqual(sess.job_list(), [])
        self.assertIsNone(sess.schedule_status())
        for call in (
            lambda: sess.job_create("x", "* * * * *", "p"),
            lambda: sess.job_delete("x"),
            lambda: sess.job_run_now("x"),
        ):
            with self.assertRaises(AgentError) as ctx:
                call()
            self.assertIn("VIBE_SCHEDULER", str(ctx.exception))
        # 未启用不落库文件
        self.assertFalse((self.ws / ".vibe" / "scheduler.db").exists())

    def test_protocol_registers_event_and_endpoints(self) -> None:
        self.assertEqual(P.SCHEDULE_STATE, "schedule.state")
        text = Path(P.__file__).read_text(encoding="utf-8")
        for path in (
            "/api/schedules",
            "/api/schedules/{id}",
            "/api/schedules/{id}/delete",
            "/api/schedules/{id}/run",
        ):
            self.assertIn(path, text)


class EnabledTests(_Harness):
    def _open_sched(self) -> Session:
        return self._open(
            async_agent_enabled=True,
            scheduler_enabled=True,
            scheduler_tick=5,
            scheduler_catch_up=True,
        )

    def test_full_facade_chain(self) -> None:
        sess = self._open_sched()
        self.assertIsNotNone(sess.scheduler)
        db = sess.agent.config.scheduler_db_path
        self.assertEqual(db, self.ws / ".vibe" / "scheduler.db")
        self.assertTrue(db.exists())
        sub = sess.bus.subscribe()

        created = sess.job_create("每晚测试", "@every 1h", "跑一遍测试并汇报")
        self.assertTrue(created["ok"])
        jid = created["jobId"]
        self.assertEqual(len(jid), 12)

        jobs = sess.job_list()
        self.assertEqual(len(jobs), 1)
        blob = json.dumps(jobs, ensure_ascii=False)
        self.assertNotIn("跑一遍测试", blob)
        self.assertNotIn("prompt", jobs[0])

        st = sess.schedule_status()
        self.assertTrue(st["enabled"])
        self.assertTrue(st["threadAlive"])
        self.assertEqual(st["jobCount"], 1)

        evs = [e for e in self._drain(sub) if e.get("type") == P.SCHEDULE_STATE]
        self.assertTrue(
            any(e["action"] == "created" and e["jobId"] == jid for e in evs), evs
        )
        self.assertTrue(all("prompt" not in e for e in evs))

        before = sess.job_list()[0]["nextRunAt"]
        upd = sess.job_update(jid, schedule="0 3 * * *")
        self.assertIsNotNone(upd)
        self.assertEqual(upd["schedule"], "0 3 * * *")
        self.assertNotEqual(upd["nextRunAt"], before)
        self.assertGreater(upd["nextRunAt"], time.time())
        self.assertIsNone(sess.job_update("不存在的id", name="x"))

        self.assertFalse(sess.job_toggle(jid, False)["enabled"])
        self.assertTrue(sess.job_toggle(jid, True)["enabled"])

        nxt = sess.job_list()[0]["nextRunAt"]
        tid = sess.job_run_now(jid)
        self.assertEqual(len(tid), 12)
        after = sess.job_list()[0]
        self.assertEqual(after["nextRunAt"], nxt)  # run_now 不改排期
        self.assertEqual(after["lastTaskId"], tid)
        self.assertEqual(after["runCount"], 1)
        self.assertEqual(after["lastState"], "triggered")
        self.assertIsNone(sess.job_run_now("不存在的id"))

        # 后台任务确实跑起来了
        st = self._wait_task(sess, tid)
        self.assertEqual(st["state"], "succeeded", st)

        # 删除
        self.assertTrue(sess.job_delete(jid))
        self.assertFalse(sess.job_delete(jid))
        self.assertEqual(sess.job_list(), [])

    def test_audit_metadata_only(self) -> None:
        sess = self._open_sched()
        sess.job_create("秘密名", "@every 1h", "秘密-SECRET-正文")
        audit = sess.agent.config.audit_path.read_text(encoding="utf-8")
        self.assertIn("schedule.job", audit)
        self.assertNotIn("SECRET", audit)

    def test_invalid_inputs_raise_agent_error(self) -> None:
        sess = self._open_sched()
        for bad in ("99 9 * * *", "not a cron"):
            with self.assertRaises(AgentError):
                sess.job_create("x", bad, "p")
        with self.assertRaises(AgentError):
            sess.job_create("   ", "@every 1h", "p")
        with self.assertRaises(AgentError):
            sess.job_create("x", "@every 1h", "   ")


class RestartCatchUpTests(_Harness):
    def _open_sched(self) -> Session:
        return self._open(
            async_agent_enabled=True,
            scheduler_enabled=True,
            scheduler_tick=5,
            scheduler_catch_up=True,
        )

    def _make_overdue(self, db: Path, *, run_count: int = 0) -> None:
        store = JobStore(db)
        try:
            rows = store.load_all()
            self.assertTrue(rows)
            store.upsert(
                replace(
                    rows[0],
                    next_run_at=time.time() - 600,
                    run_count=run_count,
                    last_state="",
                )
            )
        finally:
            store.close()

    def test_restart_catches_up_once_and_reschedules(self) -> None:
        sess = self._open_sched()
        db = sess.agent.config.scheduler_db_path
        sess.job_create("每晚测试", "@every 1h", "跑一遍测试并汇报")
        sess.close()

        self._make_overdue(db)
        sess2 = self._open_sched()
        jobs = sess2.job_list()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["runCount"], 1, jobs[0])  # 恰好一次
        self.assertGreater(jobs[0]["nextRunAt"], time.time())  # 重排到未来

    def test_catch_up_disabled_records_skipped(self) -> None:
        sess = self._open_sched()
        db = sess.agent.config.scheduler_db_path
        sess.job_create("每晚测试", "@every 1h", "跑一遍测试并汇报")
        sess.close()

        self._make_overdue(db, run_count=7)
        sess2 = self._open(
            async_agent_enabled=True,
            scheduler_enabled=True,
            scheduler_tick=5,
            scheduler_catch_up=False,
        )
        jobs = sess2.job_list()
        self.assertEqual(jobs[0]["runCount"], 7)  # 不补跑
        self.assertEqual(jobs[0]["lastState"], "skipped")
        audit = sess2.agent.config.audit_path.read_text(encoding="utf-8")
        self.assertIn('"action": "skipped"', audit)


class DependencyTests(_Harness):
    def test_scheduler_without_async_pool_is_not_started(self) -> None:
        """只开调度不开后台 → 不起调度器 + notices 明说原因（fail-closed）。"""
        sess = self._open(scheduler_enabled=True, async_agent_enabled=False)
        self.assertIsNone(sess.scheduler)
        view = sess.describe()
        self.assertFalse(view["config"]["schedulerEnabled"])
        self.assertTrue(
            any("VIBE_ASYNC_AGENT" in n for n in view["notices"]), view["notices"]
        )
        self.assertFalse((self.ws / ".vibe" / "scheduler.db").exists())


if __name__ == "__main__":
    unittest.main()
