"""内联补全 / 行内改写 / 编辑保存（阶段 D）的链路测试。

三条链路共用同一套「用户本人动作」边界（ADR-011）：
- 补全：不写盘、不进消息历史、运行中允许；越权/敏感路径 403；审计只记指纹与长度；
- 改写：预览只算不落盘；apply 按服务端缓存的 token 走 hunk 子集；运行中 409；
- 保存：expectedDigest 乐观锁，磁盘被改则 409，绝不静默覆盖。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.errors import LLMError  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.llm import (  # noqa: E402
    ChatResponse,
    OpenAICompatClient,
    window_completion_context,
)


class CompleteClient:
    """带 complete() 的假客户端：验证 FIM 优先 / chat 回退两条后端。"""

    def __init__(self, *, fim_text: str = "fim-补全", fim_supported: bool = True):
        self.fim_text = fim_text
        self.fim_supported = fim_supported
        self.fim_calls = 0
        self.chat_calls = 0

    def complete(self, prefix, suffix="", max_tokens=None, fim=True):
        if fim:
            self.fim_calls += 1
            if self.fim_supported:
                return self.fim_text, "fim"
        self.chat_calls += 1
        return f"chat-补全:{suffix}", "chat"

    def chat(self, messages, tools=None, temperature=None):
        self.chat_calls += 1
        return ChatResponse(content="```\nchat-回退补全\n```")


class _RecordingClient:
    """记录每次 complete 收到的参数：验证后端截断与 token 上限回退。"""

    def __init__(self, text: str = "rec-补全"):
        self.text = text
        self.calls: list[dict] = []

    def complete(self, prefix, suffix="", max_tokens=None, fim=True):
        self.calls.append(
            {"prefix": prefix, "suffix": suffix, "max_tokens": max_tokens, "fim": fim}
        )
        return self.text, "fim"


class _RewriteClient:
    """改写链路用的假客户端：chat() 返回固定的新选区。"""

    def __init__(self, new_selection: str):
        self.new_selection = new_selection
        self.prompts: list[str] = []

    def chat(self, messages, tools=None, temperature=None):
        self.prompts.append(messages[0]["content"])
        return ChatResponse(content=self.new_selection)


class _FimStub(OpenAICompatClient):
    """真实客户端 + 打桩的 _post：钉住能力探测缓存（不联网）。"""

    def __init__(self, *, supported: bool):
        super().__init__("http://127.0.0.1:11434/v1", "fake-model")
        self.supported = supported
        self.post_calls = 0
        self.chat_calls = 0

    def _post(self, path: str, payload: dict) -> dict:  # noqa: D102
        if path != "/completions":
            raise AssertionError(f"未预期的路径: {path}")
        self.post_calls += 1
        if not self.supported:
            raise LLMError("模型服务返回 HTTP 404\n  地址: http://x/completions")
        return {"choices": [{"text": "fim-补全"}]}

    def chat(self, messages, tools=None, temperature=None) -> ChatResponse:  # noqa: D102
        self.chat_calls += 1
        return ChatResponse(content="```\nchat-回退补全\n```")


class OpenAICompatCompleteTests(unittest.TestCase):
    """D4：FIM 优先、chat 回退、能力探测缓存。"""

    def test_fim_used_when_supported(self) -> None:
        client = _FimStub(supported=True)
        text, backend = client.complete("def f():\n    ", "\n")
        self.assertEqual((text, backend), ("fim-补全", "fim"))
        self.assertEqual(client.post_calls, 1)
        self.assertEqual(client.chat_calls, 0)

    def test_chat_fallback_when_fim_unsupported(self) -> None:
        client = _FimStub(supported=False)
        text, backend = client.complete("x", "y")
        self.assertEqual(backend, "chat")
        self.assertIn("chat-回退补全", text)  # 围栏被剥掉
        self.assertFalse(client._fim_supported)

    def test_unsupported_capability_cached(self) -> None:
        client = _FimStub(supported=False)
        client.complete("a", "b")
        client.complete("c", "d")
        # 第二次不应再白试 FIM
        self.assertEqual(client.post_calls, 1)
        self.assertEqual(client.chat_calls, 2)

    def test_fim_disabled_skips_fim(self) -> None:
        client = _FimStub(supported=True)
        text, backend = client.complete("a", "b", fim=False)
        self.assertEqual(backend, "chat")
        self.assertEqual(client.post_calls, 0)


def _read_json(response):
    return json.loads(response.read().decode("utf-8"))


class WindowContextTests(unittest.TestCase):
    """D3+ 上下文窗口截断：按整行对齐，绝不把半行喂给模型。"""

    def test_no_truncation_within_window(self) -> None:
        prefix, suffix, truncated = window_completion_context(
            "ab", "cd", prefix_chars=10, suffix_chars=10
        )
        self.assertEqual((prefix, suffix, truncated), ("ab", "cd", False))

    def test_zero_means_no_truncation(self) -> None:
        prefix, suffix, truncated = window_completion_context(
            "x" * 100, "y" * 100, prefix_chars=0, suffix_chars=0
        )
        self.assertEqual(len(prefix), 100)
        self.assertEqual(len(suffix), 100)
        self.assertFalse(truncated)

    def test_prefix_keeps_trailing_whole_lines(self) -> None:
        text = "line1\nline2\nline3"  # len 17
        prefix, _, truncated = window_completion_context(
            text, "", prefix_chars=8, suffix_chars=0
        )
        self.assertTrue(truncated)
        self.assertEqual(prefix, "line3")  # 从窗口内首个换行之后开始

    def test_suffix_keeps_leading_whole_lines(self) -> None:
        text = "aaa\nbbb\nccc"  # len 11
        _, suffix, truncated = window_completion_context(
            "", text, prefix_chars=0, suffix_chars=6
        )
        self.assertTrue(truncated)
        self.assertEqual(suffix, "aaa\n")  # 只保留窗口内最后一个完整行


class CompletionApiTests(unittest.TestCase):
    """真实 HTTP：补全 / 改写 / 保存 三条链路。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.demo = self.ws / "demo.py"
        self.demo.write_text(
            "def greet(name):\n    return f\"Hello, {name}!\"\n\n\ndef main():\n    pass\n",
            encoding="utf-8",
            newline="",
        )
        (self.ws / ".env").write_text("SECRET=1\n", encoding="utf-8", newline="")
        self.client = CompleteClient()
        self.host = HostServer(
            Config.load(self.ws), port=0, client=self.client
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        if getattr(self, "_old_client", None) is not None:
            self.host.ctx.session.agent.client = self._old_client
        self.host.shutdown()
        self._tmp.cleanup()

    def _swap_client(self, client) -> None:
        """换成记录型假客户端；tearDown 负责还原（不影响 Agent 的其它状态）。"""
        self._old_client = self.host.ctx.session.agent.client
        self.host.ctx.session.agent.client = client

    def post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Vibe-Token", self.token)
        with self.opener.open(request, timeout=15) as response:
            return _read_json(response)

    def post_status(self, path: str, body: dict) -> int:
        try:
            self.post(path, body)
        except urllib.error.HTTPError as exc:
            return exc.code
        return 200

    # ---- 补全（D5/D6） -----------------------------------------------------
    def test_complete_returns_fim_text_and_backend(self) -> None:
        data = self.post(
            "/api/complete",
            {"path": "demo.py", "prefix": "def main():\n    ", "suffix": "\n"},
        )
        self.assertEqual(data["backend"], "fim")
        self.assertEqual(data["text"], "fim-补全")
        self.assertEqual(data["path"], "demo.py")
        self.assertIn("chars", data)

    def test_complete_fallback_to_chat(self) -> None:
        client = CompleteClient(fim_supported=False)
        old = self.host.ctx.session.agent.client
        self.host.ctx.session.agent.client = client
        try:
            data = self.post(
                "/api/complete", {"path": "demo.py", "prefix": "x", "suffix": "y"}
            )
            self.assertEqual(data["backend"], "chat")
            self.assertIn("chat-", data["text"])
            self.assertEqual(client.chat_calls, 1)
        finally:
            self.host.ctx.session.agent.client = old

    def test_complete_escape_path_forbidden(self) -> None:
        self.assertEqual(
            self.post_status(
                "/api/complete", {"path": "../outside", "prefix": "x", "suffix": ""}
            ),
            403,
        )

    def test_complete_sensitive_path_forbidden(self) -> None:
        self.assertEqual(
            self.post_status(
                "/api/complete", {"path": ".env", "prefix": "x", "suffix": ""}
            ),
            403,
        )

    def test_complete_missing_path_bad_request(self) -> None:
        self.assertEqual(
            self.post_status("/api/complete", {"prefix": "x", "suffix": ""}), 400
        )

    def test_complete_audit_has_no_body(self) -> None:
        self.post("/api/complete", {"path": "demo.py", "prefix": "TOP-SECRET-前缀", "suffix": "x"})
        with self.get("/api/audit") as response:
            report = _read_json(response)
        entries = report["entries"]
        complete_entries = [e for e in entries if e["event"] == "edit.complete"]
        self.assertTrue(complete_entries)
        self.assertNotIn("TOP-SECRET-前缀", json.dumps(entries, ensure_ascii=False))
        entry = complete_entries[-1]
        self.assertIn("prefix_digest", entry)
        self.assertIn("suffix_digest", entry)
        self.assertIn("backend", entry)

    # ---- 补全增强（D3+） ---------------------------------------------------
    def test_complete_truncates_long_context(self) -> None:
        """大文件不整份上送：后端按行对齐裁到窗口内，并标出 truncated。"""
        client = _RecordingClient()
        self._swap_client(client)
        big = "".join(f"line{i}\n" for i in range(2000))  # 远超默认 4000 字符
        data = self.post("/api/complete", {"path": "demo.py", "prefix": big, "suffix": big})
        self.assertTrue(data["truncated"])
        call = client.calls[-1]
        self.assertLessEqual(len(call["prefix"]), 4000)
        self.assertLessEqual(len(call["suffix"]), 2000)
        self.assertTrue(big.endswith(call["prefix"]))    # 前缀取原文尾部
        self.assertTrue(big.startswith(call["suffix"]))  # 后缀取原文头部

    def test_complete_max_tokens_falls_back_to_config(self) -> None:
        """调用方没给 maxTokens 时用 completion_max_tokens（该配置不再形同虚设）。"""
        client = _RecordingClient()
        self._swap_client(client)
        self.post("/api/complete", {"path": "demo.py", "prefix": "a\n", "suffix": ""})
        expected = self.host.ctx.session.agent.config.completion_max_tokens
        self.assertEqual(client.calls[-1]["max_tokens"], expected)

    def test_complete_max_tokens_override_wins(self) -> None:
        client = _RecordingClient()
        self._swap_client(client)
        self.post(
            "/api/complete",
            {"path": "demo.py", "prefix": "a\n", "suffix": "", "maxTokens": 64},
        )
        self.assertEqual(client.calls[-1]["max_tokens"], 64)

    def test_complete_cache_hit_avoids_second_call(self) -> None:
        """相同请求在 TTL 内直接命中内存缓存，不重复调模型。"""
        client = _RecordingClient("dup")
        self._swap_client(client)
        body = {"path": "demo.py", "prefix": "a\n", "suffix": "b"}
        first = self.post("/api/complete", body)
        second = self.post("/api/complete", body)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["text"], "dup")
        self.assertEqual(len(client.calls), 1)

    def test_complete_cache_invalidated_on_config_change(self) -> None:
        client = _RecordingClient("dup")
        self._swap_client(client)
        body = {"path": "demo.py", "prefix": "a\n", "suffix": "b"}
        self.post("/api/complete", body)
        self.host.ctx.session.invalidate_completion_cache()
        again = self.post("/api/complete", body)
        self.assertFalse(again["cached"])
        self.assertEqual(len(client.calls), 2)

    def test_complete_cache_dropped_on_reset(self) -> None:
        client = _RecordingClient("dup")
        self._swap_client(client)
        body = {"path": "demo.py", "prefix": "a\n", "suffix": "b"}
        self.post("/api/complete", body)
        self.host.ctx.session.reset()
        again = self.post("/api/complete", body)
        self.assertFalse(again["cached"])

    def get(self, path: str):
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        return self.opener.open(request, timeout=15)

    # ---- 改写（D8）---------------------------------------------------------
    def _swap_client(self, client) -> None:
        self.host.ctx.session.agent.client = client

    def test_rewrite_preview_hunks_and_apply_subset(self) -> None:
        """模型把选区整体换掉 → 拆成 hunk → 只应用勾中的，未勾的不影响文件。"""
        old_selection = "def main():\n    pass\n"
        new_selection = "def main():\n    run()\n    cleanup()\n"
        self._swap_client(_RewriteClient(new_selection))

        preview = self.post(
            "/api/rewrite",
            {"path": "demo.py", "startLine": 5, "endLine": 6, "instruction": "加上 run 与 cleanup"},
        )
        self.assertFalse(preview["noChange"])
        self.assertTrue(preview["hunks"])
        token = preview["token"]
        self.assertTrue(token)

        accepted = [h["index"] for h in preview["hunks"]]
        result = self.post("/api/rewrite/apply", {"token": token, "accepted": accepted})
        self.assertTrue(result["changed"])
        body = self.demo.read_text(encoding="utf-8")
        self.assertIn("run()", body)
        self.assertIn("cleanup()", body)
        self.assertIn('Hello, {name}', body)  # 选区之外的代码原样保留

    def test_rewrite_apply_empty_accepted_no_change(self) -> None:
        self._swap_client(_RewriteClient("def main():\n    run()\n"))
        preview = self.post(
            "/api/rewrite",
            {"path": "demo.py", "startLine": 5, "endLine": 6, "instruction": "改动"},
        )
        token = preview["token"]
        result = self.post("/api/rewrite/apply", {"token": token, "accepted": []})
        self.assertFalse(result["changed"])
        self.assertEqual(
            self.demo.read_text(encoding="utf-8"),
            "def greet(name):\n    return f\"Hello, {name}!\"\n\n\ndef main():\n    pass\n",
        )

    def test_rewrite_apply_missing_token_bad_request(self) -> None:
        self.assertEqual(
            self.post_status("/api/rewrite/apply", {"token": "nope", "accepted": []}), 400
        )

    def test_rewrite_preview_escape_forbidden(self) -> None:
        self.assertEqual(
            self.post_status(
                "/api/rewrite",
                {"path": "../outside", "startLine": 1, "endLine": 2, "instruction": "x"},
            ),
            403,
        )

    def test_rewrite_apply_busy_conflict(self) -> None:
        """运行中调 apply 必须 409（改写会写盘，不能和 Agent 抢文件）。"""
        self._swap_client(_RewriteClient("def main():\n    run()\n"))
        preview = self.post(
            "/api/rewrite",
            {"path": "demo.py", "startLine": 5, "endLine": 6, "instruction": "改动"},
        )
        token = preview["token"]

        session = self.host.ctx.session
        session._state = "running"
        try:
            self.assertEqual(
                self.post_status("/api/rewrite/apply", {"token": token, "accepted": [0]}), 409
            )
        finally:
            session._state = "idle"

    # ---- 保存（D11）--------------------------------------------------------
    def test_save_with_stale_digest_conflict(self) -> None:
        """expectedDigest 不匹配 = 磁盘已被别处改过 → 409，绝不静默覆盖。"""
        # 打开文件时拿到指纹
        request = urllib.request.Request(
            self.base + "/api/file?path=" + urllib.parse.quote("demo.py")
        )
        request.add_header("X-Vibe-Token", self.token)
        with self.opener.open(request, timeout=15) as response:
            opened = _read_json(response)
        opened_digest = opened["digest"]

        # 别处改掉了磁盘内容（模拟 Agent 落盘 / 外部编辑器）
        self.demo.write_text("def changed():\n    pass\n", encoding="utf-8", newline="")
        self.assertEqual(
            self.post_status(
                "/api/edit/save",
                {"path": "demo.py", "content": "x = 1\n", "expectedDigest": opened_digest},
            ),
            409,
        )
        self.assertEqual(self.demo.read_text(encoding="utf-8"), "def changed():\n    pass\n")

    def test_save_success_and_sets_new_digest(self) -> None:
        opened_digest = ""
        request = urllib.request.Request(
            self.base + "/api/file?path=" + urllib.parse.quote("demo.py")
        )
        request.add_header("X-Vibe-Token", self.token)
        with self.opener.open(request, timeout=15) as response:
            opened_digest = _read_json(response)["digest"]

        result = self.post(
            "/api/edit/save",
            {"path": "demo.py", "content": "x = 1\n", "expectedDigest": opened_digest},
        )
        self.assertTrue(result["changed"])
        self.assertEqual(self.demo.read_text(encoding="utf-8"), "x = 1\n")
        self.assertTrue(result["digest"])

    def test_save_no_change(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/file?path=" + urllib.parse.quote("demo.py")
        )
        request.add_header("X-Vibe-Token", self.token)
        with self.opener.open(request, timeout=15) as response:
            opened_digest = _read_json(response)["digest"]
        result = self.post(
            "/api/edit/save",
            {"path": "demo.py", "content": self.demo.read_text(encoding="utf-8"),
             "expectedDigest": opened_digest},
        )
        self.assertFalse(result["changed"])

    def test_save_busy_conflict(self) -> None:
        session = self.host.ctx.session
        session._state = "running"
        try:
            self.assertEqual(
                self.post_status("/api/edit/save", {"path": "demo.py", "content": "x"}), 409
            )
        finally:
            session._state = "idle"

    def test_edit_dom_contract(self) -> None:
        """编辑面（D1/D9/D11）的 DOM 契约：按钮与端点都必须真实存在。

        锁的是「改个 id 或删一句绑定就静默失效」的部分 —— 编辑模式开关、
        保存按钮、改写条、hunk 卡片容器。
        """
        with self.get("/") as response:
            body = response.read().decode("utf-8")
        for element_id in (
            "btn-edit-toggle", "btn-save", "rewrite-bar", "hunk-list", "rewrite-input",
        ):
            self.assertIn(f'id="{element_id}"', body)

        with self.get("/app.js") as response:
            script = response.read().decode("utf-8")
        for endpoint in ("/api/complete", "/api/rewrite", "/api/rewrite/apply", "/api/edit/save"):
            self.assertIn(endpoint, script)


if __name__ == "__main__":
    unittest.main()
