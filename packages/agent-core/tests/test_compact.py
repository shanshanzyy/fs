"""上下文裁剪升级（C4）：删除前摘要。

## 为什么这组必须有测试

裁剪是**不可逆**的 —— `del` 掉的历史在这个进程里再也拿不回来。C4 往这条
必经路径上加了一次模型调用：一个会失败、会超时、会被上游拒的外部依赖。

所以这组守的不是「摘要写得好不好」（那没法测），而是三件能测清楚的事：

1. **关掉时逐字节等于旧行为**。默认关，绝大多数用户走这条路，
   也是这个功能唯一的「退回安全区」；
2. **摘要失败必须退回纯删除**，绝不能因为润色功能把主流程弄挂；
3. **摘要真的进了模型可见的上下文**，而不是白算一遍就丢。

判别摘要调用的方式见 `ScriptedClient.chat` —— 为什么这样判别，那里写了。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import ConfigError  # noqa: E402
from agent_core.llm import ChatResponse, ToolCall  # noqa: E402
from agent_core.loop import Agent, AgentHooks  # noqa: E402
from agent_core.loop.agent import (  # noqa: E402
    MAX_SUMMARY_CHARS,
    SUMMARY_INPUT_PER_MESSAGE,
    SUMMARY_INPUT_TOTAL,
    _SUMMARY_MARK,
    _build_compact_prompt,
    _render_for_summary,
)

#: 假模型给出的摘要。刻意带上一句「早期结论」，好在随后的上下文里找得到它。
COMPACT_OUTPUT = "约束：不要引入任何第三方库；已确认只改 a.py。"

#: 一次裁剪必然删掉的条数（见 CompactHarness._pad 的说明）。
LEGACY_TRIMMED = 5


class ScriptedClient:
    """假模型：把「摘要请求」与「主循环请求」分开记账。

    判别方式是**消息条数**：摘要请求只有孤零零一条 user 消息，而主循环的
    上下文里必定带着 system 提示。分开记账，测试才能断言「关掉开关时一次
    摘要调用都没发生」—— 这是 C4 默认关的核心保证。

    `summaries` 一次一个，取到只剩一个时反复返回最后一个（方便「换了摘要」
    与「摘要变空」这两种剧本）。`summary_error` 非空则在摘要调用时抛错。
    """

    supports_native_tools = True

    def __init__(self, steps=None, *, summaries=None, summary_error=None) -> None:
        self.steps = list(steps or [])
        self.summaries = list(summaries or [COMPACT_OUTPUT])
        self.summary_error = summary_error
        self.summary_prompts: list[str] = []
        self.main_calls: list[list[dict]] = []

    def chat(self, messages, tools=None, temperature=None):
        if len(messages) == 1 and messages[0].get("role") == "user":
            self.summary_prompts.append(str(messages[0].get("content") or ""))
            if self.summary_error is not None:
                raise self.summary_error
            text = self.summaries[0] if len(self.summaries) == 1 else self.summaries.pop(0)
            return ChatResponse(content=text)

        self.main_calls.append(copy.deepcopy(messages))
        if not self.steps:
            return ChatResponse(content="（剧本已空）")
        step = self.steps.pop(0)
        if isinstance(step, str):
            return ChatResponse(content=step)
        calls = [
            ToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(step)
        ]
        return ChatResponse(content="", tool_calls=calls)


class CompactHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent(self, client, *, compact: bool = True, budget: int = 2_000, **overrides):
        config = Config.load(
            self.ws, max_context_chars=budget, compact_on_trim=compact, **overrides
        )
        return Agent(
            config, client=client, approver=lambda request: "approve", hooks=AgentHooks()
        )

    @staticmethod
    def _pad(agent, turns: int, size: int = 300) -> None:
        """铺历史。直接 append 而不走 `_record` —— 这组不关心持久化。

        `turns=6` 时 messages 共 13 条（1 system + 12 历史）。`_plan_trim` 的
        停止条件是「消息数不再多于 8」，所以必然删掉 `LEGACY_TRIMMED` 条：
        与预算、与系统提示词长度都无关，断言才能钉死。
        """
        for index in range(turns):
            agent.messages.append(
                {"role": "user", "content": f"第 {index} 轮问题：" + "问" * size}
            )
            agent.messages.append({"role": "assistant", "content": "答" * size})

    def _audit_events(self, name: str) -> list[dict]:
        path = self.ws / ".vibe" / "audit.jsonl"
        events: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("event") == name:
                events.append(entry)
        return events

    def _summary_bodies(self, agent) -> list[str]:
        """所有「看起来像摘要」的消息正文 —— 用来断言「只有一条」。"""
        mark = _SUMMARY_MARK
        return [
            str(message.get("content") or "")
            for message in agent.messages
            if str(message.get("content") or "").startswith(mark)
        ]


class CompactConfigTest(CompactHarness):
    """C4.1：开关是给运维用的，所以环境变量那一面也得钉住。"""

    def test_off_by_default(self) -> None:
        # 干净的进程环境：不能被开发机上的 export 影响，所以先清掉
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIBE_COMPACT_ON_TRIM", None)
            config = Config.load(self.ws)
        self.assertFalse(config.compact_on_trim, "C4 必须默认关，否则等于偷偷加成本")

    def test_env_var_turns_it_on_and_off(self) -> None:
        for raw, expected in (("1", True), ("true", True), ("0", False), ("", False)):
            with self.subTest(value=raw):
                with mock.patch.dict(os.environ, {"VIBE_COMPACT_ON_TRIM": raw}):
                    self.assertEqual(Config.load(self.ws).compact_on_trim, expected)

    def test_typo_in_override_is_rejected(self) -> None:
        """字段名写错要报错 —— 静默忽略会让调用方以为自己开了摘要。"""
        with self.assertRaises(ConfigError) as ctx:
            Config.load(self.ws, compact_on_trm=True)
        self.assertIn("compact_on_trm", str(ctx.exception))


class CompactDisabledTest(CompactHarness):
    """默认关：这条路上不该出现任何新东西。"""

    def test_disabled_matches_legacy_trim(self) -> None:
        """关闭时：零次摘要调用、无摘要消息、删掉的正是最老的那几条。"""
        client = ScriptedClient()
        agent = self._agent(client, compact=False)
        self._pad(agent, 6)
        history = copy.deepcopy(agent.messages[1:])

        agent._trim_context()

        self.assertEqual(client.summary_prompts, [], "关闭时不该有任何摘要调用")
        self.assertEqual(self._summary_bodies(agent), [])
        self.assertEqual(agent.stats.trimmed, LEGACY_TRIMMED)
        self.assertEqual(len(agent.messages), 8)
        # 逐条比对旧行为：队首原样留着，只是最老的那几条没了
        self.assertEqual(agent.messages[0]["role"], "system")
        self.assertEqual(agent.messages[1:], history[LEGACY_TRIMMED:])


class CompactEnabledTest(CompactHarness):
    def test_summary_lands_right_after_system_prompt(self) -> None:
        client = ScriptedClient()
        agent = self._agent(client)
        self._pad(agent, 6)

        agent._trim_context()

        self.assertEqual(len(self._summary_bodies(agent)), 1)
        summary = agent.messages[1]
        self.assertEqual(summary["role"], "system")
        self.assertEqual(summary["content"], f"{_SUMMARY_MARK}\n{COMPACT_OUTPUT}")
        self.assertEqual(agent.messages[0]["role"], "system")
        self.assertTrue(str(agent.messages[0]["content"]).startswith("你是"))

    def test_summary_prompt_carries_the_early_constraint(self) -> None:
        """摘要提示里必须带着即将被删的那段历史 —— 否则摘要无从谈起。"""
        client = ScriptedClient()
        agent = self._agent(client)
        agent.messages.append(
            {"role": "user", "content": "早期结论：不要引入任何第三方库。" + "问" * 300}
        )
        self._pad(agent, 6)

        agent._trim_context()

        self.assertEqual(len(client.summary_prompts), 1)
        self.assertIn("不要引入任何第三方库", client.summary_prompts[0])
        self.assertIn("【待压缩的对话】", client.summary_prompts[0])

    def test_model_sees_the_summary_on_the_next_call(self) -> None:
        """C4 验收：裁剪之后，早期结论仍出现在模型可见的上下文里。"""
        client = ScriptedClient(steps=["已按约束完成。"])
        agent = self._agent(client)
        agent.messages.append({"role": "user", "content": "早期结论：不要引入任何第三方库。"})
        self._pad(agent, 6)

        answer = agent.run("继续")

        self.assertEqual(answer, "已按约束完成。")
        visible = " ".join(str(m.get("content") or "") for m in client.main_calls[0])
        self.assertIn(COMPACT_OUTPUT, visible)
        # 被删掉的原文确实看不到了 —— 说明断言的不是「反正全都留着」
        self.assertNotIn("第 0 轮问题", visible)

    def test_second_trim_feeds_previous_summary_and_replaces_in_place(self) -> None:
        """第二次裁剪：上一版摘要要喂回去，新摘要**在位替换**而不是追加。"""
        client = ScriptedClient(
            summaries=["第一版摘要：不用第三方库。", "第二版摘要：不用第三方库，只改 a.py。"]
        )
        agent = self._agent(client)
        self._pad(agent, 6)
        agent._trim_context()
        self.assertEqual(self._summary_bodies(agent), [f"{_SUMMARY_MARK}\n第一版摘要：不用第三方库。"])

        self._pad(agent, 6)
        agent._trim_context()

        self.assertEqual(len(client.summary_prompts), 2)
        self.assertIn("【上一版摘要】", client.summary_prompts[1])
        self.assertIn("第一版摘要：不用第三方库。", client.summary_prompts[1])
        bodies = self._summary_bodies(agent)
        self.assertEqual(len(bodies), 1, "摘要只能有一条，否则每裁一次上下文就长一点")
        self.assertEqual(bodies[0], f"{_SUMMARY_MARK}\n第二版摘要：不用第三方库，只改 a.py。")

    def test_empty_summary_keeps_the_previous_one(self) -> None:
        """模型回了空串时保留旧摘要 —— 空白覆盖掉已确认的约束是最坏的退化。"""
        client = ScriptedClient(summaries=["第一版摘要：不用第三方库。", ""])
        agent = self._agent(client)
        self._pad(agent, 6)
        agent._trim_context()

        self._pad(agent, 6)
        agent._trim_context()

        self.assertEqual(
            self._summary_bodies(agent), [f"{_SUMMARY_MARK}\n第一版摘要：不用第三方库。"]
        )

    def test_overlong_summary_is_hard_truncated(self) -> None:
        """提示词里的字数上限只是请求，硬截断才是保证。"""
        client = ScriptedClient(summaries=["长" * (MAX_SUMMARY_CHARS * 4)])
        agent = self._agent(client)
        self._pad(agent, 6)

        agent._trim_context()

        body = str(agent.messages[1]["content"]).split("\n", 1)[1]
        self.assertEqual(len(body), MAX_SUMMARY_CHARS)

    def test_tool_batch_is_removed_whole(self) -> None:
        """带工具调用的一轮必须整批删掉，且工具名要进摘要提示。"""
        client = ScriptedClient()
        agent = self._agent(client)
        agent.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_0",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_0", "content": "结果" * 300},
            ]
        )
        self._pad(agent, 6)

        agent._trim_context()

        self.assertIn("read_file", client.summary_prompts[0])
        for index, message in enumerate(agent.messages):
            if message.get("role") != "tool":
                continue
            previous = agent.messages[index - 1]
            self.assertEqual(previous.get("role"), "assistant")
            self.assertTrue(previous.get("tool_calls"), "tool 消息前面必须有配对的调用")


class CompactFallbackTest(CompactHarness):
    """摘要失败一律退回纯删除 —— 裁剪是必需品，摘要是锦上添花。"""

    def test_error_falls_back_to_plain_trim(self) -> None:
        client = ScriptedClient(summary_error=RuntimeError("上游 500"))
        agent = self._agent(client)
        self._pad(agent, 6)

        agent._trim_context()  # 不该抛

        self.assertEqual(agent.stats.trimmed, LEGACY_TRIMMED, "删除必须照做")
        self.assertEqual(self._summary_bodies(agent), [])
        self.assertTrue(
            any("上下文摘要失败" in note for note in agent.notices),
            f"没有如实提醒用户：{agent.notices}",
        )
        # 旧行为是「一次会话只提醒一次裁剪」，失败提醒占了这个位置（复用它，
        # 而不是再堆一条）—— 用户看到的仍然是「这次裁剪没走摘要」这件事。
        self.assertEqual(len(agent.notices), 1)
        events = self._audit_events("context.compact")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary_chars"], 0)
        self.assertEqual(events[0]["compacted_messages"], LEGACY_TRIMMED)

    def test_failure_does_not_break_the_run(self) -> None:
        """失败只降级、不中断：整轮 run 仍要正常收尾。"""
        client = ScriptedClient(steps=["照常收尾。"], summary_error=RuntimeError("超时"))
        agent = self._agent(client)
        self._pad(agent, 6)

        answer = agent.run("继续")

        self.assertEqual(answer, "照常收尾。")
        self.assertTrue(client.main_calls)


class CompactAuditTest(CompactHarness):
    def test_success_records_lengths_and_keeps_chain_valid(self) -> None:
        client = ScriptedClient()
        agent = self._agent(client)
        self._pad(agent, 6)

        agent._trim_context()

        events = self._audit_events("context.compact")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["compacted_messages"], LEGACY_TRIMMED)
        self.assertEqual(events[0]["summary_chars"], len(COMPACT_OUTPUT))
        # 摘要正文一个字都不许进审计（ADR-006）
        self.assertNotIn(COMPACT_OUTPUT, json.dumps(events[0], ensure_ascii=False))
        ok, detail = verify_chain(self.ws / ".vibe" / "audit.jsonl")
        self.assertTrue(ok, detail)


class CompactPromptTest(unittest.TestCase):
    """摘要提示自身的夹紧 —— 让「摘要调用」不至于被上游按上下文超限拒掉。"""

    def test_single_message_is_clamped(self) -> None:
        item = _render_for_summary({"role": "tool", "content": "结" * 5_000})
        self.assertIn("（中略）", item)
        self.assertLessEqual(len(item), SUMMARY_INPUT_PER_MESSAGE + 16)

    def test_tool_names_are_inlined(self) -> None:
        item = _render_for_summary(
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "edit_file"}}]}
        )
        self.assertIn("调用工具：edit_file", item)

    def test_total_input_budget_is_enforced(self) -> None:
        pending = [{"role": "user", "content": "问" * 700} for _ in range(40)]

        prompt = _build_compact_prompt("旧摘要", pending)

        self.assertIn("【上一版摘要】", prompt)
        self.assertIn("旧摘要", prompt)
        self.assertIn("未纳入本次摘要", prompt, "略过多少条要如实标出来")
        body = prompt.split("【待压缩的对话】", 1)[1]
        self.assertLessEqual(len(body), SUMMARY_INPUT_TOTAL + SUMMARY_INPUT_PER_MESSAGE + 64)

    def test_instruction_carries_the_length_limit(self) -> None:
        prompt = _build_compact_prompt("", [{"role": "user", "content": "问题"}])
        self.assertIn(str(MAX_SUMMARY_CHARS), prompt)

    def test_small_history_is_taken_whole(self) -> None:
        prompt = _build_compact_prompt("", [{"role": "user", "content": "只有一条"}])
        self.assertNotIn("未纳入本次摘要", prompt)
        self.assertIn("只有一条", prompt)


if __name__ == "__main__":
    unittest.main()