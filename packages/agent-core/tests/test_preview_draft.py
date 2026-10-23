"""审批期草稿预览（P1-2）测试。

草稿预览是全项目**唯一**一个「内容由模型产生、却还没落盘」的对外读取面。
正因为它是唯一，边界必须逐条钉死，而不是靠「反正只有内部界面会调」：

1. **绝不写盘** —— 取完草稿，磁盘上的文件字节一个都不能变；
2. **正文只经 token 取** —— `POST /api/preview/draft` 的响应里不含正文；
3. **token 绑定单次审批** —— 不可跨审批复用，同一审批幂等；
4. **审批解决即失效** —— 放行 / 拒绝 / 超时之后，token 立刻 404；
5. **与静态预览同一条口径** —— 同 `PREVIEW_CSP`、同 no-store、
   同 PathGuard 读校验、同 `.vibe/` 拒绝。

反向验证：把 `Session._revoke_drafts` 改成空实现，`test_token_dies_with_approval`
必须变红；把 `draft_scope` 的 token 校验去掉，`test_unknown_token_is_404` 必须变红。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.loop import ApprovalRequest  # noqa: E402
from agent_core.sandbox import Risk  # noqa: E402
from agent_core.tools.base import ToolPreview  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

OLD_HTML = "<!doctype html><title>旧标题</title><link rel=stylesheet href=app.css>"
NEW_HTML_TITLE = "新标题"
OLD_CSS = "body{color:red}"
NEW_CSS = "body{color:blue}"


class DraftPreviewHttpTests(unittest.TestCase):
    """真实 HTTP 层 + 真实审批会合点（Agent 线程确实会挂住等决定）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

        (self.ws / "site").mkdir()
        (self.ws / "site" / "index.html").write_text(OLD_HTML, encoding="utf-8", newline="")
        (self.ws / "site" / "app.css").write_text(OLD_CSS, encoding="utf-8", newline="")
        (self.ws / "site" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.ws / "hello.py").write_text("print('old')\n", encoding="utf-8", newline="")
        (self.ws / ".vibe").mkdir()
        (self.ws / ".vibe" / "audit.jsonl").write_text("{}\n", encoding="utf-8", newline="")

        config = Config.load(self.ws)
        self.host = HostServer(config, port=0, client=FakeClient([[]]))
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self.session = self.host.ctx.session
        # 冒充「有界面连着」：`_approve` 在没有订阅者时会直接拒绝（fail-safe），
        # 那样就走不到草稿这一步。这里订阅但不消费 —— 总线丢最旧事件、绝不阻塞。
        self.sub = self.session.bus.subscribe()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._threads: list[threading.Thread] = []

    def tearDown(self) -> None:
        # 任何一条断言失败都不能让 Agent 线程悬在审批上等 30 分钟：
        # 无条件放行一个 deny，再等它退出。
        with self.session._lock:  # noqa: SLF001 — 测试夹具，需绕过公开 API
            ids = list(self.session._pending)  # noqa: SLF001
        for approval_id in ids:
            try:
                self.session.resolve_approval(approval_id, "deny")
            except ValueError:
                pass
        for thread in self._threads:
            thread.join(timeout=10)
        self.sub.close()
        self.host.shutdown()
        self._tmp.cleanup()

    # ---- HTTP 辅助 --------------------------------------------------------
    def get(self, path: str, headers: dict | None = None):
        request = urllib.request.Request(self.base + path)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return self._opener.open(request, timeout=10)

    def post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Vibe-Token", self.token)
        return self._opener.open(request, timeout=10)

    def status_of(self, path: str, *, method: str = "GET", body: dict | None = None) -> int:
        try:
            if method == "GET":
                self.get(path).read()
            else:
                self.post(path, body or {}).read()
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code
        return 200

    # ---- 审批夹具 ---------------------------------------------------------
    def _draft_for(self, tool: str, args: dict):
        """走真实的工具层算草稿（不手搓 DraftFile）—— 顺带覆盖 `ToolSpec.draft`。"""
        return self.session.agent.registry.get(tool).draft(self.session.agent.ctx, args)

    def _start_approval(self, tool: str, args: dict) -> dict:
        """在后台线程里发起一次真实审批，等它进入「待决定」状态后返回事件载荷。"""
        draft = self._draft_for(tool, args)
        request = ApprovalRequest(
            tool=tool,
            arguments=args,
            risk=Risk.REVIEW,
            reason="测试用改动",
            preview=ToolPreview(kind="diff", body="--- a\n+++ b\n"),
            draft=draft,
        )
        thread = threading.Thread(
            target=self.session._approve, args=(request,), daemon=True  # noqa: SLF001
        )
        thread.start()
        self._threads.append(thread)

        deadline = time.time() + 10
        while time.time() < deadline:
            payload = self.session.pending_approval   # property，不是方法
            if payload:
                return payload
            time.sleep(0.02)
        self.fail("审批没有进入待决定状态（Agent 线程没挂住？）")

    def _request_draft(self, approval_id: str) -> dict:
        with self.post("/api/preview/draft", {"approvalId": approval_id}) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    # ---- ① 内容正确、且绝不写盘 -------------------------------------------
    def test_draft_serves_post_patch_html_without_writing_disk(self) -> None:
        args = {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE}
        payload = self._start_approval("edit_file", args)

        self.assertEqual(payload["draftPaths"], ["site/index.html"])
        info = self._request_draft(payload["approvalId"])
        self.assertEqual(info["primary"], "site/index.html")
        self.assertEqual(info["paths"], ["site/index.html"])
        self.assertTrue(info["url"].startswith("/preview-draft/"))

        with self.get(info["url"]) as response:
            self.assertEqual(response.status, 200)
            body = response.read().decode("utf-8")
            # 与静态预览同一条口径：CSP 焊死网络出口、不缓存、不带 Referer
            self.assertIn("connect-src 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn(NEW_HTML_TITLE, body)
        self.assertNotIn("旧标题", body)

        # 最核心的一条：草稿只在内存里，磁盘上仍是旧内容
        on_disk = (self.ws / "site" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(on_disk, OLD_HTML)

    def test_api_response_never_carries_draft_body(self) -> None:
        """`POST /api/preview/draft` 只给凭据与路径 —— 正文必须再走一次 GET。

        这条不是形式主义：一步到位的接口会让「谁读了这份未落盘内容」失去留痕点。
        """
        args = {"path": "site/index.html", "search": "旧标题", "replace": "另一个标题"}
        payload = self._start_approval("edit_file", args)
        with self.post("/api/preview/draft", {"approvalId": payload["approvalId"]}) as response:
            raw = response.read().decode("utf-8")
        self.assertNotIn("另一个标题", raw)
        self.assertNotIn("<!doctype", raw)
        info = json.loads(raw)
        self.assertEqual(sorted(info["paths"]), ["site/index.html"])

    # ---- ② 同批次资源也用改后内容 -----------------------------------------
    def test_sibling_asset_from_same_batch_uses_draft_text(self) -> None:
        """`index.html` 与 `app.css` 同批改动时，CSS 也必须取改后的。

        否则预览会是「新结构 + 旧样式」—— 那比看不到预览更容易误导审批。
        """
        args = {
            "edits": [
                {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE},
                {"path": "site/app.css", "search": "red", "replace": "blue"},
            ]
        }
        payload = self._start_approval("edit_files", args)
        info = self._request_draft(payload["approvalId"])
        self.assertEqual(info["primary"], "site/index.html")   # HTML 优先当主文档
        self.assertEqual(sorted(info["paths"]), ["site/app.css", "site/index.html"])

        css_url = info["url"].replace("index.html", "app.css")
        with self.get(css_url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/css; charset=utf-8")
            self.assertEqual(response.read().decode("utf-8"), NEW_CSS)

        self.assertEqual((self.ws / "site" / "app.css").read_text(encoding="utf-8"), OLD_CSS)

    def test_untouched_asset_falls_back_to_disk(self) -> None:
        """草稿页面引用的、本次没改的图片仍要能加载 —— 走磁盘读，照样过 PathGuard。"""
        args = {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE}
        payload = self._start_approval("edit_file", args)
        info = self._request_draft(payload["approvalId"])

        with self.get(info["url"].replace("index.html", "logo.png")) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))

    # ---- ③ token 生命周期 -------------------------------------------------
    def test_second_request_returns_same_token(self) -> None:
        """同一审批幂等：每点一次就发新 token，等于让「已失效」追不上新凭证。"""
        args = {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE}
        payload = self._start_approval("edit_file", args)
        first = self._request_draft(payload["approvalId"])
        second = self._request_draft(payload["approvalId"])
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(first["url"], second["url"])

    def test_token_dies_with_approval(self) -> None:
        """审批一解决（这里用拒绝），草稿立刻 404 —— 不留「批完还能翻旧稿」的窗口。"""
        args = {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE}
        payload = self._start_approval("edit_file", args)
        info = self._request_draft(payload["approvalId"])

        self.assertTrue(self.session.resolve_approval(payload["approvalId"], "deny"))
        for thread in self._threads:
            thread.join(timeout=10)

        self.assertEqual(self.session.draft_scope(info["token"]), {})
        self.assertEqual(self.status_of(info["url"]), 404)
        # 审批结束后再申请也是 409（状态冲突，不是参数错）
        self.assertEqual(
            self.status_of("/api/preview/draft", method="POST", body={"approvalId": payload["approvalId"]}),
            409,
        )

    def test_unknown_token_is_404(self) -> None:
        self.assertEqual(self.status_of("/preview-draft/deadbeef/site/index.html"), 404)

    def test_bare_token_redirects_to_primary(self) -> None:
        """裸 token 要 302 到主文档：停在上一层会让页内相对资源解析错层级。"""
        args = {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE}
        payload = self._start_approval("edit_file", args)
        info = self._request_draft(payload["approvalId"])
        # 只留 token 的那一层：`/preview-draft/<token>`
        bare = "/preview-draft/" + info["token"]

        with self.get(bare) as response:   # urllib 自动跟随 302
            self.assertEqual(response.status, 200)
            self.assertIn(NEW_HTML_TITLE, response.read().decode("utf-8"))

    def test_write_file_new_page_is_previewable_before_creation(self) -> None:
        """新建文件也要能预览 —— 「这个页面长什么样」在落盘前同样该看得到。"""
        args = {"path": "site/about.html", "content": "<!doctype html><h1>关于</h1>"}
        payload = self._start_approval("write_file", args)
        self.assertEqual(payload["draftPaths"], ["site/about.html"])

        info = self._request_draft(payload["approvalId"])
        self.assertEqual(info["created"], ["site/about.html"])
        with self.get(info["url"]) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("关于", response.read().decode("utf-8"))
        # 磁盘上依然不存在：预览不是「先写个临时文件再读」
        self.assertFalse((self.ws / "site" / "about.html").exists())

    # ---- ④ 沙箱口径 -------------------------------------------------------
    def test_path_traversal_and_vibe_dir_rejected(self) -> None:
        args = {"path": "site/index.html", "search": "旧标题", "replace": NEW_HTML_TITLE}
        payload = self._start_approval("edit_file", args)
        info = self._request_draft(payload["approvalId"])
        base = info["url"].rsplit("/", 1)[0]

        # `..` 必须被 PathGuard 拦下（草稿面不是「沙箱的一个例外」）
        self.assertEqual(self.status_of(base + "/" + urllib.parse.quote("../../etc/passwd")), 403)
        # host 私产（审计 / 检查点）照旧不对外
        self.assertEqual(self.status_of(base + "/.vibe/audit.jsonl"), 403)

    def test_non_web_edit_has_no_draft(self) -> None:
        """.py 改动没有草稿：草稿只服务浏览器渲染得了的文本资源。"""
        args = {"path": "hello.py", "search": "old", "replace": "new"}
        payload = self._start_approval("edit_file", args)
        self.assertEqual(payload["draftPaths"], [])
        self.assertEqual(
            self.status_of("/api/preview/draft", method="POST", body={"approvalId": payload["approvalId"]}),
            400,
        )

    # ---- ⑤ 审计只记元数据 -------------------------------------------------
    def test_audit_has_no_draft_body(self) -> None:
        args = {"path": "site/index.html", "search": "旧标题", "replace": "审计不该看到的标题"}
        payload = self._start_approval("edit_file", args)
        self._request_draft(payload["approvalId"])

        log = (self.ws / ".vibe" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("preview.draft", log)
        self.assertIn("site/index.html", log)
        # 正文绝不出现在审计里（ADR-006）
        self.assertNotIn("审计不该看到的标题", log)


class DraftPreviewLoopWiringTests(unittest.TestCase):
    """走**真实一轮 Agent 循环**：钉住 `_execute_call` 确实把草稿塞进了审批请求。

    上面那组测试是手工构造 `ApprovalRequest(draft=...)` 的，覆盖不到
    `loop/agent.py` 里那一行 `draft=spec.draft(self.ctx, call.arguments)`。
    那行写错（漏传、算错、传成 preview）在别处都不会报错 —— 只会表现为
    「预览效果按钮永不出现」，而这恰恰是最难被发现的一类退化。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "site").mkdir()
        (self.ws / "site" / "index.html").write_text(OLD_HTML, encoding="utf-8", newline="")

        self.client = FakeClient(
            [
                [("edit_file", {"path": "site/index.html", "search": "旧标题", "replace": "循环改的标题"})],
                "改完了",
            ]
        )
        self.host = HostServer(Config.load(self.ws), port=0, client=self.client)
        self.host.start_background()
        self.session = self.host.ctx.session
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self.sub = self.session.bus.subscribe()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        with self.session._lock:  # noqa: SLF001
            ids = list(self.session._pending)  # noqa: SLF001
        for approval_id in ids:
            try:
                self.session.resolve_approval(approval_id, "deny")
            except ValueError:
                pass
        self.sub.close()
        self.host.shutdown()
        self._tmp.cleanup()

    def _wait_event(self, kind: str, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self.sub.get(timeout=1.0)
            if event and event.get("type") == kind:
                return event
        self.fail(f"等不到 {kind} 事件")

    def test_real_run_approval_carries_draft_and_serves_it(self) -> None:
        self.session.submit("把首页标题改一下")
        event = self._wait_event("run.approval")

        # 这一条就是「循环把草稿塞进审批请求」的证据
        self.assertEqual(event["draftPaths"], ["site/index.html"])

        request = urllib.request.Request(
            self.base + "/api/preview/draft",
            data=json.dumps({"approvalId": event["approvalId"]}).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Vibe-Token", self.token)
        info = json.loads(self._opener.open(request, timeout=10).read().decode("utf-8"))

        with self._opener.open(self.base + info["url"], timeout=10) as response:
            self.assertIn("循环改的标题", response.read().decode("utf-8"))

        # 放行 → 改动真正落盘 → 草稿随即失效，静态预览接手
        self.assertTrue(self.session.resolve_approval(event["approvalId"], "approve"))
        self._wait_event("run.finished")
        self.assertEqual((self.ws / "site" / "index.html").read_text(encoding="utf-8").count("循环改的标题"), 1)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._opener.open(self.base + info["url"], timeout=10)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()