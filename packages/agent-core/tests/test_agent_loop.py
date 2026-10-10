"""端到端循环测试：用一个假模型驱动完整的 Agent 循环。

这是 M0 最重要的一组测试 —— 它不依赖任何真实模型服务，却能验证：
    描述 -> 规划 -> 工具调用 -> 打补丁 -> 人审 -> 落盘 -> 回报 这条闭环是否真的通。
模型换成假的之后，任何一环出问题都会在这里暴露。
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.llm import ChatResponse, ToolCall, parse_text_tool_calls  # noqa: E402
from agent_core.loop import Agent, AgentHooks, ApprovalRequest  # noqa: E402

SOURCE = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def main():\n"
    "    print(add(1, 2))\n"
)


class FakeClient:
    """按剧本返回响应的假模型。

    剧本元素两种形态：
        list[tuple[str, dict]] -> 一轮原生工具调用
        str                    -> 最终自然语言答复
    """

    supports_native_tools = True

    def __init__(self, steps, *, text_protocol: bool = False) -> None:
        self.steps = list(steps)
        self.seen_messages: list[list[dict]] = []
        self.text_protocol = text_protocol
        if text_protocol:
            self.supports_native_tools = False

    def chat(self, messages, tools=None, temperature=None):
        self.seen_messages.append(copy.deepcopy(messages))
        if not self.steps:
            return ChatResponse(content="（剧本已空）")
        step = self.steps.pop(0)
        if isinstance(step, str):
            if self.text_protocol:
                calls = parse_text_tool_calls(step)
                if calls:
                    return ChatResponse(content=step, tool_calls=calls)
            return ChatResponse(content=step)
        calls = [
            ToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(step)
        ]
        return ChatResponse(content="", tool_calls=calls)


class LoopHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_agent(self, steps, approver=None, **overrides):
        config = Config.load(self.ws, **overrides)
        client = FakeClient(steps)
        approvals: list[ApprovalRequest] = []

        def default_approver(request: ApprovalRequest) -> str:
            approvals.append(request)
            return "approve"

        agent = Agent(
            config,
            client=client,
            approver=approver or default_approver,
            hooks=AgentHooks(),
        )
        return agent, client, approvals


class HappyPathTest(LoopHarness):
    def test_read_then_edit_closes_the_loop(self) -> None:
        path = self.ws / "calc.py"
        path.write_text(SOURCE, encoding="utf-8", newline="")

        agent, client, approvals = self._make_agent(
            [
                [("read_file", {"path": "calc.py"})],
                [
                    (
                        "edit_file",
                        {
                            "path": "calc.py",
                            "search": "    return a + b\n",
                            "replace": "    return a + b  # 求和\n",
                        },
                    )
                ],
                "已完成：给 add 的返回值加了中文注释。",
            ]
        )

        answer = agent.run("给 add 函数的返回值加一行中文注释")

        self.assertIn("已完成", answer)
        self.assertIn("return a + b  # 求和", path.read_text(encoding="utf-8"))
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0].tool, "edit_file")
        self.assertEqual(agent.stats.tool_calls, 2)
        self.assertEqual(agent.stats.tool_failures, 0)

        # 模型应当真的看到了文件原文（上下文工程没白做）
        second_call = " ".join(
            str(m.get("content") or "") for m in client.seen_messages[1]
        )
        self.assertIn("return a + b", second_call)

    def test_repo_map_then_edit(self) -> None:
        (self.ws / "svc.py").write_text(
            "class TaskService:\n    def create(self, title):\n        pass\n",
            encoding="utf-8",
            newline="",
        )
        agent, _, _ = self._make_agent(
            [
                [("repo_map", {"path": "."})],
                [
                    (
                        "edit_file",
                        {
                            "path": "svc.py",
                            "search": "    def create(self, title):\n        pass",
                            "replace": "    def create(self, title):\n        return {'title': title}",
                        },
                    )
                ],
                "已完成。",
            ]
        )
        agent.run("让 create 返回一个 dict")
        self.assertIn("return {'title': title}", (self.ws / "svc.py").read_text(encoding="utf-8"))

    def test_multi_turn_memory(self) -> None:
        path = self.ws / "n.txt"
        path.write_text("value = 1\n", encoding="utf-8", newline="")
        agent, client, _ = self._make_agent(
            [
                [("edit_file", {"path": "n.txt", "search": "value = 1", "replace": "value = 2"})],
                "已改为 2。",
                # 第二轮复用同一份对话历史
                [("read_file", {"path": "n.txt"})],
                "现在是 value = 2。",
            ]
        )
        agent.run("把 value 改成 2")
        agent.run("现在值是多少？")
        self.assertIn("value = 2", path.read_text(encoding="utf-8"))
        self.assertGreater(len(client.seen_messages[-1]), 4)


class SafetyGateTest(LoopHarness):
    def test_review_approval_cannot_be_configured_off(self) -> None:
        """ADR-008：REVIEW 级（文件写入 / 命令执行）审批不可配置关闭。

        反向验证：`auto_approve_review=True` 曾能整体跳过这道闸门，与红线
        直接冲突。若有人把它重新接回闸门，这里的 approvals 会变成 0、
        文件会被改动，本用例立刻变红。
        """
        path = self.ws / "calc.py"
        path.write_text(SOURCE, encoding="utf-8", newline="")
        seen: list[ApprovalRequest] = []

        def deny(request: ApprovalRequest) -> str:
            seen.append(request)
            return "deny"

        agent, _, _ = self._make_agent(
            [
                [
                    (
                        "edit_file",
                        {
                            "path": "calc.py",
                            "search": "    return a + b\n",
                            "replace": "    return a + b  # 求和\n",
                        },
                    )
                ],
                "好的，我不改了。",
            ],
            approver=deny,
            auto_approve_review=True,  # 旧开关：必须不再有任何效力
        )
        agent.run("给 add 加注释")

        self.assertEqual(
            len(seen), 1, "REVIEW 级编辑必须进入人工审批，不能被配置跳过"
        )
        self.assertNotIn(
            "# 求和", path.read_text(encoding="utf-8"), "被拒后不得落盘"
        )

    def test_denied_command_never_reaches_approval(self) -> None:
        agent, _, approvals = self._make_agent(
            [
                [("run_command", {"command": "rm -rf build"})],
                "好的，我换个方式。",
            ]
        )
        agent.run("清理构建产物")

        self.assertEqual(approvals, [], "DENY 级命令不应进入人工审批环节")
        self.assertEqual(agent.stats.denials, 1)
        self.assertEqual(agent.stats.tool_calls, 0)
        tool_messages = [
            m for m in agent.messages if m.get("role") == "tool"
        ]
        self.assertTrue(any("安全策略拒绝" in str(m.get("content")) for m in tool_messages))

    def test_approval_denial_leaves_file_untouched(self) -> None:
        path = self.ws / "a.txt"
        path.write_text("keep me\n", encoding="utf-8", newline="")
        seen: list[str] = []

        def approver(request: ApprovalRequest) -> str:
            seen.append(request.tool)
            return "deny"

        agent, _, _ = self._make_agent(
            [
                [("edit_file", {"path": "a.txt", "search": "keep me", "replace": "changed"})],
                "好的，不改了。",
            ],
            approver=approver,
        )
        agent.run("改一下 a.txt")

        self.assertEqual(path.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(seen, ["edit_file"])
        self.assertEqual(agent.stats.denials, 1)

    def test_deny_all_disables_tool_for_session(self) -> None:
        path = self.ws / "b.txt"
        path.write_text("x\n", encoding="utf-8", newline="")
        calls: list[str] = []

        def approver(request: ApprovalRequest) -> str:
            calls.append(request.tool)
            return "deny_all"

        agent, _, _ = self._make_agent(
            [
                [("edit_file", {"path": "b.txt", "search": "x", "replace": "y"})],
                [("edit_file", {"path": "b.txt", "search": "x", "replace": "z"})],
                "明白，本次会话不再尝试修改。",
            ],
            approver=approver,
        )
        agent.run("改两次试试")
        self.assertEqual(calls, ["edit_file"], "第二次调用应被会话级禁言直接拦下")
        self.assertEqual(path.read_text(encoding="utf-8"), "x\n")

    def test_approve_all_caches_per_tool(self) -> None:
        path = self.ws / "c.txt"
        path.write_text("a\nb\n", encoding="utf-8", newline="")
        approvals: list[str] = []

        def approver(request: ApprovalRequest) -> str:
            approvals.append(request.tool)
            return "approve_all"

        config = Config.load(self.ws)
        client = FakeClient(
            [
                [("edit_file", {"path": "c.txt", "search": "a", "replace": "A"})],
                [("edit_file", {"path": "c.txt", "search": "b", "replace": "B"})],
                "两处都改好了。",
            ]
        )
        agent = Agent(config, client=client, approver=approver, hooks=AgentHooks())
        agent.run("把两行都改成大写")
        self.assertEqual(approvals, ["edit_file"], "approve_all 之后不应再重复询问")

    def test_path_escape_is_reported_not_crashed(self) -> None:
        agent, _, _ = self._make_agent(
            [
                [("read_file", {"path": "../../../etc/passwd"})],
                "无法读取该文件，它超出了工作区。",
            ]
        )
        agent.run("看看系统密码文件")
        self.assertEqual(agent.stats.tool_failures, 1)
        failures = [
            m for m in agent.messages
            if m.get("role") == "tool" and "越出工作区边界" in str(m.get("content"))
        ]
        self.assertTrue(failures, "越狱尝试必须被拒绝并明确回报原因")

    def test_write_disabled(self) -> None:
        path = self.ws / "d.txt"
        path.write_text("orig\n", encoding="utf-8", newline="")
        agent, _, approvals = self._make_agent(
            [
                [("edit_file", {"path": "d.txt", "search": "orig", "replace": "new"})],
                "当前是只读模式。",
            ],
            allow_write=False,
        )
        agent.run("改一下")
        self.assertEqual(path.read_text(encoding="utf-8"), "orig\n")
        self.assertEqual(approvals, [])
        self.assertEqual(agent.stats.denials, 1)

    def test_prepatch_failure_is_recoverable(self) -> None:
        """search 失配时不能写入任何内容，且要给出可执行的诊断。"""
        path = self.ws / "e.txt"
        path.write_text("alpha\nbeta\n", encoding="utf-8", newline="")
        agent, _, _ = self._make_agent(
            [
                [("edit_file", {"path": "e.txt", "search": "gamma", "replace": "delta"})],
                "没找到，我重新读一下文件。",
            ]
        )
        agent.run("改成 delta")
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nbeta\n")
        self.assertEqual(agent.stats.tool_failures, 1)
        text = " ".join(
            str(m.get("content")) for m in agent.messages if m.get("role") == "tool"
        )
        self.assertIn("read_file", text)


class AuditTest(LoopHarness):
    def test_audit_chain_valid_after_run(self) -> None:
        (self.ws / "f.txt").write_text("v = 1\n", encoding="utf-8", newline="")
        agent, _, _ = self._make_agent(
            [
                [("edit_file", {"path": "f.txt", "search": "v = 1", "replace": "v = 2"})],
                "完成。",
            ]
        )
        agent.run("改成 2")

        passed, detail = verify_chain(agent.config.audit_path)
        self.assertTrue(passed, detail)
        self.assertIn("链完整", detail)

    def test_session_id_is_always_present(self) -> None:
        """会话 ID 为空会让审计记录无法归组，排查问题时是硬伤。"""
        config = Config.load(self.ws)
        self.assertTrue(config.session_id.strip())

        (self.ws / "s.txt").write_text("a\n", encoding="utf-8", newline="")
        agent, _, _ = self._make_agent(
            [
                [("edit_file", {"path": "s.txt", "search": "a", "replace": "b"})],
                "完成。",
            ]
        )
        agent.run("改一下")
        raw = agent.config.audit_path.read_text(encoding="utf-8")
        self.assertNotIn('"session": ""', raw)
        self.assertIn(agent.config.session_id, raw)

    def test_audit_does_not_store_code_content(self) -> None:
        """ADR-006：审计只落元数据与指纹，绝不能出现代码正文。"""
        secret_body = "API_KEY = 'super-secret-value-12345'"
        (self.ws / "g.py").write_text(secret_body + "\n", encoding="utf-8", newline="")
        agent, _, _ = self._make_agent(
            [
                [("edit_file", {"path": "g.py", "search": "API_KEY", "replace": "TOKEN"})],
                "完成。",
            ]
        )
        agent.run("把 API_KEY 改成 TOKEN")
        raw = agent.config.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-value-12345", raw)
        self.assertIn("tool.edit_file", raw)


class TextProtocolTest(LoopHarness):
    def test_text_tool_protocol_roundtrip(self) -> None:
        path = self.ws / "h.txt"
        path.write_text("hello\n", encoding="utf-8", newline="")
        config = Config.load(self.ws)
        client = FakeClient(
            [
                '我来读取文件：\n```tool\n{"name": "read_file", "arguments": {"path": "h.txt"}}\n```',
                '现在修改它：\n```tool\n{"name": "edit_file", "arguments": '
                '{"path": "h.txt", "search": "hello", "replace": "hi"}}\n```',
                "已把 hello 改成 hi。",
            ],
            text_protocol=True,
        )
        agent = Agent(
            config, client=client, approver=lambda r: "approve", hooks=AgentHooks()
        )
        answer = agent.run("把 h.txt 里的 hello 改成 hi")

        self.assertIn("hi", path.read_text(encoding="utf-8"))
        self.assertIn("已把", answer)
        self.assertEqual(agent.stats.tool_calls, 2)


class ContextBudgetTest(LoopHarness):
    def test_trimming_keeps_tool_pairs_intact(self) -> None:
        """裁剪上下文后不能留下孤立的 tool 消息，否则服务端会直接 400。"""
        path = self.ws / "big.txt"
        path.write_text("x\n" * 400, encoding="utf-8", newline="")

        steps = []
        for index in range(6):
            steps.append([("read_file", {"path": "big.txt"})])
            steps.append(f"第 {index} 步说明。" )
        config = Config.load(self.ws, max_context_chars=4000)
        client = FakeClient(steps)
        agent = Agent(
            config, client=client, approver=lambda r: "approve", hooks=AgentHooks()
        )
        agent.reset()
        agent.config = config
        # 构造一段带工具调用的历史
        agent.messages.extend(
            [
                {"role": "user", "content": "任务" * 500},
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
                {"role": "tool", "tool_call_id": "call_0", "content": "结果" * 500},
            ]
        )
        agent._trim_context()

        for index, message in enumerate(agent.messages):
            if message.get("role") != "tool":
                continue
            previous = agent.messages[index - 1]
            self.assertEqual(
                previous.get("role"),
                "assistant",
                "tool 消息前面必须是带 tool_calls 的 assistant 消息",
            )
            self.assertTrue(previous.get("tool_calls"))

    def test_over_budget_triggers_notice(self) -> None:
        config = Config.load(self.ws, max_context_chars=600)
        client = FakeClient(["完成。"])
        agent = Agent(config, client=client, hooks=AgentHooks())
        # 铺足够长的历史，确保越过预算且消息条数多于保留下限
        for _ in range(12):
            agent.messages.append({"role": "user", "content": "长" * 200})
            agent.messages.append({"role": "assistant", "content": "长" * 200})
        agent.run("继续")
        self.assertTrue(agent.notices)
        self.assertGreater(agent.stats.trimmed, 0)


if __name__ == "__main__":
    unittest.main()
