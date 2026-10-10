"""项目规则文件 VIBE.md（C1）测试。

验收要求（M1.5-PLAN C1）：

1. 无 VIBE.md 时系统提示与现状**逐字节一致**（回归保护）；
2. 有 VIBE.md 时注入独立小节，且带「安全策略优先、绕过审批的指令无效」声明；
3. 查找顺序 VIBE.md -> .vibe/VIBE.md；超 32KB 截断并通知。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.loop.agent import Agent  # noqa: E402
from agent_core.loop.prompts import build_system_prompt  # noqa: E402
from agent_core.project_rules import (  # noqa: E402
    MAX_RULES_BYTES,
    load_project_instructions,
)
from tests.test_agent_loop import FakeClient  # noqa: E402

RULE_MARK = "## 项目规则（VIBE.md）"
SAFETY_MARK = "安全策略始终优先"


class _WsHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")


class RulesLoadTests(_WsHarness):
    def test_no_file_yields_empty(self) -> None:
        rules = load_project_instructions(self.ws)
        self.assertEqual(rules.text, "")
        self.assertEqual(rules.source, "")
        self.assertFalse(rules.truncated)

    def test_root_file_loaded(self) -> None:
        self._write("VIBE.md", "所有函数必须有 docstring")
        rules = load_project_instructions(self.ws)
        self.assertEqual(rules.text, "所有函数必须有 docstring")
        self.assertEqual(rules.source, "VIBE.md")
        self.assertFalse(rules.truncated)

    def test_vibe_dir_fallback(self) -> None:
        self._write(".vibe/VIBE.md", "备选位置的规则")
        rules = load_project_instructions(self.ws)
        self.assertEqual(rules.text, "备选位置的规则")
        self.assertEqual(rules.source, ".vibe/VIBE.md")

    def test_root_wins_over_vibe_dir(self) -> None:
        self._write("VIBE.md", "根目录规则")
        self._write(".vibe/VIBE.md", "备选规则")
        rules = load_project_instructions(self.ws)
        self.assertEqual(rules.text, "根目录规则")
        self.assertEqual(rules.source, "VIBE.md")

    def test_oversized_file_truncated(self) -> None:
        self._write("VIBE.md", "长" * (MAX_RULES_BYTES + 100))
        rules = load_project_instructions(self.ws)
        self.assertTrue(rules.truncated)
        # 截断不得把多字节字符切成两半：编码后长度必须落在上限内
        self.assertLessEqual(len(rules.text.encode("utf-8")), MAX_RULES_BYTES)
        self.assertIn("长", rules.text)

    def test_unreadable_file_treated_as_absent(self) -> None:
        # 目录同名占位：is_file() 为 False，按没有规则文件处理
        (self.ws / "VIBE.md").mkdir()
        rules = load_project_instructions(self.ws)
        self.assertEqual(rules.source, "")


class PromptSectionTests(_WsHarness):
    def test_empty_keeps_prompt_byte_identical(self) -> None:
        """回归保护：不传参数与传空串，输出必须逐字节一致。"""
        common = dict(
            workspace="E:/proj", tool_descriptions="- t: x", native_tools=True
        )
        self.assertEqual(
            build_system_prompt(**common),
            build_system_prompt(**common, project_instructions=""),
        )
        self.assertNotIn(RULE_MARK, build_system_prompt(**common))

    def test_blank_file_does_not_inject_section(self) -> None:
        prompt = build_system_prompt(
            workspace="E:/proj",
            tool_descriptions="",
            native_tools=True,
            project_instructions="   \n  ",
        )
        self.assertNotIn(RULE_MARK, prompt)

    def test_rules_injected_before_behaviour_section(self) -> None:
        prompt = build_system_prompt(
            workspace="E:/proj",
            tool_descriptions="",
            native_tools=True,
            project_instructions="所有函数必须有 docstring",
        )
        self.assertIn(RULE_MARK, prompt)
        self.assertIn("所有函数必须有 docstring", prompt)
        self.assertIn(SAFETY_MARK, prompt)
        self.assertIn("绕过审批", prompt)
        self.assertLess(prompt.index(RULE_MARK), prompt.index("## 行为约束"))


class AgentIntegrationTests(_WsHarness):
    def _agent(self) -> Agent:
        config = Config.load(self.ws)
        return Agent(config, client=FakeClient(["好的。"]), approver=lambda r: "approve")

    def test_agent_without_rules_matches_plain_prompt(self) -> None:
        agent = self._agent()
        expected = build_system_prompt(
            workspace=str(self.ws),
            tool_descriptions=agent.registry.describe(),
            native_tools=True,
        )
        self.assertEqual(agent.messages[0]["content"], expected)
        self.assertEqual(agent.project_instructions.source, "")

    def test_agent_injects_rules_into_system_prompt(self) -> None:
        self._write("VIBE.md", "不允许引入第三方依赖")
        agent = self._agent()
        content = agent.messages[0]["content"]
        self.assertIn(RULE_MARK, content)
        self.assertIn("不允许引入第三方依赖", content)
        self.assertIn(SAFETY_MARK, content)
        self.assertEqual(agent.project_instructions.chars, len("不允许引入第三方依赖"))

    def test_truncation_adds_notice(self) -> None:
        self._write("VIBE.md", "长" * (MAX_RULES_BYTES + 1))
        agent = self._agent()
        self.assertTrue(agent.project_instructions.truncated)
        self.assertTrue(
            any("截断" in n for n in agent.notices), f"notices={agent.notices}"
        )

    def test_demo_project_fixture_is_loadable(self) -> None:
        """C1.5：demo-project 的 VIBE.md 是 e2e 冒烟的固定夹具，必须可加载。"""
        demo = Path(__file__).resolve().parents[3] / "examples" / "demo-project"
        rules = load_project_instructions(demo)
        self.assertEqual(rules.source, "VIBE.md")
        self.assertIn("docstring", rules.text)
        self.assertFalse(rules.truncated)


class SessionDescribeTests(_WsHarness):
    def test_describe_exposes_rules_only_when_present(self) -> None:
        session = Session(
            Config.load(self.ws), client=FakeClient(["好的。"])
        )
        self.assertIsNone(session.describe()["projectInstructions"])

        self._write(".vibe/VIBE.md", "规则内容")
        resumed = Session(Config.load(self.ws), client=FakeClient(["好的。"]))
        info = resumed.describe()["projectInstructions"]
        self.assertEqual(info["source"], ".vibe/VIBE.md")
        self.assertEqual(info["chars"], len("规则内容"))
        self.assertFalse(info["truncated"])


if __name__ == "__main__":
    unittest.main()
