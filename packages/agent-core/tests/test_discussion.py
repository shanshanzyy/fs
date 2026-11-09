"""M14-B1：圆桌讨论引擎（无 UI）。

覆盖 `docs/M14-PLAN.md` §2-B1 要求的七条路径：

    达成 / 未达成 / 超轮 / 超时(失败) / 中断 / 解析失败 / 主持人失联

外加：逐 Turn 落盘与 resume、`requestId` 幂等、`evidence` 本地核验、
可插拔发言顺序。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("VIBE_MEMORY", "0")

from agent_core.agents.bus import DiscussionBus  # noqa: E402
from agent_core.agents.discussion import (  # noqa: E402
    Discussion,
    DiscussionEngine,
    DiscussionError,
    DiscussionStore,
    Evidence,
    conclusion_prompt,
    parse_turn,
    verify_evidence,
)
from agent_core.agents.observer import (  # noqa: E402
    DEFAULT_BUDGET,
    Observer,
    observation_budget,
)
from agent_core.agents.profile import AgentProfile  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import LLMError  # noqa: E402
from agent_core.llm import ChatResponse  # noqa: E402
from agent_core.loop import Agent, AgentHooks  # noqa: E402
from agent_core.tools import build_readonly_registry  # noqa: E402
from agent_core.tools.base import UNTRUSTED_BEGIN, UNTRUSTED_END  # noqa: E402


class ScriptedClient:
    """按剧本返回。剧本元素可以是字符串（终答）或异常实例（抛出）。"""

    supports_native_tools = True

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.calls = 0

    def chat(self, messages, tools=None, temperature=None):
        self.calls += 1
        if not self.steps:
            return ChatResponse(content="（剧本已空）")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return ChatResponse(content=step)


def _json_turn(stance: str, points: list[str], **extra) -> str:
    payload = {"stance": stance, "points": points}
    payload.update(extra)
    return "我的发言。\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def _profiles() -> list[AgentProfile]:
    return [
        AgentProfile(id="a", name="甲", role="架构师", system_prompt="你关注结构。"),
        AgentProfile(id="b", name="乙", role="审查员", system_prompt="你关注风险。"),
        AgentProfile(id="m", name="主持", role="会议主持人", system_prompt="保持中立。"),
    ]


class DiscussionHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "notes.md").write_text(
            "关键结论：采用方案 A。\n第二行。\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self) -> Config:
        return Config.load(self.ws, multi_agent_enabled=True, audit_enabled=False)

    def _engine(self, steps, **kwargs) -> tuple[DiscussionEngine, ScriptedClient]:
        client = ScriptedClient(steps)
        engine = DiscussionEngine(
            self._config(), _profiles(), client, **kwargs
        )
        return engine, client

    def _start(self, engine: DiscussionEngine, **overrides) -> Discussion:
        params = dict(
            topic="选一个实现方案",
            participants=["a", "b"],
            moderator="m",
            max_rounds=1,
        )
        params.update(overrides)
        return engine.start(**params)


class TerminationTests(DiscussionHarness):
    def test_unanimous_consensus(self) -> None:
        engine, _ = self._engine(
            [
                _json_turn("agree", ["采用方案 A"]),
                _json_turn("agree", ["同意方案 A"]),
                "最终结论：达成一致，采用方案 A。",
            ]
        )
        discussion = self._start(engine)
        self.assertEqual(discussion.state, "consensus")
        self.assertEqual(discussion.termination_reason, "unanimous")
        self.assertEqual(discussion.final_answer, "最终结论：达成一致，采用方案 A。")
        self.assertEqual(len(discussion.basis), 2)
        self.assertFalse(discussion.summary_degraded)
        self.assertTrue(DiscussionStore(self.ws).path_for(discussion.id).is_file())

    def test_max_rounds_without_consensus(self) -> None:
        engine, _ = self._engine(
            [
                _json_turn("agree", ["方案 A 更快"]),
                _json_turn("disagree", ["但缺少回滚"], rebuttals=["回滚缺失"]),
                _json_turn("agree", ["仍推荐方案 A"]),
                _json_turn("disagree", ["风险仍未解决"], rebuttals=["风险未解决"]),
                "最终结论：未达成一致。",
            ]
        )
        discussion = self._start(engine, max_rounds=2)
        self.assertEqual(discussion.state, "no_consensus")
        self.assertEqual(discussion.termination_reason, "max_rounds")
        self.assertTrue(discussion.disagreements)
        self.assertIn("回滚", discussion.disagreements[0])

    def test_unverified_basis_blocks_consensus(self) -> None:
        bad = [{"path": "missing.py", "line": 1, "quote": "不存在的话"}]
        engine, _ = self._engine(
            [
                _json_turn("agree", ["文件里有依据"], evidence=bad),
                _json_turn("agree", ["我也同意"], evidence=bad),
                "不该被调用",
            ]
        )
        discussion = self._start(engine)
        self.assertEqual(discussion.state, "no_consensus")
        self.assertEqual(discussion.termination_reason, "unverified_basis")
        self.assertFalse(discussion.turns[0].evidence[0].verified)
        self.assertEqual(discussion.turns[0].evidence[0].reason, "unreadable")

    def test_stalled_when_points_repeat(self) -> None:
        same_a = _json_turn("disagree", ["方案 A 不安全"])
        same_b = _json_turn("disagree", ["同意不安全"])
        engine, _ = self._engine([same_a, same_b, same_a, same_b, "汇总"])
        discussion = self._start(engine, max_rounds=3)
        self.assertEqual(discussion.termination_reason, "stalled")
        self.assertEqual(discussion.state, "no_consensus")
        self.assertEqual(discussion.rounds(), 2)

    def test_user_stop_before_any_turn(self) -> None:
        engine, client = self._engine(["不该被调用"], should_stop=lambda: True)
        discussion = self._start(engine)
        self.assertEqual(discussion.state, "stopped")
        self.assertEqual(discussion.termination_reason, "user_stop")
        self.assertEqual(discussion.turns, [])
        self.assertTrue(discussion.summary_degraded)
        self.assertIn("规则汇总", discussion.final_answer)
        self.assertEqual(client.calls, 0)

    def test_stop_during_turn_is_not_a_failure(self) -> None:
        # 用户停止会打断正在生成的发言，此时落盘可能抛错；这不是失败，
        # 引擎要走 user_stop 出口（stopped + finished），而不是 interrupted + failed。
        stopped = {"value": False}
        engine, _ = self._engine(
            [_json_turn("agree", ["方案 A"]), _json_turn("agree", ["同意"])],
            should_stop=lambda: stopped["value"],
        )
        original = engine.store.save
        calls = {"n": 0}

        def flaky_save(discussion):
            calls["n"] += 1
            if calls["n"] == 2:
                stopped["value"] = True
                raise RuntimeError("disk busy")
            return original(discussion)

        engine.store.save = flaky_save  # type: ignore[assignment]
        discussion = self._start(engine)
        self.assertEqual(discussion.state, "stopped")
        self.assertEqual(discussion.termination_reason, "user_stop")
        self.assertTrue(discussion.summary_degraded)

    def test_parse_failure_degrades_to_unknown(self) -> None:
        engine, _ = self._engine(
            ["我只说自然语言，不给结构化块。", _json_turn("disagree", ["不同意"]), "汇总"]
        )
        discussion = self._start(engine, max_rounds=1)
        self.assertEqual(discussion.state, "no_consensus")
        self.assertEqual(discussion.turns[0].stance, "unknown")
        self.assertTrue(discussion.turns[0].ok)
        self.assertIn("自然语言", discussion.turns[0].points[0])

    def test_error_budget_marks_failed(self) -> None:
        engine, _ = self._engine(
            [LLMError("upstream down"), LLMError("upstream down")]
        )
        discussion = self._start(engine)
        self.assertEqual(discussion.state, "failed")
        self.assertEqual(discussion.termination_reason, "error_budget")
        self.assertTrue(all(not turn.ok for turn in discussion.turns))
        self.assertTrue(discussion.summary_degraded)

    def test_moderator_failure_falls_back_to_rule_summary(self) -> None:
        engine, _ = self._engine(
            [
                _json_turn("agree", ["方案 A"]),
                _json_turn("agree", ["同意"]),
                LLMError("moderator down"),
            ]
        )
        discussion = self._start(engine)
        self.assertEqual(discussion.state, "consensus")
        self.assertTrue(discussion.summary_degraded)
        self.assertIn("规则汇总", discussion.final_answer)


class PersistenceTests(DiscussionHarness):
    def test_persist_each_turn_and_resume(self) -> None:
        stopped = {"value": False}

        def on_event(event: dict) -> None:
            if event["type"] == "turn.done" and event["seq"] == 1:
                stopped["value"] = True

        engine, _ = self._engine(
            [_json_turn("agree", ["方案 A"])],
            on_event=on_event,
            should_stop=lambda: stopped["value"],
        )
        discussion = self._start(engine, max_rounds=1)
        self.assertEqual(discussion.state, "stopped")
        self.assertEqual(len(discussion.turns), 1)

        stored = DiscussionStore(self.ws).load(discussion.id)
        self.assertEqual(len(stored.turns), 1)
        self.assertEqual(stored.turns[0].agent_id, "a")

        resumed, _client = self._engine(
            [
                _json_turn("agree", ["同意方案 A"]),
                "最终结论：达成一致。",
            ]
        )
        final = resumed.resume(discussion.id)
        self.assertEqual(final.state, "consensus")
        self.assertEqual([turn.agent_id for turn in final.turns], ["a", "b"])
        self.assertEqual(final.turns[0].turn_id, stored.turns[0].turn_id)

    def test_request_id_is_idempotent(self) -> None:
        engine, client = self._engine(
            [
                _json_turn("agree", ["方案 A"]),
                _json_turn("agree", ["同意"]),
                "汇总",
            ]
        )
        first = self._start(engine, request_id="req-1")
        calls_after_first = client.calls
        second = self._start(engine, request_id="req-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(client.calls, calls_after_first)

    def test_resume_rejects_finished_discussion(self) -> None:
        engine, _ = self._engine(
            [
                _json_turn("agree", ["方案 A"]),
                _json_turn("agree", ["同意"]),
                "汇总",
            ]
        )
        discussion = self._start(engine)
        with self.assertRaises(DiscussionError):
            engine.resume(discussion.id)


class SpeakerPolicyTests(DiscussionHarness):
    def test_moderator_selects_order(self) -> None:
        engine, client = self._engine(
            [
                "b,a",
                _json_turn("agree", ["方案 A"]),
                _json_turn("agree", ["同意"]),
                "汇总",
            ]
        )
        discussion = self._start(engine, speaker_policy="moderator_selects")
        self.assertEqual(discussion.turns[0].agent_id, "b")
        self.assertEqual(discussion.state, "consensus")
        self.assertEqual(client.calls, 4)

    def test_select_falls_back_to_fixed_order(self) -> None:
        engine, _ = self._engine(
            [
                LLMError("selector down"),
                _json_turn("agree", ["方案 A"]),
                _json_turn("agree", ["同意"]),
                "汇总",
            ]
        )
        discussion = self._start(engine, speaker_policy="moderator_selects")
        self.assertEqual([turn.agent_id for turn in discussion.turns], ["a", "b"])


class ValidationTests(DiscussionHarness):
    def test_rejects_bad_parameters(self) -> None:
        engine, _ = self._engine([])
        cases = [
            dict(topic="", participants=["a", "b"], moderator="m"),
            dict(topic="t", participants=["a"], moderator="m"),
            dict(topic="t", participants=["a", "b", "a"], moderator="m"),
            dict(topic="t", participants=["a", "nope"], moderator="m"),
            dict(topic="t", participants=["a", "b"], moderator="nope"),
            dict(topic="t", participants=["a", "b"], moderator=""),
            dict(topic="t", participants=["a", "b"], moderator="m", max_rounds=0),
            dict(topic="t", participants=["a", "b"], moderator="m", max_rounds=9),
            dict(topic="t", participants=["a", "b"], moderator="m", speaker_policy="raise_hand"),
        ]
        for params in cases:
            with self.subTest(params=params):
                with self.assertRaises(DiscussionError):
                    engine.start(**params)

    def test_gate_off_rejected(self) -> None:
        config = Config.load(self.ws, audit_enabled=False)
        with self.assertRaises(DiscussionError):
            DiscussionEngine(config, _profiles(), ScriptedClient([]))


class EvidenceTests(DiscussionHarness):
    def test_verified_quote(self) -> None:
        result = verify_evidence(
            [Evidence(path="notes.md", line=1, quote="采用方案 A")], self.ws
        )
        self.assertTrue(result[0].verified)

    def test_quote_mismatch(self) -> None:
        result = verify_evidence(
            [Evidence(path="notes.md", line=1, quote="并不存在")], self.ws
        )
        self.assertFalse(result[0].verified)
        self.assertEqual(result[0].reason, "quote_mismatch")

    def test_line_out_of_range(self) -> None:
        result = verify_evidence(
            [Evidence(path="notes.md", line=99, quote="")], self.ws
        )
        self.assertFalse(result[0].verified)
        self.assertEqual(result[0].reason, "missing")

    def test_path_escape_rejected(self) -> None:
        result = verify_evidence(
            [Evidence(path="../outside.txt", line=0, quote="")], self.ws
        )
        self.assertFalse(result[0].verified)
        self.assertEqual(result[0].reason, "missing")

    def test_internal_dir_rejected(self) -> None:
        (self.ws / ".vibe").mkdir(exist_ok=True)
        (self.ws / ".vibe" / "x.md").write_text("x", encoding="utf-8")
        result = verify_evidence(
            [Evidence(path=".vibe/x.md", line=1, quote="x")], self.ws
        )
        self.assertFalse(result[0].verified)
        self.assertEqual(result[0].reason, "missing")

    def test_parse_turn_without_json(self) -> None:
        parsed = parse_turn("没有结构化块")
        self.assertEqual(parsed.stance, "unknown")
        self.assertEqual(parsed.points, ["没有结构化块"])


class ObservationBudgetTests(unittest.TestCase):
    """预算推导：未知窗口用默认值，已知窗口按比例并钳制在 400–1200。"""

    def test_default_when_window_unknown(self) -> None:
        self.assertEqual(observation_budget(0), DEFAULT_BUDGET)

    def test_scales_with_window_and_clamps(self) -> None:
        self.assertEqual(observation_budget(131072), 655)
        self.assertEqual(observation_budget(10_000_000), 1200)
        self.assertEqual(observation_budget(1000), 400)

    def test_non_positive_ratio_falls_back(self) -> None:
        self.assertEqual(observation_budget(131072, ratio=0), DEFAULT_BUDGET)


class BusTests(unittest.TestCase):
    def test_seq_is_monotonic_and_cursor_drains_once(self) -> None:
        bus = DiscussionBus()
        bus.publish("turn.delta", agent="a", turn_id="t1", round_no=1, text="x")
        bus.publish("turn.point", agent="a", turn_id="t1", round_no=1, points=["p"])
        cursor = bus.cursor("b")
        first = cursor.drain()
        self.assertEqual([event.seq for event in first], [1, 2])
        self.assertEqual(cursor.drain(), [], "同一批事件不该被重复取走")
        bus.publish("turn.done", agent="a", turn_id="t1", round_no=1)
        self.assertEqual([event.seq for event in cursor.drain()], [3])

    def test_unknown_kind_is_rejected(self) -> None:
        bus = DiscussionBus()
        with self.assertRaises(ValueError):
            bus.publish("chat", agent="a")


class ObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = DiscussionBus()
        self.names = {"a": "甲", "b": "乙"}

    def _observer(self, owner: str = "b", **overrides) -> Observer:
        params = dict(agent_id=owner, names=self.names)
        params.update(overrides)
        return Observer(self.bus, **params)

    def _delta(self, agent: str = "a", text: str = "正文", round_no: int = 2) -> None:
        self.bus.publish(
            "turn.delta", agent=agent, turn_id="t1", round_no=round_no, text=text
        )

    def test_status_mode_shows_tools_but_no_body(self) -> None:
        self._delta(text="这段正文不该出现")
        self.bus.publish(
            "tool.call", agent="a", turn_id="t1", round_no=2, name="read_file", risk="safe"
        )
        text = self._observer(monitor="status").build(round_no=2)
        self.assertIn("read_file", text)
        self.assertNotIn("这段正文不该出现", text)
        self.assertIn(UNTRUSTED_BEGIN, text, "他人状态同样是不可信文本")

    def test_live_mode_folds_body_and_wraps_untrusted(self) -> None:
        for _ in range(4):
            self._delta(text="甲" * 100)  # 累计 400 字，触发一次折叠
        text = self._observer(monitor="live").build(round_no=2)
        self.assertIn("发言片段", text)
        self.assertIn(UNTRUSTED_BEGIN, text)
        self.assertIn("source=agent:甲", text)

    def test_full_mode_keeps_raw_delta(self) -> None:
        self._delta(text="原始增量片段")
        text = self._observer(monitor="full").build(round_no=2)
        self.assertIn("原始增量片段", text)

    def test_first_round_is_silent_but_points_still_arrive(self) -> None:
        self._delta(text="首轮正文" * 80, round_no=1)
        self.bus.publish(
            "turn.done",
            agent="a",
            turn_id="t1",
            round_no=1,
            points=["采用方案 A"],
            ok=True,
        )
        text = self._observer(monitor="live").build(round_no=1)
        self.assertNotIn("首轮正文", text, "首轮不该看到他人正文")
        self.assertIn("采用方案 A", text, "本轮结束后的要点应可见")

    def test_first_round_live_switch_opens_body(self) -> None:
        self._delta(text="首轮正文" * 80, round_no=1)
        text = self._observer(monitor="live").build(round_no=1, first_round_live=True)
        self.assertIn("首轮正文", text)

    def test_budget_truncates(self) -> None:
        for _ in range(6):
            self._delta(text="很长的一段正文" * 30)
            self.bus.publish(
                "turn.done",
                agent="a",
                turn_id="t1",
                round_no=2,
                points=["要点一", "要点二", "要点三", "要点四", "要点五"],
            )
        text = self._observer(monitor="live", budget=400).build(round_no=2)
        self.assertLessEqual(len(text), 400)
        self.assertIn(UNTRUSTED_BEGIN, text)

    def test_only_latest_three_points_kept(self) -> None:
        self.bus.publish(
            "turn.done",
            agent="a",
            turn_id="t1",
            round_no=2,
            points=["p1", "p2", "p3", "p4", "p5"],
        )
        text = self._observer().build(round_no=2)
        self.assertNotIn("p1", text)
        self.assertNotIn("p2", text)
        for kept in ("p3", "p4", "p5"):
            self.assertIn(kept, text)

    def test_own_events_are_ignored(self) -> None:
        self._delta(agent="b", text="乙" * 400)
        self.assertEqual(self._observer(owner="b").build(round_no=2), "")


class _RecordingClient:
    supports_native_tools = True

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools=None, temperature=None):
        self.seen.append([dict(item) for item in messages])
        step = self.steps.pop(0) if self.steps else "（剧本已空）"
        if isinstance(step, BaseException):
            raise step
        return ChatResponse(content=step)


class ObservationInjectionTests(DiscussionHarness):
    """观测块是临时消息：模型看得到，会话历史与最终答案里留不下。"""

    def _agent(self, client, provider) -> Agent:
        return Agent(
            self._config(),
            registry=build_readonly_registry(lsp=False),
            client=client,
            hooks=AgentHooks(),
            approver=lambda request: "deny",
            observation_provider=provider,
        )

    def test_observation_is_present_for_model_but_absent_after_run(self) -> None:
        client = _RecordingClient(["结论：没问题"])
        agent = self._agent(client, lambda: "OBS-MARK 他人动态")
        agent.run("看一下")
        self.assertTrue(
            any("OBS-MARK" in str(item.get("content", "")) for item in client.seen[0])
        )
        self.assertFalse(
            any("OBS-MARK" in str(item.get("content", "")) for item in agent.messages),
            "观测块不该留在会话历史里",
        )

    def test_provider_failure_does_not_break_the_turn(self) -> None:
        def boom() -> str:
            raise RuntimeError("观测源坏了")

        client = _RecordingClient(["结论：照常完成"])
        agent = self._agent(client, boom)
        self.assertEqual(agent.run("看一下"), "结论：照常完成")
        self.assertEqual(len(client.seen), 1)


class InterjectionTests(DiscussionHarness):
    def test_per_agent_cap_and_persisted_queue(self) -> None:
        steps = [
            _json_turn("disagree", ["r1a"], interject="甲想插话一"),
            _json_turn("disagree", ["r1b"], interject="乙想插话一"),
            _json_turn("revise", ["r2a"], interject="甲想插话二"),
            _json_turn("revise", ["r2b"], interject="乙想插话二"),
            _json_turn("disagree", ["r3a"], interject="甲想插话三"),
            _json_turn("disagree", ["r3b"], interject="乙想插话三"),
            "汇总：分歧集中在落地节奏。",
        ]
        engine, _ = self._engine(steps)
        discussion = self._start(engine, max_rounds=3)

        self.assertEqual(len(discussion.interjections), 6)
        accepted = [item for item in discussion.interjections if item["accepted"]]
        self.assertEqual(len(accepted), 4, "每人最多受理 2 次")
        rejected = [item for item in discussion.interjections if not item["accepted"]]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(item["reason"] == "已达举手上限" for item in rejected))

        reloaded = DiscussionStore(self.ws).load(discussion.id)
        self.assertEqual(len(reloaded.interjections), 6, "举手队列必须逐 Turn 落盘")


class StalledPointRepeatTests(DiscussionHarness):
    def test_same_point_three_times_without_new_evidence_stalls(self) -> None:
        """立场在变、论点不变 → 旧规则看不出来，按「同一论点 ≥3 次」判定停滞。"""
        steps = [
            _json_turn("disagree", ["整体延后"]),
            _json_turn("disagree", ["整体延后"]),
            _json_turn("revise", ["整体延后"]),
            _json_turn("revise", ["整体延后"]),
            "汇总：未达成一致。",
        ]
        engine, _ = self._engine(steps)
        discussion = self._start(engine, max_rounds=3)
        self.assertEqual(discussion.termination_reason, "stalled")
        self.assertEqual(discussion.rounds(), 2)


class AbortedSettlementTests(DiscussionHarness):
    """执行线程异常退出后，记录不能永久停在 running。

    这类收尾以前完全缺失：线程一挂，界面就永远显示「进行中」，
    用户既拿不到结论，也没法 resume。
    """

    def test_settle_aborted_marks_running_as_interrupted(self) -> None:
        engine, _ = self._engine([])
        store = DiscussionStore(self.ws)
        store.save(
            Discussion(id="d-abort", topic="t", participants=["a"], moderator="m")
        )

        self.assertTrue(engine.settle_aborted("d-abort"))

        reloaded = store.load("d-abort")
        self.assertEqual(reloaded.state, "interrupted")
        self.assertEqual(reloaded.termination_reason, "interrupted")

    def test_settle_aborted_leaves_terminal_state_alone(self) -> None:
        engine, _ = self._engine([])
        store = DiscussionStore(self.ws)
        store.save(
            Discussion(
                id="d-done",
                topic="t",
                participants=["a"],
                moderator="m",
                state="stopped",
                termination_reason="user_stop",
            )
        )

        self.assertFalse(engine.settle_aborted("d-done"))
        self.assertEqual(store.load("d-done").termination_reason, "user_stop")

    def test_settle_aborted_ignores_missing_record(self) -> None:
        engine, _ = self._engine([])
        self.assertFalse(engine.settle_aborted("d-missing"))


class ConclusionPromptTests(unittest.TestCase):
    """结论出口（M14-E）：插入对话 / 下发任务共用同一个包装器。

    验收要求只有一条：结论正文必须带来源标注，且被 `wrap_untrusted` 包裹 ——
    讨论是模型产出，和网页、命令输出一样属于不可信文本，进上下文前必须隔离。
    """

    def _discussion(self, **overrides) -> Discussion:
        data = dict(
            id="d-e",
            topic="选一个实现方案",
            participants=["a", "b"],
            moderator="m",
            state="consensus",
            termination_reason="unanimous",
            final_answer="采用方案 A。",
            basis=["a: 结构更简单", "b: 风险可控"],
        )
        data.update(overrides)
        return Discussion(**data)

    def test_body_is_wrapped_with_source_label(self) -> None:
        text = conclusion_prompt(self._discussion())
        self.assertIn(f"{UNTRUSTED_BEGIN} source=discussion:d-e>>>", text)
        self.assertIn(f"<<<{UNTRUSTED_END}", text)
        self.assertIn("采用方案 A。", text)
        # 来源标注同时写在头部，界面上不看包裹标记也能认出这是讨论结论
        self.assertIn("多智能体讨论结论", text)
        self.assertIn("议题：选一个实现方案", text)
        self.assertIn("已达成一致", text)
        self.assertIn("全员一致", text)

    def test_insert_and_dispatch_differ_in_lead(self) -> None:
        insert = conclusion_prompt(self._discussion(), as_task=False)
        dispatch = conclusion_prompt(self._discussion(), as_task=True)
        self.assertIn("插入对话", insert)
        self.assertIn("请据此继续推进", dispatch)
        self.assertIn("审批", dispatch)
        # 两种出口的正文与包裹完全一致，只有引导语不同
        self.assertIn("采用方案 A。", insert)
        self.assertIn("采用方案 A。", dispatch)

    def test_without_final_answer_falls_back_to_basis(self) -> None:
        text = conclusion_prompt(
            self._discussion(
                state="no_consensus",
                termination_reason="max_rounds",
                final_answer="",
            )
        )
        self.assertIn("没有产出汇总结论", text)
        self.assertIn("a: 结构更简单", text)
        self.assertIn("未达成一致", text)
        self.assertIn("轮次用完", text)
        self.assertIn(f"{UNTRUSTED_BEGIN} source=discussion:d-e>>>", text)

    def test_degraded_summary_is_flagged(self) -> None:
        text = conclusion_prompt(self._discussion(summary_degraded=True))
        self.assertIn("主持人汇总失败", text)


if __name__ == "__main__":
    unittest.main()
