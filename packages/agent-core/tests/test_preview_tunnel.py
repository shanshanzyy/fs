"""远端设备预览（P3 / ADR-040）测试。

这一档新增的能力只有一句话：**把远端设备上的环回端口转发到本机环回，再交给
P2 的跨源帧渲染**。它之所以要单独立 ADR，是因为它同时碰到了三件事：

  · 新增一条**出站网络路径**（出站 SSH）与一个**本机监听口**；
  · `-L` 的规格串本身是个注入面（`-L 8080:10.0.0.5:6379` 就能把远端内网的
    任意服务拽到本机）；
  · 一个会长期活着的子进程，天生带「孤儿」风险。

因此测试重心是这三条边界，而不是「功能能不能用」：

1. **规格串完全由代码拼出** —— 两端的 host 必须是 `127.0.0.1` 字面量，
   用户只能提供整数端口；`-N` 必须**不**出现（用了它就没有 stdin EOF 自清理）。
2. **父进程死亡 → 监听消失** —— 这条用「关掉子进程 stdin」来模拟，因为
   父进程被 SIGKILL 时内核做的正是这件事。
3. **门控双开才生效，且不新增 CSP 放宽点** —— 远端转发落地在 `127.0.0.1`，
   正好落在 ADR-039 已放行的那一条 `frame-src` 里。测试直接比对「只差 frame-src」。

真实 SSH 连接仍在单测之外（没有远端主机）。生命周期用 `tests/fake_ssh.py`
这个替身跑真进程：它按 `-L` 规格建本地监听、按 stdin EOF 退出 —— 这两条
正是我们要验的行为，其余（认证、指纹、复用）由 `test_remote.py` 覆盖。

反向验证：把 `forward_argv` 里的 `FORWARD_HOST` 改成 `0.0.0.0`，
`test_spec_is_loopback_only` 变红；把 `_launch` 的 `stdin=PIPE` 改成 `DEVNULL`，
`test_port_disappears_when_parent_stdin_closes` 变红；把 `_page_csp` 里的
`or preview_remote` 删掉，HTTP 层那条 CSP 断言变红。
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import (  # noqa: E402
    CSP,
    HostServer,
    validate_external_preview_url,
)
from agent_core.remote import tunnel as tunnel_mod  # noqa: E402
from agent_core.remote.connection import RemoteConnection  # noqa: E402
from agent_core.remote.errors import RemoteError  # noqa: E402
from agent_core.remote.transport import SSHTransport  # noqa: E402
from agent_core.remote.tunnel import (  # noqa: E402
    MAX_TUNNELS,
    SSHTunnel,
    TunnelManager,
    validate_remote_port,
)
from tests.test_agent_loop import FakeClient  # noqa: E402

FAKE_SSH = Path(__file__).with_name("fake_ssh.py")

UPSTREAM_BODY = b"REMOTE-APP-OK"

# 真实的 ssh 校验器要在纯函数层验；生命周期测试则换成替身。
REAL_SSH = SSHTransport


def make_conn(**overrides: object) -> RemoteConnection:
    fields: dict[str, object] = {
        "name": "devbox",
        "host": "dev.example.internal",
        "user": "dev",
        "root": "/srv/app",
    }
    fields.update(overrides)
    return RemoteConnection(**fields)  # type: ignore[arg-type]


def _closed(port: int, timeout: float = 0.4) -> bool:
    """端口是否**不再**接受连接。

    不区分 `ConnectionRefused` 与 `Timeout`：两者都证明「没人监听」，
    而 Windows 上刚关闭的环回端口有时给的是超时。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return False
    except OSError:
        return True


def _wait(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """假装成「远端那个 dev server」。"""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(UPSTREAM_BODY)))
        self.end_headers()
        self.wfile.write(UPSTREAM_BODY)

    def log_message(self, *args: object) -> None:
        pass


class _QuietServer(http.server.HTTPServer):
    """就绪探测会开一条「连上就断开」的 TCP（真 ssh 也会这样做）：

    上游多半把它记成一次空连接/重置。这是**功能本身的行为**，不是测试污染，
    所以只把这条噪声吞掉，不去改探测方式。
    """

    def handle_error(self, request: object, client_address: object) -> None:
        pass


class FakeSSHTransport:
    """`spawn_forward` 起一个本机 `python tests/fake_ssh.py`，参数形状与真 ssh 一致。"""

    spawned: list[tuple[int, int]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def spawn_forward(self, *, local_port: int, remote_port: int) -> subprocess.Popen:
        type(self).spawned.append((local_port, remote_port))
        return subprocess.Popen(  # noqa: S603 — 测试替身
            [
                sys.executable,
                str(FAKE_SSH),
                "-o",
                "BatchMode=yes",
                "-L",
                f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                "-p",
                "22",
                "dev@dev.example.internal",
                "cat >/dev/null",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )


# ---------------------------------------------------------------- 纯函数层
class RemotePortValidationTests(unittest.TestCase):
    """端口矩阵：拒绝的必须是「看起来能用」的形态。"""

    def ok(self, raw: object) -> int:
        return validate_remote_port(raw)

    def bad(self, raw: object) -> str:
        with self.assertRaises(ValueError) as ctx:
            validate_remote_port(raw)
        return str(ctx.exception)

    def test_accepts_int_and_numeric_string(self) -> None:
        self.assertEqual(self.ok(5173), 5173)
        self.assertEqual(self.ok("5173"), 5173)
        self.assertEqual(self.ok(" 3000 "), 3000)
        self.assertEqual(self.ok(1), 1)
        self.assertEqual(self.ok(65535), 65535)

    def test_rejects_bool(self) -> None:
        # `True == 1`：不挡住就会静默变成「转发到远端 1 号端口」。
        self.bad(True)
        self.bad(False)

    def test_rejects_out_of_range(self) -> None:
        self.bad(0)
        self.bad(65536)
        self.bad(-1)
        self.bad("99999")

    def test_rejects_non_numeric_and_empty(self) -> None:
        for raw in ("", "   ", "abc", "80a", None, 5173.0, [], {}):
            self.bad(raw)

    def test_rejects_injection_shapes(self) -> None:
        """任何带分隔符的输入都必须挡在 `-L` 规格之外。"""
        for raw in ("1:127.0.0.1:22", "5173 -oProxyCommand=x", "5173;rm -rf /"):
            self.bad(raw)


# ---------------------------------------------------------------- argv 层
class ForwardArgvTests(unittest.TestCase):
    """`-L` 规格：两端必须都是环回字面量，且不能带 `-N`。"""

    def argv(self, local_port: int = 40001, remote_port: int = 5173) -> list[str]:
        return REAL_SSH(make_conn()).forward_argv(
            local_port=local_port, remote_port=remote_port
        )

    def spec(self, argv: list[str]) -> str:
        index = argv.index("-L")
        return argv[index + 1]

    def test_spec_is_loopback_only(self) -> None:
        spec = self.spec(self.argv())
        self.assertEqual(spec, "127.0.0.1:40001:127.0.0.1:5173")
        self.assertNotIn("0.0.0.0", spec)
        self.assertNotIn("::", spec)

    def test_ports_are_rendered_as_integers(self) -> None:
        """规格串不接受字符串拼接：给字符串就报错，而不是被 ssh 当三段解析。"""
        transport = REAL_SSH(make_conn())
        with self.assertRaises(RemoteError):
            transport.forward_argv(  # type: ignore[arg-type]
                local_port="40001:127.0.0.1:22", remote_port=5173
            )
        with self.assertRaises(RemoteError):
            transport.forward_argv(local_port=40001, remote_port=True)  # type: ignore[arg-type]

    def test_does_not_use_dash_n(self) -> None:
        """`-N` 不建会话 → ssh 不看 stdin → 父进程死了会留下孤儿监听。"""
        argv = self.argv()
        self.assertNotIn("-N", argv)
        self.assertEqual(argv[-1], "cat >/dev/null")

    def test_exit_on_forward_failure_is_set(self) -> None:
        # 没有它，端口占用时 ssh 照样保持运行 —— 「进程活着」与「转发可用」脱钩。
        argv = self.argv()
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("-T", argv)

    def test_keeps_the_hardening_from_base_argv(self) -> None:
        argv = self.argv()
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("NumberOfPasswordPrompts=0", argv)
        self.assertEqual(argv[-4:-2], ["-p", "22"])
        self.assertEqual(argv[-2], "dev@dev.example.internal")


# ---------------------------------------------------------------- 生命周期
class TunnelLifecycleTests(unittest.TestCase):
    """用替身跑真进程：建立在位 → 能穿过 → 关掉就没了 → 引用计数 → 自清理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        FakeSSHTransport.spawned = []
        self._patch = mock.patch.object(tunnel_mod, "SSHTransport", FakeSSHTransport)
        self._patch.start()
        self._backoff = tunnel_mod._BACKOFF_FACTOR
        tunnel_mod._BACKOFF_FACTOR = 0.02

        self.upstream = _QuietServer(("127.0.0.1", 0), _UpstreamHandler)
        self.upstream_port = self.upstream.server_address[1]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()

        self.states: list[str] = []
        self.manager = TunnelManager(
            on_state=lambda snap: self.states.append(str(snap.get("state"))),
            transport_factory=lambda conn: FakeSSHTransport(),
        )
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.manager.close_all()
        self.upstream.shutdown()
        self.upstream.server_close()
        tunnel_mod._BACKOFF_FACTOR = self._backoff
        self._patch.stop()
        self._tmp.cleanup()

    def test_open_reports_up_and_forwards_bytes(self) -> None:
        info = self.manager.open(self.conn, self.upstream_port)
        self.assertEqual(info["state"], "up")
        self.assertTrue(info["url"].startswith("http://127.0.0.1:"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(info["url"], timeout=5) as response:
            self.assertEqual(response.read(), UPSTREAM_BODY)

    def test_url_passes_the_same_gate_as_the_frame(self) -> None:
        """交出去的地址必须能被 P2 的校验接受，否则就是「开得成、白屏」。"""
        info = self.manager.open(self.conn, self.upstream_port)
        url, port = validate_external_preview_url(info["url"], host_port=1)
        self.assertEqual(url, info["url"])
        self.assertEqual(port, info["localPort"])

    def test_close_releases_the_listening_port(self) -> None:
        info = self.manager.open(self.conn, self.upstream_port)
        port = info["localPort"]
        self.assertFalse(_closed(port))
        self.assertTrue(self.manager.close(info["id"])["closed"])
        self.assertTrue(_wait(lambda: _closed(port)), "关闭后本地端口仍在监听")
        self.assertEqual(self.manager.list(), [])

    def test_second_open_shares_one_tunnel_by_refcount(self) -> None:
        """两个标签看同一个远端服务 → 共用一条转发；关掉一个另一个还能用。"""
        first = self.manager.open(self.conn, self.upstream_port)
        second = self.manager.open(self.conn, self.upstream_port)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["refs"], 2)
        self.assertEqual(len(FakeSSHTransport.spawned), 1)

        partial = self.manager.close(first["id"])
        self.assertFalse(partial["closed"])
        self.assertEqual(partial["refs"], 1)
        self.assertFalse(_closed(first["localPort"]))

        self.assertTrue(self.manager.close(second["id"])["closed"])
        self.assertTrue(_wait(lambda: _closed(first["localPort"])))

    def test_replaces_tunnel_when_connection_changed(self) -> None:
        """连接被换过（换主机/密钥）→ 不能复用旧通道，否则会以为连的是新机器。"""
        first = self.manager.open(self.conn, self.upstream_port)
        moved = make_conn(host="other.example.internal")
        second = self.manager.open(moved, self.upstream_port)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(_wait(lambda: _closed(first["localPort"])))

    def test_caps_concurrent_tunnels(self) -> None:
        for index in range(MAX_TUNNELS):
            self.manager.open(self.conn, self.upstream_port + index)
        with self.assertRaises(ValueError):
            self.manager.open(self.conn, self.upstream_port + MAX_TUNNELS)

    def test_port_disappears_when_parent_stdin_closes(self) -> None:
        """模拟「父进程被 SIGKILL」：内核关掉管道写端 = stdin EOF。

        这是 ADR-040 里「孤儿不可能存在」的全部依据 —— 若实现改成 `-N` 或把
        stdin 设成 DEVNULL，这里会立刻变红。
        """
        info = self.manager.open(self.conn, self.upstream_port)
        tunnel = self.manager.get(info["id"])
        assert tunnel is not None
        proc = tunnel._proc
        assert proc is not None and proc.stdin is not None

        proc.stdin.close()
        self.assertTrue(
            _wait(lambda: proc.poll() is not None, timeout=8.0),
            "子进程在 stdin 关闭后没有退出",
        )
        self.assertTrue(
            _wait(lambda: _closed(info["localPort"]), timeout=5.0),
            "子进程退出后本地端口仍在监听",
        )

    def test_reconnects_after_unexpected_exit(self) -> None:
        info = self.manager.open(self.conn, self.upstream_port)
        tunnel = self.manager.get(info["id"])
        assert tunnel is not None
        proc = tunnel._proc
        assert proc is not None

        proc.kill()  # 意外中断（不是我们调的 stop）
        self.assertTrue(
            _wait(lambda: tunnel.state == "up" and tunnel.restarts >= 1, timeout=10.0),
            f"没有重连成功，状态={tunnel.state} 错误={tunnel.last_error}",
        )
        self.assertIn("reconnecting", self.states)
        # 重连必须**沿用同一个本地端口**：否则已经打开的 iframe 指向一个死地址。
        self.assertEqual(tunnel.snapshot()["localPort"], info["localPort"])

    def test_stop_is_idempotent(self) -> None:
        info = self.manager.open(self.conn, self.upstream_port)
        tunnel = self.manager.get(info["id"])
        assert tunnel is not None
        tunnel.stop()
        tunnel.stop()
        self.assertEqual(tunnel.state, "closed")

    def test_close_unknown_id_fails_loudly(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.close("nope")

    def test_bind_failure_retries_with_another_port(self) -> None:
        """首次「端口占用」→ 换一个端口重试，而不是把一次竞态当永久失败。"""
        marker = self.tmp / "fail-once"
        with mock.patch.dict(os.environ, {"VIBE_FAKE_SSH_FAIL_ONCE": str(marker)}):
            info = self.manager.open(self.conn, self.upstream_port)
        self.assertEqual(info["state"], "up")
        self.assertEqual(len(FakeSSHTransport.spawned), 2)
        self.assertNotEqual(
            FakeSSHTransport.spawned[0][0], FakeSSHTransport.spawned[1][0]
        )

    def test_failed_open_leaves_nothing_behind(self) -> None:
        """远端没有服务不算失败（监听照样建起来）；**本地建不起来**才算。"""
        with mock.patch.object(
            FakeSSHTransport, "spawn_forward", side_effect=RemoteError("ssh 不在", kind="transport")
        ):
            with self.assertRaises(RemoteError):
                self.manager.open(self.conn, self.upstream_port)
        self.assertEqual(self.manager.list(), [])


# ---------------------------------------------------------------- HTTP 层
class _TunnelHttpBase(unittest.TestCase):
    """真实 HTTP 层。`REMOTE` / `ENABLED` 决定两个门控的取值。"""

    REMOTE = True
    ENABLED = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "page.html").write_text("<!doctype html><h1>hi</h1>", encoding="utf-8")
        (self.ws / ".vibe").mkdir()

        self.upstream = _QuietServer(("127.0.0.1", 0), _UpstreamHandler)
        self.upstream_port = self.upstream.server_address[1]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()

        # 连接清单：一条本机地址（不会真去连，转发由替身接管）
        (self.ws / ".vibe" / "remote.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "default": "devbox",
                    "connections": [
                        {"name": "devbox", "host": "dev.example.internal", "user": "dev",
                         "root": "/srv/app"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        base = Config.load(self.ws)
        config = replace(
            base,
            remote_enabled=self.REMOTE,
            preview_remote=self.ENABLED,
            preview_external=False,
        )
        self._patch = mock.patch.object(tunnel_mod, "SSHTransport", FakeSSHTransport)
        self._patch.start()
        tunnel_mod._BACKOFF_FACTOR = 0.02

        self.host = HostServer(config, port=0, client=FakeClient([[]]))
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._patch.stop()
        self.upstream.shutdown()
        self.upstream.server_close()
        self._tmp.cleanup()

    # -- 请求助手 -----------------------------------------------------------
    def get(self, path: str):
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        return self._opener.open(request, timeout=10)

    def post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Vibe-Token", self.token)
        return self._opener.open(request, timeout=20)

    def audit_text(self) -> str:
        path = self.ws / ".vibe" / "audit.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def page_csp(self) -> str:
        with self.get("/") as response:
            return response.headers["Content-Security-Policy"]


class TunnelDisabledTests(_TunnelHttpBase):
    """门控关着（默认）：探测回 false，POST 直接 403，且不留审计。"""

    REMOTE = True
    ENABLED = False

    def test_probe_reports_disabled(self) -> None:
        with self.get("/api/preview/tunnel") as response:
            payload = json.loads(response.read())
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["tunnels"], [])

    def test_post_is_forbidden(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/preview/tunnel", {"connection": "devbox", "remotePort": 5173})
        self.assertEqual(ctx.exception.code, 403)

    def test_no_audit_written(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self.post("/api/preview/tunnel", {"connection": "devbox", "remotePort": 5173})
        self.assertNotIn("preview.tunnel", self.audit_text())

    def test_page_csp_stays_untouched(self) -> None:
        self.assertEqual(self.page_csp(), CSP)


class TunnelRequiresRemoteTests(_TunnelHttpBase):
    """只有 VIBE_PREVIEW_REMOTE、没有 VIBE_REMOTE：入口也不该出现。"""

    REMOTE = False
    ENABLED = True

    def test_probe_stays_disabled(self) -> None:
        with self.get("/api/preview/tunnel") as response:
            self.assertFalse(json.loads(response.read())["enabled"])

    def test_post_reports_remote_disabled(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/preview/tunnel", {"connection": "devbox", "remotePort": 5173})
        self.assertEqual(ctx.exception.code, 400)


class TunnelEnabledTests(_TunnelHttpBase):
    """双门控打开：能开、能列、能关，且 CSP 只多出 P2 那一条。"""

    REMOTE = True
    ENABLED = True

    def test_csp_is_relaxed_by_the_same_single_directive(self) -> None:
        """P3 不新增放宽点：与 P2 共用同一条 `frame-src`。"""
        before = dict(
            part.split(" ", 1) for part in CSP.split("; ") if " " in part
        )
        after = dict(
            part.split(" ", 1) for part in self.page_csp().split("; ") if " " in part
        )
        self.assertEqual(set(before), set(after))
        self.assertEqual({key for key in before if before[key] != after[key]}, {"frame-src"})
        self.assertEqual(after["frame-src"], "'self' http://127.0.0.1:*")
        # 数据出口仍然锁在本源
        self.assertEqual(after["connect-src"], "'self'")

    def test_open_list_close_roundtrip(self) -> None:
        with self.post(
            "/api/preview/tunnel", {"connection": "devbox", "remotePort": self.upstream_port}
        ) as response:
            created = json.loads(response.read())
        self.assertTrue(created["ok"])
        self.assertEqual(created["remotePort"], self.upstream_port)
        self.assertEqual(created["state"], "up")
        self.assertTrue(created["url"].startswith("http://127.0.0.1:"))

        with self.get("/api/preview/tunnel") as response:
            listed = json.loads(response.read())
        self.assertTrue(listed["enabled"])
        self.assertEqual([item["id"] for item in listed["tunnels"]], [created["id"]])

        with self.post("/api/preview/tunnel/close", {"id": created["id"]}) as response:
            closed = json.loads(response.read())
        self.assertTrue(closed["closed"])
        self.assertTrue(_wait(lambda: _closed(created["localPort"])))

    def test_rejects_bad_port_with_400(self) -> None:
        for bad in (0, 70000, "abc", True):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post(
                    "/api/preview/tunnel", {"connection": "devbox", "remotePort": bad}
                )
            self.assertEqual(ctx.exception.code, 400, f"remotePort={bad!r}")

    def test_unknown_connection_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/preview/tunnel", {"connection": "nope", "remotePort": 5173})
        self.assertEqual(ctx.exception.code, 400)

    def test_audit_records_ports_but_not_host(self) -> None:
        with self.post(
            "/api/preview/tunnel", {"connection": "devbox", "remotePort": self.upstream_port}
        ) as response:
            created = json.loads(response.read())
        with self.post("/api/preview/tunnel/close", {"id": created["id"]}):
            pass
        log = self.audit_text()
        self.assertIn("preview.tunnel", log)
        self.assertIn(f'"remote_port": {self.upstream_port}', log)
        self.assertIn(f'"local_port": {created["localPort"]}', log)
        # 主机名 / 用户名 / 正文都不该进审计（ADR-006）
        self.assertNotIn("dev.example.internal", log)
        self.assertNotIn("REMOTE-APP-OK", log)

    def test_session_close_releases_every_tunnel(self) -> None:
        with self.post(
            "/api/preview/tunnel", {"connection": "devbox", "remotePort": self.upstream_port}
        ) as response:
            created = json.loads(response.read())
        session = self.host.ctx.session
        session.close()
        self.assertTrue(_wait(lambda: _closed(created["localPort"])))


if __name__ == "__main__":
    unittest.main()
