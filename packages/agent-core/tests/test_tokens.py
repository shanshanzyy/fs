"""token 估算与预算口径（P2-17）测试。

这一层的价值全在「估算结果是**裁剪时机**的输入」。它错了不会报错，只会让
上下文要么被裁得太早（模型忘了前面说过什么），要么裁得太晚（被上游按超限
直接拒掉）—— 两种都是靠肉眼难发现的静默错。因此这里钉四件事：

1. **回退必须真的在**。tiktoken 是可选依赖，缺了它估算函数仍要给出合理正值；
   一旦这条路径退化成返回 0，预算判断会永远认为「还没超」，裁剪彻底失效。
2. **加权方向对不对**。中文一字一 token、英文约四分之一 —— 方向反了或不做区分，
   就等于换了个单位继续按字符算，那这次改造毫无意义。
3. **多模态不炸**。图片 part 走 base64 时字符串长度与真实开销无关，
   按长度算会把预算瞬间拉爆；也不能因为遇到没见过的 part 类型就抛异常。
4. **`context_budget_tokens=0` 时是字符口径**。这是向后兼容的支点：
   不配新变量的既有部署，裁剪时机必须与改造前逐字节一致。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.context import tokens  # noqa: E402
from agent_core.host import protocol as P  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.loop import Agent  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

#: 两轮循环的剧本：一轮工具调用 + 一轮最终答复。
_TWO_ROUNDS = [[("list_dir", {})], "看完了。"]


class HeuristicTest(unittest.TestCase):
    """启发式估算器：回退路径的正确性。"""

    def test_fallback_returns_positive_value(self) -> None:
        """核心回归：没有 tiktoken 时估算仍返回正值，绝不是 0。

        返回 0 意味着「上下文永远是空的」，裁剪永远不触发 —— 这是最危险的
        静默降级，因此单独锁一条。
        """
        with mock.patch.object(tokens, "_encoder", return_value=None):
            self.assertEqual(tokens.estimator_name("any"), "heuristic")
            value = tokens.estimate_tokens("这是一段普通的中文提示词", model="any")
            self.assertGreater(value, 0)
            # 消息级估算同理：固定开销也要算进去
            total = tokens.estimate_messages(
                [{"role": "user", "content": "hello world"}], model="any"
            )
            self.assertGreaterEqual(total, tokens.PER_MESSAGE_TOKENS)

    def test_cjk_costs_more_than_ascii_of_same_length(self) -> None:
        """同长度下中文串必须比 ASCII 串「贵」—— 方向反了这次改造就白做。"""
        ascii_text = "a" * 24
        cjk_text = "中" * 24
        self.assertEqual(len(ascii_text), len(cjk_text))
        with mock.patch.object(tokens, "_encoder", return_value=None):
            self.assertGreater(
                tokens.estimate_tokens(cjk_text), tokens.estimate_tokens(ascii_text)
            )

    def test_empty_input_is_zero(self) -> None:
        self.assertEqual(tokens.estimate_tokens(""), 0)
        self.assertEqual(tokens.heuristic_tokens(""), 0)
        self.assertEqual(tokens.estimate_messages([]), 0)


class MultimodalTest(unittest.TestCase):
    """多模态：不抛异常，且图片按固定开销计而不是按 base64 长度。"""

    def test_image_part_uses_fixed_cost(self) -> None:
        base64_url = "data:image/png;base64," + "A" * 200_000
        messages = [
            {"role": "system", "content": "你是助手"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": base64_url}},
                    {"type": "text", "text": "看图改代码"},
                ],
            },
        ]
        total = tokens.estimate_messages(messages)
        self.assertGreaterEqual(total, tokens.IMAGE_TOKENS)
        # 关键断言：20 万字符的 base64 **没有**被按字符算进去。
        self.assertLess(total, tokens.IMAGE_TOKENS + 200)

    def test_unknown_parts_do_not_raise(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "未见过的新类型", "payload": {"a": 1}}]},
            {"role": "user", "content": None},
            {"role": "assistant", "tool_calls": [{"function": {"name": "x", "arguments": {"a": 1}}}]},
            {"role": "assistant", "tool_calls": [{"function": {"name": "y", "arguments": "{bad json"}}]},
            {"role": "user", "content": [12345, None]},
        ]
        total = tokens.estimate_messages(messages)
        self.assertGreater(total, 0)


class BudgetUnitTests(unittest.TestCase):
    """预算口径：`context_budget_tokens` 为 0 时走字符，配了才走 token。"""

    #: 每条的正文长度。40 个 ASCII 字符：字符口径记 40，启发式 token 口径记 10。
    #: 两个口径差 4 倍，因此「走哪条路」在这组用例里是可判别的。
    BODY = 40
    COUNT = 20

    def _agent(self, max_context_chars: int = 400, **overrides) -> Agent:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name).resolve()
        config = replace(
            Config.load(workspace),
            audit_enabled=False,
            max_context_chars=max_context_chars,
            **overrides,
        )
        agent = Agent(config, client=FakeClient([]))
        agent.messages[0]["content"] = "S"  # 固定系统提示长度，好算账
        for _ in range(self.COUNT):
            agent.messages.append({"role": "user", "content": "a" * self.BODY})
        return agent

    def test_zero_budget_keeps_char_semantics(self) -> None:
        """`context_budget_tokens=0` 时仍是字符口径 —— 裁剪时机与改造前一致。

        21 条消息（1 条系统 + 20 条 40 字符）：总量 801 字符、预算 400，
        每删一条减 40。删到 11 条时剩 361 ≤ 400 且剩余 10 条 > 8 的硬下限，
        故恰好删 11 条。若误走 token 口径（总量约 285 ≤ 400），这里会是 0。
        """
        agent = self._agent(context_budget_tokens=0)
        self.assertEqual(agent._token_budget(), 0)
        agent._trim_context()
        self.assertEqual(agent.stats.trimmed, 11)
        # 裁完必须真的落进预算内 —— 只断言条数还不够，口径错也能凑出 11 条
        remaining = sum(len(str(m.get("content") or "")) for m in agent.messages)
        self.assertLessEqual(remaining, 400)

    def test_token_budget_switches_unit(self) -> None:
        """配了 token 预算就按 token 估：同样的消息、同样的数字，一条都不裁。

        同一条消息在字符口径下记 40、在启发式 token 口径下记 10 —— 差 4 倍，
        因此「走哪条路」在这组数据上是可判别的（误走字符口径就会删掉 11 条）。
        """
        # 字符预算故意设得比 token 总量还小：一旦预算数值与长度单位被错配
        #（拿 token 的总量 285 去比字符的阈值 200），就会误裁 7 条 —— 这条用例
        # 的价值全在这个交叉上，两个数字若一样就什么也测不出来。
        agent = self._agent(max_context_chars=200, context_budget_tokens=400)
        self.assertEqual(agent._token_budget(), 400)
        self.assertEqual(agent._context_budget(), 400, "预算数值必须来自 token 配置")
        with mock.patch.object(tokens, "_encoder", return_value=None):
            total = agent._total_units()
            self.assertLess(total, 400)
            self.assertGreater(total, 200, "数据必须跨在两个阈值之间，否则测不出错配")
            agent._trim_context()
        self.assertEqual(agent.stats.trimmed, 0)


class ContextBudgetDefaultTests(unittest.TestCase):
    """上下文字符预算（VIBE_MAX_CONTEXT_CHARS）：默认值与覆盖两条路。

    默认从 120000 提到 400000 —— 与治理面策略默认一致；同秒内多问几轮时，
    120000 会频繁触发裁剪、把较早的对话丢掉（界面会弹「已裁剪较早的对话」）。
    这里钉住默认值，避免日后又被悄悄改回小值。
    """

    def _workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def test_default_is_400k(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("VIBE_MAX_CONTEXT_CHARS", None)
            config = Config.load(self._workspace())
        self.assertEqual(config.max_context_chars, 400_000)

    def test_env_var_overrides_default(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_MAX_CONTEXT_CHARS": "123456"}):
            config = Config.load(self._workspace())
        self.assertEqual(config.max_context_chars, 123456)


class ContextStatsTests(unittest.TestCase):
    """`context_stats()` 的接线：只读、分段自洽、口径字段如实。"""

    def _agent(self, **overrides) -> Agent:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name).resolve()
        config = replace(Config.load(workspace), audit_enabled=False, **overrides)
        return Agent(config, client=FakeClient([]))

    def test_segments_sum_to_used(self) -> None:
        agent = self._agent()
        agent.messages.append({"role": "user", "content": "看这个文件"})
        agent.messages.append({"role": "assistant", "content": "好"})
        agent.messages.append({"role": "tool", "tool_call_id": "c1", "content": "内容"})
        agent.messages.append(
            {"role": "user", "content": "[工具结果] read_file\n内容"}
        )
        stats = agent.context_stats()
        self.assertEqual(
            sum(stats["segments"].values()),
            stats["usedTokens"],
            "分段之和必须等于总量，否则预算条与数字对不上",
        )
        self.assertEqual(stats["segments"]["rules"], 0, "工作区没有 VIBE.md 时规则段为 0")
        # 工具结果两条（原生 tool 消息 + 文本协议的 [工具结果]）都要归到工具段
        self.assertGreater(stats["segments"]["toolOutputs"], 0)
        self.assertEqual(stats["budgetTokens"], 0)
        self.assertIn(stats["estimator"], ("tiktoken", "heuristic"))
        self.assertEqual(stats["cache"], "off", "FakeClient 没有 cache_state，应如实报 off")
        self.assertFalse(stats["trimmedLastRun"])

    def test_rules_excluded_from_system_segment(self) -> None:
        """系统提示里嵌着规则正文，规则段必须从系统段里扣出来，不能算两遍。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name).resolve()
        (workspace / "VIBE.md").write_text("规则" * 50, encoding="utf-8")
        config = replace(Config.load(workspace), audit_enabled=False)
        agent = Agent(config, client=FakeClient([]))
        stats = agent.context_stats()
        self.assertGreater(stats["segments"]["rules"], 0)
        self.assertEqual(
            stats["segments"]["system"],
            max(
                0,
                tokens.estimate_messages(agent.messages[:1], model=config.model)
                - stats["segments"]["rules"],
            ),
        )
        self.assertEqual(sum(stats["segments"].values()), stats["usedTokens"])

    def test_public_api_is_declared(self) -> None:
        """估算器是公开能力（端点与界面都要用），必须在 `__all__` 里。"""
        for name in ("estimate_tokens", "estimate_messages", "estimator_name"):
            self.assertIn(name, tokens.__all__)


class ContextEventTests(unittest.TestCase):
    """`run.context` 事件与 `GET /api/context/stats` 的接线。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_event_fires_per_round_and_at_finish(self) -> None:
        """每轮**裁剪之后**发一次，收尾再补一次 —— 预算条不能停在中途。

        走 `_worker` 而不是直接 `agent.run()`：收尾那一条广播在会话层的收尾
        分支里，绕开它就等于没测到「任务结束后预算条会落到最终值」。
        """
        session = Session(
            Config.load(self.ws, audit_enabled=False),
            client=FakeClient(_TWO_ROUNDS),
        )
        with mock.patch.object(session.agent, "context_stats", wraps=session.agent.context_stats) as stats:
            session._worker("run-1", "看看目录")  # noqa: SLF001 — 只为覆盖收尾广播
        events = [e for e in session.bus.snapshot(400) if e["type"] == P.RUN_CONTEXT]
        self.assertEqual(len(events), 3, "两轮各一次 + 收尾一次")
        # 每次必须带预算条要的四个关键字段
        for event in events:
            for key in ("budgetTokens", "usedTokens", "segments", "estimator", "cache"):
                self.assertIn(key, event, f"run.context 缺 {key!r}")
        # 事件载荷来自 context_stats 的快照，且它确实被调用了（不是空跑）
        self.assertGreaterEqual(stats.call_count, 3)

    def test_http_endpoint_is_readonly(self) -> None:
        """`GET /api/context/stats` 返回与快照同构的只读数据。"""
        host = HostServer(
            Config.load(self.ws, audit_enabled=True),
            port=0,
            client=FakeClient(_TWO_ROUNDS),
        )
        host.start_background()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(
                f"http://127.0.0.1:{host.port}/api/context/stats"
            )
            request.add_header("X-Vibe-Token", host.guard.token)
            with opener.open(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        finally:
            host.shutdown()
        for key in ("budgetTokens", "usedTokens", "segments", "estimator", "cache"):
            self.assertIn(key, data)
        self.assertEqual(data["budgetTokens"], 0, "默认配置不启用 token 预算")

    def test_context_in_session_snapshot(self) -> None:
        """首帧数据走 `session.ready` / `GET /api/state` 的 `context` 字段。"""
        session = Session(
            Config.load(self.ws, audit_enabled=False),
            client=FakeClient([]),
        )
        describe = session.describe()
        self.assertIn("context", describe)
        self.assertEqual(
            describe["context"]["usedTokens"],
            session.agent.context_stats()["usedTokens"],
        )


if __name__ == "__main__":
    unittest.main()