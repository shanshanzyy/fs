"""联网检索工具单测（M7 / A7）。

覆盖四块：

1. **SSRF 六步防护矩阵** —— 协议 / host 名单 / 解析后 IP / 逐跳重定向 /
   响应限制 / 解码。全部用**确定性假网络**（patch getaddrinfo + 假 opener），
   不依赖任何真实 DNS 或外网 —— 离线可跑且不会 flaky。
2. **取正文的清洗** —— Content-Type 白名单、超限整体拒绝、charset 回退、
   多字节按字符截断、HTML→文本（剔除 script/style）。
3. **两 provider 形态** —— searxng 走真实本地 http.server（真 IO）；
   gateway 形态用既有凭证/网关范式断言「不可达即报错、不静默降级」。
4. **不变量** —— 两工具恒 REVIEW、结果恒 wrap_untrusted、不进只读面、
   `web_enabled=False` 时工具清单与基线逐字节一致、审计字段含 query_digest。

刻意不复用 `tools/web.py` 的内部 helper 做断言基线：测试要独立于被测代码，
否则被测代码改了语义、测试也跟着「自洽」地绿。
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog, digest  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import ToolError  # noqa: E402
from agent_core.llm import ChatResponse, ToolCall  # noqa: E402
from agent_core.loop import Agent, AgentHooks, ApprovalRequest  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.tools import build_default_registry, build_readonly_registry  # noqa: E402
from agent_core.tools import web as web_mod  # noqa: E402
from agent_core.tools.base import (  # noqa: E402
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    ToolContext,
)

# ------------------------------------------------------------------------- 假网络
_ADDR_TABLE: dict[str, list[str]] = {
    "public.test": ["93.184.216.34"],
    "private.test": ["10.1.2.3"],
    "loopback.test": ["127.0.0.1"],
    "linklocal.test": ["169.254.1.1"],
    "v6loop.test": ["::1"],
    # 公网 + 私网混排：用来证明「任一命中私网即整体拒绝」
    "mixed.test": ["93.184.216.34", "127.0.0.1"],
    "127.0.0.1": ["127.0.0.1"],
    "localhost": ["127.0.0.1"],
}


def _fake_getaddrinfo(host: str, port, *args, **kwargs):  # noqa: ARG001
    """确定性 DNS：只认表里的名字，其余一律 gaierror（等价「解析失败」）。"""
    ips = _ADDR_TABLE.get(str(host or "").lower())
    if ips is None:
        raise socket.gaierror(-2, "Name or service not known")
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        out.append((family, socket.SOCK_STREAM, 6, "", (ip, port or 80)))
    return out


class _FakeResp:
    """最小 urllib 响应替身：只实现被测代码真正读的 status / headers / read。"""

    def __init__(self, status: int = 200, headers: dict | None = None, body: bytes = b"") -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = body

    def read(self, n: int | None = None) -> bytes:
        return self._body if n is None else self._body[:n]

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self, handler) -> None:
        self._handler = handler

    def open(self, request, timeout=None):  # noqa: ARG002
        return self._handler(request)


def _net_case(test: unittest.TestCase):
    """装配「假 DNS + 假 opener」，返回可变的 handler 槽（含 patch 句柄，便于临时还原）。"""
    box = {"handler": lambda request: _FakeResp(), "patches": []}
    patches = [
        mock.patch.object(web_mod.socket, "getaddrinfo", _fake_getaddrinfo),
        mock.patch.object(web_mod, "_build_opener", lambda host: _FakeOpener(box["handler"])),
    ]
    for patch in patches:
        patch.start()
        test.addCleanup(patch.stop)
    box["patches"] = patches
    return box


def _events(path: Path, event: str) -> list[dict]:
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("event") == event:
            rows.append(obj)
    return rows


class _WebCase(unittest.TestCase):
    """公共夹具：临时工作区 + 假网络 + ctx/config 构造。"""

    PATCH_NET = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        self.net = _net_case(self) if self.PATCH_NET else None

    def restore_net(self) -> None:
        """还原真实网络（searxng 走真本地 http.server 的用例需要）。"""
        if self.net:
            for patch in self.net["patches"]:
                patch.stop()
            self.net = None

    def set_response(self, *, status: int = 200, headers: dict | None = None, body: bytes = b"") -> None:
        self.net["handler"] = lambda request: _FakeResp(status=status, headers=headers, body=body)

    def set_handler(self, handler) -> None:
        self.net["handler"] = handler

    def ctx(self, **overrides: object) -> ToolContext:
        config = Config.load(self.ws, web_enabled=True, **overrides)
        return ToolContext(
            config=config,
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=AuditLog(
                config.audit_path,
                session_id=config.session_id,
                enabled=config.audit_enabled,
            ),
        )

    def fetch(self, url: str, ctx: ToolContext | None = None, **overrides: object):
        """直接走工具入口 `_web_fetch`（含解码与转文本）。"""
        ctx = ctx or self.ctx(**overrides)
        return web_mod._web_fetch(ctx, {"url": url})

    def fetch_raw(self, url: str, ctx: ToolContext):
        config = ctx.config
        return web_mod._fetch(
            url,
            timeout=config.web_timeout,
            max_bytes=config.web_max_bytes,
            allow_private=config.web_fetch_allow_private,
            deny=web_mod._split_hosts(config.web_deny_hosts),
            allow=web_mod._split_hosts(config.web_allow_hosts),
        )


# --------------------------------------------------------------------- SSRF 矩阵
class SsrfTests(_WebCase):
    def test_non_http_scheme_rejected(self) -> None:
        for bad in ("file:///etc/passwd", "ftp://public.test/x", "gopher://public.test/"):
            with self.subTest(url=bad):
                with self.assertRaises(ToolError) as caught:
                    self.fetch_raw(bad, self.ctx())
                self.assertIn("http/https", str(caught.exception))

    def test_private_ip_rejected_by_default(self) -> None:
        for host in ("private.test", "loopback.test", "linklocal.test", "v6loop.test"):
            with self.subTest(host=host):
                with self.assertRaises(ToolError) as caught:
                    self.fetch_raw(f"http://{host}/", self.ctx())
                self.assertIn("SSRF", str(caught.exception))

    def test_any_private_address_in_mixed_answer_rejects(self) -> None:
        """getaddrinfo 返回公网+私网混排时，只要有一个私网就整体拒绝。"""
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://mixed.test/", self.ctx())
        self.assertIn("127.0.0.1", str(caught.exception))

    def test_allow_private_flag_opens_the_door(self) -> None:
        self.set_response(headers={"Content-Type": "text/plain"}, body=b"ok")
        result = self.fetch("http://loopback.test/", web_fetch_allow_private=True)
        self.assertTrue(result.ok)

    def test_deny_list_wins_over_allow_list(self) -> None:
        """host 同时命中黑白名单时以黑名单为准（治理面下发的拒绝恒生效）。"""
        ctx = self.ctx(web_allow_hosts="*.test", web_deny_hosts="public.test")
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://public.test/", ctx)
        self.assertIn("黑名单", str(caught.exception))

    def test_non_empty_allowlist_narrows_to_it(self) -> None:
        ctx = self.ctx(web_allow_hosts="docs.example.com")
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://public.test/", ctx)
        self.assertIn("白名单", str(caught.exception))

    def test_url_with_embedded_credentials_rejected(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://user:pass@public.test/", self.ctx())
        self.assertIn("用户名/密码", str(caught.exception))

    def test_redirect_target_is_revalidated(self) -> None:
        """每跳都重跑校验：第一跳到私网地址必须被拦。"""

        def handler(request):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "http://private.test/"}, None
            )

        self.set_handler(handler)
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://public.test/", self.ctx())
        self.assertIn("SSRF", str(caught.exception))

    def test_redirect_loop_stops_after_hop_limit(self) -> None:
        def handler(request):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "http://public.test/"}, None
            )

        self.set_handler(handler)
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://public.test/", self.ctx())
        self.assertIn("重定向", str(caught.exception))

    def test_missing_location_header_on_redirect_rejected(self) -> None:
        def handler(request):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

        self.set_handler(handler)
        with self.assertRaises(ToolError) as caught:
            self.fetch_raw("http://public.test/", self.ctx())
        self.assertIn("Location", str(caught.exception))


# ------------------------------------------------------------- 响应限制与解码
class FetchBodyTests(_WebCase):
    def test_non_document_content_type_rejected(self) -> None:
        self.set_response(headers={"Content-Type": "image/png"}, body=b"\x89PNG")
        with self.assertRaises(ToolError) as caught:
            self.fetch("http://public.test/")
        self.assertIn("Content-Type", str(caught.exception))

    def test_content_length_over_limit_rejected(self) -> None:
        self.set_response(
            headers={"Content-Type": "text/plain", "Content-Length": "999999"},
            body=b"x" * 10,
        )
        with self.assertRaises(ToolError) as caught:
            self.fetch("http://public.test/", web_max_bytes=1024)
        self.assertIn("超过上限", str(caught.exception))

    def test_stream_over_limit_rejected_even_without_content_length(self) -> None:
        """无 Content-Length 时按流式读取判定，同样整体拒绝（不截断）。"""
        self.set_response(headers={"Content-Type": "text/plain"}, body=b"a" * 4000)
        with self.assertRaises(ToolError) as caught:
            self.fetch("http://public.test/", web_max_bytes=2000)
        self.assertIn("上限", str(caught.exception))

    def test_missing_charset_falls_back_to_utf8(self) -> None:
        body = "你好，世界".encode("utf-8")
        self.set_response(headers={"Content-Type": "text/plain"}, body=body)
        result = self.fetch("http://public.test/")
        self.assertIn("你好，世界", result.content)

    def test_charset_header_is_honored(self) -> None:
        body = "中文正文".encode("gbk")
        self.set_response(headers={"Content-Type": "text/plain; charset=gbk"}, body=body)
        result = self.fetch("http://public.test/")
        self.assertIn("中文正文", result.content)

    def test_multibyte_truncation_is_by_character_not_byte(self) -> None:
        """中文切点：按字节截会切出半个字符（U+FFFD）。按字符截不应出现替换符。"""
        body = ("汉" * 500).encode("utf-8")
        self.set_response(headers={"Content-Type": "text/plain"}, body=body)
        result = web_mod._web_fetch(
            self.ctx(), {"url": "http://public.test/", "max_chars": 40}
        )
        self.assertNotIn("\ufffd", result.content, "截断切在了多字节字符中间")
        self.assertIn("汉" * 40, result.content)

    def test_html_is_converted_and_script_style_dropped(self) -> None:
        page = (
            "<html><head><title>标题A</title>"
            "<style>body{color:red}</style></head>"
            "<body><h1>正文标题</h1><p>段落一</p>"
            "<script>alert('x')</script><div>段尾</div></body></html>"
        ).encode("utf-8")
        self.set_response(headers={"Content-Type": "text/html; charset=utf-8"}, body=page)
        result = self.fetch("http://public.test/")
        self.assertIn("标题A", result.content)
        self.assertIn("正文标题", result.content)
        self.assertIn("段落一", result.content)
        self.assertNotIn("alert(", result.content, "script 内容应被剔除")
        self.assertNotIn("color:red", result.content, "style 内容应被剔除")


# --------------------------------------------------------------------- 两 provider
class _SearxngHandler(BaseHTTPRequestHandler):
    payload: bytes = b'{"results":[]}'

    def do_GET(self) -> None:  # noqa: N802
        if "/search" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # noqa: ARG002
        pass  # 静音测试日志


class SearchProviderTests(_WebCase):
    PATCH_NET = False

    def test_searxng_provider_parses_json_results(self) -> None:
        payload = json.dumps(
            {
                "results": [
                    {"title": "T1", "url": "http://a.example/1", "content": "S1"},
                    {"title": "T2", "url": "http://b.example/2", "content": "S2"},
                ]
            }
        ).encode("utf-8")
        _SearxngHandler.payload = payload
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SearxngHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/search"
        ctx = self.ctx(web_search_provider="searxng", web_search_url=endpoint)
        result = web_mod._web_search(ctx, {"query": "python asyncio"})
        self.assertTrue(result.ok)
        self.assertIn("T1", result.content)
        self.assertIn("http://a.example/1", result.content)
        self.assertEqual(result.meta["provider"], "searxng")

    def test_searxng_missing_endpoint_reports_error(self) -> None:
        ctx = self.ctx(web_search_provider="searxng", web_search_url="")
        with self.assertRaises(ToolError) as caught:
            web_mod._web_search(ctx, {"query": "x"})
        self.assertIn("VIBE_WEB_SEARCH_URL", str(caught.exception))

    def test_gateway_without_credentials_reports_error(self) -> None:
        """gateway 形态未接入治理面 → 明确报错，不静默降级。"""
        ctx = self.ctx()
        with self.assertRaises(ToolError):
            web_mod._web_search(ctx, {"query": "x"})

    def test_query_too_long_rejected(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(ToolError) as caught:
            web_mod._web_search(ctx, {"query": "q" * 401})
        self.assertIn("过长", str(caught.exception))

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(ToolError):
            web_mod._web_search(self.ctx(), {"query": "   "})

    def test_search_error_is_audited_with_digest(self) -> None:
        ctx = self.ctx(web_search_provider="searxng", web_search_url="")
        with self.assertRaises(ToolError):
            web_mod._web_search(ctx, {"query": "secret-query"})
        rows = _events(ctx.config.audit_path, "tool.web_search")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query"], "secret-query")
        self.assertEqual(rows[0]["query_digest"], digest("secret-query"))
        self.assertTrue(rows[0]["error"])


# --------------------------------------------------------------------- 不变量
class InvariantTests(unittest.TestCase):
    def test_both_specs_are_always_review(self) -> None:
        self.assertEqual(web_mod.WEB_SEARCH_SPEC.risk, Risk.REVIEW)
        self.assertEqual(web_mod.WEB_FETCH_SPEC.risk, Risk.REVIEW)

    def test_disabled_registry_matches_baseline(self) -> None:
        """web=False 时工具清单与不含 web 的默认面逐字节一致。"""
        baseline = build_default_registry(lsp=False).names()
        disabled = build_default_registry(lsp=False, web=False).names()
        self.assertEqual(baseline, disabled)
        self.assertNotIn("web_search", baseline)
        self.assertNotIn("web_fetch", baseline)

    def test_enabled_registry_adds_exactly_the_two_tools(self) -> None:
        baseline = build_default_registry(lsp=False).names()
        enabled = build_default_registry(lsp=False, web=True).names()
        self.assertEqual(sorted(set(enabled) - set(baseline)), ["web_fetch", "web_search"])

    def test_web_tools_never_enter_readonly_registry(self) -> None:
        names = build_readonly_registry(lsp=False).names()
        self.assertNotIn("web_search", names)
        self.assertNotIn("web_fetch", names)


class WrapTests(_WebCase):
    def test_fetch_result_is_wrapped_untrusted(self) -> None:
        self.set_response(headers={"Content-Type": "text/plain"}, body=b"hello")
        result = web_mod._web_fetch(self.ctx(), {"url": "http://public.test/"})
        self.assertIn(UNTRUSTED_BEGIN, result.content)
        self.assertIn(UNTRUSTED_END, result.content)

    def test_search_result_is_wrapped_untrusted(self) -> None:
        self.restore_net()
        payload = json.dumps(
            {"results": [{"title": "T", "url": "http://a.example/", "content": "S"}]}
        ).encode("utf-8")
        _SearxngHandler.payload = payload
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SearxngHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/search"
        ctx = self.ctx(web_search_provider="searxng", web_search_url=endpoint)
        result = web_mod._web_search(ctx, {"query": "q"})
        self.assertIn(UNTRUSTED_BEGIN, result.content)
        self.assertIn(UNTRUSTED_END, result.content)


# --------------------------------------------------------------------- 配置派生
class ConfigDerivationTests(unittest.TestCase):
    def test_endpoint_derivation(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name)
        gateway = Config.load(ws, web_search_provider="gateway", server_url="http://gov:8787")
        self.assertEqual(gateway.web_search_endpoint, "http://gov:8787/api/v1/search")
        no_gov = Config.load(ws, web_search_provider="gateway", server_url="")
        self.assertEqual(no_gov.web_search_endpoint, "")
        searx = Config.load(
            ws, web_search_provider="searxng", web_search_url="http://sx:8888/search"
        )
        self.assertEqual(searx.web_search_endpoint, "http://sx:8888/search")

    def test_invalid_provider_env_raises_before_start(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict("os.environ", {"VIBE_WEB_SEARCH": "bing"}):
            with self.assertRaises(Exception):
                Config.load(Path(tmp.name))


# --------------------------------------------------------------------- Part A E2E
class _PageHandler(BaseHTTPRequestHandler):
    """一个真 HTTP 页面服务：正文里放可识别的中文，便于断言「进没进上下文」。"""

    protocol_version = "HTTP/1.1"
    body = (
        "<html><head><title>文档标题</title></head>"
        "<body><p>正文甲乙丙</p><script>var x=1;</script></body></html>"
    ).encode("utf-8")

    def log_message(self, *args: object) -> None:  # 静音
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


class _ScriptedClient:
    """按剧本返回响应的假模型（同 test_agent_loop 的最小复刻）。

    剧本元素：`list[tuple[str, dict]]` = 一轮原生工具调用；`str` = 最终答复。
    """

    supports_native_tools = True

    def __init__(self, steps: list) -> None:
        self.steps = list(steps)
        self.seen_messages: list[list[dict]] = []

    def chat(self, messages, tools=None, temperature=None):  # noqa: ARG002
        import copy

        self.seen_messages.append(copy.deepcopy(messages))
        if not self.steps:
            return ChatResponse(content="（剧本已空）")
        step = self.steps.pop(0)
        if isinstance(step, str):
            return ChatResponse(content=step)
        return ChatResponse(
            content="",
            tool_calls=[
                ToolCall(id=f"call_{index}", name=name, arguments=arguments)
                for index, (name, arguments) in enumerate(step)
            ],
        )


class WebFetchApprovalE2E(unittest.TestCase):
    """Part A E2E：假模型触发 `web_fetch` → 审批浮层出完整 URL → 批准/拒绝两分支。

    为什么用**真** HTTP 页面而不是假网络：这条 E2E 要证明的是端到端接线
    （注册表带上 web 工具、审批闸门在取回之前拦住、结果包裹后进上下文），
    patch 掉网络就把「真的取到了正文」这一环证没了。
    allow_private=True 只是为了让 127.0.0.1 这个本机页面通过 SSRF 闸门。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def _serve_page(self) -> tuple[ThreadingHTTPServer, str]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        server.requests = []  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # 注册顺序：close 先注册 → LIFO 下 shutdown 先跑（停循环）再 close，
        # 否则 socket 会在 serve_forever 仍在 select 时被关掉（WinError 10038）。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}/doc"

    def _agent(self, steps: list, approver) -> tuple[Agent, _ScriptedClient]:
        config = Config.load(
            self.ws, web_enabled=True, web_fetch_allow_private=True
        )
        client = _ScriptedClient(steps)
        agent = Agent(config, client=client, approver=approver, hooks=AgentHooks())
        return agent, client

    @staticmethod
    def _context_text(client: _ScriptedClient) -> str:
        """模型在一次调用里看到的全部文本（用它判断正文进没进上下文）。"""
        return " ".join(
            str(m.get("content") or "") for m in client.seen_messages[-1]
        )

    def test_approved_fetch_body_enters_context(self) -> None:
        server, url = self._serve_page()
        requests: list[ApprovalRequest] = []

        def approver(request: ApprovalRequest) -> str:
            requests.append(request)
            return "approve"

        agent, client = self._agent(
            [[("web_fetch", {"url": url})], "已取回文档正文。"], approver
        )
        answer = agent.run("把这个页面的内容读进来")

        self.assertIn("已取回", answer)
        # 恒 REVIEW：取回之前必然问过人，且浮层里给出**完整 URL**
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].tool, "web_fetch")
        self.assertTrue(requests[0].risk is Risk.REVIEW)
        self.assertIn(url, requests[0].preview.body)
        # 真的发了请求（不是「批了却没取」）
        self.assertEqual(list(server.requests), ["/doc"])
        # 批准后：正文确实进了模型上下文，且按不可信内容包裹
        context = self._context_text(client)
        self.assertIn("正文甲乙丙", context)
        self.assertIn(UNTRUSTED_BEGIN, context)
        # script 里的噪音不该进正文
        self.assertNotIn("var x", context)

    def test_denied_fetch_never_reaches_upstream_or_context(self) -> None:
        server, url = self._serve_page()
        requests: list[ApprovalRequest] = []

        def approver(request: ApprovalRequest) -> str:
            requests.append(request)
            return "deny"

        agent, client = self._agent(
            [[("web_fetch", {"url": url})], "好的，我换个办法。"], approver
        )
        answer = agent.run("把这个页面的内容读进来")

        self.assertIn("换个办法", answer)
        self.assertEqual(len(requests), 1)
        self.assertIn(url, requests[0].preview.body)
        # 拒绝 = 一开始就不该发请求（闸门在取回之前）
        self.assertEqual(server.requests, [], "被拒绝的抓取竟然真的打了上游")
        # 正文一律不进上下文；模型只看到「被拒绝」这一事实
        context = self._context_text(client)
        self.assertNotIn("正文甲乙丙", context)
        self.assertIn("拒绝", context)


if __name__ == "__main__":
    unittest.main()