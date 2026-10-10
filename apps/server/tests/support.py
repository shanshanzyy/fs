"""测试夹具：路径引导、临时配置、假时钟、假上游。

这个文件只有一个职责：让每个测试都能**用三行**拿到一个可用的服务端。
把装配的复杂度收在这里，测试本身才能只剩「输入 → 断言」。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- 路径
_HERE = Path(__file__).resolve()
_SERVER_ROOT = _HERE.parent.parent              # apps/server
_REPO_ROOT = _SERVER_ROOT.parent.parent         # 仓库根
_AGENT_CORE = _REPO_ROOT / "packages" / "agent-core"

for _path in (_SERVER_ROOT, _AGENT_CORE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


# --------------------------------------------------------------------------- 配置
def make_config(tmpdir: str | Path, **overrides: Any) -> Any:
    """造一个指向临时目录的 ServerConfig。

    每个测试一份独立的 db / data_dir，测试之间不共享任何状态 ——
    共享的话失败会互相污染，排查时得先猜「是不是上一个测试留下的」。
    """
    from vibe_server.config import ServerConfig

    base = Path(tmpdir)
    kwargs: dict[str, Any] = {
        "data_dir": base / "data",
        # 这里传纯路径而不是 `sqlite:///...`：ServerConfig.load 会用 Path() 包一层，
        # URL 前缀会被当成目录名弄坏（Windows 上变成 `sqlite:\E:\...`）。
        # URL 形式是 `open_store()` 的入口，不是配置的入口。
        "db_path": base / "vibe.db",
        "host": "127.0.0.1",
        "port": 0,
        "policy_required": True,
        "policy_max_staleness": 3600,
    }
    kwargs.update(overrides)
    return ServerConfig.load(**kwargs)


# --------------------------------------------------------------------------- 应用
class TempDirTestCase(unittest.TestCase):
    """自带临时目录的测试基类。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="vibe-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.config = make_config(self._tmp)

    @property
    def tmp(self) -> Path:
        return self._tmp

    def make_app(self, **kwargs: Any) -> Any:
        from vibe_server.app import build_app
        from vibe_server.clock import FrozenClock

        clock = kwargs.pop("clock", None) or FrozenClock.at(1_700_000_000)
        app = build_app(self.config, clock=clock, **kwargs)
        self.addCleanup(lambda: _close_quietly(app.store))
        return app


def _close_quietly(store: Any) -> None:
    try:
        store.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- 真 HTTP
def serve_app(test: unittest.TestCase, app: Any) -> str:
    """起一个真的 HTTP 服务端（走生产那条 `VibeHandler` 路径），返回基址。

    为什么要真的起服务而不是只调 service：路由、认证、路由级权限、
    错误映射全部在 HTTP 层，而那正是「接口通不通」唯一无法自证的部分。
    """
    from http.server import ThreadingHTTPServer

    from vibe_server.httpd import VibeHandler

    server = ThreadingHTTPServer(("127.0.0.1", 0), VibeHandler)
    server.app = app  # VibeHandler.app 读的就是 self.server.app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def _stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    test.addCleanup(_stop)
    return f"http://127.0.0.1:{server.server_address[1]}"


def http_get(base: str, path: str, *, token: str = "") -> tuple[int, str, Any]:
    """GET 一个路径，返回 (状态码, 正文, 头)。

    **4xx/5xx 也当正常结果返回**：权限拒绝、404 映射正是被测对象，
    让它们抛异常会逼着每个测试各写一遍 try/except。
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(base + path)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


# --------------------------------------------------------------------------- 主体
def bootstrap_and_login(
    app: Any,
    *,
    email: str = "owner@corp.example",
    password: str = "Str0ng!Passw0rd",
) -> dict:
    """建一个组织所有者并登录，返回 `login()` 的完整载荷。

    服务端**所有**接口都要认证，所以「先有个能用的主体」是几乎每个测试的第一步。
    把它收在这里，测试就只剩「触发 → 断言」。
    """
    app.auth_service.bootstrap_org(email=email, password=password, display_name="Owner")
    return app.auth_service.login(
        email=email,
        password=password,
        device_name="test-device",
        platform="test",
        fingerprint="fp-test-1",
    )


def actor_of(app: Any, access_token: str) -> Any:
    """把访问令牌换成 Actor（走的是和 HTTP 层同一条认证路径）。"""
    return app.auth_service.authenticate(access_token)


def owner_actor(app: Any) -> Any:
    """直接拿到一个组织所有者主体。"""
    session = bootstrap_and_login(app)
    return actor_of(app, session["accessToken"])


__all__ = [
    "MockUpstream",
    "TempDirTestCase",
    "actor_of",
    "bootstrap_and_login",
    "frozen",
    "http_get",
    "make_config",
    "mock_upstream",
    "owner_actor",
    "serve_app",
]


# --------------------------------------------------------------------------- 假时钟
def frozen(start: int = 1_700_000_000) -> Any:
    from vibe_server.clock import FrozenClock

    return FrozenClock.at(start)


# --------------------------------------------------------------------------- 假上游模型
class MockUpstream:
    """一个假的上游模型服务（OpenAI 兼容形状）。

    为什么要真的起一个 HTTP 服务而不是打桩 `urlopen`：
    网关的正确性有一大半在**它怎么处理真实的 HTTP 语义**上 ——
    状态码、超时、usage 字段缺失、连接被拒。打桩会把这些全绕过去，
    测出来的「通过」没有意义。
    """

    def __init__(
        self,
        *,
        status: int = 200,
        content: str = "ok",
        usage: dict[str, int] | None = None,
        delay: float = 0.0,
        malformed: bool = False,
        sse_chunks: tuple[str, ...] | None = None,
        sse_usage: bool = True,
    ) -> None:
        self.status = status
        self.content = content
        self.usage = usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 7}
        self.delay = delay
        self.malformed = malformed
        # 给了 sse_chunks 就按 SSE 回（不分块长度，用 chunked 编码逐帧写）。
        # sse_usage=False 模拟「上游没开 stream_options.include_usage」——
        # 这是真实存在的上游行为，网关必须能靠估算结算而不是拿不到就不结算。
        self.sse_chunks = sse_chunks
        self.sse_usage = sse_usage
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> "MockUpstream":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音
                pass

            def _write_chunk(self, data: bytes) -> None:
                if not data:  # 终止块
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()

            def _write_sse(self, outer: "MockUpstream") -> None:
                """按 SSE 逐帧返回。用 chunked 编码，因为长度事先未知 ——
                这正好也覆盖了「网关必须能处理不带 Content-Length 的响应」。"""
                self.send_response(outer.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in outer.sse_chunks or ():
                    frame = {
                        "choices": [
                            {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                        ]
                    }
                    self._write_chunk(b"data: " + json.dumps(frame).encode("utf-8") + b"\n\n")
                last: dict[str, Any] = {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                }
                if outer.sse_usage:
                    last["usage"] = outer.usage
                self._write_chunk(b"data: " + json.dumps(last).encode("utf-8") + b"\n\n")
                self._write_chunk(b"data: [DONE]\n\n")
                self._write_chunk(b"")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    outer.requests.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    outer.requests.append({"_raw": raw.decode("utf-8", "replace")})
                outer.auth_headers.append(self.headers.get("Authorization") or "")

                if outer.delay:
                    import time

                    time.sleep(outer.delay)

                if outer.malformed:
                    body = b"{not json"
                elif outer.sse_chunks is not None:
                    try:
                        self._write_sse(outer)
                    except OSError:
                        pass  # 客户端提前断开：网关侧的收尾才是被测对象
                    return
                else:
                    body = json.dumps(
                        {
                            "id": "chatcmpl-test",
                            "object": "chat.completion",
                            "model": "mock",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": outer.content},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": outer.usage,
                        }
                    ).encode("utf-8")

                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def mock_upstream(test: unittest.TestCase, **kwargs: Any) -> MockUpstream:
    """起一个假上游，并在测试结束时自动关掉。"""
    server = MockUpstream(**kwargs).start()
    test.addCleanup(server.stop)
    return server
