"""计划视图（P2-12）测试。

重点不在「能不能显示一个列表」，而在三件**静默错**：

1. **校验失守**。步骤超条数/超长/状态非法/重复 id 若被放进计划，界面上就会出现
   一份模型自己都没打算给的清单，而且不报错、只是错。这里逐条钉住「整批拒绝」。
2. **全量替换退化成追加**。`update_plan` 每次给出完整步骤表；一旦变成追加，
   计划会随每轮更新膨胀，而这个问题只在多轮之后才暴露。
3. **计划偷偷落盘**。计划是运行态（SAFE + 纯内存）。一旦它写进工作区，
   就会变成一份会跟着代码一起被提交的产物，同时「不改文件」的承诺也失效了。

另外钉住两条接线：工具 → `run.plan` 事件 → 会话快照；以及人工勾选端点。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.host import protocol as P  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools import ToolContext, build_default_registry  # noqa: E402
from agent_core.tools.plan import (  # noqa: E402
    MAX_PLAN_STEPS,
    MAX_STEP_TEXT,
    PLAN_STATUSES,
)
from tests.test_agent_loop import FakeClient  # noqa: E402


class PlanToolTests(unittest.TestCase):
    """工具层：校验 / 幂等 / 不落盘。不经过 Agent，直接把工具跑起来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self, sink=None) -> ToolContext:
        config = Config.load(self.ws)
        return ToolContext(
            config=config,
            paths=PathGuard(config.workspace, allow_sensitive=False),
            commands=CommandGuard(config.workspace, allow_network=False),
            # 审计关掉：本组用例关心的是「工具做了什么」，不是审计内容；
            # 开着会在临时工作区里写 .vibe/，反而干扰第 ④ 条断言。
            audit=AuditLog(config.audit_path, session_id=config.session_id, enabled=False),
            plan_sink=sink,
        )

    def _run(self, args: dict, sink=None):
        registry = build_default_registry(lsp=False)
        return registry.execute(self._ctx(sink), "update_plan", args)

    # ---- 注册 ----
    def test_registered_as_safe_tool(self) -> None:
        registry = build_default_registry(lsp=False)
        spec = registry.get("update_plan")
        self.assertIsNotNone(spec, "update_plan 必须默认注册（G2）")
        self.assertEqual(spec.risk.value, "safe", "计划是纯内存动作，必须是 SAFE 级")
        self.assertIn("update_plan", registry.names())

    # ---- ① 校验 ----
    def test_rejects_too_many_steps(self) -> None:
        steps = [{"id": str(i), "text": f"第 {i} 步"} for i in range(MAX_PLAN_STEPS + 1)]
        result = self._run({"steps": steps})
        self.assertFalse(result.ok)
        self.assertIn("上限", result.content)

    def test_rejects_overlong_text(self) -> None:
        result = self._run({"steps": [{"text": "长" * (MAX_STEP_TEXT + 1)}]})
        self.assertFalse(result.ok)
        self.assertIn("过长", result.content)

    def test_rejects_unknown_status(self) -> None:
        result = self._run({"steps": [{"text": "改代码", "status": "doing"}]})
        self.assertFalse(result.ok)
        self.assertIn("status", result.content)

    def test_rejects_duplicate_ids(self) -> None:
        result = self._run(
            {"steps": [{"id": "a", "text": "一"}, {"id": "a", "text": "二"}]}
        )
        self.assertFalse(result.ok)
        self.assertIn("重复", result.content)

    def test_rejects_empty_text_and_non_list(self) -> None:
        self.assertFalse(self._run({"steps": [{"text": "   "}]}).ok)
        self.assertFalse(self._run({"steps": "先看代码"}).ok)
        self.assertFalse(self._run({"steps": ["先看代码"]}).ok)

    # ---- ② 幂等替换 ----
    def test_full_replacement_is_idempotent(self) -> None:
        seen: list[list[dict[str, str]]] = []
        first = [
            {"id": "1", "text": "读代码", "status": "in_progress"},
            {"text": "改代码"},
        ]
        second = [{"id": "1", "text": "读代码", "status": "done"}]

        result = self._run({"steps": first}, sink=seen.append)
        self.assertTrue(result.ok)
        # 缺省 id / status 会被补上：界面按 id 定位，模型省一次多余的输出
        self.assertEqual(
            seen[-1],
            [
                {"id": "1", "text": "读代码", "status": "in_progress"},
                {"id": "s2", "text": "改代码", "status": "pending"},
            ],
        )
        self.assertIn("待办", result.content)

        self.assertTrue(self._run({"steps": second}, sink=seen.append).ok)
        # 第二次是**替换**而不是追加：最终只剩第二次给的那一条
        self.assertEqual(seen[-1], [{"id": "1", "text": "读代码", "status": "done"}])

    def test_empty_steps_clears_plan(self) -> None:
        seen: list[list[dict[str, str]]] = []
        result = self._run({"steps": []}, sink=seen.append)
        self.assertTrue(result.ok)
        self.assertEqual(seen, [[]])
        self.assertIn("清空", result.content)

    def test_works_without_sink(self) -> None:
        """没人接收（直接调 registry 的场景）时照旧校验并回话，不炸。"""
        result = self._run({"steps": [{"text": "看代码"}]})
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["revision"], -1)

    # ---- ④ 不落盘 ----
    def test_plan_never_touches_disk(self) -> None:
        for _ in range(2):
            self._run({"steps": [{"id": "1", "text": "看代码", "status": "done"}]})
        self.assertFalse(
            (self.ws / ".vibe").exists(),
            "计划是运行态：跑完不该在工作区留下任何文件（连 .vibe 都不该建）",
        )
        self.assertEqual(list(self.ws.iterdir()), [], "工作区应保持原样")


class PlanSessionTests(unittest.TestCase):
    """会话层：工具 → run.plan 事件 → 快照，以及人工勾选。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _session(self, steps) -> Session:
        return Session(Config.load(self.ws, audit_enabled=True), client=FakeClient(steps))

    def _events(self, session: Session, type_: str) -> list[dict]:
        return [e for e in session.bus.snapshot(400) if e["type"] == type_]

    def _planned_session(self) -> Session:
        session = self._session(
            [
                [
                    (
                        "update_plan",
                        {
                            "steps": [
                                {"id": "1", "text": "读代码", "status": "done"},
                                {"text": "改代码", "status": "in_progress"},
                            ]
                        },
                    )
                ],
                "做完了。",
            ]
        )
        self.assertEqual(session.agent.run("把登录逻辑改一下"), "做完了。")
        return session

    def test_tool_emits_plan_event_and_snapshot(self) -> None:
        session = self._planned_session()
        plans = self._events(session, P.RUN_PLAN)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["revision"], 1)
        self.assertEqual([s["id"] for s in plans[0]["steps"]], ["1", "s2"])
        self.assertEqual([s["status"] for s in plans[0]["steps"]], ["done", "in_progress"])

        snapshot = session.describe()["plan"]
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(len(snapshot["steps"]), 2)
        # 快照是拷贝：改动它不该动到 Agent 手里的计划
        snapshot["steps"][0]["status"] = "skipped"
        self.assertEqual(session.agent.plan[0]["status"], "done")

    def test_no_plan_event_when_tool_unused(self) -> None:
        """③ 不用这个工具时，界面拿到的计划快照为空、事件流里一条 run.plan 都没有。"""
        session = self._session([[("list_dir", {})], "看完了。"])
        session.agent.run("看看目录")
        self.assertEqual(self._events(session, P.RUN_PLAN), [])
        self.assertIsNone(
            session.describe()["plan"]["steps"] or None,
            "没有计划时快照必须是空列表 —— 界面据此不显示卡片",
        )

    def test_toggle_updates_status_and_records_audit(self) -> None:
        session = self._planned_session()
        with mock.patch.object(session.agent.audit, "record") as record:
            result = session.plan_toggle("s2", "done")
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(session.agent.plan[1]["status"], "done")

        call = next(c for c in record.call_args_list if c.args[0] == "plan.toggle")
        self.assertEqual(call.kwargs["id"], "s2")
        self.assertEqual(call.kwargs["status"], "done")
        self.assertEqual(call.kwargs["steps_count"], 2)

        plans = self._events(session, P.RUN_PLAN)
        self.assertEqual(len(plans), 2, "勾选后也要广播一次，界面才跟得上")
        self.assertEqual(plans[-1]["revision"], 2)

    def test_toggle_does_not_conflict_while_running(self) -> None:
        """运行中不 409：勾选只是改内存状态，不与写盘争用（见 protocol.py）。"""
        session = self._planned_session()
        session._state = P.STATE_RUNNING  # noqa: SLF001 — 只为钉住这条边界
        self.assertTrue(session.plan_toggle("1", "pending")["ok"])

    def test_toggle_rejects_bad_input(self) -> None:
        session = self._planned_session()
        with self.assertRaises(ValueError):
            session.plan_toggle("nope", "done")
        with self.assertRaises(ValueError):
            session.plan_toggle("1", "finished")
        with self.assertRaises(ValueError):
            session.plan_toggle("", "done")
        self.assertIn("done", PLAN_STATUSES)

    def test_reset_clears_plan(self) -> None:
        session = self._planned_session()
        session.reset()
        self.assertEqual(session.describe()["plan"]["steps"], [])


class PlanHttpTests(unittest.TestCase):
    """`/api/plan/toggle` 的接线（状态码与载荷，逻辑在会话层）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.host = HostServer(
            Config.load(self.ws, audit_enabled=True),
            port=0,
            client=FakeClient(
                [
                    [("update_plan", {"steps": [{"id": "1", "text": "读代码"}]})],
                    "做完了。",
                ]
            ),
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._tmp.cleanup()

    def _get(self, path: str):
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        return self._opener.open(request, timeout=15)

    def _post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("X-Vibe-Token", self.token)
        request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=15)

    def _code(self, path: str, body: dict) -> int:
        try:
            with self._post(path, body) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_toggle_round_trip(self) -> None:
        self.host.session.agent.run("把登录逻辑改一下")

        with self._post("/api/plan/toggle", {"id": "1", "status": "done"}) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["revision"], 2)

        with self._get("/api/state") as response:
            state = json.loads(response.read().decode("utf-8"))
        self.assertEqual(state["plan"]["steps"][0]["status"], "done")

    def test_bad_requests_are_rejected(self) -> None:
        self.host.session.agent.run("把登录逻辑改一下")
        self.assertEqual(self._code("/api/plan/toggle", {}), 400)
        self.assertEqual(
            self._code("/api/plan/toggle", {"id": "1", "status": "finished"}), 400
        )
        self.assertEqual(
            self._code("/api/plan/toggle", {"id": "ghost", "status": "done"}), 400
        )


if __name__ == "__main__":
    unittest.main()