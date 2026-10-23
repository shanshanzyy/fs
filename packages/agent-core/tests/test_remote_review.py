"""M11 远程审查面：本会话远端改动台账 + 留底 vs 远端的 unified diff + 只读自审工具。

SSH 一律用假传输（见 `tests/test_remote.py.FakeTransport`）。这里钉的是三件事：

1. **台账如实** —— save / patch / create 各自登记成什么动作、有没有留底；
2. **diff 正确** —— 新增 / 修改 / 删除三种情形的分档，以及多块改动拆成多个 hunk；
3. **审计不出正文** —— diff 只走端点响应；审计里一个字符的正文都不许出现
   （这是 F4 反向验证④的靶子：一旦有人把 diff 塞进审计，这里必须变红）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.remote.session import ChangeEntry  # noqa: E402
from agent_core.remote.store import RemoteStore, store_path_for  # noqa: E402
from agent_core.sandbox import Risk  # noqa: E402
from agent_core.tools import build_readonly_registry  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402
from tests.test_remote import FakeTransport, make_conn  # noqa: E402

APP_PY = "/srv/app/src/app.py"
OLD_TEXT = "".join(f"line{i}\n" for i in range(12))
NEW_TEXT = OLD_TEXT.replace("line0\n", "LINEZERO\n").replace("line11\n", "LINEELEVEN\n")


class RemoteReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir(exist_ok=True)
        self.transport = FakeTransport({APP_PY: OLD_TEXT.encode()})
        self._patch_transport = self._enter(
            "agent_core.remote.workspace.transport_for", lambda conn: self.transport
        )
        self.config = Config.load(self.ws, remote_enabled=True, session_id="m11rev")
        store = RemoteStore.load(store_path_for(self.config))
        store.upsert(make_conn(allow_write=True))
        self.host = HostServer(self.config, port=0, client=FakeClient([]))
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self.session = self.host.ctx.session
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.post("/api/remote/open", {"connection": "devbox"})

    def tearDown(self) -> None:
        self.host.shutdown()
        self._patch_transport.stop()
        self._tmp.cleanup()

    # ---- 助手 -------------------------------------------------------------
    def _enter(self, target: str, value):
        patcher = mock.patch(target, value)
        patcher.start()
        return patcher

    def _call(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-Vibe-Token", self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=10)

    def get(self, path: str) -> dict:
        with self._call("GET", path) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, body=None) -> dict:
        with self._call("POST", path, body if body is not None else {}) as response:
            return json.loads(response.read().decode("utf-8"))

    def save(self, path: str, content: str) -> dict:
        return self.post("/api/remote/save", {"path": path, "content": content})

    def audit_lines(self) -> list[dict]:
        path = Path(self.config.audit_path)
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def run_tool(self, args: dict | None = None):
        return self.session.registry.execute(
            self.session.agent.ctx, "remote_review_changes", args or {}
        )

    # ---- 台账 -------------------------------------------------------------
    def test_ledger_registers_save_and_patch_with_backups(self) -> None:
        self.save("src/app.py", NEW_TEXT)
        self.post(
            "/api/remote/patch",
            {"path": "src/app.py", "search": "LINEZERO", "replace": "line0"},
        )
        changes = self.get("/api/remote/changes")
        self.assertEqual(changes["connection"], "devbox")
        entry = changes["changes"][0]
        self.assertEqual(entry["rel_path"], "src/app.py")
        self.assertEqual(entry["action"], "edit")
        self.assertTrue(entry["backed_up"])
        self.assertTrue(entry["backup_path"])
        self.assertEqual(len(changes["changes"]), 1)  # 同一文件重复改只留最新一条

    def test_ledger_create_has_no_backup(self) -> None:
        self.save("docs/new.md", "# hi\n")
        entry = self.get("/api/remote/changes")["changes"][0]
        self.assertEqual(entry["action"], "create")
        self.assertFalse(entry["backed_up"])
        self.assertEqual(entry["backup_path"], "")

    # ---- diff 四种分档 -----------------------------------------------------
    def test_diff_of_modified_file(self) -> None:
        self.save("src/app.py", NEW_TEXT)
        data = self.get("/api/remote/diff?path=src/app.py")
        self.assertTrue(data["available"])
        self.assertEqual(data["added"], 2)
        self.assertEqual(data["removed"], 2)
        self.assertIn("-line0", data["diff"])
        self.assertIn("+LINEZERO", data["diff"])

    def test_diff_splits_two_blocks_into_two_hunks(self) -> None:
        self.save("src/app.py", NEW_TEXT)
        data = self.get("/api/remote/diff?path=src/app.py")
        self.assertEqual(len(data["hunks"]), 2, data["diff"])
        first, second = data["hunks"]
        self.assertEqual(first["tag"], "replace")
        # `to_api()` 的 oldStart 是 1 基（贴合编辑器习惯），oldEnd 是 0 基半开区间的上界
        self.assertEqual(first["oldStart"], 1)
        self.assertEqual(first["oldEnd"], 1)
        self.assertEqual(first["oldLines"], ["line0"])
        self.assertEqual(first["newLines"], ["LINEZERO"])
        self.assertEqual(second["oldStart"], 12)
        self.assertEqual(second["oldLines"], ["line11"])

    def test_diff_of_new_file_is_all_added(self) -> None:
        self.save("docs/new.md", "# hi\nthere\n")
        data = self.get("/api/remote/diff?path=docs/new.md")
        self.assertTrue(data["available"])
        self.assertEqual(data["removed"], 0)
        self.assertEqual(data["added"], 2)
        self.assertEqual(data["hunks"][0]["tag"], "insert")

    def test_diff_reports_deleted_remote_file(self) -> None:
        self.save("src/app.py", NEW_TEXT)
        self.transport.files.pop(APP_PY)
        data = self.get("/api/remote/diff?path=src/app.py")
        self.assertFalse(data["available"])
        self.assertIn("已不存在", data["reason"])

    def test_diff_without_record_is_explained(self) -> None:
        data = self.get("/api/remote/diff?path=src/app.py")
        self.assertFalse(data["available"])
        self.assertIn("没有改动记录", data["reason"])

    def test_diff_belonging_to_another_connection_is_explained(self) -> None:
        """别的连接的改动，现时读不到「远端当前内容」—— 如实说，不假装成功。"""
        sessions = self.session.remote_sessions()
        sessions.note_change(
            ChangeEntry(rel_path="src/app.py", connection="other", backed_up=False)
        )
        data = self.get("/api/remote/diff?path=src/app.py&connection=other")
        self.assertFalse(data["available"])
        self.assertIn("other", data["reason"])

    def test_diff_requires_path(self) -> None:
        try:
            with self._call("GET", "/api/remote/diff") as response:
                self.fail(f"应返回 400，实际 {response.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    # ---- 只读自审工具 ------------------------------------------------------
    def test_review_tool_is_safe_and_not_in_readonly_registry(self) -> None:
        """只读恒 SAFE；子 Agent 的工具面**结构上不含**任何远程工具。"""
        spec = self.session.registry.get("remote_review_changes")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, Risk.SAFE)
        self.assertFalse(
            set(build_readonly_registry(lsp=False).names()) & {"remote_review_changes"}
        )

    def test_review_tool_lists_backups_without_body(self) -> None:
        self.save("src/app.py", NEW_TEXT)
        result = self.run_tool()
        self.assertTrue(result.ok)
        self.assertIn("remote_review_changes", result.content)
        self.assertIn("src/app.py", result.content)
        self.assertIn("改动前 sha256:", result.content)
        # 留底目录里的正文一个字都不该出现在工具输出里
        self.assertNotIn("line0", result.content)
        self.assertNotIn("LINEZERO", result.content)

    def test_review_tool_reports_nothing_yet(self) -> None:
        result = self.run_tool()
        self.assertTrue(result.ok)
        self.assertIn("尚未", result.content)

    def test_review_tool_audit_has_no_body(self) -> None:
        self.save("src/app.py", NEW_TEXT)
        self.run_tool()
        lines = [
            line for line in self.audit_lines()
            if line["event"] == "tool.remote_review_changes"
        ]
        self.assertTrue(lines)
        self.assertIn("exists", lines[0])
        blob = json.dumps(lines, ensure_ascii=False)
        # 留底正文与远端正文都不许进审计（ADR-006）
        self.assertNotIn("line0", blob)
        self.assertNotIn("LINEZERO", blob)

    # ---- 审计不含 diff 正文 ------------------------------------------------
    def test_audit_never_carries_diff_body(self) -> None:
        """diff 只走端点响应与界面；审计里连一行 diff 都不许有（ADR-006）。"""
        self.save("src/app.py", NEW_TEXT)
        lines = self.audit_lines()
        self.assertTrue(any(line["event"] == "remote.file" for line in lines))
        blob = json.dumps(lines, ensure_ascii=False)
        for leak in ("LINEZERO", "line0", "@@", "+++", "---"):
            self.assertNotIn(leak, blob, f"审计里出现了 diff 正文片段: {leak}")


if __name__ == "__main__":
    unittest.main()
