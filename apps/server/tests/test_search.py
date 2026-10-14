"""M7：治理面搜索中转端点（POST /api/v1/search）的行为契约。

这组测试的存在理由：搜索上游只活在服务端，客户端零密钥。因此
「上游配没配、通不通、回什么形状」全部是服务端一个人要负责的语义。
把它们放在真 HTTP 上跑，才能覆盖路由级权限、错误映射与审计脱敏闸门 ——
只调 `searxng_search()` 会把这些全绕过去。

上游用真的 HTTP 服务模拟（不打桩 urlopen）：SearXNG 的响应字段随部署而变，
字段白名单清洗必须在真实 JSON 上验证；连接被拒也必须是真实语义。
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.support import (
    TempDirTestCase,
    bootstrap_and_login,
    frozen,
    make_config,
    serve_app,
)


# --------------------------------------------------------------------------- HTTP 工具
def http_post(base: str, path: str, payload: dict, *, token: str = "") -> tuple[int, Any]:
    """POST 一个 JSON 体，4xx/5xx 也当正常结果返回（权限拒绝正是被测对象）。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def dead_port() -> int:
    """拿一个刚被释放的端口 —— 连它必然 connection refused。

    写死端口号在别人的机器上可能是活的；这里让内核替我们挑一个空闲端口
    再立刻关掉，保证「不可达」这一分支是确定的。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------- 假 SearXNG
class MockSearxng:
    """一个假的 SearXNG JSON 端点（GET ``?q=...&format=json``）。"""

    def __init__(
        self,
        *,
        status: int = 200,
        results: list[dict] | None = None,
        raw: bytes | None = None,
    ) -> None:
        self.status = status
        self.results = results if results is not None else []
        self.raw = raw
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "MockSearxng":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音
                pass

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(self.path)
                if outer.raw is not None:
                    body = outer.raw
                else:
                    body = json.dumps({"results": outer.results}).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None, "先 start()"
        return f"http://127.0.0.1:{self._server.server_address[1]}/search"


# --------------------------------------------------------------------------- 基类
class SearchBase(TempDirTestCase):
    def app_with(self, **config_overrides: Any):
        """造一个指向给定搜索上游的应用。"""
        from vibe_server.app import build_app

        self.config = make_config(self.tmp, **config_overrides)
        app = build_app(self.config, clock=frozen())
        self.addCleanup(app.store.close)
        return app

    def capture_audit(self, app: Any) -> list[dict]:
        """把审计记录器包一层捕获，同时仍走真实的脱敏闸门与落库。

        只替换成「纯捕获」会跳过 redaction.validate_event —— 那正是
        要验证的「越界字段会被拒」这一半，所以这里必须包住原实现。
        """
        seen: list[dict] = []
        original = app.audit_recorder

        def recorder(*, actor: Any, device_id: str = "", event_type: str, payload: Any = None):
            seen.append(
                {
                    "actor": actor,
                    "device_id": device_id,
                    "event_type": event_type,
                    "payload": payload,
                }
            )
            return original(actor=actor, device_id=device_id, event_type=event_type, payload=payload)

        app.audit_recorder = recorder
        return seen

    def served(self, app: Any) -> tuple[str, str]:
        """起真 HTTP 并登录，返回 (base, token)。"""
        base = serve_app(self, app)
        session = bootstrap_and_login(app)
        return base, session["accessToken"]


# --------------------------------------------------------------------------- 中转语义
class RelayTests(SearchBase):
    def test_returns_cleaned_results(self) -> None:
        """只回 title/url/content 三字段，其余一律丢弃 —— 不让上游决定
        服务端回给客户端的 JSON 形状。缺 url 的条目无意义，直接跳过。"""
        upstream = MockSearxng(
            results=[
                {"title": "甲", "url": "https://a.example/1", "content": "摘要甲", "engine": "bing", "score": 9},
                {"title": "无链接", "content": "没有 url"},
                {"url": "https://b.example/2", "content": "只有链接"},
            ]
        ).start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        base, token = self.served(app)

        status, body = http_post(base, "/api/v1/search", {"query": "python asyncio"}, token=token)
        self.assertEqual(status, 200, body)
        results = body["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(set(results[0]), {"title", "url", "content"})
        self.assertEqual(results[0]["url"], "https://a.example/1")
        self.assertEqual(results[1]["title"], "")  # 缺 title 收敛成空串

    def test_upstream_received_query_and_format(self) -> None:
        upstream = MockSearxng(results=[{"url": "https://a.example/1"}]).start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        base, token = self.served(app)

        http_post(base, "/api/v1/search", {"query": "asyncio 教程"}, token=token)
        self.assertTrue(upstream.requests, "上游没收到请求")
        path = upstream.requests[-1]
        self.assertIn("format=json", path)
        self.assertIn("asyncio", urllib.parse.unquote(path))

    def test_caps_max_results_at_20(self) -> None:
        """maxResults 要封顶：单次请求拉回几 MB 结果列表是拒绝服务的入口。"""
        upstream = MockSearxng(
            results=[{"url": f"https://a.example/{i}", "title": str(i)} for i in range(30)]
        ).start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        base, token = self.served(app)

        status, body = http_post(
            base, "/api/v1/search", {"query": "x", "maxResults": 999}, token=token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["results"]), 20)


# --------------------------------------------------------------------------- 错误分支
class RelayErrorTests(SearchBase):
    def test_requires_authentication(self) -> None:
        app = self.app_with(search_url="http://127.0.0.1:1/search")
        base = serve_app(self, app)
        status, body = http_post(base, "/api/v1/search", {"query": "x"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthenticated")

    def test_without_upstream_returns_502(self) -> None:
        """没配上游要明确报错，而不是静默回空结果 —— 后者会让客户端
        以为「搜了，真没结果」。"""
        app = self.app_with()  # search_url 默认空
        base, token = self.served(app)
        status, body = http_post(base, "/api/v1/search", {"query": "x"}, token=token)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "search_upstream_error")

    def test_upstream_unreachable_returns_502(self) -> None:
        app = self.app_with(search_url=f"http://127.0.0.1:{dead_port()}/search")
        base, token = self.served(app)
        status, body = http_post(base, "/api/v1/search", {"query": "x"}, token=token)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "search_upstream_error")

    def test_upstream_error_status_returns_502(self) -> None:
        upstream = MockSearxng(status=500).start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        base, token = self.served(app)
        status, body = http_post(base, "/api/v1/search", {"query": "x"}, token=token)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "search_upstream_error")

    def test_upstream_non_json_returns_502(self) -> None:
        upstream = MockSearxng(raw=b"<html>not json</html>").start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        base, token = self.served(app)
        status, body = http_post(base, "/api/v1/search", {"query": "x"}, token=token)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "search_upstream_error")

    def test_upstream_without_results_array_returns_502(self) -> None:
        upstream = MockSearxng(raw=json.dumps({"foo": 1}).encode("utf-8")).start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        base, token = self.served(app)
        status, body = http_post(base, "/api/v1/search", {"query": "x"}, token=token)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "search_upstream_error")

    def test_rejects_empty_query(self) -> None:
        app = self.app_with(search_url="http://127.0.0.1:1/search")
        base, token = self.served(app)
        status, body = http_post(base, "/api/v1/search", {"query": "   "}, token=token)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_rejects_overlong_query(self) -> None:
        """查询词超长通常意味着把一段日志/代码当检索式塞进来了，直接拒 ——
        不把这份东西转发给上游。"""
        app = self.app_with(search_url=f"http://127.0.0.1:{dead_port()}/search")
        base, token = self.served(app)
        status, body = http_post(
            base, "/api/v1/search", {"query": "x" * 401}, token=token
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")


# --------------------------------------------------------------------------- 审计
class RelayAuditTests(SearchBase):
    def test_audit_records_only_query_digest(self) -> None:
        """服务端也不留存查询内容：只记指纹与长度。

        这条是「零内容留存」的地基 —— 若哪天有人为了排查方便把 query
        原文塞进 payload，脱敏闸门与这里都会变红。
        """
        from vibe_server.hashing import fingerprint

        upstream = MockSearxng(
            results=[{"url": "https://a.example/1"}, {"url": "https://a.example/2"}]
        ).start()
        self.addCleanup(upstream.stop)
        app = self.app_with(search_url=upstream.url)
        seen = self.capture_audit(app)
        base, token = self.served(app)

        query = "敏感查询词 asyncio"
        status, _ = http_post(base, "/api/v1/search", {"query": query}, token=token)
        self.assertEqual(status, 200)

        events = [e for e in seen if e["event_type"] == "web.search_relay"]
        self.assertEqual(len(events), 1, seen)
        payload = events[0]["payload"]
        self.assertEqual(payload["query_digest"], fingerprint(query))
        self.assertEqual(payload["query_chars"], len(query))
        self.assertEqual(payload["results"], 2)
        # 绝不能出现查询原文键
        self.assertNotIn("query", payload)
        self.assertNotIn(query, json.dumps(payload, ensure_ascii=False))

    def test_no_audit_when_upstream_fails(self) -> None:
        """上游失败时不记成功事件 —— 记一条「搜了 0 条」会掩盖真实故障。"""
        app = self.app_with(search_url=f"http://127.0.0.1:{dead_port()}/search")
        seen = self.capture_audit(app)
        base, token = self.served(app)
        status, _ = http_post(base, "/api/v1/search", {"query": "x"}, token=token)
        self.assertEqual(status, 502)
        self.assertEqual([e for e in seen if e["event_type"] == "web.search_relay"], [])


__all__ = ["MockSearxng", "RelayAuditTests", "RelayErrorTests", "RelayTests", "dead_port"]
