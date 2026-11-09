"""M14-C：讨论的 HTTP 面、事件流与审计三同步。

C 段的验收口径（`docs/M14-PLAN.md` §2）：

- 六类端点语义正确（400 / 404 / 405 / 409）；
- 事件序列可断言（`discussion.*`，与 `run.*` 分离）；
- `verify_chain` 通过；
- `VIBE_MULTI_AGENT=0` 时端点一律 400（fail-closed）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("VIBE_MEMORY", "0")

from agent_core.audit import verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.store import SessionStore  # noqa: E402
from agent_core.llm import ChatResponse  # noqa: E402
from agent_core.tools.base import UNTRUSTED_BEGIN, UNTRUSTED_END  # noqa: E402

TERMINAL = {"consensus", "no_consensus", "stopped", "failed", "interrupted"}


class ScriptedClient:
    """按剧本返回；剧本元素可以是字符串（终答）或异常实例（抛出）。"""

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


class GatedClient:
    """第一次调用就卡住，便于在「讨论正在跑」时测 409 / stop。"""

    supports_native_tools = True

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.started = threading.Event()
        self.gate = threading.Event()
        self.calls = 0

    def chat(self, messages, tools=None, temperature=None):
        self.calls += 1
        self.started.set()
        self.gate.wait(5)
        return ChatResponse(content=self.answer)


def _json_turn(stance: str, points: list[str], **extra) -> str:
    payload = {"stance": stance, "points": points}
    payload.update(extra)
    return "我的发言。\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


class DiscussionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "proj"
        self.ws.mkdir()
        self.store = SessionStore(self.root / "sessions.db")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        if getattr(self, "host", None) is not None:
            self.host.shutdown()
        self._tmp.cleanup()

    # ---- 基础设施 ---------------------------------------------------------
    def _start(self, *, enabled: bool = True, client=None) -> None:
        self.client = client or ScriptedClient([_json_turn("agree", ["测试要点"])] * 6)
        self.host = HostServer(
            Config.load(self.ws, multi_agent_enabled=enabled),
            port=0,
            store=self.store,
            client=self.client,
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", "X-Vibe-Token": self.token},
            method=method,
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                # BaseHTTPRequestHandler 对未实现的动词直接回 HTML 501。
                return exc.code, {"error": raw[:200]}

    def _profiles(self) -> tuple[str, str, str]:
        """建三位档案，返回 (参与者甲, 参与者乙, 主持人)。"""
        ids = []
        for name in ("甲", "乙", "主持"):
            status, payload = self._request(
                "POST", "/api/agents", {"profile": {"name": name}}
            )
            self.assertEqual(status, 200, payload)
            ids.append(payload["agent"]["id"])
        return ids[0], ids[1], ids[2]

    def _wait_terminal(self, discussion_id: str, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, payload = self._request("GET", f"/api/discussions/{discussion_id}")
            # 起线程后到首次落盘之间有一瞬竞态：400「不存在」时继续等。
            if status == 400:
                time.sleep(0.02)
                continue
            self.assertEqual(status, 200, payload)
            finished = any(
                event["type"] == "discussion.finished"
                and event.get("discussionId") == discussion_id
                for event in self._events()
            )
            if payload.get("state") in TERMINAL and finished:
                return payload
            time.sleep(0.05)
        self.fail("讨论在超时前没有进入终态")

    def _events(self) -> list[dict]:
        return self.host.ctx.session.bus.snapshot(limit=2000)

    def _audit_events(self) -> list[str]:
        path = self.ws / ".vibe" / "audit.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line).get("event", ""))
        return events

    # ---- 门控 -------------------------------------------------------------
    def test_gate_off_returns_400_for_all_endpoints(self) -> None:
        self._start(enabled=False)
        checks = [
            ("GET", "/api/discussions", None),
            ("POST", "/api/discussions", {"topic": "x"}),
            ("GET", "/api/discussions/abc", None),
            ("POST", "/api/discussions/abc/message", {"text": "x"}),
            ("POST", "/api/discussions/abc/resume", {}),
            ("POST", "/api/discussions/abc/stop", {}),
            ("POST", "/api/discussions/abc/insert", {}),
            ("POST", "/api/discussions/abc/dispatch", {}),
        ]
        for method, path, body in checks:
            status, payload = self._request(method, path, body)
            self.assertEqual(status, 400, (path, payload))
            self.assertIn("VIBE_MULTI_AGENT", payload["error"])
        # fail-closed：不落讨论目录
        self.assertFalse((self.ws / ".vibe" / "discussions").exists())
        # 关闭时 /api/state 一个键都不追加（页签靠「无 multiAgent」隐藏）
        status, state = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("multiAgent", state)

    # ---- 参数与状态语义 ---------------------------------------------------
    def test_request_id_is_required(self) -> None:
        self._start()
        a, b, m = self._profiles()
        status, payload = self._request(
            "POST",
            "/api/discussions",
            {"topic": "选型", "participants": [a, b], "moderator": m},
        )
        self.assertEqual(status, 400)
        self.assertIn("requestId", payload["error"])

    def test_bad_parameters_fail_synchronously(self) -> None:
        self._start()
        a, b, _ = self._profiles()
        base = {
            "topic": "选型",
            "participants": [a, b],
            "requestId": "req-1",
        }
        # 缺主持人
        status, payload = self._request("POST", "/api/discussions", dict(base))
        self.assertEqual(status, 400)
        self.assertIn("主持人", payload["error"])
        # 档案不存在
        status, payload = self._request(
            "POST", "/api/discussions", {**base, "moderator": "no-such"}
        )
        self.assertEqual(status, 400)
        self.assertIn("不存在", payload["error"])
        # 参与者重复
        status, payload = self._request(
            "POST",
            "/api/discussions",
            {**base, "participants": [a, a], "moderator": a},
        )
        self.assertEqual(status, 400)
        # 未开跑：不应留下讨论文件
        self.assertEqual(
            list((self.ws / ".vibe" / "discussions").glob("*.json"))
            if (self.ws / ".vibe" / "discussions").exists()
            else [],
            [],
        )

    def test_unknown_id_and_method_semantics(self) -> None:
        self._start()
        status, _ = self._request("GET", "/api/discussions/nope")
        self.assertEqual(status, 400)
        # GET 集合与 GET 单个 id 支持；POST 单个 id、GET 动作路径不支持。
        status, _ = self._request("POST", "/api/discussions/nope", {})
        self.assertEqual(status, 405)
        status, _ = self._request("GET", "/api/discussions/nope/message")
        self.assertEqual(status, 405)
        status, _ = self._request("POST", "/api/discussions/nope/dance", {})
        self.assertEqual(status, 404)
        status, _ = self._request("POST", "/api/discussions/nope/message", {"text": "x"})
        self.assertEqual(status, 400)

    # ---- 正常路径：事件 + 审计 + 增量 -------------------------------------
    def test_full_discussion_emits_events_and_verifies_audit(self) -> None:
        self._start(
            client=ScriptedClient(
                [_json_turn("agree", ["甲同意"]), _json_turn("agree", ["乙同意"])]
                + ["主持人汇总：达成一致。"] * 2
            )
        )
        a, b, m = self._profiles()
        status, state = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["multiAgent"]["enabled"])
        status, payload = self._request(
            "POST",
            "/api/discussions",
            {
                "topic": "选一个实现方案",
                "participants": [a, b],
                "moderator": m,
                "maxRounds": 1,
                "requestId": "req-full",
            },
        )
        self.assertEqual(status, 200, payload)
        discussion_id = payload["discussionId"]
        self.assertEqual(payload["state"], "running")

        final = self._wait_terminal(discussion_id)
        self.assertEqual(final["state"], "no_consensus")
        self.assertEqual(final["terminationReason"], "max_rounds")
        self.assertEqual(len(final["turns"]), 2)

        # 列表：摘要里有这一场，且不含 turns 正文
        status, listing = self._request("GET", "/api/discussions")
        self.assertEqual(status, 200)
        ids = [item["id"] for item in listing["discussions"]]
        self.assertIn(discussion_id, ids)

        # 增量：after=1 只回第二条
        status, partial = self._request(
            "GET", f"/api/discussions/{discussion_id}?after=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(partial["turns"]), 1)
        self.assertEqual(partial["after"], 1)
        self.assertEqual(partial["turns"][0]["round"], 1)

        # 事件序列（与 run.* 分离）
        types = [event["type"] for event in self._events()]
        for expected in (
            "discussion.started",
            "discussion.round",
            "discussion.turn_started",
            "discussion.turn",
            "discussion.finished",
        ):
            self.assertIn(expected, types)
        # 讨论事件都带 discussionId，且不混入 run.* 状态机
        discussion_events = [
            event for event in self._events() if event["type"].startswith("discussion.")
        ]
        self.assertTrue(all(e.get("discussionId") == discussion_id for e in discussion_events))
        self.assertFalse(any(event["type"].startswith("run.") for event in self._events()))

        # 审计三同步：五个事件都落了，且链可验证
        audited = self._audit_events()
        self.assertIn("multiagent.start", audited)
        self.assertIn("multiagent.turn", audited)
        self.assertIn("multiagent.finish", audited)
        ok, detail = verify_chain(self.ws / ".vibe" / "audit.jsonl")
        self.assertTrue(ok, detail)
        # 审计不含正文：议题与发言原文都不应出现在审计文件里
        raw = (self.ws / ".vibe" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("选一个实现方案", raw)
        self.assertNotIn("甲同意", raw)

    def test_request_id_is_idempotent(self) -> None:
        self._start()
        a, b, m = self._profiles()
        body = {
            "topic": "选型",
            "participants": [a, b],
            "moderator": m,
            "maxRounds": 1,
            "requestId": "same-req",
        }
        status, first = self._request("POST", "/api/discussions", body)
        self.assertEqual(status, 200, first)
        self._wait_terminal(first["discussionId"])

        status, second = self._request("POST", "/api/discussions", body)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["discussionId"], first["discussionId"])
        self.assertTrue(second.get("reused"))
        # 只落一份
        self.assertEqual(
            len(list((self.ws / ".vibe" / "discussions").glob("*.json"))), 1
        )

    # ---- 并发、插话、停止 -------------------------------------------------
    def test_second_start_conflicts_and_stop_records_audit(self) -> None:
        self._start(client=GatedClient(_json_turn("agree", ["卡住"])))
        a, b, m = self._profiles()
        body = {
            "topic": "选型",
            "participants": [a, b],
            "moderator": m,
            "maxRounds": 1,
            "requestId": "req-slow",
        }
        status, first = self._request("POST", "/api/discussions", body)
        self.assertEqual(status, 200, first)
        self.assertTrue(self.client.started.wait(10))
        discussion_id = first["discussionId"]

        # 第二场 → 409
        status, payload = self._request(
            "POST", "/api/discussions", {**body, "requestId": "req-other"}
        )
        self.assertEqual(status, 409, payload)

        # 讨论中插话 → 200
        status, payload = self._request(
            "POST", f"/api/discussions/{discussion_id}/message", {"text": "别忘了兼容旧版"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["chars"], len("别忘了兼容旧版"))

        # 停止 → 200，放开闸门后应收尾为 stopped
        status, payload = self._request(
            "POST", f"/api/discussions/{discussion_id}/stop", {}
        )
        self.assertEqual(status, 200, payload)
        self.client.gate.set()
        final = self._wait_terminal(discussion_id)
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["terminationReason"], "user_stop")

        audited = self._audit_events()
        self.assertIn("multiagent.stop", audited)
        self.assertIn("multiagent.finish", audited)
        ok, detail = verify_chain(self.ws / ".vibe" / "audit.jsonl")
        self.assertTrue(ok, detail)

    def test_resume_continues_from_last_incomplete_turn(self) -> None:
        self._start(
            client=ScriptedClient(
                [_json_turn("agree", ["乙同意"])] + ["主持人汇总：达成一致。"] * 2
            )
        )
        a, b, m = self._profiles()
        discussion_id = "resume01"
        folder = self.ws / ".vibe" / "discussions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{discussion_id}.json").write_text(
            json.dumps(
                {
                    "id": discussion_id,
                    "topic": "续跑测试",
                    "participants": [a, b],
                    "moderator": m,
                    "maxRounds": 1,
                    "speakerPolicy": "round_robin",
                    "state": "interrupted",
                    "terminationReason": "interrupted",
                    "seq": 1,
                    "requestId": "req-resume",
                    "turns": [
                        {
                            "turnId": f"{discussion_id}/r1/{a}",
                            "runId": "r1",
                            "agentId": a,
                            "round": 1,
                            "stance": "agree",
                            "points": ["甲同意"],
                            "rebuttals": [],
                            "changedBy": [],
                            "interject": "",
                            "evidence": [],
                            "text": "甲同意",
                            "chars": 3,
                            "ok": True,
                            "error": "",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        status, payload = self._request(
            "POST", f"/api/discussions/{discussion_id}/resume", {}
        )
        self.assertEqual(status, 200, payload)
        final = self._wait_terminal(discussion_id)
        self.assertEqual(final["state"], "no_consensus")
        self.assertEqual(len(final["turns"]), 2)
        self.assertEqual(final["turns"][1]["agentId"], b)
        # 续跑不重复记 start
        self.assertEqual(self._audit_events().count("multiagent.start"), 0)

    def test_message_and_stop_rejected_when_idle(self) -> None:
        self._start()
        a, b, m = self._profiles()
        status, payload = self._request(
            "POST",
            "/api/discussions",
            {
                "topic": "选型",
                "participants": [a, b],
                "moderator": m,
                "maxRounds": 1,
                "requestId": "req-idle",
            },
        )
        self.assertEqual(status, 200, payload)
        self._wait_terminal(payload["discussionId"])

        status, _ = self._request(
            "POST", f"/api/discussions/{payload['discussionId']}/message", {"text": "x"}
        )
        self.assertEqual(status, 400)
        status, _ = self._request(
            "POST", f"/api/discussions/{payload['discussionId']}/stop", {}
        )
        self.assertEqual(status, 400)

    # ---- 结论出口（E）------------------------------------------------------
    def _run_discussion(self) -> tuple[str, str]:
        """跑完一场 1 轮讨论，返回 (discussionId, sessionId)。"""
        self._start(
            client=ScriptedClient(
                [
                    _json_turn("agree", ["甲同意"]),
                    _json_turn("agree", ["乙同意"]),
                    "主持人汇总：采用方案 A。",
                ]
            )
        )
        a, b, m = self._profiles()
        status, payload = self._request(
            "POST",
            "/api/discussions",
            {
                "topic": "选一个实现方案",
                "participants": [a, b],
                "moderator": m,
                "maxRounds": 1,
                "requestId": "req-conclusion",
            },
        )
        self.assertEqual(status, 200, payload)
        discussion_id = payload["discussionId"]
        final = self._wait_terminal(discussion_id)
        self.assertEqual(final["state"], "no_consensus")
        status, state = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        return discussion_id, state["sessionId"]

    def test_insert_records_user_message_with_source_and_wrapper(self) -> None:
        discussion_id, session_id = self._run_discussion()
        before = len(self.store.get_messages(session_id))

        status, payload = self._request(
            "POST", f"/api/discussions/{discussion_id}/insert", {}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mode"], "insert")
        self.assertEqual(payload["discussionId"], discussion_id)
        self.assertGreater(payload["chars"], 0)

        messages = self.store.get_messages(session_id)
        self.assertEqual(len(messages), before + 1)
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn(
            f"{UNTRUSTED_BEGIN} source=discussion:{discussion_id}>>>", last["content"]
        )
        self.assertIn(f"<<<{UNTRUSTED_END}", last["content"])
        self.assertIn("多智能体讨论结论", last["content"])

        # 插入只是「记下来」，不启动新一轮
        self.assertFalse(any(e["type"].startswith("run.") for e in self._events()))

        # 同一场可以反复插入（每次都是一条新的用户消息）
        status, again = self._request(
            "POST", f"/api/discussions/{discussion_id}/insert", {}
        )
        self.assertEqual(status, 200, again)
        self.assertEqual(len(self.store.get_messages(session_id)), before + 2)

    def test_dispatch_starts_a_run_with_the_conclusion(self) -> None:
        discussion_id, session_id = self._run_discussion()

        status, payload = self._request(
            "POST", f"/api/discussions/{discussion_id}/dispatch", {}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mode"], "dispatch")
        self.assertTrue(payload["runId"])

        # 结论作为本轮用户消息落库（run 开始后才 append，这里等它出现）
        marker = f"source=discussion:{discussion_id}"
        found = None
        deadline = time.time() + 10.0
        while time.time() < deadline and found is None:
            for message in self.store.get_messages(session_id):
                content = str(message.get("content") or "")
                if message.get("role") == "user" and marker in content:
                    found = content
            if found is None:
                time.sleep(0.02)
        self.assertIsNotNone(found, "下发后应把结论作为用户消息写入历史")
        self.assertIn(UNTRUSTED_BEGIN, found)
        self.assertIn("请据此继续推进", found)
        self.assertTrue(any(e["type"] == "run.started" for e in self._events()))

    def test_insert_and_dispatch_reject_running_discussion(self) -> None:
        self._start(client=GatedClient(_json_turn("agree", ["卡住"])))
        a, b, m = self._profiles()
        status, payload = self._request(
            "POST",
            "/api/discussions",
            {
                "topic": "选型",
                "participants": [a, b],
                "moderator": m,
                "maxRounds": 1,
                "requestId": "req-running",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(self.client.started.wait(10))
        discussion_id = payload["discussionId"]

        for action in ("insert", "dispatch"):
            status, body = self._request(
                "POST", f"/api/discussions/{discussion_id}/{action}", {}
            )
            self.assertEqual(status, 400, (action, body))
            self.assertIn("进行中", body["error"])

        # 收尾：别让后台线程挂着
        self._request("POST", f"/api/discussions/{discussion_id}/stop", {})
        self.client.gate.set()
        self._wait_terminal(discussion_id)

    def test_insert_conflicts_while_agent_busy(self) -> None:
        # 讨论已结束，但主 Agent 正在跑别的任务 —— 插入应被 409 挡住，
        # 不然会在模型执行到一半时改写历史。
        self._start(client=GatedClient("干完了"))
        discussion_id = "done01"
        folder = self.ws / ".vibe" / "discussions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{discussion_id}.json").write_text(
            json.dumps(
                {
                    "id": discussion_id,
                    "topic": "已完成",
                    "participants": ["a"],
                    "moderator": "m",
                    "state": "consensus",
                    "terminationReason": "unanimous",
                    "finalAnswer": "采用方案 A。",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        status, payload = self._request("POST", "/api/run", {"text": "先干活"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(self.client.started.wait(10))

        status, body = self._request(
            "POST", f"/api/discussions/{discussion_id}/insert", {}
        )
        self.assertEqual(status, 409, body)

        self._request("POST", "/api/interrupt", {})
        self.client.gate.set()


if __name__ == "__main__":
    unittest.main()
