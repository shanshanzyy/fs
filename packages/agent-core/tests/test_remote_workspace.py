"""远程工作区（M13 / ADR-042）测试。

验收要求对应 `docs/M13-PLAN.md` §8：

1. **路由默认根的四条判定**：裸路径 → 走远端；显式 `@remote/` 前缀 → 仍走 ns 分支
   （默认根不遮蔽显式写法）；远端绝对路径 → 落在工作区根之内才收；`../` 越界 → 报人话错。
2. **本机模式逐字节不变**：未设 `remote_workspace` 时 `match()` 与 M12 完全同构。
3. **锚点隔离**：连接名带任意分隔符也出不去锚点根 —— 这是唯一一处跨界检查。
4. **切换的原子性（先连后切）**：连不上、连接不在清单、两者都给 → 一律 ValueError，
   且**旧会话原封不动**。
5. **清单钉住**：切过去之后 `remote_config` 必须仍指向旧工作区那份 `remote.json`。
"""

from __future__ import annotations

import json
import posixpath
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.errors import ToolError  # noqa: E402
from agent_core.host.server import HostServer, ServerContext  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.remote.anchor import (  # noqa: E402
    anchor_dir,
    ensure_anchor,
    list_anchors,
    safe_name,
)
from agent_core.remote.connection import RemoteConnection  # noqa: E402
from agent_core.remote.degrade import remote_degraded  # noqa: E402
from agent_core.remote.errors import RemoteConfigError, RemoteError  # noqa: E402
from agent_core.remote.roots import default_root_rel, parse_ns_root  # noqa: E402
from agent_core.remote.routing import RemoteRouter  # noqa: E402
from agent_core.remote.store import RemoteStore, store_path_for  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402
from tests.test_remote import FakeTransport, make_conn  # noqa: E402


class _HostCase(unittest.TestCase):
    """起真实 HTTP host（`port=0`）+ 注入**假传输**（与 M11 的 UI 测试同一套脚手架）。

    连接清单写进 host 正在用的同一份 `remote.json`，传输层换成内存实现 ——
    于是整套切换流程能单机跑通，不必有一台真远端机器。
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

    def _start_host(self, *, remote: bool = True, conns=None, **overrides):
        kwargs = {"session_id": "m13ui", **overrides}
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

    # ------------------------------------------------------------------ HTTP
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

    def expect_status(self, method: str, path: str, body=None, code: int = 400) -> dict:
        try:
            with self._request(method, path, body) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, code, f"{method} {path} 状态码应为 {code}")
            return json.loads(exc.read().decode("utf-8") or "{}")


def make_conn(**overrides) -> RemoteConnection:
    fields = {
        "name": "devbox",
        "host": "10.0.0.12",
        "user": "dev",
        "root": "/srv/app",
    }
    fields.update(overrides)
    return RemoteConnection(**fields)  # type: ignore[arg-type]


class _FakeClient:
    """最小 LLM 客户端替身：会话构造只需要它存在，本文件不跑任何模型调用。"""

    model = "fake-model"

    def support_tools(self) -> bool:
        return False

    def chat(self, messages, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("本测试不跑模型")


class _FakeGuard:
    def __init__(self, root: str) -> None:
        self.root_resolved = root

    def check_list(self, rel: str | None = None) -> str:
        return posixpath.join(self.root_resolved, rel or "")

    def relative(self, abs_path: str) -> str:
        return str(abs_path)[len(self.root_resolved) :].lstrip("/")


class _FakeWorkspace:
    """`RemoteWorkspace` 替身：只提供「连上了」所需要的最小行为。"""

    def __init__(self, root: str = "/srv/app") -> None:
        self.guard = _FakeGuard(root)
        self.conn: object = None
        self.listed: list[str] = []

    def list_dir(self, rel: str | None = None) -> list[dict[str, object]]:
        self.listed.append(rel or "")
        return []

    def capabilities(self) -> dict[str, object]:
        return {"degraded": []}


class RootsParsingTests(unittest.TestCase):
    """纯文本层：`roots.parse_ns_root` / `roots.default_root_rel`。"""

    def test_parse_splits_connection_and_sub(self) -> None:
        self.assertEqual(parse_ns_root("@remote/devbox/a/b"), ("devbox", "a/b"))
        self.assertEqual(parse_ns_root("@remote/devbox"), ("devbox", ""))
        self.assertEqual(parse_ns_root(""), ("", ""))

    def test_parse_rejects_foreign_form(self) -> None:
        for raw in ("devbox/app", "/srv/app", "ftp://x/y"):
            with self.subTest(raw=raw), self.assertRaises(RemoteConfigError):
                parse_ns_root(raw)

    def test_parse_rejects_dotdot(self) -> None:
        with self.assertRaises(RemoteConfigError):
            parse_ns_root("@remote/devbox/../../etc")

    def test_default_root_rel_joins_relative(self) -> None:
        self.assertEqual(default_root_rel("app", "src/x.py"), "app/src/x.py")
        # 默认根就是连接 root 本身时不额外加一层
        self.assertEqual(default_root_rel("", "src/x.py"), "src/x.py")

    def test_default_root_rel_accepts_absolute_inside_root(self) -> None:
        got = default_root_rel("app", "/srv/app/app/src/x.py", root="/srv/app")
        self.assertEqual(got, "app/src/x.py")

    def test_default_root_rel_rejects_absolute_outside_root(self) -> None:
        with self.assertRaises(RemoteConfigError) as caught:
            default_root_rel("app", "/srv/other/src/x.py", root="/srv/app")
        self.assertIn("不在远程工作区之内", str(caught.exception))

    def test_default_root_rel_rejects_composed_escape(self) -> None:
        """**拼接后再逃逸**才是这里真正的坑：入参本身干干净净。"""
        with self.assertRaises(RemoteConfigError) as caught:
            default_root_rel("app", "../../etc/passwd")
        self.assertIn("逃出", str(caught.exception))

    def test_default_root_rel_rejects_non_string(self) -> None:
        for raw in (None, 3, True, ["src"]):
            with self.subTest(raw=raw), self.assertRaises(RemoteConfigError):
                default_root_rel("app", raw)  # type: ignore[arg-type]

    def test_default_root_rel_rejects_empty(self) -> None:
        with self.assertRaises(RemoteConfigError):
            default_root_rel("app", "")


class DefaultRootRoutingTests(unittest.TestCase):
    """`RemoteRouter` 的默认根分支。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()
        store = RemoteStore.load(self.ws / ".vibe" / "remote.json")
        # **刻意不加任何 root**：默认根靠 `remote_workspace` 生效，不依赖清单登记。
        store.upsert(make_conn(allow_write=True))
        self.config = Config.load(self.ws, remote_enabled=True)

    def _router(self, root: str = "") -> RemoteRouter:
        cfg = (
            self.config
            if not root
            else Config.load(self.ws, remote_enabled=True, remote_workspace=root)
        )
        return RemoteRouter(cfg)

    def test_local_mode_plain_path_still_returns_none(self) -> None:
        """本机模式：与 M12 完全同构 —— 这是「逐字节不变」的可断言锚点。"""
        router = self._router()
        self.assertEqual(router.default_ns, "")
        for raw in ("src/app.py", "./a.txt", "", "."):
            with self.subTest(path=raw):
                self.assertIsNone(router.match(raw))

    def test_default_root_routes_plain_path(self) -> None:
        router = self._router("@remote/devbox/app")
        hit = router.match("src/app.py")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.connection, "devbox")
        self.assertEqual(hit.sub, "app")
        self.assertEqual(hit.remote_rel(), "app/src/app.py")
        self.assertEqual(hit.ns_path, "@remote/devbox/app/src/app.py")

    def test_explicit_prefix_outside_default_root_still_needs_registration(self) -> None:
        """默认根**不越权**：默认根只决定「没写前缀时去哪」，不解除 M12 的登记纪律。

        `@remote/devbox/src/...` 落在默认根 `app` 之外 → 仍要求该目录已加入工作区。
        反过来，落在默认根之内就**不要求**登记（决策 14：在内存里合成）。
        """
        router = self._router("@remote/devbox/app")
        with self.assertRaises(ToolError) as caught:
            router.match("@remote/devbox/src/app.py")
        self.assertIn("加入工作区", str(caught.exception))
        hit = router.match("@remote/devbox/app/src/app.py")
        assert hit is not None
        self.assertEqual(hit.remote_rel(), "app/src/app.py")

    def test_explicit_prefix_inside_default_root_needs_no_registration(self) -> None:
        """ADR-042 决策 14：默认根在内存里合成，不必已写进用户的 roots 清单。"""
        router = self._router("@remote/devbox/app")
        hit = router.match("@remote/devbox/app/src/app.py")
        assert hit is not None
        self.assertEqual(hit.remote_rel(), "app/src/app.py")

    def test_other_connections_still_need_registration(self) -> None:
        """默认根之外，M12 那套「先加入工作区」的纪律一格不减。"""
        router = self._router("@remote/devbox/app")
        with self.assertRaises(ToolError) as caught:
            router.match("@remote/other/src/app.py")
        self.assertIn("other", str(caught.exception))

    def test_absolute_remote_path_is_accepted(self) -> None:
        router = self._router("@remote/devbox/app")
        hit = router.match("/srv/app/app/src/app.py")
        assert hit is not None
        self.assertEqual(hit.remote_rel(), "app/src/app.py")

    def test_dotdot_escape_is_refused(self) -> None:
        router = self._router("@remote/devbox/app")
        with self.assertRaises(ToolError) as caught:
            router.match("../../../etc/passwd")
        self.assertIn("逃出", str(caught.exception))

    def test_missing_connection_is_refused(self) -> None:
        router = self._router("@remote/ghost/app")
        with self.assertRaises(ToolError) as caught:
            router.match("src/app.py")
        self.assertIn("ghost", str(caught.exception))

    def test_describe_lists_default_root_first(self) -> None:
        router = self._router("@remote/devbox/app")
        self.assertTrue(router.describe().startswith("@remote/devbox/app"))


class AnchorTests(unittest.TestCase):
    """本机锚点：目录名收敛 + 幂等 + 列表容错。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve() / "home"

    def test_safe_name_strips_separators_and_dots(self) -> None:
        # `../../etc` → 分隔符与句点全换成 `_`，前导下划线再被剥掉。结果恒为
        # 单层、恒不含句点 —— 于是 `..` 这个形态在结构上不可能出现。
        for raw, expected in (("../../etc", "etc"), ("a/b", "a_b"), ("", "_default")):
            with self.subTest(raw=raw):
                self.assertEqual(safe_name(raw), expected)

    def test_anchor_stays_under_home(self) -> None:
        path = anchor_dir("../../etc", "pwd", home=self.home)
        self.assertTrue(str(path.resolve()).startswith(str(self.home.resolve())))

    def test_ensure_anchor_is_idempotent(self) -> None:
        first = ensure_anchor("devbox", "app", home=self.home)
        marker = (first / ".vibe-workspace-anchor.json").read_text(encoding="utf-8")
        ensure_anchor("devbox", "app", home=self.home)
        self.assertEqual(
            (first / ".vibe-workspace-anchor.json").read_text(encoding="utf-8"), marker
        )
        self.assertTrue((first / ".vibe").is_dir())

    def test_list_anchors_skips_unmarked(self) -> None:
        ensure_anchor("devbox", "app", home=self.home)
        (self.home / "devbox" / "deadbeef0000").mkdir(parents=True)  # 没有标记的半截目录
        anchors = list_anchors(home=self.home)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["connection"], "devbox")
        self.assertEqual(anchors[0]["sub"], "app")

    def test_list_anchors_on_missing_home(self) -> None:
        self.assertEqual(list_anchors(home=self.home / "nope"), [])


class DegradeTests(unittest.TestCase):
    """降级清单：本机模式恒空，远程模式每条都得有人话原因。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name).resolve()

    def test_local_mode_is_empty(self) -> None:
        for overrides in ({}, {"remote_enabled": True}):
            with self.subTest(**overrides):
                cfg = Config.load(self.ws, **overrides)
                self.assertEqual(remote_degraded(cfg), {})

    def test_remote_mode_lists_human_reasons(self) -> None:
        cfg = Config.load(
            self.ws, remote_enabled=True, remote_workspace="@remote/devbox/app"
        )
        reasons = remote_degraded(cfg)
        self.assertTrue(reasons, "远程工作区模式必须给出降级清单")
        for key in ("git", "lsp", "index_watch", "repo_map", "local_write"):
            with self.subTest(key=key):
                self.assertTrue(str(reasons.get(key, "")).strip())
        self.assertIn("devbox", reasons["git"])


class RemoteSwitchTests(unittest.TestCase):
    """`ServerContext.switch_workspace(remote=…)`：先连后切 + 清单钉住。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.ws = root / "ws"
        (self.ws / ".vibe").mkdir(parents=True)
        self.home = root / "home"
        self.store_path = self.ws / ".vibe" / "remote.json"
        store = RemoteStore.load(self.store_path)
        store.upsert(make_conn(allow_write=True))

        self.config = Config.load(self.ws, remote_enabled=True)
        self.session = Session(self.config, client=_FakeClient())
        self.ctx = ServerContext(self.session, _FakeGuard("/srv/app"))  # type: ignore[arg-type]

    def _patched(self, fail: bool = False):
        """把「连远端」与「锚点根」两处同時替身掉，其余全部走真实代码。"""
        target = "agent_core.remote.workspace.open_workspace"
        if fail:
            return [
                mock.patch(target, side_effect=RemoteError("Connection refused")),
                mock.patch(
                    "agent_core.remote.anchor.anchor_home", return_value=self.home
                ),
            ]
        return [
            mock.patch(target, return_value=_FakeWorkspace()),
            mock.patch("agent_core.remote.anchor.anchor_home", return_value=self.home),
        ]

    # ------------------------------------------------------------------ 拒绝路径
    def test_remote_payload_needs_remote_enabled(self) -> None:
        self.ctx.session.config = Config.load(self.ws)  # VIBE_REMOTE 未开
        with self.assertRaises(ValueError) as caught:
            self.ctx.switch_workspace("", remote={"connection": "devbox"})
        self.assertIn("VIBE_REMOTE", str(caught.exception))

    def test_path_and_remote_are_exclusive(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.ctx.switch_workspace(
                str(self.ws), remote={"connection": "devbox", "sub": "app"}
            )
        self.assertIn("互斥", str(caught.exception))

    def test_non_object_payload_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.ctx.switch_workspace("", remote="devbox")  # type: ignore[arg-type]

    def test_unknown_connection_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.ctx.switch_workspace("", remote={"connection": "ghost"})
        self.assertIn("ghost", str(caught.exception))

    def test_dotdot_sub_is_refused_before_connecting(self) -> None:
        """子目录写法**连都不用连**就能判非法 —— 纯文本层的事先做的事情。"""
        with self.assertRaises(ValueError):
            self.ctx.switch_workspace("", remote={"connection": "devbox", "sub": "../x"})

    def test_unreachable_connection_leaves_session_untouched(self) -> None:
        """「先连后切」的核心：连不上就**整体不生效**（ADR-042 决策 7）。"""
        before = self.ctx.session
        patches = self._patched(fail=True)
        with patches[0], patches[1]:
            with self.assertRaises(ValueError) as caught:
                self.ctx.switch_workspace("", remote={"connection": "devbox", "sub": "app"})
        self.assertIn("Connection refused", str(caught.exception))
        self.assertIs(self.ctx.session, before)

    # ------------------------------------------------------------------ 成功路径
    def test_switch_sets_remote_workspace_and_pins_store(self) -> None:
        patches = self._patched()
        with patches[0], patches[1]:
            result = self.ctx.switch_workspace(
                "", remote={"connection": "devbox", "sub": "app"}
            )
        self.assertTrue(result["switched"])
        self.assertEqual(result["remoteWorkspace"], "@remote/devbox/app")

        new_cfg = self.ctx.session.config
        self.assertEqual(new_cfg.remote_workspace, "@remote/devbox/app")
        # 清单必须**钉在原处**（决策 13）：否则用户配好的连接会「凭空消失」。
        self.assertEqual(str(new_cfg.remote_config), str(self.store_path))
        # 工作区换成了本机锚点（里面只有 .vibe 资产，不放代码）
        self.assertNotEqual(str(new_cfg.workspace), str(self.ws))
        self.assertTrue(str(new_cfg.workspace).startswith(str(self.home)))

    def test_describe_shape_local_vs_remote(self) -> None:
        """本机模式 `describe()` **不含** remote 键（决策 11：逐字节一致）。"""
        before = self.ctx.session.describe()
        self.assertNotIn("remoteWorkspace", before)
        patches = self._patched()
        with patches[0], patches[1]:
            self.ctx.switch_workspace("", remote={"connection": "devbox", "sub": "app"})
        after = self.ctx.session.describe()
        self.assertEqual(after["remoteWorkspace"], "@remote/devbox/app")
        self.assertEqual(after["remoteConnection"], "devbox")
        self.assertEqual(after["remoteSub"], "app")
        self.assertTrue(after["remoteDegraded"])

    def test_switch_back_clears_remote_workspace(self) -> None:
        """切回本机必须把 `remote_workspace` 清掉 —— 不清就会改错地方。"""
        other = Path(self._tmp.name).resolve() / "ws2"
        other.mkdir()
        patches = self._patched()
        with patches[0], patches[1]:
            self.ctx.switch_workspace("", remote={"connection": "devbox", "sub": "app"})
        self.ctx.switch_workspace(str(other))
        self.assertEqual(self.ctx.session.config.remote_workspace, "")
        self.assertEqual(str(self.ctx.session.config.workspace), str(other.resolve()))

    def test_switching_to_the_same_remote_root_is_noop(self) -> None:
        patches = self._patched()
        with patches[0], patches[1]:
            first = self.ctx.switch_workspace(
                "", remote={"connection": "devbox", "sub": "app"}
            )
            self.assertTrue(first["switched"])
            again = self.ctx.switch_workspace(
                "", remote={"connection": "devbox", "sub": "app"}
            )
        self.assertFalse(again["switched"])
        self.assertIn("未做切换", again["reason"])


class WorkspaceViewTests(unittest.TestCase):
    """`get /api/workspaces` 的远程候选来源：扫锚点目录反推，目录不存在时为空表。"""

    def test_missing_home_yields_empty_remote_list(self) -> None:
        self.assertEqual(list_anchors(home="/nonexistent/vibe/anchor"), [])


# --------------------------------------------------------------------------- #
# HTTP 层：协议形状与门控（复用 M11 的真 host + 假传输脚手架）
# --------------------------------------------------------------------------- #
class RemoteWorkspaceHttpTest(_HostCase):
    """`POST /api/workspaces/open` 的 `remote` 载荷。

    这一层要钉的是**协议形状**：
      - 未启用 VIBE_REMOTE → 400 且 hint 里点名环境变量（界面据此提示怎么开）；
      - `path` 与 `remote` 互斥 → 400；
      - 未知连接 → 400 且列出可用连接；
      - 成功 → `remoteWorkspace` / `remote` 两个字段齐全，`config` 不再涌现
        `remoteWorkspace` 之外的杂音。
    """

    files: dict[str, bytes] = {}

    def _switch(self, body, code: int = 400) -> dict:
        return self.expect_status("POST", "/api/workspaces/open", body, code=code)

    def test_remote_payload_needs_remote_enabled(self) -> None:
        self._start_host(remote=False)
        body = self._switch({"remote": {"connection": "devbox", "sub": "app"}})
        self.assertIn("VIBE_REMOTE", body.get("error", ""))

    def test_path_and_remote_are_exclusive(self) -> None:
        self._start_host(remote=True, conns=[make_conn()])
        body = self._switch({"path": str(self.ws), "remote": {"connection": "devbox"}})
        self.assertIn("互斥", body.get("error", ""))

    def test_unknown_connection_lists_alternatives(self) -> None:
        self._start_host(remote=True, conns=[make_conn()])
        body = self._switch({"remote": {"connection": "ghost"}})
        self.assertIn("ghost", body.get("error", ""))
        self.assertIn("devbox", body.get("error", ""))

    def test_switch_to_remote_root(self) -> None:
        self._start_host(remote=True, conns=[make_conn(allow_write=True)])
        ok = self._switch({"remote": {"connection": "devbox", "sub": "app"}}, code=200)
        self.assertTrue(ok["switched"])
        self.assertEqual(ok["remoteWorkspace"], "@remote/devbox/app")
        self.assertEqual(ok["remote"]["connection"], "devbox")
        self.assertEqual(ok["remote"]["sub"], "app")
        # 工作区换成**本机锚点**：里面只有 .vibe 资产，不放任何代码。
        anchor = Path(ok["workspace"])
        self.assertTrue(anchor.is_dir())
        self.assertTrue((anchor / ".vibe").is_dir())

    def test_state_reports_remote_workspace_after_switch(self) -> None:
        self._start_host(remote=True, conns=[make_conn(allow_write=True)])
        self._switch({"remote": {"connection": "devbox", "sub": "app"}}, code=200)
        state = self.get_json("/api/state")
        self.assertEqual(state["remoteWorkspace"], "@remote/devbox/app")
        self.assertEqual(state["remoteConnection"], "devbox")
        self.assertTrue(state["remoteDegraded"])
        # 降级群：本机 Git 面板必须**回原因**而不是空结果（决策 9）
        git = self.get_json("/api/git/status")
        self.assertIs(git.get("available"), False)
        self.assertTrue(str(git.get("reason", "")).strip())

    def test_local_mode_state_has_no_remote_keys(self) -> None:
        """本机模式 `describe()` 不含任何 remote* 键（决策 11：逐字节一致）。"""
        self._start_host(remote=True, conns=[make_conn()])
        state = self.get_json("/api/state")
        for key in ("remoteWorkspace", "remoteConnection", "remoteSub", "remoteDegraded"):
            with self.subTest(key=key):
                self.assertNotIn(key, state)

    def test_local_workspaces_view_lists_remote_candidates(self) -> None:
        self._start_host(remote=True, conns=[make_conn()])
        view = self.get_json("/api/workspaces")
        self.assertIs(view.get("remoteEnabled"), True)
        self.assertIn("remoteWorkspaces", view)

    def test_switch_back_to_local_clears_remote(self) -> None:
        """切回本机后 `remote*` 键必须整体消失 —— 留着就会把改动写到错的机器上。"""
        self._start_host(remote=True, conns=[make_conn(allow_write=True)])
        self._switch({"remote": {"connection": "devbox", "sub": "app"}}, code=200)
        other = Path(self._tmp.name).resolve() / "ws2"
        other.mkdir()
        self.expect_status(
            "POST", "/api/workspaces/open", {"path": str(other)}, code=200
        )
        state = self.get_json("/api/state")
        self.assertNotIn("remoteWorkspace", state)
        self.assertEqual(str(Path(state["workspace"]).resolve()), str(other.resolve()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
