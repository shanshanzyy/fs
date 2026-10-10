"""跨会话记忆（P2-15）测试。

四条纪律的落地验证：
1. 写入恒人审：`remember` 工具是 ``Risk.REVIEW``（「必须人审」用例是反向验证的锚点）;
2. 注入一律 `wrap_untrusted`：拼进系统提示的记忆正文必须被不可信块包裹；
3. 检索有上限：按词法相关度取 top-N，不相关不注入；
4. `VIBE_MEMORY=0` 时**不读不写**：不建库、不注入、不注册工具（行为与未实现前一致）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.loop import Agent  # noqa: E402
from agent_core.memory import (  # noqa: E402
    MAX_TEXT_CHARS,
    MemoryLimitExceeded,
    MemoryStore,
)
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.tools import ToolContext, build_default_registry  # noqa: E402
from agent_core.tools.base import UNTRUSTED_BEGIN  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

REMEMBER = "remember"


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self._stores: list[MemoryStore] = []

    def tearDown(self) -> None:
        # 先关连接再删目录：Windows 下未关闭的 sqlite 会锁住库文件，
        # 让临时目录清理报 PermissionError，变成假的测试失败。
        for store in self._stores:
            store.close()
        self._tmp.cleanup()

    def _store(self, name: str = "mem.db", **kwargs) -> MemoryStore:
        store = MemoryStore(self.ws / name, **kwargs)
        self._stores.append(store)
        return store

    def _ctx(self, store: MemoryStore | None) -> tuple[ToolContext, Config]:
        config = Config.load(self.ws, memory_enabled=False)
        audit = AuditLog(
            config.audit_path, session_id=config.session_id, enabled=config.audit_enabled
        )
        ctx = ToolContext(
            config=config,
            paths=PathGuard(config.workspace),
            commands=CommandGuard(config.workspace),
            audit=audit,
            memory_store=store,
        )
        return ctx, config

    def _events(self, config: Config, name: str) -> list[dict]:
        path = config.audit_path
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("event") == name:
                out.append(entry)
        return out


class MemoryStoreTests(_Harness):
    def test_add_list_count_remove(self) -> None:
        store = self._store()
        rec = store.add("本仓库用 pytest -q 跑测试", tags=["test", "pytest"])
        self.assertEqual(store.count(), 1)
        self.assertEqual(rec.scope, "user")
        self.assertEqual(rec.tags, ("test", "pytest"))
        listed = store.list()
        self.assertEqual([r.id for r in listed], [rec.id])
        self.assertTrue(store.remove(rec.id))
        self.assertEqual(store.count(), 0)
        self.assertFalse(store.remove(rec.id))

    def test_empty_text_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._store().add("   ")

    def test_overlong_text_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._store().add("长" * (MAX_TEXT_CHARS + 1))

    def test_max_items_enforced(self) -> None:
        store = self._store(max_items=2)
        store.add("一")
        store.add("二")
        with self.assertRaises(MemoryLimitExceeded):
            store.add("三")

    def test_search_ranks_by_token_overlap(self) -> None:
        store = self._store()
        store.add("本仓库用 pytest 跑测试", tags=["test"])
        store.add("提交信息用中文")
        hits = store.search("pytest 怎么配置")
        self.assertEqual(len(hits), 1)
        self.assertIn("pytest", hits[0].text)

    def test_search_ignores_unrelated(self) -> None:
        store = self._store()
        store.add("本仓库用 pytest 跑测试")
        self.assertEqual(store.search("画一个圆形"), [])

    def test_search_limit_caps_results(self) -> None:
        store = self._store()
        for i in range(5):
            store.add(f"pytest 备注 {i}")
        self.assertEqual(len(store.search("pytest", limit=2)), 2)
        self.assertEqual(store.search("pytest", limit=0), [])


class RememberToolTests(_Harness):
    def test_remember_requires_review(self) -> None:
        spec = build_default_registry(memory=True).get(REMEMBER)
        assert spec is not None
        # 写入记忆会被注入后续每轮提示，是持久化注入面 —— 恒人审（ADR-008）
        self.assertEqual(spec.risk, Risk.REVIEW)

    def test_remember_not_registered_when_memory_disabled(self) -> None:
        self.assertNotIn(REMEMBER, build_default_registry(memory=False).names())
        self.assertIn(REMEMBER, build_default_registry(memory=True).names())

    def test_remember_writes_and_audits(self) -> None:
        store = self._store()
        ctx, config = self._ctx(store)
        registry = build_default_registry(memory=True)
        result = registry.execute(
            ctx, REMEMBER, {"text": "本仓库用 pytest -q 跑测试", "tags": ["test"]}
        )
        self.assertTrue(result.ok, result.content)
        self.assertEqual(store.count(), 1)
        self.assertEqual(store.list()[0].scope, "agent")
        events = self._events(config, "memory.add")
        self.assertEqual(len(events), 1)
        # 审计只记元数据：正文（text）绝不出现
        self.assertNotIn("text", events[0])
        self.assertEqual(events[0]["chars"], len("本仓库用 pytest -q 跑测试"))

    def test_remember_without_store_reports_disabled(self) -> None:
        ctx, _ = self._ctx(None)
        registry = build_default_registry(memory=True)
        result = registry.execute(ctx, REMEMBER, {"text": "随便记点什么"})
        self.assertFalse(result.ok)
        self.assertIn("未启用", result.content)


class AgentMemoryTests(_Harness):
    def _agent(self, steps, store: MemoryStore | None, **overrides) -> Agent:
        config = Config.load(self.ws, memory_enabled=True, memory_db="", **overrides)
        return Agent(
            config,
            client=FakeClient(steps),
            approver=lambda request: "approve",
            memory=store,
        )

    def test_injection_is_wrapped_untrusted(self) -> None:
        store = self._store()
        store.add("本仓库用 pytest -q 跑测试", tags=["test"])
        agent = self._agent(["完成"], store)
        agent.run("帮我看下 pytest 怎么配")
        content = agent.messages[0]["content"]
        self.assertIn("## 记忆", content)
        self.assertIn("pytest", content)
        # 记忆正文属不可信内容：进提示词前必须被包裹
        self.assertIn(UNTRUSTED_BEGIN, content)

    def test_unrelated_task_gets_no_memory(self) -> None:
        store = self._store()
        store.add("本仓库用 pytest -q 跑测试")
        agent = self._agent(["完成"], store)
        agent.run("画一个圆形")
        self.assertNotIn("## 记忆", agent.messages[0]["content"])

    def test_injection_respects_limit_and_audits(self) -> None:
        store = self._store()
        for i in range(4):
            store.add(f"pytest 备注 {i}")
        agent = self._agent(["完成"], store, memory_inject_limit=2)
        agent.run("pytest")
        events = self._events(agent.config, "memory.inject")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["count"], 2)
        self.assertNotIn("text", events[0])

    def test_reset_restores_base_prompt(self) -> None:
        store = self._store()
        store.add("本仓库用 pytest 跑测试")
        agent = self._agent(["完成"], store)
        agent.run("pytest")
        self.assertIn("## 记忆", agent.messages[0]["content"])
        agent.reset()
        self.assertEqual(agent.messages[0]["content"], agent._base_system_prompt)
        self.assertNotIn("## 记忆", agent.messages[0]["content"])

    def test_disabled_does_not_touch_memory(self) -> None:
        db = self.ws / "should-not-exist.db"
        config = Config.load(self.ws, memory_enabled=False, memory_db=str(db))
        agent = Agent(config, client=FakeClient(["完成"]), approver=lambda r: "approve")
        self.assertIsNone(agent.memory)
        self.assertNotIn(REMEMBER, agent.registry.names())
        agent.run("pytest 怎么配")
        self.assertNotIn("## 记忆", agent.messages[0]["content"])
        self.assertFalse(db.exists(), "关闭记忆时不应创建记忆库文件")


if __name__ == "__main__":
    unittest.main()