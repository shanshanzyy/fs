"""LLM 层测试：响应解析、脏参数容错、可达性探测。

其中 probe 相关用例是回归锁 —— 早期版本把「连接失败」和「服务正常但无模型」
混为一谈，导致环境自检谎报通过，属于最浪费排查时间的缺陷类型。
"""

from __future__ import annotations

import copy
import os
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.errors import LLMError  # noqa: E402
from agent_core.llm import (  # noqa: E402
    OpenAICompatClient,
    parse_arguments,
    parse_text_tool_calls,
    probe_endpoint,
)


def _closed_port() -> int:
    """占一个端口再释放，得到一个大概率无人监听的端口号。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ProbeTest(unittest.TestCase):
    def test_unreachable_endpoint_is_reported_as_failure(self) -> None:
        """核心回归：连不上必须返回 False，不能被当成「可达但无模型」。"""
        port = _closed_port()
        reachable, detail = probe_endpoint(f"http://127.0.0.1:{port}/v1", "k", timeout=2)
        self.assertFalse(reachable, f"端口 {port} 不应该有服务在监听，实际判定: {detail}")
        self.assertTrue(detail)

    def test_list_models_swallows_errors_but_probe_does_not(self) -> None:
        port = _closed_port()
        client = OpenAICompatClient(
            f"http://127.0.0.1:{port}/v1", model="x", api_key="k", timeout=2, max_retries=0
        )
        self.assertEqual(client.list_models(), [])
        reachable, _ = client.probe()
        self.assertFalse(reachable)

    def test_localhost_ignores_env_proxy(self) -> None:
        """公司内网机器常设 HTTP_PROXY，本地推理请求必须绕过它。

        这里断言的是功能性质（opener 上没有挂载任何代理），而不是实现细节：
        urllib 的 ProxyHandler({}) 恰恰是因为「没挂上任何 *_open 方法」而被
        OpenerDirector 跳过，从而达到禁用代理的效果。
        """
        saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
        os.environ["https_proxy"] = "http://127.0.0.1:1"
        try:
            local = OpenAICompatClient(
                "http://127.0.0.1:11434/v1", model="x", api_key="k", timeout=1
            )
            attached = [
                getattr(handler, "proxies", None)
                for handler in local._opener.handlers
                if getattr(handler, "proxies", None)
            ]
            self.assertEqual(attached, [], f"本地地址不应挂载代理，实际: {attached}")

            remote = OpenAICompatClient(
                "https://api.example.com/v1", model="x", api_key="k", timeout=1
            )
            remote_proxies = [
                getattr(handler, "proxies", None)
                for handler in remote._opener.handlers
                if getattr(handler, "proxies", None)
            ]
            self.assertTrue(remote_proxies, "非本地地址应当保留系统代理支持")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ResponseParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OpenAICompatClient(
            "http://127.0.0.1:1/v1", model="m", api_key="k", timeout=1, max_retries=0
        )

    def test_native_tool_calls(self) -> None:
        response = self.client._to_response(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "src/app.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )
        self.assertTrue(response.has_tool_calls)
        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "src/app.py"})
        self.assertEqual(response.usage["prompt_tokens"], 10)

    def test_tool_call_with_dict_arguments(self) -> None:
        response = self.client._to_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "grep",
                                        "arguments": {"pattern": "TODO"},
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        self.assertEqual(response.tool_calls[0].arguments, {"pattern": "TODO"})

    def test_tool_call_with_malformed_arguments_does_not_crash(self) -> None:
        response = self.client._to_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "grep", "arguments": "{not json"}}
                            ]
                        }
                    }
                ]
            }
        )
        self.assertEqual(response.tool_calls[0].arguments, {})

    def test_text_protocol_fallback(self) -> None:
        text = (
            "我先看看项目结构：\n"
            "```tool\n"
            '{"name": "repo_map", "arguments": {"path": "."}}\n'
            "```\n"
            "然后再决定。"
        )
        response = self.client._to_response(
            {"choices": [{"message": {"content": text}}]}
        )
        self.assertTrue(response.has_tool_calls)
        self.assertEqual(response.tool_calls[0].name, "repo_map")
        self.assertTrue(response.tool_calls[0].id.startswith("textcall_"))

    def test_json_fence_is_not_treated_as_tool_call(self) -> None:
        """普通的 ```json 示例代码块不能被误判成工具调用。"""
        text = '示例配置如下：\n```json\n{"name": "example", "arguments": {}}\n```\n完成。'
        response = self.client._to_response(
            {"choices": [{"message": {"content": text}}]}
        )
        self.assertFalse(response.has_tool_calls)

    def test_content_as_parts_array(self) -> None:
        response = self.client._to_response(
            {"choices": [{"message": {"content": [{"text": "你"}, {"text": "好"}]}}]}
        )
        self.assertEqual(response.content, "你好")

    def test_missing_choices_raises(self) -> None:
        with self.assertRaises(LLMError):
            self.client._to_response({"error": "oops"})


class _FakePost:
    """按序号给出响应/异常的假 `_post`，同时记录每次真正发出的请求体。

    用假的 `_post` 而不是起一个 HTTP 服务：本组用例要断言的是**请求体里有没有
    那个字段**，以及「被拒之后还发不发第二次」—— 这些都是请求构造层的性质，
    不该让 socket 参与进来。
    """

    def __init__(self, results) -> None:
        self._results = list(results)
        self.payloads: list[dict] = []

    def __call__(self, path: str, payload: dict) -> dict:
        self.payloads.append(copy.deepcopy(payload))
        index = min(len(self.payloads) - 1, len(self._results) - 1)
        result = self._results[index]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def keys_seen(self) -> list[bool]:
        return ["prompt_cache_key" in payload for payload in self.payloads]


_OK = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class PromptCachingTest(unittest.TestCase):
    """prompt caching（P2-17）：注入 / 探测 / 永久回退三态。

    这里锁住的核心是**「探测失败不能变成静默错」**：上游严格拒收未知字段时，
    请求必须原样重发一次（用户不该因为一个优化项而丢掉这次回答），且此后
    再也不试（不能每次白吃一次失败）。
    """

    def _client(self, mode: str, results=None, key: str = "sess-1"):
        client = OpenAICompatClient(
            "http://127.0.0.1:1/v1", model="m", api_key="k", timeout=1, max_retries=0,
            prompt_cache=mode, cache_key=key,
        )
        fake = _FakePost(results if results is not None else [_OK])
        client._post = fake  # 实例属性遮蔽绑定方法，只影响本用例
        return client, fake

    def test_off_never_injects(self) -> None:
        """`off`（直接构造本类时的默认值）必须逐字节不改请求体。"""
        client, fake = self._client("off")
        self.assertEqual(client.cache_state, "off")
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(fake.keys_seen, [False])

    def test_on_injects_stable_key(self) -> None:
        """`on` 时每次请求都带同一个前缀键 —— 键变了上游就永远命不中缓存。"""
        client, fake = self._client("on", key="sess-42")
        self.assertEqual(client.cache_state, "on")
        client.chat([{"role": "user", "content": "hi"}])
        client.chat([{"role": "user", "content": "再问一句"}])
        self.assertEqual(fake.keys_seen, [True, True])
        self.assertEqual([p["prompt_cache_key"] for p in fake.payloads], ["sess-42", "sess-42"])

    def test_auto_downgrades_once_when_upstream_rejects(self) -> None:
        """auto 探测到「不认识这个字段」：永久回退，且**只探测一次**。

        调用序必须是 3 次 _post：① 带键被拒 → ② 去掉键重发（这次回答要给到
        用户）→ ③ 后续请求不再带键。第 ③ 次若又带键，说明降级没生效。
        """
        rejected = LLMError("HTTP 400: unknown field 'prompt_cache_key' in request body")
        client, fake = self._client("auto", results=[rejected, _OK, _OK])
        first = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(first.content, "ok", "被拒后必须重发一次，不能就这样丢掉回答")
        self.assertEqual(client.cache_state, "unsupported")
        self.assertEqual(fake.keys_seen, [True, False])
        client.chat([{"role": "user", "content": "再问一句"}])
        self.assertEqual(fake.keys_seen, [True, False, False])
        self.assertEqual(len(fake.payloads), 3)

    def test_auto_success_reports_on(self) -> None:
        """auto 且上游接受：状态转为 on，后续继续注入。"""
        client, fake = self._client("auto", results=[_OK, _OK])
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.cache_state, "on")
        client.chat([{"role": "user", "content": "再来"}])
        self.assertEqual(fake.keys_seen, [True, True])

    def test_plain_400_is_not_mistaken_for_unsupported(self) -> None:
        """只有 400/422 **且**报错里点名该字段，才算「上游不支持」。

        少了第二个条件，任何一次「消息格式不对」都会被误判成不支持，然后
        静默降级 —— 那是本次改造里最难查的 bug，因此单独钉一条。
        """
        client, fake = self._client("auto", results=[LLMError("HTTP 400: messages 格式非法")])
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(fake.keys_seen, [True], "真错误不该被重发掩盖")
        self.assertNotEqual(client.cache_state, "unsupported")

    def test_cache_state_off_without_key(self) -> None:
        """没有稳定键（空 session_id）时不该注入 —— 无键注入毫无意义。"""
        client, fake = self._client("auto", key="")
        self.assertEqual(client.cache_state, "off")
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(fake.keys_seen, [False])


class ArgumentParsingTest(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(parse_arguments('{"a": 1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        self.assertEqual(parse_arguments('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_surrounding_prose(self) -> None:
        self.assertEqual(parse_arguments('好的：{"a": 1} 以上就是参数'), {"a": 1})

    def test_single_quotes(self) -> None:
        self.assertEqual(parse_arguments("{'a': 1}"), {"a": 1})

    def test_trailing_comma(self) -> None:
        self.assertEqual(parse_arguments('{"a": 1,}'), {"a": 1})

    def test_nested_object(self) -> None:
        raw = '{"a": {"b": [1, 2]}, "c": "d"}'
        self.assertEqual(parse_arguments(raw), {"a": {"b": [1, 2]}, "c": "d"})

    def test_already_dict(self) -> None:
        self.assertEqual(parse_arguments({"a": 1}), {"a": 1})

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(parse_arguments("完全不是 JSON"), {})
        self.assertEqual(parse_arguments(None), {})
        self.assertEqual(parse_arguments(""), {})

    def test_string_containing_braces(self) -> None:
        raw = '{"search": "def f(): { return 1 }"}'
        self.assertEqual(
            parse_arguments(raw), {"search": "def f(): { return 1 }"}
        )


class TextToolCallParsingTest(unittest.TestCase):
    def test_multiple_calls(self) -> None:
        text = (
            '```tool\n{"name": "read_file", "arguments": {"path": "a"}}\n```\n'
            '```tool\n{"name": "grep", "arguments": {"pattern": "b"}}\n```'
        )
        calls = parse_text_tool_calls(text)
        self.assertEqual([c.name for c in calls], ["read_file", "grep"])

    def test_function_wrapper_form(self) -> None:
        text = '```tool\n{"function": {"name": "grep", "arguments": {"pattern": "x"}}}\n```'
        calls = parse_text_tool_calls(text)
        self.assertEqual(calls[0].name, "grep")
        self.assertEqual(calls[0].arguments, {"pattern": "x"})

    def test_missing_name_skipped(self) -> None:
        self.assertEqual(parse_text_tool_calls('```tool\n{"arguments": {}}\n```'), [])
        self.assertEqual(parse_text_tool_calls("普通文本，没有代码块"), [])


if __name__ == "__main__":
    unittest.main()
