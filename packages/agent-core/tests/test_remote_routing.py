"""远端透明路由（M12）测试。

验收要求（对应方案 §测试与反向验证）：

1. **前缀判定的四条纪律**：非命名空间写法 → 走本地；未知连接 / 未加根 / 裸前缀
   → 各自抛出**人话**错误，且**不回退本地**（把 `@remote/...` 当本地路径解析，
   最坏情况是写到了错的机器上）；
2. **长根优先**：嵌套登记时取最具体（sub 最长）的那个根；
3. **`routed_specs` 只换实现**：`name` / `description` / `parameters` / 顺序
   一字不改，改的只是 `handler` / `assessor` / `previewer` / `drafter`；
4. **四件套选臂一致**：`assess` / `preview` / 执行都按**同一个** `hit` 选臂
   （结构上排除「审批时按本地预演、执行时却打到远端」这种错配）；
5. **门控**：`remote=False` 与 `remote=True` 的工具清单、schema 逐字段一致
   —— 路由不改变模型看到的东西。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.errors import ToolError  # noqa: E402
from agent_core.remote.connection import RemoteConnection  # noqa: E402
from agent_core.remote.routing import ROUTED_TOOLS, RemoteRouter, routed_specs  # noqa: E402
from agent_core.remote.store import RemoteStore  # noqa: E402
from agent_core.sandbox import Risk  # noqa: E402
from agent_core.tools import build_default_registry  # noqa: E402
from agent_core.tools.base import ToolPreview, ToolResult, ToolSpec  # noqa: E402


def make_conn(**overrides) -> RemoteConnection:
    fields = {
        "name": "devbox",
        "host": "10.0.0.12",
        "user": "dev",
        "root": "/srv/app",
    }
    fields.update(overrides)
    return RemoteConnection(**fields)  # type: ignore[arg-type]


class _StubPaths:
    """远端臂只读这两项策略字段（见 tools/remote.py::_workspace_for）。"""

    allow_sensitive = False
    denied_patterns: tuple[str, ...] = ()


class _StubCtx:
    """轻量上下文：够路由壳与远端臂用，不必拉起整个 Agent。"""

    def __init__(self, config, router) -> None:
        self.config = config
        self.remote_router = router
        self.paths = _StubPaths()
        self.reads: list[str] = []
        self.changes: list[dict] = []

    def mark_read(self, rel: str) -> None:
        self.reads.append(rel)

    def record_change(self, **kwargs) -> None:
        self.changes.append(kwargs)


class RouterMatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()
        path = self.ws / ".vibe" / "remote.json"
        store = RemoteStore.load(path)
        store.upsert(make_conn(allow_write=True))
        # 一条**没有任何根**的连接：用来钉住「未加入工作区」这条分支。
        store.upsert(make_conn(name="bare"))
        store.add_root("devbox", "")            # root 本身
        store.add_root("devbox", "项目")          # 较浅
        store.add_root("devbox", "项目/smart-writer")  # 最深
        self.config = Config.load(self.ws)
        self.router = RemoteRouter(self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plain_local_path_returns_none(self) -> None:
        for raw in ("src/app.py", "./a.txt", None, ""):
            with self.subTest(path=raw):
                self.assertIsNone(self.router.match(raw))

    def test_unknown_connection_is_refused(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.router.match("@remote/nope/src/app.py")
        message = str(caught.exception)
        self.assertIn("nope", message)
        self.assertIn("devbox", message)  # 错误消息必须列出可用连接名

    def test_bare_prefix_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.router.match("@remote/")

    def test_unjoined_directory_is_refused(self) -> None:
        """连接存在但该目录没加入工作区 → 人话报错，且**不回退本地**。"""
        with self.assertRaises(ToolError) as caught:
            self.router.match("@remote/bare/src/app.py")
        self.assertIn("加入工作区", str(caught.exception))

    def test_hit_resolves_namespace_path(self) -> None:
        hit = self.router.match("@remote/devbox/项目/smart-writer/src/app.py")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.connection, "devbox")
        self.assertEqual(hit.sub, "项目/smart-writer")
        self.assertEqual(hit.remote_rel(), "项目/smart-writer/src/app.py")
        self.assertEqual(
            hit.ns_path, "@remote/devbox/项目/smart-writer/src/app.py"
        )

    def test_longest_root_wins(self) -> None:
        """嵌套登记时取最具体 —— 与「看起来在哪个根里」一致。"""
        hit = self.router.match("@remote/devbox/项目/other/x.py")
        assert hit is not None
        self.assertEqual(hit.sub, "项目")

    def test_root_itself_covers_everything(self) -> None:
        hit = self.router.match("@remote/devbox/whatever/deep/x.py")
        assert hit is not None
        self.assertEqual(hit.sub, "")
        self.assertEqual(hit.ns_path, "@remote/devbox/whatever/deep/x.py")

    def test_backslashes_are_normalized(self) -> None:
        """前缀之后的分隔符允许写反斜杠（Windows 上模型容易这么写）。"""
        hit = self.router.match(r"@remote/devbox\项目\a.py")
        assert hit is not None
        self.assertEqual(hit.remote_rel(), "项目/a.py")

    def test_describe_lists_roots(self) -> None:
        text = self.router.describe()
        self.assertIn("@remote/devbox", text)
        self.assertIn("@remote/devbox/项目/smart-writer", text)


# --------------------------------------------------------------------------- #
# 路由壳：只换实现，不改工具
# --------------------------------------------------------------------------- #
class _SentinelArm:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[str] = []

    def handler(self, ctx, args, hit) -> ToolResult:
        self.calls.append("handler")
        return ToolResult(ok=True, content=f"arm:{self.tag}", meta={"path": hit.ns_path})

    def assessor(self, ctx, args, hit):
        self.calls.append("assessor")
        return Risk.REVIEW, f"arm:{self.tag}"

    def previewer(self, ctx, args, hit) -> ToolPreview:
        self.calls.append("previewer")
        return ToolPreview(kind="text", body=self.tag, summary=f"arm:{self.tag}")

    drafter = None


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} 的说明\n第二行",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=lambda ctx, args: ToolResult(ok=True, content="local"),
        risk=Risk.SAFE,
    )


class RoutedSpecsShapeTest(unittest.TestCase):
    def test_only_named_tools_are_replaced(self) -> None:
        specs = [_spec("read_file"), _spec("grep"), _spec("repo_map")]
        arms = {"read_file": _SentinelArm("r"), "grep": _SentinelArm("g")}
        out = routed_specs(specs, arms=arms)

        self.assertEqual([s.name for s in out], [s.name for s in specs])  # 顺序不变
        for before, after in zip(specs, out):
            self.assertEqual(before.description, after.description)
            self.assertEqual(before.parameters, after.parameters)
            self.assertEqual(before.risk, after.risk)
        # repo_map 无臂 → 原样返回（同一个对象），六个可路由工具的成员换掉
        self.assertIs(out[2], specs[2])
        self.assertIsNot(out[0].handler, specs[0].handler)
        self.assertIsNot(out[1].handler, specs[1].handler)

    def test_routed_tools_tuple_matches_arms(self) -> None:
        from agent_core.tools.remote import ROUTE_ARMS

        self.assertEqual(tuple(ROUTE_ARMS), ROUTED_TOOLS)


class ShellSelectionTest(unittest.TestCase):
    """四件套必须按**同一个** hit 选臂。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()
        store = RemoteStore.load(self.ws / ".vibe" / "remote.json")
        store.upsert(make_conn(allow_write=True))
        store.add_root("devbox", "")
        self.config = Config.load(self.ws)
        self.router = RemoteRouter(self.config)
        self.ctx = _StubCtx(self.config, self.router)
        self.arm = _SentinelArm("remote")
        self.spec = routed_specs(
            [_spec("read_file")], arms={"read_file": self.arm}
        )[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_namespace_path_uses_remote_arm(self) -> None:
        args = {"path": "@remote/devbox/src/app.py"}
        risk, reason = self.spec.assess(self.ctx, args)
        self.assertEqual(risk, Risk.REVIEW)
        self.assertIn("arm:remote", reason)
        preview = self.spec.preview(self.ctx, args)
        self.assertEqual(preview.body, "remote")
        result = self.spec.handler(self.ctx, args)
        self.assertEqual(result.content, "arm:remote")
        self.assertEqual(
            result.meta["path"], "@remote/devbox/src/app.py"
        )
        self.assertEqual(self.arm.calls, ["assessor", "previewer", "handler"])

    def test_plain_path_uses_local_arm(self) -> None:
        args = {"path": "src/app.py"}
        result = self.spec.handler(self.ctx, args)
        self.assertEqual(result.content, "local")
        self.assertEqual(self.arm.calls, [])  # 远端臂一次都没被调

    def test_router_absent_and_ns_path_is_refused(self) -> None:
        """直接调 registry 的场景（无路由器）：本地路径照旧，前缀必须报错。"""
        ctx = _StubCtx(self.config, None)
        with self.assertRaises(ToolError):
            self.spec.handler(ctx, {"path": "@remote/devbox/a.py"})
        result = self.spec.handler(ctx, {"path": "src/app.py"})
        self.assertEqual(result.content, "local")

    def test_assess_refuses_unresolvable_ns_path(self) -> None:
        """解析不了 → DENY，而不是回退本地（见 match 的说明）。"""
        risk, reason = self.spec.assess(self.ctx, {"path": "@remote/nope/a.py"})
        self.assertEqual(risk, Risk.DENY)
        self.assertIn("nope", reason)


# --------------------------------------------------------------------------- #
# 门控：路由不改变模型看到的工具面
# --------------------------------------------------------------------------- #
class RegistryGateTest(unittest.TestCase):
    def test_tool_list_and_schemas_unchanged_by_routing(self) -> None:
        off = build_default_registry(lsp=False, remote=False)
        on = build_default_registry(lsp=False, remote=True)
        off_names = [n for n in off.names() if n in ROUTED_TOOLS]
        on_names = [n for n in on.names() if n in ROUTED_TOOLS]
        self.assertEqual(off_names, on_names)
        # `registry.names()` 是**排序后**的视图，逐项顺序由 RoutedSpecsShapeTest 钉住。
        self.assertEqual(set(off_names), set(ROUTED_TOOLS))
        for name in ROUTED_TOOLS:
            with self.subTest(tool=name):
                before = off.get(name)
                after = on.get(name)
                assert before is not None and after is not None
                self.assertEqual(before.description, after.description)
                self.assertEqual(before.openai_schema(), after.openai_schema())


# --------------------------------------------------------------------------- #
# 端到端：路由后的同名工具真的打到远端链路（复用 test_remote 的内存传输）
# --------------------------------------------------------------------------- #
class RoutedEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_remote import FakeTransport

        from agent_core.audit import AuditLog
        from agent_core.sandbox import CommandGuard, PathGuard
        from agent_core.tools.base import ToolContext

        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()
        store = RemoteStore.load(self.ws / ".vibe" / "remote.json")
        store.upsert(make_conn(allow_write=True))
        store.add_root("devbox", "")
        self.config = Config.load(
            self.ws, remote_enabled=True, session_id="testsess", audit_enabled=False
        )
        self.transport = FakeTransport({"/srv/app/src/app.py": b"value = 1\n"})
        self._patch = mock.patch(
            "agent_core.remote.workspace.transport_for", lambda conn: self.transport
        )
        self._patch.start()
        self.router = RemoteRouter(self.config)
        self.ctx = ToolContext(
            config=self.config,
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws, allow_network=False),
            audit=AuditLog(
                self.config.audit_path, session_id="testsess", enabled=False
            ),
            remote_router=self.router,
        )
        self.registry = build_default_registry(lsp=False, remote=True)

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_read_writes_namespace_path_in_meta(self) -> None:
        result = self.registry.execute(
            self.ctx, "read_file", {"path": "@remote/devbox/src/app.py"}
        )
        self.assertTrue(result.ok, result.content)
        self.assertIn("value = 1", result.content)
        self.assertEqual(result.meta["path"], "@remote/devbox/src/app.py")
        self.assertEqual(result.meta["remote"]["rel"], "src/app.py")
        self.assertIn("@remote/devbox/src/app.py", self.ctx.read_files)

    def test_edit_backs_up_then_writes(self) -> None:
        result = self.registry.execute(
            self.ctx,
            "edit_file",
            {
                "path": "@remote/devbox/src/app.py",
                "search": "value = 1",
                "replace": "value = 2",
            },
        )
        self.assertTrue(result.ok, result.content)
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"value = 2\n")
        # 写前留底：改动前内容必须在本机留有副本（M11/ADR-004 红线）
        backups = list((Path(str(self.config.checkpoint_dir)) / "remote").rglob("app.py"))
        self.assertTrue(backups, "未找到远端改动前的本机留底")
        self.assertEqual(backups[0].read_bytes(), b"value = 1\n")
        # 改动登记在**命名空间路径**下，与本地同名文件区分开
        self.assertEqual(
            [record.path for record in self.ctx.changes],
            ["@remote/devbox/src/app.py"],
        )

    def test_write_requires_explicit_overwrite(self) -> None:
        refused = self.registry.execute(
            self.ctx,
            "write_file",
            {"path": "@remote/devbox/src/app.py", "content": "x\n"},
        )
        self.assertFalse(refused.ok)
        allowed = self.registry.execute(
            self.ctx,
            "write_file",
            {"path": "@remote/devbox/src/app.py", "content": "x\n", "overwrite": True},
        )
        self.assertTrue(allowed.ok, allowed.content)
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"x\n")

    def test_list_dir_and_find_files_hit_remote(self) -> None:
        listed = self.registry.execute(
            self.ctx, "list_dir", {"path": "@remote/devbox/src"}
        )
        self.assertTrue(listed.ok, listed.content)
        self.assertIn("app.py", listed.content)
        found = self.registry.execute(
            self.ctx, "find_files", {"path": "@remote/devbox/src", "pattern": "*.py"}
        )
        self.assertTrue(found.ok, found.content)
        self.assertIn("@remote/devbox/src/app.py", found.content)

    def test_grep_assess_is_review_with_connection_name(self) -> None:
        """检索恒 REVIEW，且审批理由里必须点名**哪台机器**（反向锚点 5）。

        理由里没有连接名的话，审批人看到的只是「在远端检索」—— 无法判断这条
        模式串要发到哪台设备上。而模式串本身是会外发的（与 web 类工具同理）。
        """
        spec = self.registry.get("grep")
        assert spec is not None
        risk, reason = spec.assess(
            self.ctx, {"path": "@remote/devbox/src", "pattern": "value"}
        )
        self.assertEqual(risk, Risk.REVIEW)
        self.assertIn("devbox", reason)


class ReservedPrefixTest(unittest.TestCase):
    """未路由的本地工具收到命名空间路径时必须**明确报错**，不静默当成本地文件。"""

    def setUp(self) -> None:
        from agent_core.loop.agent import _policy_state

        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self._policy_state = _policy_state

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_policy_state_sets_prefix_only_when_enabled(self) -> None:
        from agent_core.sandbox import PathGuard

        _, on, _ = self._policy_state(
            Config.load(self.ws, remote_enabled=True), None
        )
        _, off, _ = self._policy_state(
            Config.load(self.ws, remote_enabled=False), None
        )
        self.assertEqual(on.reserved_prefixes, ("@remote/",))
        self.assertEqual(off.reserved_prefixes, ())
        self.assertIsInstance(off, PathGuard)

    def test_no_prefix_configured_keeps_old_behaviour(self) -> None:
        from agent_core.sandbox import PathGuard

        guard = PathGuard(self.ws)
        # 不配保留前缀时照旧解析（既有行为逐字节不变）
        self.assertTrue(str(guard.resolve("@remote/devbox/a.py")).endswith("a.py"))

    def test_reserved_prefix_gives_explicit_reason(self) -> None:
        from agent_core.errors import SandboxViolation
        from agent_core.sandbox import PathGuard

        guard = PathGuard(self.ws, reserved_prefixes=("@remote/",))
        with self.assertRaises(SandboxViolation) as caught:
            guard.check_read("@remote/devbox/a.py")
        self.assertEqual(caught.exception.kind, "reserved_ns")
        self.assertIn("保留命名空间", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
