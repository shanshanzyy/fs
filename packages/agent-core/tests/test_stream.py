"""流式输出（A1）测试：SSE 解析、增量聚合、围栏守卫、Agent 接入、host 事件节流。

围栏守卫用例是回归锁 —— 文本协议下模型的工具调用是 content 里的 ```tool
围栏，一旦守卫失效，用户就会看到半截 JSON 被逐字打出来。
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host import protocol as P  # noqa: E402
from agent_core.host.bus import EventBus  # noqa: E402
from agent_core.host.session import Session, _SessionHooks  # noqa: E402
from agent_core.llm import ChatResponse, OpenAICompatClient, ToolCall  # noqa: E402
from agent_core.llm.openai_compat import _DeltaGuard  # noqa: E402
from agent_core.loop import Agent, AgentHooks  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402


# --------------------------------------------------------------------------- SSE 测试桩
def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **({"usage": usage} if usage else {}),
    }


class _SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        request = json.loads(raw or b"{}")
        spec = self.server.spec  # type: ignore[attr-defined]
        assert request.get("stream") is True, "chat_stream 必须发送 stream=true"

        if spec.get("mode") == "json":
            # 上游不支持流式的场景：无视 stream=true，回普通 JSON
            data = json.dumps(spec["payload"], ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True  # 靠 EOF 分帧
        for frame in spec["frames"]:
            self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class SSEHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
        self.server.daemon_threads = True
        self.server.spec: dict = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.client = OpenAICompatClient(
            f"http://127.0.0.1:{port}/v1", model="test-model", timeout=5, max_retries=0
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _stream(self, frames: list[dict], **kwargs) -> tuple[ChatResponse, list[str]]:
        self.server.spec = {"frames": frames}
        deltas: list[str] = []
        response = self.client.chat_stream(
            [{"role": "user", "content": "hi"}], on_delta=deltas.append, **kwargs
        )
        return response, deltas


class ChatStreamTest(SSEHarness):
    def test_text_stream_aggregates_exactly(self) -> None:
        usage = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
        response, deltas = self._stream(
            [
                _chunk({"role": "assistant", "content": ""}),
                _chunk({"content": "你好"}),
                _chunk({"content": "，世"}),
                _chunk({"content": "界"}),
                _chunk({}, finish="stop"),
                _chunk({}, usage=usage),
            ]
        )
        self.assertEqual(response.content, "你好，世界")
        self.assertEqual(response.finish_reason, "stop")
        self.assertEqual(response.usage, usage)
        self.assertEqual(response.tool_calls, [])
        # 增量按序拼接必须等于全文 —— 只延迟，不丢弃、不改写
        self.assertEqual("".join(deltas), "你好，世界")

    def test_tool_call_fragments_merge_across_chunks(self) -> None:
        args = '{"path": "src/utils.py", "limit": 10}'
        response, deltas = self._stream(
            [
                _chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": ""},
                            }
                        ],
                    }
                ),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[:14]}}]}),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[14:26]}}]}),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[26:]}}]}),
                _chunk({}, finish="tool_calls"),
            ]
        )
        self.assertEqual(deltas, [], "工具调用不应产生任何文本增量")
        self.assertEqual(response.finish_reason, "tool_calls")
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "src/utils.py", "limit": 10})

    def test_fence_guard_never_leaks_tool_json(self) -> None:
        """围栏跨片断开也不能泄漏 —— 回归锁。"""
        text = (
            "好的，我来读取文件。\n"
            '```tool\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
        )
        # 6 字符一切，制造 "`"+"`tool" 这类跨片边界
        pieces = [text[i : i + 6] for i in range(0, len(text), 6)]
        response, deltas = self._stream(
            [_chunk({"content": p}) for p in pieces] + [_chunk({}, finish="stop")]
        )
        emitted = "".join(deltas)
        self.assertNotIn("```", emitted, "围栏开头之后的内容不允许流给界面")
        self.assertEqual(emitted, "好的，我来读取文件。\n")
        # 双通道兜底不受影响：聚合结果仍解析出工具调用
        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a.py"})

    def test_non_sse_response_silently_falls_back(self) -> None:
        payload = {
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "普通回复"}}
            ],
            "usage": {},
        }
        self.server.spec = {"mode": "json", "payload": payload}
        deltas: list[str] = []
        response = self.client.chat_stream(
            [{"role": "user", "content": "hi"}], on_delta=deltas.append
        )
        self.assertEqual(response.content, "普通回复")
        self.assertEqual(deltas, [])


class DeltaGuardTest(unittest.TestCase):
    def test_holds_back_partial_fence_then_flushes(self) -> None:
        """不是围栏的普通文本：扣留的尾部在流结束时必须完整冲刷出来。"""
        pieces: list[str] = []
        guard = _DeltaGuard(pieces.append)
        for ch in "一段普通的中文回答，末尾没有任何围栏。":
            guard.feed(ch)
        guard.flush()
        self.assertEqual("".join(pieces), "一段普通的中文回答，末尾没有任何围栏。")

    def test_no_callback_is_safe(self) -> None:
        guard = _DeltaGuard(None)
        guard.feed("hello ")
        guard.feed("world")
        guard.flush()  # 不抛异常即可

    def test_fence_inside_code_block_language_tag_not_matched(self) -> None:
        """普通 markdown 代码块（```python）不是工具围栏，照常流式。"""
        pieces: list[str] = []
        guard = _DeltaGuard(pieces.append)
        for ch in "看这段：\n```python\nprint(1)\n```":
            guard.feed(ch)
        guard.flush()
        self.assertEqual("".join(pieces), "看这段：\n```python\nprint(1)\n```")


# --------------------------------------------------------------------------- Agent 接入
class FakeStreamClient:
    """带 chat_stream 的假模型。剧本形态与 FakeClient 相同。"""

    supports_native_tools = True

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.seen_messages: list[list[dict]] = []
        self.stream_calls = 0

    def chat(self, messages, tools=None, temperature=None):
        # 与 chat_stream 同一份剧本；「优先走流式」由 stream_calls 计数断言
        self.seen_messages.append(copy.deepcopy(messages))
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

    def chat_stream(self, messages, tools=None, temperature=None, on_delta=None):
        self.stream_calls += 1
        self.seen_messages.append(copy.deepcopy(messages))
        if not self.steps:
            return ChatResponse(content="（剧本已空）")
        step = self.steps.pop(0)
        if isinstance(step, str):
            if on_delta is not None:
                for i in range(0, len(step), 5):
                    on_delta(step[i : i + 5])
            return ChatResponse(content=step)
        calls = [
            ToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(step)
        ]
        return ChatResponse(content="", tool_calls=calls)


class CollectHooks(AgentHooks):
    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.texts: list[str] = []

    def on_assistant_delta(self, text: str) -> None:
        self.deltas.append(text)

    def on_assistant_text(self, text: str) -> None:
        self.texts.append(text)


class AgentStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_agent_prefers_streaming_and_forwards_deltas(self) -> None:
        config = Config.load(self.ws)
        hooks = CollectHooks()
        client = FakeStreamClient(["任务完成。"])
        agent = Agent(config=config, client=client, hooks=hooks)
        answer = agent.run("hi")
        self.assertEqual(answer, "任务完成。")
        self.assertEqual(client.stream_calls, 1)
        self.assertEqual("".join(hooks.deltas), "任务完成。")
        # 完整文本仍通过 on_assistant_text 送达一次
        self.assertEqual(hooks.texts, ["任务完成。"])

    def test_agent_tool_rounds_produce_no_deltas(self) -> None:
        config = Config.load(self.ws)
        hooks = CollectHooks()
        client = FakeStreamClient([[("repo_map", {"path": "."})], "完成。"])
        agent = Agent(config=config, client=client, hooks=hooks)
        agent.run("hi")
        self.assertEqual("".join(hooks.deltas), "完成。")

    def test_stream_disabled_falls_back_to_chat(self) -> None:
        config = Config.load(self.ws, stream_enabled=False)
        hooks = CollectHooks()
        client = FakeStreamClient(["完成。"])
        agent = Agent(config=config, client=client, hooks=hooks)
        answer = agent.run("hi")
        self.assertEqual(answer, "完成。")
        self.assertEqual(client.stream_calls, 0, "开关关闭时不得调用 chat_stream")
        self.assertEqual(hooks.deltas, [])

    def test_client_without_chat_stream_falls_back(self) -> None:
        """旧式客户端（如各测试里的 FakeClient）没有 chat_stream —— 必须照常工作。"""
        config = Config.load(self.ws)
        hooks = CollectHooks()
        client = FakeClient(["完成。"])
        agent = Agent(config=config, client=client, hooks=hooks)
        answer = agent.run("hi")
        self.assertEqual(answer, "完成。")
        self.assertEqual(hooks.deltas, [])


# --------------------------------------------------------------------------- host 事件
class SessionDeltaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.config = Config.load(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_delta_events_are_throttled_but_ordered(self) -> None:
        session = Session(self.config, client=FakeStreamClient(["好" * 300]), bus=EventBus())
        subscription = session.bus.subscribe()
        session.submit("hi")
        # run.finished 在状态翻转为 idle 之后才发布（见 _worker），
        # 轮询状态有竞态 —— 直接等工作线程结束，保证全部事件已入队。
        assert session._thread is not None
        session._thread.join(timeout=5)
        self.assertEqual(session.state, "idle")

        deltas: list[str] = []
        finished = None
        while True:
            event = subscription.get(timeout=0.2)
            if event is None:
                break
            if event["type"] == P.RUN_ASSISTANT_DELTA:
                deltas.append(event["text"])
            elif event["type"] == P.RUN_FINISHED:
                finished = event
        subscription.close()

        published = "".join(deltas)
        self.assertTrue(published, "300 字的答复应触发至少一次增量发布")
        # 节流允许尾部少量滞留，但已发布的部分必须是全文的严格前缀（只延迟不丢失不改序）
        self.assertTrue(("好" * 300).startswith(published))
        self.assertGreaterEqual(len(published), 200, "节流参数下 300 字应发布出大部分内容")
        # 节流必须显著合并增量（60 次喂入 → 远少于 60 条事件），而非逐段发布
        self.assertLessEqual(len(deltas), 10)
        self.assertIsNotNone(finished)
        assert finished is not None
        self.assertEqual(finished["answer"], "好" * 300)

    def test_hook_flush_thresholds(self) -> None:
        """字数阈值：不足阈值的片段滞留缓冲，达到阈值立即发布。"""
        session = Session(self.config, client=FakeStreamClient([]), bus=EventBus())
        hooks = session.agent.hooks
        assert isinstance(hooks, _SessionHooks)
        received: list[str] = []
        sub = session.bus.subscribe()
        hooks.on_assistant_delta("字" * 95)  # 差 1 字不到阈值
        self.assertEqual(received, [])
        self.assertEqual(self._drain(sub), [])
        hooks.on_assistant_delta("字")  # 凑满 96
        events = self._drain(sub)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "字" * 96)
        sub.close()

    @staticmethod
    def _drain(subscription) -> list[dict]:
        events = []
        while True:
            event = subscription.get(timeout=0.05)
            if event is None:
                return events
            events.append(event)


if __name__ == "__main__":
    unittest.main()
