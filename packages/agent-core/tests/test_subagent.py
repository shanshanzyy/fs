"""多 Agent 编排（M3）专项测试。

分五层验证「主从只读子 Agent」这条链，从最底层的白名单一直到审计链完整性：

    工具层   —— `task` 只在开关打开时注册、参数校验、无 sink 明确失败、结果恒包裹
    注册表层 —— 子 Agent 的工具面结构上不含任何写 / 执行 / `task` / `mcp__*`
    引擎层   —— 子 Agent 能返回结构化摘要、失败降级、中断透传、独立迭代预算
    审计层   —— `subagent.run` 只记元数据；父/子共用同一 AuditLog（链不断）
    事件层   —— 各 phase 齐全且 `callId` 关联；关闭时无 `task`、无 `run.subagent`

**为什么单独一个文件而不是塞进 test_agent_loop.py**：这里测的是一条**新的**
子链路，它有自己的一组不变量（只读面、共用审计实例、无递归）。混进主循环测试
会让「哪条边界被破坏了」在失败时看不清。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog, verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import LLMError, ToolError  # noqa: E402
from agent_core.host import protocol as P  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.llm import ChatResponse, ToolCall  # noqa: E402
from agent_core.loop import Agent, AgentHooks  # noqa: E402
from agent_core.loop.subagent import run_subagent  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools import (  # noqa: E402
    TASK_SPEC,
    ToolContext,
    ToolResult,
    build_default_registry,
    build_readonly_registry,
    wrap_untrusted,
)
from agent_core.tools.base import UNTRUSTED_BEGIN  # noqa: E402
from agent_core.tools.task import (  # noqa: E402
    MAX_DESCRIPTION_CHARS,
    MAX_TASK_CHARS,
    normalize_task,
)


class FakeClient:
    """按剧本返回响应的假模型（脚本元素：`list[(name, args)]` = 一轮工具调用；`str` = 终答）。"""

    supports_native_tools = True

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.seen_messages: list[list[dict]] = []

    def chat(self, messages, tools=None, temperature=None):
        self.seen_messages.append(messages)
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


class ExplodingClient:
    """每次调用都抛 LLMError —— 模拟上游不可达。"""

    supports_native_tools = True

    def chat(self, messages, tools=None, temperature=None):
        raise LLMError("upstream down")


class SubAgentHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self, **overrides) -> ToolContext:
        config = Config.load(self.ws, **overrides)
        return ToolContext(
            config=config,
            paths=PathGuard(config.workspace),
            commands=CommandGuard(config.workspace),
            # 关掉落盘：工具层测试只验行为，不需要审计文件
            audit=AuditLog(config.audit_path, session_id=config.session_id, enabled=False),
        )

    def _parent(self, steps, client=None, **overrides):
        config = Config.load(self.ws, subagent_enabled=True, **overrides)
        client = client or FakeClient(steps)
        agent = Agent(
            config,
            client=client,
            hooks=AgentHooks(),
            approver=lambda request: "deny",
        )
        return agent, client


# --------------------------------------------------------------------- 工具层
class TaskToolTests(SubAgentHarness):
    def test_registered_only_when_subagent_enabled(self) -> None:
        off = build_default_registry(lsp=False, subagent=False)
        on = build_default_registry(lsp=False, subagent=True)
        self.assertNotIn("task", off.names(), "未启用子 Agent 时不该出现 task 工具")
        self.assertIn("task", on.names(), "启用后应注册 task 工具")
        # 其余工具一个不多一个不少 —— 「不启用即逐字节一致」的保证
        self.assertEqual(on.names(), sorted(off.names() + ["task"]))

    def test_not_registered_in_readonly_registry(self) -> None:
        """无递归：子 Agent 的工具面里**没有** task。"""
        self.assertNotIn("task", build_readonly_registry(lsp=False).names())

    def test_normalize_rejects_empty_and_too_long(self) -> None:
        with self.assertRaises(ToolError):
            normalize_task({"task": "   "})
        with self.assertRaises(ToolError):
            normalize_task({"task": "x" * (MAX_TASK_CHARS + 1)})
        with self.assertRaises(ToolError):
            normalize_task({"task": "ok", "description": "d" * (MAX_DESCRIPTION_CHARS + 1)})
        # 合法输入：description 可省略
        task, description = normalize_task({"task": "  找调用点  "})
        self.assertEqual(task, "找调用点")
        self.assertEqual(description, "")

    def test_without_sink_fails_clearly(self) -> None:
        """直接调 registry（没有 Agent 绑定 sink）时如实报「未启用」。"""
        registry = build_default_registry(lsp=False, subagent=True)
        ctx = self._ctx()  # 未设 subagent_sink
        result = registry.execute(ctx, "task", {"task": "探索一下"})
        self.assertFalse(result.ok)
        self.assertIn("未启用", result.content)

    def test_result_is_wrapped_as_untrusted(self) -> None:
        """子 Agent 结论是工作区文本的再加工 —— 必须按不可信数据包裹。"""
        registry = build_default_registry(lsp=False, subagent=True)
        ctx = self._ctx()
        ctx.subagent_sink = lambda task: ToolResult(
            ok=True,
            content="结论：add 在 calc.py:1",
            meta={"iterations": 2, "tool_calls": 3, "answer_chars": 18},
        )
        result = registry.execute(ctx, "task", {"task": "找 add", "description": "定位 add"})
        self.assertTrue(result.ok)
        self.assertIn(UNTRUSTED_BEGIN, result.content)
        self.assertIn("source=subagent", result.content)
        self.assertIn("结论：add 在 calc.py:1", result.content)
        # 元数据透传 + description 落进 meta
        self.assertEqual(result.meta["iterations"], 2)
        self.assertEqual(result.meta["description"], "定位 add")

    def test_failure_is_forwarded_verbatim(self) -> None:
        """委派失败不能把「失败消息」也当正文包起来 —— 它不是不可信数据。"""
        registry = build_default_registry(lsp=False, subagent=True)
        ctx = self._ctx()
        ctx.subagent_sink = lambda task: ToolResult(ok=False, content="子 Agent 执行失败：boom")
        result = registry.execute(ctx, "task", {"task": "探索一下"})
        self.assertFalse(result.ok)
        self.assertEqual(result.content, "子 Agent 执行失败：boom")
        self.assertNotIn(UNTRUSTED_BEGIN, result.content)


# ------------------------------------------------------------------- 注册表层
class ReadonlyRegistryTests(unittest.TestCase):
    FORBIDDEN = {
        "edit_file", "write_file", "edit_files", "run_command",
        "update_plan", "remember", "task",
    }
    REQUIRED = {
        "repo_map", "list_dir", "read_file", "grep",
        "find_files", "semantic_search", "git_diff",
    }

    def test_excludes_write_execute_and_recursion(self) -> None:
        names = set(build_readonly_registry(lsp=False).names())
        leaked = names & self.FORBIDDEN
        self.assertEqual(leaked, set(), f"只读面里混进了禁项：{sorted(leaked)}")
        self.assertFalse({n for n in names if n.startswith("mcp__")}, "只读面不该有外部工具")

    def test_includes_all_read_only_tools(self) -> None:
        names = set(build_readonly_registry(lsp=False).names())
        missing = self.REQUIRED - names
        self.assertEqual(missing, set(), f"只读面缺了读工具：{sorted(missing)}")

    def test_every_readonly_tool_is_safe(self) -> None:
        """只读面里不该有任何 REVIEW/DENY 级工具（它是「不进审批」的结构依据）。"""
        registry = build_readonly_registry(lsp=False)
        for name in registry.names():
            spec = registry.get(name)
            assert spec is not None
            self.assertEqual(spec.risk.value, "safe", f"{name} 不是 SAFE 级")


# --------------------------------------------------------------------- 引擎层
class SubAgentEngineTests(SubAgentHarness):
    def test_returns_structured_summary(self) -> None:
        agent, client = self._parent(["结论：add 定义在 calc.py:1"])
        events: list[dict] = []
        result = run_subagent(agent, "找 add 函数", call_id="c1", emit=events.append)
        self.assertTrue(result.ok, result.error)
        self.assertIn("calc.py:1", result.answer)
        self.assertEqual(result.iterations, 1)
        # 只读面确实被用到：子 Agent 至少问过模型一轮
        self.assertEqual(len(client.seen_messages), 1)

    def test_child_cannot_write(self) -> None:
        """子 Agent 试图写盘 → 工具不存在 → 文件未落盘，且父任务不受影响。"""
        agent, client = self._parent(
            [
                [("write_file", {"path": "hack.txt", "content": "x"})],
                "写不了，改用只读方式给出结论",
            ]
        )
        result = run_subagent(agent, "试着写个文件", call_id="c2", emit=lambda ev: None)
        self.assertTrue(result.ok)
        self.assertFalse((self.ws / "hack.txt").exists(), "子 Agent 竟然写盘成功了")
        # 子 Agent 拿到的回灌是「不存在该工具」——证明它确实走的是只读面
        blob = json.dumps(client.seen_messages[1], ensure_ascii=False)
        self.assertIn("不存在名为", blob)
        self.assertIn("write_file", blob)

    def test_child_cannot_recurse(self) -> None:
        agent, client = self._parent(
            [
                [("task", {"task": "再派一个子 Agent"})],
                "没有 task 工具，无法递归",
            ]
        )
        result = run_subagent(agent, "试着递归", call_id="c3", emit=lambda ev: None)
        self.assertTrue(result.ok)
        # 无递归：子表里没有 task，回灌同样是「不存在该工具」
        blob = json.dumps(client.seen_messages[1], ensure_ascii=False)
        self.assertIn("不存在名为", blob)
        self.assertIn("task", blob)

    def test_exception_degrades_to_ok_false(self) -> None:
        agent, _ = self._parent([], client=ExplodingClient())
        result = run_subagent(agent, "会炸的任务", call_id="c4", emit=lambda ev: None)
        self.assertFalse(result.ok)
        self.assertIn("LLMError", result.error)

    def test_should_stop_is_passed_through(self) -> None:
        """父被中断时子 Agent 在轮次边界同样停下，且不再打扰模型。"""
        agent, client = self._parent(["不该被调用"])
        agent.should_stop = lambda: True
        result = run_subagent(agent, "探索", call_id="c5", emit=lambda ev: None)
        self.assertTrue(result.ok)
        self.assertIn("已中断", result.answer)
        self.assertEqual(client.seen_messages, [], "中断后不该再调用模型")

    def test_subagent_max_iterations_is_effective(self) -> None:
        """子 Agent 有自己的迭代预算，不与父任务同上限。"""
        steps = [[("list_dir", {"path": "."})] for _ in range(10)]
        agent, _ = self._parent(steps, subagent_max_iterations=2)
        result = run_subagent(agent, "一直列目录", call_id="c6", emit=lambda ev: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.iterations, 2)
        self.assertIn("最大迭代", result.answer)


# --------------------------------------------------------------------- 审计层
class SubAgentAuditTests(SubAgentHarness):
    def _records(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_delegation_via_parent_run(self) -> None:
        agent, _ = self._parent(
            [
                [("task", {"task": "探索 add 函数在哪", "description": "定位 add"})],
                "子结论：add 在 calc.py:1",   # 子 Agent 消费
                "父结论：已按子 Agent 结论处理",  # 父 Agent 消费
            ]
        )
        answer = agent.run("看看 add 函数定义在哪")
        self.assertIn("父结论", answer)

        path = agent.config.audit_path
        ok, msg = verify_chain(path)
        self.assertTrue(ok, f"父/子共用审计实例后链应完整，实际：{msg}")

        subs = [r for r in self._records(path) if r.get("event") == "subagent.run"]
        self.assertEqual(len(subs), 1, "一次委派应恰好产生一条 subagent.run")
        record = subs[0]
        self.assertTrue(record["ok"])
        self.assertEqual(record["iterations"], 1)
        self.assertEqual(record["tool_calls"], 0)
        self.assertEqual(record["answer_chars"], len("子结论：add 在 calc.py:1"))
        self.assertIn("task_digest", record)
        # 只记元数据：正文（任务 / 结论）一个字符都不进审计
        self.assertNotIn("answer", record)
        self.assertNotIn("task", record)
        self.assertNotIn("探索", json.dumps(record, ensure_ascii=False))

    def test_parent_and_child_share_one_audit_instance(self) -> None:
        agent, _ = self._parent([[("task", {"task": "探索"})], "子结论", "父结论"])
        child_seen: list = []
        original = Agent.__init__

        # 直接验证身份：子 Agent 构造时拿到的 audit 必须就是父的那一个
        def spy(self, *args, **kwargs):
            original(self, *args, **kwargs)
            if self is not agent:
                child_seen.append(self.audit)

        Agent.__init__ = spy  # type: ignore[method-assign]
        try:
            agent.run("委派一次")
        finally:
            Agent.__init__ = original  # type: ignore[method-assign]
        self.assertTrue(child_seen, "没有捕获到子 Agent 的构造")
        self.assertTrue(
            all(instance is agent.audit for instance in child_seen),
            "子 Agent 必须复用父的同一个 AuditLog 实例，否则哈希链会交错断裂",
        )


# --------------------------------------------------------------------- 事件层
class SubAgentSessionTests(SubAgentHarness):
    def _session(self, steps, **overrides) -> Session:
        return Session(
            Config.load(self.ws, audit_enabled=True, **overrides),
            client=FakeClient(steps),
        )

    def _events(self, session: Session, type_: str) -> list[dict]:
        return [e for e in session.bus.snapshot(400) if e["type"] == type_]

    def test_enabled_emits_lifecycle_and_links_call_id(self) -> None:
        session = self._session(
            [
                [("task", {"task": "探索 add 在哪", "description": "定位"})],
                "子结论：add 在 calc.py:1",
                "父结论：已处理",
            ],
            subagent_enabled=True,
        )
        self.assertIn("task", session.registry.names())
        session.agent.run("看看 add 函数")

        subs = self._events(session, P.RUN_SUBAGENT)
        phases = [e["phase"] for e in subs]
        self.assertIn("started", phases)
        self.assertIn("finished", phases)
        self.assertNotIn("failed", phases)
        # callId 关联父侧 task 工具卡片
        calls = self._events(session, P.RUN_TOOL_CALL)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "task")
        self.assertTrue(all(e["callId"] == calls[0]["callId"] for e in subs))
        # finished 帧带汇总元数据，且不带正文
        done = next(e for e in subs if e["phase"] == "finished")
        self.assertEqual(done["iterations"], 1)
        self.assertEqual(done["tool_calls"], 0)
        self.assertNotIn("answer", done)

    def test_disabled_has_no_tool_and_no_events(self) -> None:
        session = self._session([[("list_dir", {})], "看完了。"])
        self.assertNotIn("task", session.registry.names(), "未启用时工具清单不该出现 task")
        session.agent.run("看看目录")
        self.assertEqual(self._events(session, P.RUN_SUBAGENT), [])


if __name__ == "__main__":
    unittest.main()