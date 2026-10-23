"""浏览器预览 / 网页自检（O / P1-11）测试。

这一阶段把两件容易混淆的事分开，测试也按这条线分：

1. **静态预览** —— `GET /preview/<relpath>` 把工作区里的网页文件原样送出。
   它开了一个新的对外出口，因此安全边界必须逐条钉死：越权（`../`）、
   host 私产（`.vibe/`）、MIME 白名单（`.exe` 不在内）、目录回落到 `index.html`，
   以及「与静态壳一致地免令牌、但 Host/Origin 双校验照常」。
2. **网页自检** —— `POST /api/visual/check` 拿壳给的截图与基线做**粗粒度**比对。
   关键契约是：**没有截图能力时如实回 `{available:false}`，绝不 500**
   （浏览器直开就是这么用的），以及基线只落 host 侧的 `.vibe/visual/`。

反向验证（§12 / O9）：临时让 `/preview/` 跳过 PathGuard，
`test_traversal_rejected` 与 `test_vibe_dir_rejected` 必须变红。

P0 增补：目录请求缺尾斜杠时必须 **301**（否则页内相对资源解析到错误层级），
以及 `preview.changed` 事件的载荷契约（**只带路径、不含内容**，超限截断）。
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host import visual  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402


# ---------------------------------------------------------------- PNG 手工编码
def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """手搓一张 8 位真彩 PNG（逐行过滤类型 0），够 `visual.decode_png` 解出来即可。

    自造而不是放二进制夹具：测试目录里塞一张 png 会让「这文件为什么在这」
    在半年后没人说得清；这里一眼能看出它就是一块纯色。
    """
    row = bytes(rgb) * width
    raw = bytearray()
    for _ in range(height):
        raw.append(0)      # 每行的过滤器字节
        raw += row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- 纯解码 / 差异
class VisualUnitTests(unittest.TestCase):
    """先把 `visual` 这个纯函数模块钉住，HTTP 层的断言才好读。"""

    def test_decode_roundtrips_dimensions_and_pixels(self) -> None:
        width, height, rgb = visual.decode_png(png(3, 2, (1, 2, 3)))
        self.assertEqual((width, height), (3, 2))
        self.assertEqual(len(rgb), 3 * 2 * 3)
        self.assertEqual(rgb[:3], bytes((1, 2, 3)))

    def test_non_png_rejected(self) -> None:
        self.assertFalse(visual.is_png(b"definitely not a png"))
        with self.assertRaises(ValueError):
            visual.decode_png(b"definitely not a png")

    def test_identical_images_have_zero_diff(self) -> None:
        blob = png(32, 32, (7, 7, 7))
        ratio, changed, total = visual.diff_ratio(blob, blob)
        self.assertEqual(changed, 0)
        self.assertEqual(ratio, 0.0)
        self.assertGreater(total, 0)

    def test_black_vs_white_shows_high_diff(self) -> None:
        ratio, changed, total = visual.diff_ratio(png(32, 32, (255, 255, 255)), png(32, 32, (0, 0, 0)))
        self.assertEqual(changed, total)
        self.assertAlmostEqual(ratio, 1.0)


# ---------------------------------------------------------------- HTTP 夹具
class PreviewHttpTests(unittest.TestCase):
    """真实 HTTP 层：预览静态服务 + 网页自检。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

        # 网页资源：一个带 index.html 的目录、一个裸 html、一个无首页的目录
        (self.ws / "site").mkdir()
        (self.ws / "site" / "index.html").write_text(
            "<!doctype html><title>首页</title>", encoding="utf-8", newline=""
        )
        (self.ws / "site" / "app.css").write_text("body{color:red}", encoding="utf-8", newline="")
        (self.ws / "page.html").write_text("<!doctype html><h1>hi</h1>", encoding="utf-8", newline="")
        (self.ws / "plain").mkdir()          # 目录，但没有 index.html
        (self.ws / "bin.exe").write_bytes(b"MZ\x90\x00binary")
        (self.ws / "data.zip").write_bytes(b"PK\x03\x04archive")

        # host 私产：预览端口不该把它暴露出去
        (self.ws / ".vibe").mkdir()
        (self.ws / ".vibe" / "page.html").write_text("SECRET-CONTENT", encoding="utf-8", newline="")
        (self.ws / ".vibe" / "audit.jsonl").write_text("{}\n", encoding="utf-8", newline="")

        config = Config.load(self.ws)
        self.host = HostServer(config, port=0, client=FakeClient([[]]))
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._tmp.cleanup()

    def get(self, path: str, token: str | None = None, headers: dict | None = None):
        request = urllib.request.Request(self.base + path)
        if token is not None:
            request.add_header("X-Vibe-Token", token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return self._opener.open(request, timeout=10)

    def post(self, path: str, body: dict, token: str | None = None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("X-Vibe-Token", token)
        return self._opener.open(request, timeout=10)

    def status_of(self, path: str, **kwargs) -> int:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(path, **kwargs)
        return ctx.exception.code

    # ---- 正常路径 ---------------------------------------------------------
    def test_serves_html_with_preview_csp(self) -> None:
        """预览响应必须自带预览 CSP 与 no-referrer —— 出口安全在这一层焊死。"""
        with self.get("/preview/page.html") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertIn("connect-src 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("<h1>hi</h1>", response.read().decode("utf-8"))

    def test_preview_needs_no_token(self) -> None:
        """与静态壳一致：iframe 子资源带不了令牌，故 /preview/* 不查令牌。"""
        with self.get("/preview/page.html") as response:
            self.assertEqual(response.status, 200)

    def test_preview_still_checks_origin(self) -> None:
        """免令牌 ≠ 免校验：Host/Origin 两道闸对预览照样生效（防 DNS Rebinding）。"""
        self.assertEqual(
            self.status_of("/preview/page.html", headers={"Origin": "http://evil.example.com"}),
            403,
        )
        self.assertEqual(
            self.status_of("/preview/page.html", headers={"Host": "evil.example.com"}),
            403,
        )

    # ---- ① 越权 / 私产 -----------------------------------------------------
    def test_traversal_rejected(self) -> None:
        """`..%2f` 被解码成 `../` 后必须被 PathGuard 拦下，绝不能读出工作区外的文件。"""
        outside = self.ws.parent / "outside.txt"
        outside.write_text("OUTSIDE-SECRET", encoding="utf-8", newline="")
        try:
            self.assertEqual(self.status_of("/preview/..%2foutside.txt"), 403)
        finally:
            outside.unlink(missing_ok=True)

    def test_vibe_dir_rejected(self) -> None:
        """`.vibe/` 是 host 私产：即便后缀在 MIME 白名单内也不许预览。"""
        code = self.status_of("/preview/.vibe/page.html")
        self.assertEqual(code, 403)

    # ---- ② MIME 白名单 -----------------------------------------------------
    def test_executable_rejected(self) -> None:
        self.assertEqual(self.status_of("/preview/bin.exe"), 403)

    def test_archive_rejected(self) -> None:
        self.assertEqual(self.status_of("/preview/data.zip"), 403)

    def test_css_served(self) -> None:
        with self.get("/preview/site/app.css") as response:
            self.assertEqual(response.headers["Content-Type"], "text/css; charset=utf-8")

    # ---- ③ 目录回落 --------------------------------------------------------
    def test_directory_falls_back_to_index(self) -> None:
        for path in ("/preview/site", "/preview/site/"):
            with self.get(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("首页", response.read().decode("utf-8"))

    def test_directory_without_slash_redirects(self) -> None:
        """无尾斜杠必须 301 到带斜杠 —— 否则页内相对资源会解析到错误层级。

        上面那条用例用的是**会跟随重定向**的 opener，它只能证明「最终拿得到
        首页」，证明不了 URL 层级是对的。这里用不跟随的 opener 直接看 301。
        """

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                return None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(
                urllib.request.Request(self.base + "/preview/site"), timeout=10
            )
        self.assertEqual(ctx.exception.code, 301)
        self.assertEqual(ctx.exception.headers["Location"], "/preview/site/")

    def test_directory_without_index_is_404(self) -> None:
        self.assertEqual(self.status_of("/preview/plain"), 404)

    def test_missing_file_is_404(self) -> None:
        self.assertEqual(self.status_of("/preview/nope.html"), 404)

    # ---- ④ 网页自检 --------------------------------------------------------
    def test_missing_image_reports_unavailable_not_500(self) -> None:
        """浏览器直开没有捕获能力：如实回 available:false，**不是** 500。"""
        with self.post(
            "/api/visual/check",
            {"url": "page.html", "width": 800, "height": 600},
            token=self.token,
        ) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode("utf-8"))
        self.assertFalse(data["available"])
        self.assertFalse(data["ok"])
        self.assertTrue(data["reason"])

    def test_missing_url_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/visual/check", {"image": _b64(png(4, 4, (0, 0, 0)))}, token=self.token)
        self.assertEqual(ctx.exception.code, 400)

    def test_non_png_image_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(
                "/api/visual/check",
                {"url": "page.html", "image": _b64(b"not a png at all")},
                token=self.token,
            )
        self.assertEqual(ctx.exception.code, 400)

    def test_first_check_without_baseline_reports_missing(self) -> None:
        blob = png(40, 30, (10, 20, 30))
        with self.post(
            "/api/visual/check",
            {"url": "page.html", "width": 40, "height": 30, "image": _b64(blob)},
            token=self.token,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["available"])
        self.assertTrue(data["ok"])
        self.assertTrue(data["baselineMissing"])

    def test_set_baseline_then_identical_is_zero_diff(self) -> None:
        blob = png(40, 30, (10, 20, 30))
        body = {"url": "page.html", "width": 40, "height": 30, "image": _b64(blob)}
        with self.post("/api/visual/check", {**body, "setBaseline": True}, token=self.token) as response:
            saved = json.loads(response.read().decode("utf-8"))
        self.assertTrue(saved["saved"])
        # 基线只落 host 侧 .vibe/visual/，不写工作区源码
        baseline_files = list((self.ws / ".vibe" / "visual").glob("*.png"))
        self.assertEqual(len(baseline_files), 1)

        with self.post("/api/visual/check", body, token=self.token) as response:
            again = json.loads(response.read().decode("utf-8"))
        self.assertTrue(again["ok"])
        self.assertFalse(again["baselineMissing"])
        self.assertEqual(again["diffRatio"], 0.0)

    def test_changed_page_shows_positive_diff(self) -> None:
        base = png(40, 30, (0, 0, 0))
        with self.post(
            "/api/visual/check",
            {"url": "page.html", "width": 40, "height": 30, "image": _b64(base), "setBaseline": True},
            token=self.token,
        ) as response:
            response.read()
        with self.post(
            "/api/visual/check",
            {"url": "page.html", "width": 40, "height": 30, "image": _b64(png(40, 30, (255, 255, 255)))},
            token=self.token,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertGreater(data["diffRatio"], 0.5)
        self.assertEqual(data["changedBlocks"], data["totalBlocks"])

    def test_device_widths_use_separate_baselines(self) -> None:
        """换设备宽度落到另一条基线键上 —— 手机/桌面截图不该互相判为「变了」。"""
        blob = png(40, 30, (1, 2, 3))
        with self.post(
            "/api/visual/check",
            {"url": "page.html", "width": 40, "height": 30, "image": _b64(blob), "setBaseline": True},
            token=self.token,
        ) as response:
            response.read()
        with self.post(
            "/api/visual/check",
            {"url": "page.html", "width": 375, "height": 30, "image": _b64(blob)},
            token=self.token,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["baselineMissing"])   # 375 宽还没有基线


class PreviewChangedEventTests(unittest.TestCase):
    """`preview.changed` 的载荷契约（P0）。

    这个事件会进 SSE 流（并带历史回放），所以「只带路径、不带内容」是硬约束：
    一旦有人往 payload 里塞正文，ADR-006 就被绕过了，而且不会有人发现。
    """

    class _Bus:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def publish(self, event: dict) -> None:
            self.events.append(event)

    def _emit(self, paths: list[str]) -> list[dict]:
        from agent_core.host.session import Session

        holder = type("Holder", (), {"bus": self._Bus()})()
        Session._on_files_changed(holder, paths)
        return holder.bus.events

    def test_paths_are_deduped_and_sorted(self) -> None:
        events = self._emit(["b.css", "a.html", "a.html"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "preview.changed")
        self.assertEqual(events[0]["paths"], ["a.html", "b.css"])
        self.assertEqual(events[0]["count"], 2)
        self.assertFalse(events[0]["truncated"])

    def test_payload_carries_no_content(self) -> None:
        """字段集合必须恰好是这几个 —— 多一个正文键就是红线失守。"""
        events = self._emit(["secret.html"])
        self.assertEqual(
            set(events[0]), {"type", "ts", "paths", "count", "truncated"}
        )

    def test_empty_change_emits_nothing(self) -> None:
        self.assertEqual(self._emit([]), [])

    def test_truncates_and_flags(self) -> None:
        """一次改动过多（切分支 / 批量生成）时截断路径，并让界面全刷。"""
        events = self._emit([f"f{i:04d}.html" for i in range(250)])
        self.assertEqual(len(events[0]["paths"]), 200)
        self.assertEqual(events[0]["count"], 250)
        self.assertTrue(events[0]["truncated"])


class WatcherChangeHookTests(unittest.TestCase):
    """`IndexWatcher.on_change`：只在真有变化时回调，且只给相对路径。

    它与审计那条「不写链」的纪律并不冲突：回调只拿路径、只做广播。
    回调里抛异常必须被吞掉 —— 后台轮询线程不能因为订阅方故障而死。
    """

    def setUp(self) -> None:
        from agent_core.index import IndexWatcher

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "page.html").write_text("A", encoding="utf-8")
        self.seen: list[list[str]] = []
        self.watcher = IndexWatcher(
            self.root, interval=1, max_files=50, on_change=self.seen.append
        )
        self.watcher._snapshot = self.watcher._scan()

    def tearDown(self) -> None:
        self.watcher.stop()
        self._tmp.cleanup()

    def test_no_change_does_not_notify(self) -> None:
        self.assertIsNone(self.watcher.poll_once())
        self.assertEqual(self.seen, [])

    def test_change_notifies_relative_path(self) -> None:
        # 改大小而不只改 mtime：mtime 的粒度取决于文件系统，靠 size 更稳。
        (self.root / "page.html").write_text("ABCDEFG", encoding="utf-8")
        self.watcher.poll_once()
        self.assertEqual(len(self.seen), 1)
        self.assertIn("page.html", self.seen[0])

    def test_subscriber_failure_does_not_raise(self) -> None:
        from agent_core.index import IndexWatcher

        def boom(_paths: list[str]) -> None:
            raise RuntimeError("订阅方炸了")

        watcher = IndexWatcher(
            self.root, interval=1, max_files=50, on_change=boom
        )
        watcher._snapshot = watcher._scan()
        (self.root / "page.html").write_text("ZZZZZZZZZ", encoding="utf-8")
        watcher.poll_once()      # 不得抛出
        watcher.stop()


if __name__ == "__main__":
    unittest.main()