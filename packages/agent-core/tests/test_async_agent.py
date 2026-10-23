"""后台 / 并行 Agent（M9-D）测试。

验收要点（对齐 M9-PLAN E4）：
1. 任务池 submit/status/result/cancel 全链路可用；
2. 并行上限 `max_workers` 生效（池容量受控，不无限起线程）；
3. 子任务**只读工具面**（无写盘 / 执行工具）；
4. 审计链共用 + `task_id` 归属（`task.spawn` 事件带 task_id，只记元数据）；
5. 后台任务**由用户本人发起** —— 不新增 Agent 派生的 spawn 工具（`build_default_registry`
   不含任何后台派生工具，工具清单与改造前一致）；
6. `VIBE_ASYNC_AGENT` 未设时 `config.async_agent_enabled` 为 False、无任务池。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.async_agent import AgentTaskPool, TaskState  # noqa: E402
from agent_core.audit import verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.llm import ChatResponse  # noqa: E402
from agent_core.loop import Agent, AgentHooks  # noqa: E402
from agent_core.tools import build_default_registry, build_readonly_registry  # noqa: E402


class _FakeClient:
    supports_native_tools = True

    def __init__(self, content: str = "结论") -> None:
        self._content = content

    def chat(self, messages, tools=None, temperature=None):
        return ChatResponse(content=self._content)


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_agent(self, **overrides):
        config = Config.load(self.ws, **overrides)
        return Agent(config, client=_FakeClient(), hooks=AgentHooks())

    def _read_task_spawn_events(self, agent) -> list[dict]:
        path = agent.config.audit_path
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and "task.spawn" in line
        ]

    def _wait(self, pool: AgentTaskPool, task_id: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = pool.status(task_id)
            if st is not None and st["state"] in (
                TaskState.SUCCEEDED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            ):
                return st
            time.sleep(0.02)
        return pool.status(task_id) or {}


class ConfigTests(_Harness):
    def test_default_disabled(self) -> None:
        """VIBE_ASYNC_AGENT 未设时默认关。"""
        cfg = Config.load(self.ws)
        self.assertFalse(cfg.async_agent_enabled)
        self.assertEqual(cfg.async_agent_max, 4)
        self.assertEqual(cfg.async_agent_timeout, 600)

    def test_enabled_fields(self) -> None:
        cfg = Config.load(
            self.ws, async_agent_enabled=True, async_agent_max=8, async_agent_timeout=30
        )
        self.assertTrue(cfg.async_agent_enabled)
        self.assertEqual(cfg.async_agent_max, 8)
        self.assertEqual(cfg.async_agent_timeout, 30)


class NoSpawnToolTests(_Harness):
    def test_no_agent_spawn_tool(self) -> None:
        """后台任务由用户本人发起 —— 工具清单里没有派生工具（D3 关键决策）。"""
        reg = build_default_registry()
        names = set(reg.names())
        self.assertNotIn("spawn_task", names)
        self.assertNotIn("spawn", names)
        self.assertNotIn("async_task", names)

    def test_readonly_registry_has_no_write(self) -> None:
        """子任务只读工具面：无写盘 / 执行工具。"""
        ro = build_readonly_registry()
        names = set(ro.names())
        for forbidden in ("edit_file", "write_file", "edit_files", "run_command", "task"):
            self.assertNotIn(forbidden, names)


class PoolTests(_Harness):
    def test_submit_status_result_cancel(self) -> None:
        agent = self._make_agent(async_agent_enabled=True)
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            tid = pool.submit("分析项目结构")
            self.assertEqual(len(tid), 12)

            st = self._wait(pool, tid)
            self.assertEqual(st["state"], TaskState.SUCCEEDED, st)
            self.assertGreater(st["chars"], 0)

            # result 含 wrap_untrusted 包裹的结论
            result = pool.result(tid)
            self.assertIsNotNone(result)
            self.assertIn("UNTRUSTED_DATA", result["result"])
            self.assertIn("结论", result["result"])
        finally:
            pool.shutdown()

    def test_list_tasks_omits_prompt_and_result(self) -> None:
        """列表只回摘要，不含任务正文与结论（ADR-006）。"""
        agent = self._make_agent(async_agent_enabled=True)
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            tid = pool.submit("秘密任务内容-SECRET-PROMPT")
            self._wait(pool, tid)
            lst = pool.list_tasks()
            self.assertEqual(len(lst), 1)
            blob = json.dumps(lst, ensure_ascii=False)
            self.assertNotIn("SECRET-PROMPT", blob)
            self.assertNotIn("prompt", lst[0])
        finally:
            pool.shutdown()

    def test_empty_prompt_raises(self) -> None:
        agent = self._make_agent(async_agent_enabled=True)
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            with self.assertRaises(ValueError):
                pool.submit("   ")
        finally:
            pool.shutdown()

    def test_cancel_cooperative(self) -> None:
        """cancel 对已完成任务返回 False，对运行中/待定返回 True。"""
        agent = self._make_agent(async_agent_enabled=True)
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            tid = pool.submit("分析项目结构")
            # 提交后立刻 cancel（此时大概率 pending/running）
            self.assertTrue(pool.cancel(tid))
            # 已完成/取消后再 cancel 返回 False（最终态不可再取消）
            self._wait(pool, tid)
            self.assertFalse(pool.cancel(tid))
            # 不存在的任务返回 False
            self.assertFalse(pool.cancel("deadbeefdead"))
        finally:
            pool.shutdown()


class AuditTests(_Harness):
    def test_task_spawn_records_metadata_only(self) -> None:
        agent = self._make_agent(async_agent_enabled=True)
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            tid = pool.submit("秘密任务内容-SECRET")
            self._wait(pool, tid)

            events = self._read_task_spawn_events(agent)
            self.assertEqual(len(events), 2)  # running + succeeded
            self.assertEqual(events[0]["state"], "running")
            self.assertEqual(events[1]["state"], "succeeded")
            # 两条事件都带 task_id 归属
            for ev in events:
                self.assertEqual(ev["task_id"], tid)
            # 只记元数据：不含任务正文
            blob = json.dumps(events, ensure_ascii=False)
            self.assertNotIn("SECRET", blob)
        finally:
            pool.shutdown()

    def test_shared_audit_chain_verifies(self) -> None:
        """审计链共用父实例 —— 后台任务并发写后链仍可验证。"""
        agent = self._make_agent(async_agent_enabled=True)
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            tids = [pool.submit(f"任务{i}") for i in range(3)]
            for tid in tids:
                self._wait(pool, tid)
            ok, _ = verify_chain(agent.config.audit_path)
            self.assertTrue(ok)
        finally:
            pool.shutdown()

    def test_failed_task_records_failed_state(self) -> None:
        """后台任务异常收敛为 failed 状态，不弄挂主会话。"""
        agent = self._make_agent(async_agent_enabled=True)

        class _BoomClient:
            supports_native_tools = True

            def chat(self, messages, tools=None, temperature=None):
                raise RuntimeError("boom")

        agent = Agent(
            Config.load(self.ws, async_agent_enabled=True),
            client=_BoomClient(),
            hooks=AgentHooks(),
        )
        pool = AgentTaskPool(parent=agent, max_workers=2, timeout=10)
        try:
            tid = pool.submit("会失败的任务")
            st = self._wait(pool, tid)
            self.assertEqual(st["state"], TaskState.FAILED, st)
            events = self._read_task_spawn_events(agent)
            self.assertEqual(events[-1]["state"], "failed")
        finally:
            pool.shutdown()


if __name__ == "__main__":
    unittest.main()
