"""会话持久化（A2）测试。

三条硬要求来自 M1.5-PLAN 的验收标准：

1. 消息往返无损 —— 存进去的序列必须能原样重放（含 tool_calls 结构）；
2. 恢复防线 —— 孤立 tool 消息必须在服务端被 400 拒绝，不能等模型服务炸；
3. 损坏容忍 —— 库文件被截断/写坏时 host 必须能起来（历史可丢，服务不可亡）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.host.store import SessionStore, validate_message_group  # noqa: E402
from tests.test_stream import FakeStreamClient  # noqa: E402


def _assistant_tool_calls(call_id: str = "call_1", name: str = "read_file") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": '{"path": "a.py"}'},
            }
        ],
    }


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.db"
        self.store = SessionStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _append(self, sid: str, role: str, content: str = "", meta: str | None = None, title: str | None = None):
        self.store.append_message(
            sid,
            workspace="w:\\demo",
            model="test-model",
            role=role,
            content=content,
            tool_meta=meta,
            title=title,
        )

    def test_append_and_list_with_workspace_filter(self) -> None:
        self._append("s1", "user", "你好")
        self.store.append_message(
            "s2", workspace="w:\\other", model="m", role="user", content="hi"
        )

        all_rows = self.store.list_sessions()
        self.assertEqual({r["id"] for r in all_rows}, {"s1", "s2"})

        only_demo = self.store.list_sessions(workspace="w:\\demo")
        self.assertEqual([r["id"] for r in only_demo], ["s1"])

        everything = self.store.list_sessions(workspace=None)
        self.assertEqual({r["id"] for r in everything}, {"s1", "s2"})

    def test_title_written_once(self) -> None:
        self._append("s1", "user", "修复 average 函数", title="修复 average 函数")
        self._append("s1", "assistant", "好的")
        row = self.store.get_session("s1")
        self.assertEqual(row["title"], "修复 average 函数")

    def test_message_roundtrip_preserves_structure(self) -> None:
        original = [
            {"role": "user", "content": "看看 a.py"},
            _assistant_tool_calls(),
            {"role": "tool", "tool_call_id": "call_1", "content": "文件内容…"},
            {"role": "assistant", "content": "读完了。"},
        ]
        for m in original:
            extra = {k: v for k, v in m.items() if k not in ("role", "content")}
            self._append(
                "s1",
                m["role"],
                m["content"],
                meta=json.dumps(extra, ensure_ascii=False) if extra else None,
            )

        restored = self.store.get_messages("s1")
        self.assertEqual(restored, original)
        # 校验通过：这条序列可以直接重放
        self.assertIsNone(validate_message_group(restored))

    def test_seq_increments_per_session(self) -> None:
        self._append("s1", "user", "1")
        self._append("s1", "assistant", "2")
        self._append("s2", "user", "x")
        self._append("s1", "assistant", "3")
        contents = [m["content"] for m in self.store.get_messages("s1")]
        self.assertEqual(contents, ["1", "2", "3"])

    def test_delete_session(self) -> None:
        self._append("s1", "user", "你好")
        self.store.delete_session("s1")
        self.assertIsNone(self.store.get_session("s1"))
        self.assertEqual(self.store.get_messages("s1"), [])

    def test_corrupt_db_is_quarantined_and_rebuilt(self) -> None:
        self._append("s1", "user", "旧数据")
        self.store.close()
        # 模拟截断损坏：写入非法内容
        self.path.write_bytes(b"this is not a sqlite database at all" * 10)
        store2 = SessionStore(self.path)
        try:
            self.assertTrue(store2.recovered, "坏库应触发重建")
            self.assertIsNone(store2.get_session("s1"))  # 旧数据没了，但服务活着
            store2.append_message(
                "s2", workspace="w", model="m", role="user", content="新数据"
            )
            self.assertEqual(store2.get_messages("s2")[0]["content"], "新数据")
        finally:
            store2.close()


class ValidateMessageGroupTests(unittest.TestCase):
    def test_valid_native_sequence(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            _assistant_tool_calls("call_1"),
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            _assistant_tool_calls("call_2", "list_dir"),
            {"role": "tool", "tool_call_id": "call_2", "content": "r2"},
            {"role": "assistant", "content": "完成"},
        ]
        self.assertIsNone(validate_message_group(messages))

    def test_orphan_tool_message_rejected(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        ]
        error = validate_message_group(messages)
        self.assertIsNotNone(error)
        self.assertIn("孤立", error)

    def test_tool_after_unrelated_assistant_rejected(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "纯文本回复"},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        ]
        self.assertIsNotNone(validate_message_group(messages))

    def test_duplicate_tool_result_rejected(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            _assistant_tool_calls("call_1"),
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1 重复"},
        ]
        self.assertIsNotNone(validate_message_group(messages))

    def test_text_protocol_sequence_is_untouched(self) -> None:
        """文本协议的结果是 user 消息，不在成组约束范围内。"""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "```tool\n{...}\n```"},
            {"role": "user", "content": "[工具结果] read_file\n..."},
            {"role": "assistant", "content": "完成"},
        ]
        self.assertIsNone(validate_message_group(messages))


class PersistIntegrationTests(unittest.TestCase):
    """Session 落盘 → 重建 → 继续对话 的完整链路。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.db = Path(self._tmp.name) / "sessions.db"
        self.store = SessionStore(self.db)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _wait_idle(self, session: Session, timeout: float = 5.0) -> None:
        assert session._thread is not None
        session._thread.join(timeout=timeout)
        self.assertEqual(session.state, "idle")

    def test_messages_are_persisted_exactly(self) -> None:
        config = Config.load(self.ws)
        session = Session(
            config,
            client=FakeStreamClient([[("list_dir", {})], "看完了。"]),
            store=self.store,
        )
        session.submit("看看目录")
        self._wait_idle(session)

        rows = self.store.get_messages(config.session_id)
        self.assertEqual([m["role"] for m in rows], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(rows, session.agent.messages[1:])  # 去掉 system 逐字一致
        self.assertIsNone(validate_message_group(rows))
        row = self.store.get_session(config.session_id)
        self.assertEqual(row["title"], "看看目录")
        self.assertEqual(row["workspace"], str(self.ws))

    def test_resume_replays_and_continues_same_session(self) -> None:
        config = Config.load(self.ws)
        session = Session(
            config,
            client=FakeStreamClient([[("list_dir", {})], "看完了。"]),
            store=self.store,
        )
        session.submit("看看目录")
        self._wait_idle(session)
        sid = config.session_id
        history = self.store.get_messages(sid)

        # 进程重启的等价物：全新 Config（新随机 session_id）+ 同一个库
        config2 = Config.load(self.ws)
        self.assertNotEqual(config2.session_id, sid)
        session2 = Session(
            config2,
            client=FakeStreamClient(["好的，继续。"]),
            store=self.store,
        )
        session2.resume(history, session_id=sid)
        # 历史重放：system + 原序列
        self.assertEqual(session2.agent.messages[1:], history)
        # Config 是 frozen dataclass，resume 用 replace 换实例 —— 会话上的
        # 引用才是权威；落回原 sid 的最终证据在下面的 grown 断言里。
        self.assertEqual(session2.config.session_id, sid, "恢复后新消息应落回原会话")

        session2.submit("继续")
        self._wait_idle(session2)
        grown = self.store.get_messages(sid)
        self.assertEqual(len(grown), len(history) + 2)  # 新 user + 新 assistant
        self.assertEqual(grown[: len(history)], history)  # 历史前缀不动
        self.assertEqual(grown[-1]["content"], "好的，继续。")
        # 恢复后的新消息不能覆盖原会话标题
        self.assertEqual(self.store.get_session(sid)["title"], "看看目录")

    def test_resume_rejects_busy_session(self) -> None:
        from agent_core.host.session import BusyError

        config = Config.load(self.ws)
        session = Session(config, client=FakeStreamClient(["第一轮"]), store=self.store)
        session.submit("hi")
        assert session._thread is not None
        session._thread.join(timeout=5)
        session._state = "running"  # 直接构造忙碌态
        try:
            with self.assertRaises(BusyError):
                session.resume([{"role": "user", "content": "x"}])
        finally:
            session._state = "idle"


class SessionApiTests(unittest.TestCase):
    """HTTP 层：/api/sessions 三件套 + 400 防线。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = (self.root / "ws-a").resolve()
        self.ws.mkdir()
        self.store = SessionStore(self.root / "sessions.db")
        self.host = HostServer(
            Config.load(self.ws),
            port=0,
            client=FakeStreamClient([[("list_dir", {})], "看完了。"]),
            store=self.store,
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self.store.close()
        self._tmp.cleanup()

    def _url(self, path: str) -> str:
        return self.base + path

    def _get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self._url(path))
        request.add_header("X-Vibe-Token", self.token)
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self._url(path), data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("X-Vibe-Token", self.token)
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _run_once(self) -> str:
        status, data = self._post("/api/run", {"text": "看看目录"})
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, state = self._get("/api/state")
            if state["state"] == "idle":
                return data["runId"]
            time.sleep(0.05)
        self.fail("任务没有在超时内结束")

    def test_list_and_messages_after_run(self) -> None:
        self._run_once()
        sid = self.host.config.session_id

        status, listing = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in listing["sessions"]], [sid])
        self.assertEqual(listing["sessions"][0]["title"], "看看目录")

        status, detail = self._get(f"/api/sessions/{sid}/messages")
        self.assertEqual(status, 200)
        self.assertEqual(
            [m["role"] for m in detail["messages"]],
            ["user", "assistant", "tool", "assistant"],
        )

    def test_messages_of_unknown_session_404(self) -> None:
        status, _ = self._get("/api/sessions/nope/messages")
        self.assertEqual(status, 404)

    def test_resume_from_other_workspace_rejected(self) -> None:
        self._run_once()
        sid = self.host.config.session_id

        # 另一个工作区的 host，共享同一个库
        ws_b = (self.root / "ws-b").resolve()
        ws_b.mkdir()
        host_b = HostServer(
            Config.load(ws_b),
            port=0,
            client=FakeStreamClient(["ok"]),
            store=self.store,
        )
        host_b.start_background()
        try:
            base_b = f"http://127.0.0.1:{host_b.port}"
            request = urllib.request.Request(
                f"{base_b}/api/sessions/{sid}/resume", data=b"{}", method="POST"
            )
            request.add_header("X-Vibe-Token", host_b.guard.token)
            try:
                self._opener.open(request, timeout=10)
                self.fail("跨工作区恢复应当被拒绝")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                body = json.loads(exc.read().decode("utf-8"))
                self.assertIn("工作区", body["error"])
        finally:
            host_b.shutdown()

    def test_rejected_body_is_drained_without_reset(self) -> None:
        """回归：**直接回 400 而不读请求体**的分支必须排空请求体。

        keep-alive 下残留的请求体字节会让连接关闭时发出 TCP RST，客户端读到
        400 响应头、再读响应体时即报 `ConnectionAbortedError [WinError 10053]`
        —— 这曾是 ~1/3 概率的 flaky（请求体小时往往侥幸躲过）。这里用 512 KiB
        把「残留」放大成确定复现：修复后必须每轮都稳定读到 400 正文。
        """
        self._run_once()
        sid = self.host.config.session_id

        ws_b = (self.root / "ws-b").resolve()
        ws_b.mkdir()
        host_b = HostServer(
            Config.load(ws_b), port=0, client=FakeStreamClient(["ok"]), store=self.store
        )
        host_b.start_background()
        payload = b'{"pad":"' + b"x" * (512 * 1024) + b'"}'
        try:
            base_b = f"http://127.0.0.1:{host_b.port}"
            for _ in range(5):
                request = urllib.request.Request(
                    f"{base_b}/api/sessions/{sid}/resume", data=payload, method="POST"
                )
                request.add_header("X-Vibe-Token", host_b.guard.token)
                try:
                    self._opener.open(request, timeout=10)
                    self.fail("跨工作区恢复应当被拒绝")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 400)
                    body = json.loads(exc.read().decode("utf-8"))
                    self.assertIn("工作区", body["error"])
        finally:
            host_b.shutdown()

    def test_resume_rejects_orphan_tool_messages(self) -> None:
        sid = "handmade"
        self.store.append_message(
            sid, workspace=str(self.ws), model="m", role="user", content="hi"
        )
        self.store.append_message(
            sid,
            workspace=str(self.ws),
            model="m",
            role="tool",
            content="孤儿结果",
            tool_meta=json.dumps({"tool_call_id": "call_x"}),
        )
        status, body = self._post(f"/api/sessions/{sid}/resume", {})
        self.assertEqual(status, 400)
        self.assertIn("孤立", body["error"])

    def test_resume_ok_and_continues(self) -> None:
        self._run_once()
        sid = self.host.config.session_id
        before = self.store.get_messages(sid)

        status, body = self._post(f"/api/sessions/{sid}/resume", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["restored"], len(before))

        # 恢复后状态里的 sessionId 应切到被恢复的会话
        _, state = self._get("/api/state")
        self.assertEqual(state["sessionId"], sid)


if __name__ == "__main__":
    unittest.main()
