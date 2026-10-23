"""M11 远程设备工作面：连接管理面 + 远程文件面（Session 门面 + HTTP 端点）。

SSH 一律用**假传输**（`tests.test_remote.FakeTransport`）—— 本文件不起真 ssh，
因此单机可跑。覆盖重点刻意偏在**安全语义**上：写前校验不落盘、连接清单原子写
round-trip、权限位热生效、保存的乐观锁、留底不可写则中止、jail 逃逸拒绝、
二进制 / 超限拒绝、门控 fail-closed（端点 400 + 会话如实上报未启用）。

不覆盖真实链路（需要一台真远端），那部分留作手动冒烟（见 M11-PLAN F5）。
"""

from __future__ import annotations

import hashlib
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
from agent_core.errors import SandboxViolation, ToolError  # noqa: E402
from agent_core.remote.store import (  # noqa: E402
    FORMAT_VERSION,
    RemoteStore,
    store_path_for,
)
from tests.test_agent_loop import FakeClient  # noqa: E402
from tests.test_remote import FakeTransport, _UnchangedPlan, make_conn  # noqa: E402

APP_PY = "/srv/app/src/app.py"
APP_TEXT = "value = 1\nsecond = 2\n"


# --------------------------------------------------------------------------- #
# 公共脚手架：起一个后台 host，并把传输层换成内存实现
# --------------------------------------------------------------------------- #
class _HostCase(unittest.TestCase):
    """起真实 HTTP host（`port=0`）+ 注入假传输。

    连接清单直接写进 host 使用的同一份 `remote.json` —— 面板与端点看到的是
    同一份配置，不存在「测试里加了连接、端点上却看不见」的错位。
    """

    files: dict[str, bytes] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir(exist_ok=True)
        self.transport = FakeTransport(dict(self.files))
        self._patch = mock.patch(
            "agent_core.remote.workspace.transport_for",
            lambda conn: self.transport,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- host 装配 --------------------------------------------------------
    def _start_host(self, *, remote: bool = True, conns=None, **overrides):
        kwargs = {"session_id": "m11ui", **overrides}
        if remote:
            kwargs["remote_enabled"] = True
        config = Config.load(self.ws, **kwargs)
        if remote and conns:
            store = RemoteStore.load(store_path_for(config))
            for conn in conns:
                store.upsert(conn)
        host = HostServer(config, port=0, client=FakeClient([]))
        host.start_background()
        self.addCleanup(host.shutdown)
        self.base = f"http://127.0.0.1:{host.port}"
        self.token = host.guard.token
        self.host = host
        self.config = config
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return host

    # ---- HTTP 助手 --------------------------------------------------------
    def _request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-Vibe-Token", self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=10)

    def get_json(self, path: str) -> dict:
        with self._request("GET", path) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, body=None) -> dict:
        with self._request("POST", path, body if body is not None else {}) as response:
            return json.loads(response.read().decode("utf-8"))

    def expect_status(self, method: str, path: str, body=None, code: int = 400) -> dict:
        try:
            with self._request(method, path, body) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, code, f"{method} {path} 状态码应为 {code}")
            return json.loads(exc.read().decode("utf-8") or "{}")

    def audit_lines(self) -> list[dict]:
        path = Path(self.config.audit_path)
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


# --------------------------------------------------------------------------- #
# 门控：VIBE_REMOTE 未设时 fail-closed
# --------------------------------------------------------------------------- #
class RemoteGatingTest(_HostCase):
    files: dict[str, bytes] = {}

    def test_endpoints_fail_closed_when_disabled(self) -> None:
        """未启用时端点一律 400（fail-closed），不静默降级成空结果。"""
        self._start_host(remote=False)
        body = self.expect_status("GET", "/api/remote/connections", code=400)
        self.assertIn("未启用", body.get("error", ""))
        self.expect_status("POST", "/api/remote/open", {}, code=400)

    def test_unknown_path_is_404_even_when_enabled(self) -> None:
        self._start_host(remote=True)
        self.expect_status("GET", "/api/remote/nope", code=404)

    def test_session_reports_remote_disabled(self) -> None:
        """界面靠 config.remoteEnabled 决定面板去留 —— 未启用时必须是 false。"""
        self._start_host(remote=False)
        state = self.get_json("/api/state")
        self.assertIs(state["config"]["remoteEnabled"], False)

    def test_session_reports_remote_enabled(self) -> None:
        self._start_host(remote=True)
        state = self.get_json("/api/state")
        self.assertIs(state["config"]["remoteEnabled"], True)

    def test_roots_endpoints_fail_closed_when_disabled(self) -> None:
        """M12 的三个根端点必须与 M11 同一条门控：未启用一律 400，不静默给空清单。

        「静默回一个空 roots」比 400 更坏 —— 界面会画出「工作区里没有远端根」，
        用户以为加不进去，实际是整个功能没开。
        """
        self._start_host(remote=False)
        body = self.expect_status("GET", "/api/remote/roots", code=400)
        self.assertIn("未启用", body.get("error", ""))
        self.expect_status("POST", "/api/remote/roots", {"connection": "x"}, code=400)
        self.expect_status(
            "POST", "/api/remote/roots/delete", {"connection": "x"}, code=400
        )


# --------------------------------------------------------------------------- #
# 连接管理面
# --------------------------------------------------------------------------- #
class RemoteConnectionApiTest(_HostCase):
    files: dict[str, bytes] = {APP_PY: APP_TEXT.encode()}

    def setUp(self) -> None:
        super().setUp()
        self._start_host(remote=True)

    def _list(self) -> dict:
        return self.get_json("/api/remote/connections")

    def test_add_then_list(self) -> None:
        data = self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "user": "dev",
                            "root": "/srv/app"}, "default": True},
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["connection"]["name"], "devbox")
        listed = self._list()
        self.assertEqual(listed["default"], "devbox")
        self.assertEqual([c["name"] for c in listed["connections"]], ["devbox"])
        self.assertTrue(listed["connections"][0]["default"])

    def test_add_is_persisted_atomically_and_round_trips(self) -> None:
        """落盘必须能被重新读回（原子写的 round-trip），且没有 .tmp 残渣。"""
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "user": "dev",
                            "root": "/srv/app"}},
        )
        path = store_path_for(self.config)
        raw = json.loads(path.read_text(encoding="utf-8"))
        names = [item["name"] for item in raw["connections"]]
        self.assertEqual(names, ["devbox"])
        leftovers = [p.name for p in path.parent.glob("*.tmp*")]
        self.assertEqual(leftovers, [], "原子写留下了临时文件残渣")
        # 用 store 重新加载，确认写出的确实是一份能用的清单
        self.assertEqual(
            [c.name for c in RemoteStore.load(path).list()], ["devbox"]
        )

    def test_bad_spec_rejected_and_not_persisted(self) -> None:
        """`-oProxyCommand=…` 当主机名会被 ssh 吃掉 —— 必须在落盘前拦住。"""
        body = self.expect_status(
            "POST",
            "/api/remote/connections",
            {"connection": {"name": "evil", "host": "-oProxyCommand=/bin/sh",
                            "root": "/srv/app"}},
            code=400,
        )
        self.assertIn("error", body)
        self.assertEqual(self._list()["connections"], [])

    def test_bad_root_rejected(self) -> None:
        self.expect_status(
            "POST",
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "root": "srv/app"}},
            code=400,
        )
        self.assertEqual(self._list()["connections"], [])

    def test_update_keeps_unspecified_fields(self) -> None:
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "user": "dev",
                            "root": "/srv/app"}},
        )
        data = self.post_json(
            "/api/remote/connections/devbox",
            {"note": "改过的备注"},
        )
        self.assertEqual(data["connection"]["note"], "改过的备注")
        self.assertEqual(data["connection"]["host"], "10.0.0.12")

    def test_default_and_delete(self) -> None:
        for name in ("a", "b"):
            self.post_json(
                "/api/remote/connections",
                {"connection": {"name": name, "host": "10.0.0." + str(len(name)),
                                "root": "/srv/app"}},
            )
        self.assertEqual(self.post_json("/api/remote/connections/a/default")["default"], "a")
        removed = self.post_json("/api/remote/connections/a/delete")
        self.assertEqual(removed["removed"], "a")
        # 删默认项后**不自动选接任者**（既有语义）：静默改默认会打错设备
        self.assertEqual(removed["default"], "")
        self.assertEqual([c["name"] for c in self._list()["connections"]], ["b"])

    def test_permissions_are_hot(self) -> None:
        """权限位是热开关：改完立刻对活动连接生效，不必重开。"""
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "user": "dev",
                            "root": "/srv/app"}},
        )
        opened = self.post_json("/api/remote/open", {"connection": "devbox"})
        self.assertFalse(opened["allowWrite"])
        # 只读连接上保存被拒
        self.expect_status(
            "POST", "/api/remote/save",
            {"path": "src/app.py", "content": "x\n"}, code=400,
        )
        perm = self.post_json(
            "/api/remote/connections/devbox/permissions", {"allowWrite": True}
        )
        self.assertTrue(perm["connection"]["allow_write"])
        # 改完立即可写（活动连接已被刷新）
        state = self.get_json("/api/remote/state")
        self.assertTrue(state["allowWrite"])
        saved = self.post_json(
            "/api/remote/save",
            {"path": "src/app.py", "content": "value = 9\nsecond = 2\n"},
        )
        self.assertTrue(saved["ok"])

    def test_permissions_requires_a_field(self) -> None:
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12",
                            "root": "/srv/app"}},
        )
        self.expect_status(
            "POST", "/api/remote/connections/devbox/permissions", {}, code=400
        )

    def test_test_endpoint_does_not_mutate_the_list(self) -> None:
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "user": "dev",
                            "root": "/srv/app"}},
        )
        before = self._list()
        result = self.post_json("/api/remote/connections/devbox/test")
        self.assertTrue(result["ok"])
        self.assertEqual(result["capabilities"]["os"], "Linux")
        self.assertEqual(self._list(), before)

    def test_unknown_connection_is_explained(self) -> None:
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12",
                            "root": "/srv/app"}},
        )
        body = self.expect_status(
            "POST", "/api/remote/open", {"connection": "ghost"}, code=400
        )
        self.assertIn("devbox", body.get("error", ""))  # 报错里列出可用的那条

    def test_connection_actions_are_audited(self) -> None:
        self.post_json(
            "/api/remote/connections",
            {"connection": {"name": "devbox", "host": "10.0.0.12", "user": "dev",
                            "root": "/srv/app"}},
        )
        events = [line for line in self.audit_lines() if line["event"] == "remote.connection"]
        self.assertTrue(any(line["action"] == "added" for line in events))
        # 审计只记元数据：host / 密钥路径不出机
        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("10.0.0.12", blob)


# --------------------------------------------------------------------------- #
# 远程文件面
# --------------------------------------------------------------------------- #
class RemoteFileApiTest(_HostCase):
    files: dict[str, bytes] = {
        APP_PY: APP_TEXT.encode(),
        "/srv/app/src/blob.bin": b"MZ\x00\x01binary",
    }

    def setUp(self) -> None:
        super().setUp()
        self._start_host(remote=True, conns=[make_conn(allow_write=True, allow_shell=True)])
        self.post_json("/api/remote/open", {"connection": "devbox"})

    def _save(self, path: str, content: str, digest: str = "") -> dict:
        body = {"path": path, "content": content}
        if digest:
            body["expectedDigest"] = digest
        return self.post_json("/api/remote/save", body)

    def test_tree_lists_one_level_and_marks_kind(self) -> None:
        data = self.get_json("/api/remote/tree?path=src")
        self.assertEqual(data["path"], "src")
        by_name = {entry["name"]: entry for entry in data["entries"]}
        self.assertEqual(by_name["app.py"]["type"], "file")
        self.assertEqual(by_name["app.py"]["size"], len(APP_TEXT))
        self.assertFalse(by_name["app.py"]["link"])

    def test_tree_escape_rejected(self) -> None:
        self.expect_status("GET", "/api/remote/tree?path=../../etc", code=403)

    def test_read_file_returns_metadata(self) -> None:
        data = self.get_json("/api/remote/file?path=src/app.py")
        self.assertEqual(data["text"], APP_TEXT)
        self.assertEqual(data["newline"], "LF")
        self.assertEqual(data["size"], len(APP_TEXT))
        self.assertTrue(data["digest"])
        self.assertEqual(data["connection"], "devbox")

    def test_read_file_requires_path(self) -> None:
        self.expect_status("GET", "/api/remote/file", code=400)

    def test_read_escape_rejected(self) -> None:
        self.expect_status("GET", "/api/remote/file?path=../../../etc/passwd", code=403)

    def test_read_binary_rejected(self) -> None:
        """二进制如实拒绝，不做 latin-1 误解码的伪文本预览。"""
        body = self.expect_status("GET", "/api/remote/file?path=src/blob.bin", code=400)
        self.assertIn("二进制", body.get("error", ""))

    def test_read_over_limit_rejected(self) -> None:
        with mock.patch("agent_core.host.session.MAX_SCAN_BYTES", 8):
            body = self.expect_status(
                "GET", "/api/remote/file?path=src/app.py", code=400
            )
        self.assertIn("上限", body.get("error", ""))

    def test_read_missing_file_is_404(self) -> None:
        self.expect_status("GET", "/api/remote/file?path=src/nope.py", code=404)

    def test_save_optimistic_lock_refuses_stale(self) -> None:
        """`expectedDigest` 不符 → 409，且**远端文件一个字都没动**。"""
        before = dict(self.transport.files)
        body = self.expect_status(
            "POST", "/api/remote/save",
            {"path": "src/app.py", "content": "hijack\n", "expectedDigest": "deadbeef"},
            code=409,
        )
        self.assertIn("重新加载", body.get("error", ""))
        self.assertEqual(self.transport.files, before)

    def test_save_writes_backups_and_registers_change(self) -> None:
        digest = self.get_json("/api/remote/file?path=src/app.py")["digest"]
        data = self._save("src/app.py", "value = 10\nsecond = 2\n", digest)
        self.assertTrue(data["changed"])
        self.assertEqual(data["action"], "save")
        self.assertIn("-value = 1", data["diff"])
        self.assertIn("+value = 10", data["diff"])
        self.assertEqual(self.transport.files[APP_PY], b"value = 10\nsecond = 2\n")
        backup = (
            Path(self.config.checkpoint_dir) / "remote" / "devbox" / "m11ui" / "src" / "app.py"
        )
        self.assertTrue(backup.is_file(), "改动前必须在本机留底")
        self.assertEqual(backup.read_bytes(), APP_TEXT.encode())
        changes = self.get_json("/api/remote/changes")
        self.assertEqual(len(changes["changes"]), 1)
        entry = changes["changes"][0]
        self.assertEqual(entry["rel_path"], "src/app.py")
        self.assertTrue(entry["backed_up"])
        self.assertEqual(entry["backup_path"], str(backup))

    def test_save_identical_content_does_not_write(self) -> None:
        writes = len(self.transport.writes)
        data = self._save("src/app.py", APP_TEXT)
        self.assertFalse(data.get("changed", False))
        self.assertEqual(len(self.transport.writes), writes)

    def test_save_creates_new_file(self) -> None:
        data = self._save("docs/new.md", "# hi\n")
        self.assertEqual(data["action"], "create")
        self.assertEqual(self.transport.files["/srv/app/docs/new.md"], b"# hi\n")
        entry = self.get_json("/api/remote/changes")["changes"][0]
        self.assertFalse(entry["backed_up"])  # 纯新建没有「改动前内容」可留底
        self.assertEqual(entry["backup_path"], "")

    def test_save_aborts_when_backup_unwritable(self) -> None:
        """留底存不下就**不许写** —— 不可回滚的改动宁可不发生（F4 反向验证②的靶子）。"""
        before = dict(self.transport.files)
        blocker = Path(self.config.checkpoint_dir) / "remote"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("占位文件，让 mkdir 失败\n", encoding="utf-8")
        body = self.expect_status(
            "POST", "/api/remote/save", {"path": "src/app.py", "content": "x\n"}, code=400
        )
        self.assertIn("中止", body.get("error", ""))
        self.assertEqual(self.transport.files, before)
        self.assertEqual(self.transport.writes, [])

    def test_save_on_readonly_connection_refused(self) -> None:
        self.post_json(
            "/api/remote/connections/devbox/permissions", {"allowWrite": False}
        )
        body = self.expect_status(
            "POST", "/api/remote/save", {"path": "src/app.py", "content": "x\n"}, code=400
        )
        self.assertIn("allow_write=off", body.get("error", ""))

    def test_save_escape_rejected(self) -> None:
        before = dict(self.transport.files)
        self.expect_status(
            "POST", "/api/remote/save",
            {"path": "../../evil.py", "content": "x\n"}, code=403,
        )
        self.assertEqual(self.transport.files, before)

    def test_patch_applies_structured_edit(self) -> None:
        data = self.post_json(
            "/api/remote/patch",
            {"path": "src/app.py", "search": "second = 2", "replace": "second = 3"},
        )
        self.assertTrue(data["changed"])
        self.assertEqual(data["strategy"], "exact")
        self.assertIn("+second = 3", data["diff"])
        self.assertEqual(self.transport.files[APP_PY], b"value = 1\nsecond = 3\n")
        entry = self.get_json("/api/remote/changes")["changes"][0]
        self.assertEqual(entry["action"], "edit")

    def test_patch_locator_failure_does_not_write(self) -> None:
        writes = list(self.transport.writes)
        self.expect_status(
            "POST", "/api/remote/patch",
            {"path": "src/app.py", "search": "不存在的片段", "replace": "x"}, code=400,
        )
        self.assertEqual(self.transport.writes, writes)

    def test_patch_no_change_does_not_write(self) -> None:
        """`changed=false` 必须不写回（无改动却写回会把整份文件的换行统一掉）。"""
        patch = mock.patch(
            "agent_core.remote.workspace.plan_patch", lambda *a, **kw: _UnchangedPlan()
        )
        with patch:
            data = self.post_json(
                "/api/remote/patch",
                {"path": "src/app.py", "search": "value = 1", "replace": "value = 1"},
            )
        self.assertFalse(data["changed"])
        self.assertEqual(self.transport.writes, [])

    def test_save_audit_carries_no_body(self) -> None:
        """审计只记元数据：远端文件正文一个字符都不出机（ADR-006、F4 反向验证④）。"""
        marker = "SECRET_MARKER_9f3a"
        self._save("src/app.py", f"value = 1\nsecond = 2  # {marker}\n")
        lines = [line for line in self.audit_lines() if line["event"] == "remote.file"]
        self.assertTrue(any(line["action"] == "save" for line in lines))
        blob = json.dumps(lines, ensure_ascii=False)
        self.assertNotIn(marker, blob)
        entry = next(line for line in lines if line["action"] == "save")
        self.assertEqual(set(entry) & {"connection", "path", "bytes", "sha256",
                                       "added", "removed", "backed_up"}, 
                         {"connection", "path", "bytes", "sha256", "added", "removed",
                          "backed_up"})


# --------------------------------------------------------------------------- #
# 会话门面（不经 HTTP）：未开连接时的报错必须可照着做
# --------------------------------------------------------------------------- #
class RemoteFacadeTest(_HostCase):
    files: dict[str, bytes] = {APP_PY: APP_TEXT.encode()}

    def test_require_workspace_gives_actionable_error(self) -> None:
        """没开连接就点面板 = 必须给一条**能照着做**的报错，不是一个空列表。"""
        from agent_core.errors import AgentError
        from agent_core.host.session import Session

        self._start_host(remote=True)
        session = Session(self.config, client=FakeClient([]))
        with self.assertRaises(AgentError) as ctx:
            session.remote_tree("")
        self.assertIn("「远程」面板", str(ctx.exception))

    def test_backup_helper_raises_when_unwritable(self) -> None:
        """`_backup_before_write` 落盘失败抛 `ToolError` —— 写入方据此中止（不写远端）。"""
        from agent_core.host.session import Session
        from agent_core.tools.remote import _backup_before_write

        self._start_host(remote=True)
        session = Session(self.config, client=FakeClient([]))
        blocker = Path(self.config.checkpoint_dir) / "remote"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("占位\n", encoding="utf-8")
        with self.assertRaises(ToolError):
            _backup_before_write(
                session.agent.ctx, make_conn(), "src/app.py", b"old\n"
            )


# --------------------------------------------------------------------------- #
# 远程工作区根（M12）：用户把远端目录「加入工作区」的那条端点
# --------------------------------------------------------------------------- #
class RemoteRootApiTest(_HostCase):
    """M12：`/api/remote/roots` 三个端点。

    这里刻意把重心放在**「不静默成功」**上：未知连接 / 写法非法 / 重复登记 /
    移出一个不存在的根，每一条都必须 400 并把原因说清楚。静默成功会让用户
    以为工作区里已经挂好了那个目录，而模型随后报「没有这个根」—— 排查方向
    会被误导到模型侧，实际是登记根本没落地。
    """

    files: dict[str, bytes] = {APP_PY: APP_TEXT.encode()}

    def setUp(self) -> None:
        super().setUp()
        self._start_host(remote=True, conns=[make_conn()])

    def _roots(self) -> dict:
        return self.get_json("/api/remote/roots")

    # ---- 只读面 -----------------------------------------------------------
    def test_roots_start_empty(self) -> None:
        data = self._roots()
        self.assertIs(data["enabled"], True)
        self.assertEqual(data["roots"], [])
        self.assertEqual(data["problems"], [])

    def test_roots_are_carried_in_state(self) -> None:
        """界面靠 `remote.state` 的 roots 决定工作区树要不要重画 —— 必须带上。"""
        self.post_json("/api/remote/roots", {"connection": "devbox", "sub": ""})
        state = self.get_json("/api/remote/state")
        self.assertEqual(state["roots"], ["@remote/devbox"])

    # ---- 新增 -------------------------------------------------------------
    def test_add_root_round_trips_atomically(self) -> None:
        data = self.post_json(
            "/api/remote/roots",
            {"connection": "devbox", "sub": "", "label": "整个项目"},
        )
        self.assertTrue(data["ok"])
        root = data["roots"][0]
        self.assertEqual(root["connection"], "devbox")
        self.assertEqual(root["sub"], "")
        self.assertEqual(root["nsPath"], "@remote/devbox")
        self.assertEqual(root["label"], "整个项目")
        self.assertTrue(root["ok"])
        self.assertEqual(root["reason"], "")

        path = store_path_for(self.config)
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 不升 FORMAT_VERSION：老客户端把 roots 当未知键忽略，仍能读连接清单。
        self.assertEqual(raw["version"], FORMAT_VERSION)
        self.assertEqual([r["connection"] for r in raw["roots"]], ["devbox"])
        leftovers = [p.name for p in path.parent.glob("*.tmp*")]
        self.assertEqual(leftovers, [], "原子写留下了临时文件残渣")
        self.assertEqual(
            [root.key for root in RemoteStore.load(path).roots], ["devbox:"]
        )

    def test_add_root_with_sub_is_normalized(self) -> None:
        """带尾斜杠 / 反斜杠的写法归一化后再落盘，避免同一目录登记两份。"""
        data = self.post_json(
            "/api/remote/roots", {"connection": "devbox", "sub": "项目\\smart-writer\\"}
        )
        root = data["roots"][0]
        self.assertEqual(root["sub"], "项目/smart-writer")
        self.assertEqual(root["nsPath"], "@remote/devbox/项目/smart-writer")
        # 归一化后重复登记被认出是同一条
        self.expect_status(
            "POST",
            "/api/remote/roots",
            {"connection": "devbox", "sub": "项目/smart-writer"},
            code=400,
        )

    def test_add_root_default_label(self) -> None:
        data = self.post_json("/api/remote/roots", {"connection": "devbox", "sub": "src"})
        self.assertEqual(data["roots"][0]["label"], "devbox:src")

    def test_add_root_unknown_connection_lists_available(self) -> None:
        body = self.expect_status(
            "POST", "/api/remote/roots", {"connection": "ghost"}, code=400
        )
        self.assertIn("devbox", body.get("error", ""))
        self.assertEqual(self._roots()["roots"], [])

    def test_add_root_duplicate_rejected(self) -> None:
        self.post_json("/api/remote/roots", {"connection": "devbox", "sub": "src"})
        body = self.expect_status(
            "POST", "/api/remote/roots", {"connection": "devbox", "sub": "src"}, code=400
        )
        self.assertIn("已在工作区", body.get("error", ""))
        self.assertEqual(len(self._roots()["roots"]), 1)

    def test_add_root_absolute_sub_rejected(self) -> None:
        """sub 必须相对连接 root —— 传绝对路径会让「哪个 root」失去意义。"""
        body = self.expect_status(
            "POST", "/api/remote/roots", {"connection": "devbox", "sub": "/etc"}, code=400
        )
        self.assertIn("相对路径", body.get("error", ""))
        self.assertEqual(self._roots()["roots"], [])

    def test_add_root_parent_segment_rejected(self) -> None:
        self.expect_status(
            "POST", "/api/remote/roots", {"connection": "devbox", "sub": "../x"}, code=400
        )
        self.assertEqual(self._roots()["roots"], [])

    def test_add_root_missing_connection_is_400(self) -> None:
        body = self.expect_status("POST", "/api/remote/roots", {}, code=400)
        self.assertIn("connection", body.get("error", ""))

    # ---- 移出 -------------------------------------------------------------
    def test_remove_root_then_list_empty(self) -> None:
        self.post_json("/api/remote/roots", {"connection": "devbox", "sub": "src"})
        data = self.post_json(
            "/api/remote/roots/delete", {"connection": "devbox", "sub": "src"}
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["roots"], [])
        self.assertEqual(self._roots()["roots"], [])

    def test_remove_unknown_root_rejected(self) -> None:
        """「以为移出了、其实还在」会让模型继续写一个用户以为撤掉的位置。"""
        body = self.expect_status(
            "POST", "/api/remote/roots/delete", {"connection": "devbox", "sub": "src"}, code=400
        )
        self.assertIn("不在工作区", body.get("error", ""))

    def test_remove_root_missing_connection_is_400(self) -> None:
        self.expect_status("POST", "/api/remote/roots/delete", {}, code=400)

    # ---- 审计（复用 remote.connection，不新增事件） ------------------------
    def test_root_changes_are_audited_without_paths(self) -> None:
        self.post_json("/api/remote/roots", {"connection": "devbox", "sub": "src"})
        self.post_json(
            "/api/remote/roots/delete", {"connection": "devbox", "sub": "src"}
        )
        events = [line for line in self.audit_lines() if line["event"] == "remote.connection"]
        actions = [line["action"] for line in events]
        self.assertIn("root_add", actions)
        self.assertIn("root_remove", actions)
        # 只记元数据：远端绝对路径 / 子目录正文不出机
        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("/srv/app", blob)
        self.assertNotIn("10.0.0.12", blob)

    # ---- 坏根如实上报（不抛错、不静默吞掉） -------------------------------
    def test_bad_root_is_dropped_into_problems(self) -> None:
        """清单里的坏根（连接不存在）在载入时被跳过，但**必须留下原因**。

        静默丢弃会让用户觉得「我明明加过、怎么没了」—— 而实际原因写在
        problems 里，只是没被展示出来而已。
        """
        path = self.ws / ".vibe" / "remote.json"
        path.write_text(
            json.dumps(
                {
                    "version": FORMAT_VERSION,
                    "default": "",
                    "connections": [],
                    "roots": [{"connection": "ghost", "sub": ""}],
                }
            ),
            encoding="utf-8",
        )
        self._start_host(remote=True)
        data = self._roots()
        self.assertEqual(data["roots"], [])
        self.assertEqual(len(data["problems"]), 1)
        self.assertIn("ghost", data["problems"][0])


# --------------------------------------------------------------------------- #
# 工作区树指向非活动连接：如实报「该连接未打开」，不偷偷换连接
# --------------------------------------------------------------------------- #
class RemoteTreeForConnectionTest(_HostCase):
    files: dict[str, bytes] = {APP_PY: APP_TEXT.encode()}

    def setUp(self) -> None:
        super().setUp()
        self._start_host(remote=True, conns=[make_conn()])
        self.post_json("/api/remote/open", {"connection": "devbox"})

    def test_tree_for_active_connection_lists(self) -> None:
        data = self.get_json("/api/remote/tree?connection=devbox&path=src")
        self.assertTrue(data["ok"])
        self.assertIn("app.py", [entry["name"] for entry in data["entries"]])

    def test_tree_for_other_connection_reports_not_open(self) -> None:
        """工作区里的远端根可能指着**非活动**连接 —— 不能替用户静默换连接。

        静默换连接的后果是把用户的浏览位置和改动台账串到另一台机器上，
        而界面上看起来「一切正常」。
        """
        data = self.get_json("/api/remote/tree?connection=other&path=")
        self.assertIs(data["ok"], False)
        self.assertEqual(data["reason"], "该连接未打开")
        self.assertEqual(data["connection"], "other")
        self.assertEqual(data["entries"], [])
        # 活动连接没有被偷偷换掉
        self.assertEqual(self.get_json("/api/remote/state")["connection"], "devbox")

    def test_tree_without_connection_uses_active(self) -> None:
        """不传 connection 时行为与 M11 一致：作用于活动连接（界面自用路径）。"""
        data = self.get_json("/api/remote/tree?path=src")
        self.assertTrue(data["ok"])
        self.assertIn("app.py", [entry["name"] for entry in data["entries"]])


class RemoteRefResolutionTest(_HostCase):
    """工作区树右键远端文件/目录「加入对话」的后端承接面（M13）。

    引用值就是模型工具看到的 `@remote/<连接>/<路径>`，因此必须与 `read_file`
    走**同一条路由**：根未加入工作区就如实报错，不偷偷回退到本机同名路径。
    """

    files: dict[str, bytes] = {APP_PY: APP_TEXT.encode()}

    def setUp(self) -> None:
        super().setUp()
        self._start_host(remote=True, conns=[make_conn()])
        self.post_json("/api/remote/roots", {"connection": "devbox", "sub": ""})
        self.resolver = self.host.session._remote_ref_resolver()
        self.assertIsNotNone(self.resolver)

    def test_file_ref_reads_remote_body(self) -> None:
        label, body = self.resolver("file", "@remote/devbox/src/app.py")
        self.assertEqual(label, "@remote/devbox/src/app.py")
        self.assertIn("second = 2", body)
        self.assertIn("---8<--- 文件内容开始", body)

    def test_folder_ref_lists_one_level(self) -> None:
        label, body = self.resolver("folder", "@remote/devbox/src")
        self.assertEqual(label, "@remote/devbox/src")
        self.assertIn("app.py", body)

    def test_unknown_connection_and_escape_are_rejected(self) -> None:
        # 不猜连接：「other」不在清单里 → 明确报错，不回退任何一条连接
        with self.assertRaises(ToolError):
            self.resolver("file", "@remote/other/src/app.py")
        # 越出连接 root 的路径交给远端 jail 拒绝
        with self.assertRaises(SandboxViolation):
            self.resolver("file", "@remote/devbox/../etc/passwd")


# --------------------------------------------------------------------------- #
# P4：远端逐条回退端点（`POST /api/remote/restore`）
# --------------------------------------------------------------------------- #
CP_TURN = "cp-20261021-abcdef"
OLD_TEXT = b"value = 0\nsecond = 2\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RemoteRollbackApiTest(_HostCase):
    """轮次 manifest 驱动远端恢复：权限位、穿越、乐观锁、新建文件删除。

    这些用例全部走真实 HTTP，落点与 `_backup_before_write` 一致
    （`<checkpoints>/remote/<连接>/<会话>/<轮次>/`），只认存在 manifest 的轮次。
    """

    files: dict[str, bytes] = {APP_PY: APP_TEXT.encode()}

    def _start(self, *, allow_write: bool = True):
        self._start_host(remote=True, conns=[make_conn(allow_write=allow_write)])
        self.turn_dir = (
            self.config.checkpoint_dir
            / "remote"
            / "devbox"
            / "m11ui"
            / CP_TURN
        )
        return self

    def _make_turn(self, entries: list[tuple[str, bytes | None, bytes]]) -> None:
        """按 `_backup_before_write` 的落点手写一份轮次目录。"""
        self.turn_dir.mkdir(parents=True, exist_ok=True)
        files: list[dict] = []
        for rel, before, after in entries:
            if before is not None:
                target = self.turn_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(before)
            files.append(
                {
                    "path": rel,
                    "existed": before is not None,
                    "sha256Before": _sha(before) if before is not None else "",
                    "sha256After": _sha(after),
                    "bytes": len(after),
                }
            )
        (self.turn_dir / "manifest.json").write_text(
            json.dumps(
                {"turn": CP_TURN, "connection": "devbox", "files": files},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_restore_requires_write_permission(self) -> None:
        self._start(allow_write=False)
        self._make_turn([("src/app.py", OLD_TEXT, APP_TEXT.encode())])
        data = self.expect_status(
            "POST", "/api/remote/restore", {"checkpointId": CP_TURN}, code=400
        )
        self.assertIn("allow_write=off", data["error"])
        self.assertEqual(self.transport.files[APP_PY], APP_TEXT.encode())

    def test_restore_rejects_unknown_and_traversal_ids(self) -> None:
        self._start()
        self._make_turn([("src/app.py", OLD_TEXT, APP_TEXT.encode())])
        self.expect_status(
            "POST", "/api/remote/restore", {"checkpointId": "cp-nope"}, code=400
        )
        # 检查点 id 会被当成目录名 —— 穿越必须在校验阶段就被拒
        self.expect_status(
            "POST", "/api/remote/restore", {"checkpointId": "../../etc"}, code=400
        )
        self.expect_status("POST", "/api/remote/restore", {}, code=400)
        self.expect_status("GET", "/api/remote/restore", code=405)

    def test_restore_writes_original_and_audits_metadata_only(self) -> None:
        self._start()
        self._make_turn([("src/app.py", OLD_TEXT, APP_TEXT.encode())])
        data = self.post_json("/api/remote/restore", {"checkpointId": CP_TURN})
        self.assertTrue(data["ok"])
        self.assertEqual(data["restored"], ["src/app.py"])
        self.assertEqual(data["deleted"], [])
        self.assertEqual(self.transport.files[APP_PY], OLD_TEXT)
        self.assertTrue(data["redoId"].startswith("rollback-"))
        # 回退前内容留底，可再做一次恢复
        redo = self.config.checkpoint_dir / "remote" / "devbox" / "m11ui" / data["redoId"]
        self.assertEqual((redo / "src" / "app.py").read_bytes(), APP_TEXT.encode())

        events = [line for line in self.audit_lines() if line["event"] == "remote.rollback"]
        self.assertEqual([e["action"] for e in events], ["restore"])
        self.assertEqual(events[0]["connection"], "devbox")
        self.assertEqual(events[0]["path"], "src/app.py")
        self.assertTrue(events[0]["backed_up"])
        self.assertNotIn("value", json.dumps(events[0], ensure_ascii=False))

    def test_stale_file_rejects_whole_restore(self) -> None:
        self._start()
        self._make_turn([("src/app.py", OLD_TEXT, APP_TEXT.encode())])
        self.transport.files[APP_PY] = b"value = 9\nsecond = 2\n"
        writes = list(self.transport.writes)
        data = self.expect_status(
            "POST", "/api/remote/restore", {"checkpointId": CP_TURN}, code=400
        )
        self.assertIn("整体取消", data["error"])
        self.assertEqual(self.transport.files[APP_PY], b"value = 9\nsecond = 2\n")
        self.assertEqual(self.transport.writes, writes)
        self.assertFalse(
            any(line["event"] == "remote.rollback" for line in self.audit_lines())
        )

    def test_restore_deletes_file_created_in_turn(self) -> None:
        new_path = "/srv/app/docs/new.md"
        self._start()
        self.transport.files[new_path] = b"# hi\n"
        self._make_turn([("docs/new.md", None, b"# hi\n")])
        data = self.post_json("/api/remote/restore", {"checkpointId": CP_TURN})
        self.assertEqual(data["deleted"], ["docs/new.md"])
        self.assertNotIn(new_path, self.transport.files)
        self.assertEqual(data["redoId"], "")

    def test_externally_changed_new_file_is_skipped(self) -> None:
        new_path = "/srv/app/docs/new.md"
        self._start()
        self.transport.files[new_path] = b"# changed\n"
        self._make_turn([("docs/new.md", None, b"# hi\n")])
        data = self.post_json("/api/remote/restore", {"checkpointId": CP_TURN})
        self.assertEqual(data["deleted"], [])
        self.assertEqual([item["path"] for item in data["skipped"]], ["docs/new.md"])
        self.assertIn(new_path, self.transport.files)


if __name__ == "__main__":
    unittest.main()