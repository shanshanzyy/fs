"""真实应用预览（P2 / ADR-039）测试。

这一层新增的能力只有一句话：**允许把本机 dev server 嵌进预览面板**。
它之所以要单独立 ADR，是因为它动了整个项目唯一一处 CSP：

    frame-src 'self'   →   frame-src 'self' http://127.0.0.1:*

所以测试的重心不是"功能能不能用"，而是**这个放宽有没有被围栏圈住**：

1. **默认逐字节不变** —— 门控关着时，界面 CSP 必须与改造前完全一致
   （不是"差不多"：多一个空格都算改了契约）。
2. **放宽只有一条指令** —— 从 `CSP` 派生，逐条比对后只允许 `frame-src` 不同；
   万一将来有人手抄一份 CSP 常量，这条断言会立刻变红。
3. **校验放行的集合 = CSP 放行的集合** —— 校验只认 `http://127.0.0.1:<port>`，
   CSP 也只放 `127.0.0.1`。差一个 `localhost` 就是"填得进、白屏"。
4. **静态预览的隔离没有被顺手削弱** —— `PREVIEW_CSP` 里 `connect-src 'none'`
   必须原样还在。P2 的隔离靠**跨源**，不靠放松这一条。

反向验证：把 `validate_external_preview_url` 里的 `port == host_port` 判断删掉，
`test_rejects_own_port` 立刻变红；把 `csp_with_external_preview` 改成返回常量
`CSP`，`test_only_frame_src_is_relaxed` 与 HTTP 层那条 CSP 断言同时变红。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import (  # noqa: E402
    CSP,
    PREVIEW_CSP,
    HostServer,
    csp_with_external_preview,
    validate_external_preview_url,
)
from tests.test_agent_loop import FakeClient  # noqa: E402

DEFAULT_FRAME_SRC = "'self'"
RELAXED_FRAME_SRC = "'self' http://127.0.0.1:*"


def directives(csp: str) -> dict[str, str]:
    """把 CSP 拆成 `{指令: 值}`，用于逐条比对（顺序无关）。"""
    out: dict[str, str] = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(" ")
        out[name] = value.strip()
    return out


# ---------------------------------------------------------------- 纯函数层
class CspDerivationTests(unittest.TestCase):
    """CSP 派生：放宽必须是"一条指令的差集"，不能整段重写。"""

    def test_default_csp_still_denies_foreign_frames(self) -> None:
        self.assertEqual(directives(CSP)["frame-src"], DEFAULT_FRAME_SRC)
        self.assertNotIn("127.0.0.1", CSP)

    def test_only_frame_src_is_relaxed(self) -> None:
        relaxed = csp_with_external_preview()
        before, after = directives(CSP), directives(relaxed)

        # 指令集合不许变（多一条 / 少一条都是改契约）
        self.assertEqual(set(before), set(after))
        # 差异必须**恰好**只在 frame-src
        differing = {name for name in before if before[name] != after[name]}
        self.assertEqual(differing, {"frame-src"})
        self.assertEqual(after["frame-src"], RELAXED_FRAME_SRC)

    def test_relaxed_csp_keeps_connect_src_locked_to_self(self) -> None:
        """放宽 frame-src 不等于放宽数据出口：界面的 connect-src 仍是 'self'。"""
        after = directives(csp_with_external_preview())
        self.assertEqual(after["connect-src"], "'self'")
        self.assertEqual(after["frame-ancestors"], "'none'")

    def test_static_preview_csp_is_untouched(self) -> None:
        """P2 的隔离靠跨源，所以**不**sacrifice 静态预览的出口封锁。"""
        preview = directives(PREVIEW_CSP)
        self.assertEqual(preview["connect-src"], "'none'")
        self.assertEqual(preview["frame-ancestors"], "'self'")

    def test_derivation_fails_closed_when_marker_missing(self) -> None:
        """`CSP` 若被改到没有 `frame-src 'self';`，派生必须原样返回（不放宽）。"""
        import agent_core.host.server as server_mod

        original = server_mod.CSP
        try:
            server_mod.CSP = "default-src 'self'"
            self.assertEqual(csp_with_external_preview(), "default-src 'self'")
        finally:
            server_mod.CSP = original


# ---------------------------------------------------------------- 地址校验
class ExternalUrlValidationTests(unittest.TestCase):
    """校验矩阵：接受的必须能被 CSP 放行，拒绝的必须是"看起来能用"的形态。"""

    HOST_PORT = 8080

    def ok(self, raw: str) -> tuple[str, int]:
        return validate_external_preview_url(raw, host_port=self.HOST_PORT)

    def bad(self, raw: object) -> str:
        with self.assertRaises(ValueError) as ctx:
            validate_external_preview_url(raw, host_port=self.HOST_PORT)
        return str(ctx.exception)

    # ---- 接受 -------------------------------------------------------------
    def test_accepts_plain_loopback(self) -> None:
        self.assertEqual(self.ok("http://127.0.0.1:5173"), ("http://127.0.0.1:5173/", 5173))

    def test_accepts_trailing_slash_and_path_and_query(self) -> None:
        self.assertEqual(
            self.ok("http://127.0.0.1:5173/app/?tab=1"),
            ("http://127.0.0.1:5173/app/?tab=1", 5173),
        )

    def test_strips_surrounding_whitespace(self) -> None:
        # 从浏览器地址栏复制常见带首尾空格；只剥外层，不放过内部空白
        self.assertEqual(self.ok("  http://127.0.0.1:3000  ")[1], 3000)

    # ---- 拒绝：来源形态 ---------------------------------------------------
    def test_rejects_https(self) -> None:
        self.assertIn("http", self.bad("https://127.0.0.1:5173/"))

    def test_rejects_localhost_alias(self) -> None:
        """CSP 里只列了 127.0.0.1 字面量：放行 localhost 就是"填得进、渲染不出来"。"""
        self.bad("http://localhost:5173/")

    def test_rejects_foreign_host(self) -> None:
        self.bad("http://example.com:5173/")

    def test_rejects_lookalike_host(self) -> None:
        self.bad("http://127.0.0.1.evil.com:5173/")

    def test_rejects_userinfo_confusion(self) -> None:
        self.bad("http://127.0.0.1:5173@evil.com/")

    def test_rejects_own_port(self) -> None:
        """指向 host 自己 = 同源 iframe，跨源隔离的前提当场消失。"""
        message = self.bad(f"http://127.0.0.1:{self.HOST_PORT}/")
        self.assertIn(str(self.HOST_PORT), message)

    # ---- 拒绝：端口 -------------------------------------------------------
    def test_rejects_missing_port(self) -> None:
        self.bad("http://127.0.0.1/")

    def test_rejects_zero_port(self) -> None:
        self.bad("http://127.0.0.1:0/")

    def test_rejects_out_of_range_port(self) -> None:
        self.bad("http://127.0.0.1:70000/")

    def test_rejects_non_numeric_port(self) -> None:
        self.bad("http://127.0.0.1:http/")

    # ---- 拒绝：路径与其它 ------------------------------------------------
    def test_rejects_dotdot_segment(self) -> None:
        self.bad("http://127.0.0.1:5173/a/../../b")

    def test_rejects_fragment(self) -> None:
        self.bad("http://127.0.0.1:5173/#/route")

    def test_rejects_backslash(self) -> None:
        self.bad("http://127.0.0.1:5173\\@evil.com/")

    def test_rejects_inner_whitespace(self) -> None:
        self.bad("http://127.0.0.1:5173/a b")

    def test_rejects_empty_and_wrong_type(self) -> None:
        self.bad("")
        self.bad("   ")
        self.bad(None)
        self.bad(5173)

    def test_rejects_overlong(self) -> None:
        self.bad("http://127.0.0.1:5173/" + "a" * 400)


# ---------------------------------------------------------------- HTTP 层
class _ExternalPreviewBase(unittest.TestCase):
    """真实 HTTP 层。`EXTERNAL` 决定门控开还是关，两个子类共用全部断言。"""

    EXTERNAL = False

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "page.html").write_text("<!doctype html><h1>hi</h1>", encoding="utf-8", newline="")
        (self.ws / ".vibe").mkdir()

        base = Config.load(self.ws)
        config = replace(base, preview_external=self.EXTERNAL)
        self.host = HostServer(config, port=0, client=FakeClient([[]]))
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._tmp.cleanup()

    def get(self, path: str, token: str | None = None):
        request = urllib.request.Request(self.base + path)
        if token is not None:
            request.add_header("X-Vibe-Token", token)
        return self._opener.open(request, timeout=10)

    def post(self, path: str, body: dict, token: str | None = None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("X-Vibe-Token", token)
        return self._opener.open(request, timeout=10)

    def page_csp(self) -> str:
        with self.get("/") as response:
            return response.headers["Content-Security-Policy"]

    def audit_text(self) -> str:
        path = self.ws / ".vibe" / "audit.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class ExternalPreviewDisabledTests(_ExternalPreviewBase):
    """门控关闭（默认）：功能不存在，且**界面 CSP 一个字节都不变**。"""

    EXTERNAL = False

    def test_page_csp_is_byte_identical_to_before(self) -> None:
        self.assertEqual(self.page_csp(), CSP)

    def test_probe_reports_disabled(self) -> None:
        with self.get("/api/preview/external", token=self.token) as response:
            self.assertEqual(json.loads(response.read())["enabled"], False)

    def test_post_is_forbidden(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/preview/external", {"url": "http://127.0.0.1:5173/"}, token=self.token)
        self.assertEqual(ctx.exception.code, 403)

    def test_no_audit_written_when_disabled(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self.post("/api/preview/external", {"url": "http://127.0.0.1:5173/"}, token=self.token)
        self.assertNotIn("preview.external", self.audit_text())


class ExternalPreviewEnabledTests(_ExternalPreviewBase):
    """门控开启：能登记，且放宽的范围仍然是"只有 frame-src、只有环回"。"""

    EXTERNAL = True

    def test_page_csp_relaxes_exactly_one_directive(self) -> None:
        before, after = directives(CSP), directives(self.page_csp())
        self.assertEqual(set(before), set(after))
        self.assertEqual({k for k in before if before[k] != after[k]}, {"frame-src"})
        self.assertEqual(after["frame-src"], RELAXED_FRAME_SRC)

    def test_probe_reports_enabled(self) -> None:
        with self.get("/api/preview/external", token=self.token) as response:
            self.assertEqual(json.loads(response.read())["enabled"], True)

    def test_accepts_loopback_and_normalizes(self) -> None:
        with self.post(
            "/api/preview/external", {"url": "http://127.0.0.1:5173/app"}, token=self.token
        ) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["url"], "http://127.0.0.1:5173/app")
        self.assertEqual(payload["port"], 5173)

    def test_rejects_foreign_host_with_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/preview/external", {"url": "http://evil.com:5173/"}, token=self.token)
        self.assertEqual(ctx.exception.code, 400)

    def test_rejects_own_port_with_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(
                "/api/preview/external", {"url": f"http://127.0.0.1:{self.host.port}/"},
                token=self.token,
            )
        self.assertEqual(ctx.exception.code, 400)

    def test_audit_records_port_only(self) -> None:
        """审计要能回答"连了哪个服务"，不回答"页面上有什么"（ADR-006）。"""
        with self.post(
            "/api/preview/external",
            {"url": "http://127.0.0.1:5173/secret?token=abc"},
            token=self.token,
        ):
            pass
        log = self.audit_text()
        self.assertIn("preview.external", log)
        self.assertIn('"port": 5173', log)
        # 路径 / query 不该进审计
        self.assertNotIn("secret", log)
        self.assertNotIn("abc", log)

    def test_endpoint_still_requires_token(self) -> None:
        """新端点没绕开既有鉴权：/api/* 一律要令牌。"""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/preview/external")
        self.assertIn(ctx.exception.code, (401, 403))

    def test_static_preview_response_still_locks_egress(self) -> None:
        """/preview/ 的 CSP 不因 P2 变化：跨源隔离不是靠放松它换来的。"""
        with self.get("/preview/page.html") as response:
            self.assertEqual(
                response.headers["Content-Security-Policy"], PREVIEW_CSP
            )


if __name__ == "__main__":
    unittest.main()
