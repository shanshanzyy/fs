"""测试驱动的自动修复闭环（M9-C）测试。

验收要点：
1. `test_loop` 工具恒 SAFE（只调度既有工具，不直接写盘/执行）；
2. 测试通过时闭环成功收口（rounds 计数、审计 passed=1）；
3. 测试失败时把失败摘要回灌为 user 消息，提示「修复后再次调用」；
4. `max_rounds` 到顶仍未绿时**如实报告并停止**（不死循环）；
5. `test_loop` 不进 build_readonly_registry（子 Agent 不承担自动修复）；
6. 审计只记 digest / 计数（command_digest / rounds / passed / failed / ms），不含输出正文；
7. `VIBE_TEST_LOOP` 未设时不注册工具、行为逐字节一致。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.llm import ChatResponse, ToolCall  # noqa: E402
from agent_core.loop import Agent, AgentHooks  # noqa: E402
from agent_core.sandbox import Risk  # noqa: E402
from agent_core.tools import (  # noqa: E402
    TEST_LOOP_SPEC,
    build_default_registry,
    build_readonly_registry,
)
from agent_core.tools.test_loop import _test_loop  # noqa: E402


class _FakeClient:
    supports_native_tools = True

    def chat(self, messages, tools=None, temperature=None):
        return ChatResponse(content="完成")


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_agent(self, **overrides):
        config = Config.load(self.ws, **overrides)
        return Agent(config, client=_FakeClient(), hooks=AgentHooks())


class RegistryTests(_Harness):
    def test_default_off(self) -> None:
        """VIBE_TEST_LOOP 未设时，工具清单不含 test_loop。"""
        reg = build_default_registry()
        self.assertNotIn("test_loop", reg.names())

    def test_enabled_registers(self) -> None:
        reg = build_default_registry(test_loop=True)
        self.assertIn("test_loop", reg.names())

    def test_not_in_readonly_registry(self) -> None:
        """test_loop 不进只读注册表 —— 子 Agent 不承担自动修复。"""
        ro = build_readonly_registry(lsp=False)
        self.assertNotIn("test_loop", ro.names())

    def test_spec_is_safe(self) -> None:
        self.assertIs(TEST_LOOP_SPEC.risk, Risk.SAFE)


class ToolUnitTests(_Harness):
    def test_missing_sink_fails_clearly(self) -> None:
        """直接调 registry 无 sink 时，如实报「未启用」。"""
        agent = self._make_agent()  # 默认关，无 test_loop_sink
        result = _test_loop(agent.ctx, {"command": "pytest -q"})
        self.assertFalse(result.ok)
        self.assertIn("未启用", result.content)

    def test_missing_command_raises(self) -> None:
        from agent_core.errors import ToolError

        agent = self._make_agent(test_loop_enabled=True)
        with self.assertRaises(ToolError):
            _test_loop(agent.ctx, {})


class TestLoopRunTests(_Harness):
    """直接驱动 Agent._run_test_loop，mock 掉 subprocess.run 精确控制测试结果。"""

    def _run(self, agent, command, max_rounds, returncode, stdout="", stderr=""):
        call = ToolCall(id="c1", name="test_loop", arguments={"command": command})
        completed = subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )
        with mock.patch("agent_core.loop.agent.subprocess.run", return_value=completed):
            return agent._run_test_loop(call, command, max_rounds)

    def test_pass_records_success(self) -> None:
        agent = self._make_agent(test_loop_enabled=True)
        result = self._run(agent, "git status", 3, returncode=0)
        self.assertTrue(result.ok)
        self.assertIn("测试通过", result.content)
        # 审计 passed=1 / failed=0
        event = self._last_test_loop_event(agent)
        self.assertEqual(event["passed"], 1)
        self.assertEqual(event["failed"], 0)
        self.assertNotIn("command", event)  # 只有 command_digest

    def test_fail_feeds_back_and_prompts_retry(self) -> None:
        agent = self._make_agent(test_loop_enabled=True)
        result = self._run(
            agent, "git status", 3, returncode=1,
            stdout="1 failed", stderr="AssertionError",
        )
        self.assertFalse(result.ok)
        self.assertIn("再次调用 test_loop", result.content)
        # 失败摘要已回灌为 user 消息
        user_msgs = [m for m in agent.messages if m.get("role") == "user"]
        self.assertTrue(any("测试闭环" in str(m.get("content")) for m in user_msgs))
        event = self._last_test_loop_event(agent)
        self.assertEqual(event["passed"], 0)
        self.assertEqual(event["failed"], 1)

    def test_max_rounds_stops_cleanly(self) -> None:
        agent = self._make_agent(test_loop_enabled=True)
        result = self._run(
            agent, "git status", 1, returncode=1,
            stdout="fail", stderr="",
        )
        self.assertFalse(result.ok)
        self.assertIn("重试上限", result.content)

    def test_command_denied_records_audit(self) -> None:
        agent = self._make_agent(test_loop_enabled=True)
        call = ToolCall(id="c1", name="test_loop", arguments={"command": "rm -rf /"})
        result = agent._run_test_loop(call, "rm -rf /", 3)
        self.assertFalse(result.ok)
        self.assertIn("安全策略拒绝", result.content)

    def _last_test_loop_event(self, agent) -> dict:
        path = agent.config.audit_path
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if "test_loop.run" in line:
                return json.loads(line)
        raise AssertionError("未找到 test_loop.run 审计事件")


class AuditFieldTests(_Harness):
    def test_audit_omits_command_body(self) -> None:
        """审计事件里只有 command_digest，没有命令原文。"""
        agent = self._make_agent(test_loop_enabled=True)
        call = ToolCall(id="c1", name="test_loop", arguments={"command": "echo hello"})
        agent._run_test_loop(call, "echo secret-password-here", 3)
        path = agent.config.audit_path
        if not path.exists():
            self.skipTest("无审计输出")
        for line in path.read_text(encoding="utf-8").splitlines():
            if "test_loop.run" not in line:
                continue
            data = json.loads(line)
            self.assertNotIn("secret-password-here", json.dumps(data, ensure_ascii=False))
            self.assertIn("command_digest", data)


if __name__ == "__main__":
    unittest.main()
