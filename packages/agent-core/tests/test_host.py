"""桌面接入层（host）测试。

这一层的两件事必须被锁死，否则会以很难排查的方式出问题：

1. **本机服务的安全闸门** —— 一个只监听 127.0.0.1 的端口并不天然安全，
   DNS Rebinding / 同机进程都可能打进来。这里逐条验证拒绝逻辑真的生效。
2. **审批会合点** —— Agent 线程会在这里阻塞。要验证的不只是「批准能过」，
   更要验证「没人能批准时它拒绝」（失效安全），因为那才是这道闸门的意义。
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
from agent_core.host.bus import EventBus  # noqa: E402
from agent_core.host.files import (  # noqa: E402
    FileAccessError,
    build_tree,
    list_dirs,
    read_text_file,
)
from agent_core.host.security import Forbidden, HostGuard  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.session import BusyError, Session  # noqa: E402
from agent_core.host.store import SessionStore  # noqa: E402
from agent_core.llm import ChatResponse  # noqa: E402
from agent_core.sandbox import PathGuard  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402


# 核心工具名（不含条件注册的 lsp_diagnostics）—— 见 build_default_registry。
# 断言「这 10 个必到」而不是「总数恰好 10」，让测试与机器无关。
CORE_TOOLS = frozenset(
    {
        "repo_map",
        "list_dir",
        "read_file",
        "grep",
        "find_files",
        "semantic_search",
        "git_diff",
        "edit_file",
        "write_file",
        "edit_files",
        "update_plan",
        "run_command",
    }
)


def _headers(**overrides: str) -> dict[str, str]:
    base = {"Host": "127.0.0.1:7777", "X-Vibe-Token": "secret-token"}
    base.update(overrides)
    return base


def _slice(text: str, start: str, end: str) -> str:
    """截出 `start` 到其后第一个 `end` 之间的片段。

    给「两件事的先后顺序」这类只能靠读源码钉住的契约用（见
    test_approval_buttons_stay_live_after_session_reset）。
    """
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _strip_comments(text: str) -> str:
    """剥掉 `//` 行注释。

    做顺序断言前必须剥 —— 否则注释里提到的函数名会被当成代码位置，
    断言随即指向一个和实际执行顺序无关的地方。
    """
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


class GuardTests(unittest.TestCase):
    """本机 HTTP 的安全闸门。"""

    def setUp(self) -> None:
        self.guard = HostGuard(port=7777, token="secret-token")

    def check(self, method="GET", path="/api/state", query=None, **headers):
        self.guard.check(
            method=method,
            path=path,
            headers=_headers(**headers),
            query=query or {},
        )

    # ---- token ------------------------------------------------------------
    def test_valid_token_passes(self) -> None:
        self.check()

    def test_missing_token_rejected(self) -> None:
        with self.assertRaises(Forbidden) as ctx:
            self.check(**{"X-Vibe-Token": ""})
        self.assertIn("令牌", str(ctx.exception))

    def test_wrong_token_rejected(self) -> None:
        with self.assertRaises(Forbidden):
            self.check(**{"X-Vibe-Token": "wrong"})

    def test_token_via_query_allowed(self) -> None:
        """SSE 用 EventSource，无法自定义请求头，只能走查询参数。"""
        self.check(**{"X-Vibe-Token": ""}, query={"token": ["secret-token"]})

    def test_health_needs_no_token(self) -> None:
        """Electron 主进程在拿到 token 之前要靠它做就绪探测。"""
        self.check(path="/api/health", **{"X-Vibe-Token": ""})

    def test_static_shell_needs_no_token(self) -> None:
        """静态壳不查令牌 —— `<link>`/`<script>` 子资源请求带不了令牌。

        若连样式表都要求令牌，浏览器加载 `/?token=…` 后 CSS/JS 全部 403，
        用户看到的是一页裸 HTML（真实发生过：重做 IDE 界面后首启即现）。
        静态壳里没有任何数据，令牌守的是 /api/*。
        """
        for path in ("/", "/styles.css", "/app.js", "/markdown.js", "/diff.js"):
            self.check(path=path, **{"X-Vibe-Token": ""})

    def test_static_shell_still_checked_for_host_and_origin(self) -> None:
        """放行令牌 ≠ 放行一切：DNS Rebinding / CSRF 两道闸对所有请求生效。"""
        with self.assertRaises(Forbidden):
            self.check(path="/styles.css", Host="evil.example.com")
        with self.assertRaises(Forbidden):
            self.check(path="/app.js", Origin="http://evil.example.com")

    # ---- Host / Origin ----------------------------------------------------
    def test_foreign_host_rejected(self) -> None:
        """DNS Rebinding 的核心防线：Host 必须真的是本机。"""
        with self.assertRaises(Forbidden) as ctx:
            self.check(Host="evil.example.com")
        self.assertIn("本机", str(ctx.exception))

    def test_host_port_mismatch_rejected(self) -> None:
        with self.assertRaises(Forbidden):
            self.check(Host="127.0.0.1:9999")

    def test_localhost_host_allowed(self) -> None:
        self.check(Host="localhost:7777")

    def test_foreign_origin_rejected(self) -> None:
        with self.assertRaises(Forbidden) as ctx:
            self.check(Origin="http://evil.example.com")
        self.assertIn("白名单", str(ctx.exception))

    def test_local_origin_allowed(self) -> None:
        self.check(Origin="http://127.0.0.1:7777")
        self.check(Origin="http://localhost:7777")

    def test_null_origin_off_by_default(self) -> None:
        with self.assertRaises(Forbidden):
            self.check(Origin="null")

    def test_method_allowlist(self) -> None:
        with self.assertRaises(Forbidden):
            self.check(method="DELETE")

    def test_launch_url_carries_token(self) -> None:
        self.assertIn("token=secret-token", self.guard.launch_url)

    def test_header_names_are_case_insensitive(self) -> None:
        """回归：HTTP 头名大小写不敏感。

        Chrome 发 `Host`，Node 的 fetch 发 `host`。曾经因为调用方用普通 dict
        装着原始大小写，导致同一个服务在浏览器里正常、在 Electron 里全部 403 ——
        这类 bug 只在特定客户端上暴露，所以必须在这里锁住。
        """
        self.guard.check(
            method="GET",
            path="/api/state",
            headers={"host": "127.0.0.1:7777", "x-vibe-token": "secret-token"},
            query={},
        )
        self.guard.check(
            method="GET",
            path="/api/state",
            headers={"HOST": "127.0.0.1:7777", "X-VIBE-TOKEN": "secret-token"},
            query={},
        )

    def test_lowercase_host_still_checked(self) -> None:
        """归一化不能把校验一起绕过。"""
        with self.assertRaises(Forbidden):
            self.guard.check(
                method="GET",
                path="/api/state",
                headers={"host": "evil.example.com", "x-vibe-token": "secret-token"},
                query={},
            )


class BusTests(unittest.TestCase):
    def test_history_replay_after_broken_connection(self) -> None:
        """界面刷新后能补齐漏掉的事件，而不是从空白开始。"""
        bus = EventBus()
        bus.publish({"type": "a"})
        bus.publish({"type": "b"})
        bus.publish({"type": "c"})

        sub = bus.subscribe(since=1)
        received = []
        while True:
            event = sub.get(timeout=0.05)
            if event is None:
                break
            received.append(event["type"])
        sub.close()
        self.assertEqual(received, ["b", "c"])

    def test_ids_are_monotonic(self) -> None:
        bus = EventBus()
        ids = [bus.publish({"type": "x"})["id"] for _ in range(5)]
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(bus.last_id, 5)

    def test_slow_client_never_blocks_producer(self) -> None:
        """生产者是 Agent 工作线程，绝不能因为某个客户端卡住而停摆。"""
        bus = EventBus(queue_size=3)
        sub = bus.subscribe()
        for index in range(50):
            bus.publish({"type": "bulk", "n": index})  # 不读队列，应始终不阻塞

        # 队列里应当是最近 3 条（丢最旧），而不是最早 3 条
        drained = []
        while True:
            event = sub.get(timeout=0.05)
            if event is None:
                break
            drained.append(event["n"])
        sub.close()
        self.assertEqual(drained, [47, 48, 49])

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        sub = bus.subscribe()
        self.assertEqual(bus.subscriber_count, 1)
        sub.close()
        self.assertEqual(bus.subscriber_count, 0)
        bus.publish({"type": "after"})
        self.assertIsNone(sub.get(timeout=0.05))


class FileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("print(1)\n", encoding="utf-8", newline="")
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "junk.js").write_text("x", encoding="utf-8", newline="")
        (self.ws / ".env").write_text("SECRET=1\n", encoding="utf-8", newline="")
        (self.ws / "id_rsa").write_text("key", encoding="utf-8", newline="")
        (self.ws / "crlf.txt").write_bytes("a\r\nb\r\n".encode("utf-8"))
        self.guard = PathGuard(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _flatten(self, nodes):
        for node in nodes:
            yield node
            yield from self._flatten(node.get("children") or [])

    def test_tree_skips_ignored_dirs(self) -> None:
        tree = build_tree(self.guard)
        names = {n["name"] for n in self._flatten(tree["nodes"])}
        self.assertIn("src", names)
        self.assertIn("app.py", names)
        self.assertNotIn("node_modules", names)

    def test_tree_marks_sensitive_files(self) -> None:
        tree = build_tree(self.guard)
        by_name = {n["name"]: n for n in self._flatten(tree["nodes"])}
        self.assertTrue(by_name[".env"]["sensitive"])
        self.assertTrue(by_name["id_rsa"]["sensitive"])
        self.assertFalse(by_name["crlf.txt"].get("sensitive"))

    def test_read_normal_file(self) -> None:
        data = read_text_file(self.guard, "src/app.py")
        self.assertEqual(data["text"], "print(1)\n")
        self.assertEqual(data["newline"], "LF")

    def test_read_reports_crlf(self) -> None:
        data = read_text_file(self.guard, "crlf.txt")
        self.assertEqual(data["newline"], "CRLF")

    def test_sensitive_file_rejected(self) -> None:
        """界面能看到的范围必须与 Agent 能读到的范围一致。"""
        for path in (".env", "id_rsa"):
            with self.assertRaises(FileAccessError):
                read_text_file(self.guard, path)

    def test_escape_rejected(self) -> None:
        with self.assertRaises(FileAccessError):
            read_text_file(self.guard, "../../etc/passwd")

    def test_missing_path_rejected(self) -> None:
        with self.assertRaises(FileAccessError):
            read_text_file(self.guard, "")

    def test_list_dirs_lists_only_subdirectories(self) -> None:
        """目录浏览器只列目录名：不套用工程忽略规则（它是「挑工作区」不是「读工程」），
        也绝不回传文件名 —— 后者会把「界面能看到的 = Agent 能读到的」这条边界捅破。"""
        data = list_dirs(str(self.ws))
        names = {item["name"] for item in data["dirs"]}
        self.assertEqual(names, {"src", "node_modules"})
        self.assertEqual(data["path"], str(self.ws))
        self.assertEqual(data["parent"], str(self.ws.parent))

    def test_list_dirs_empty_path_lists_roots(self) -> None:
        """空 path = 还没选盘：给盘符（POSIX 给 /）与主目录快捷入口。"""
        data = list_dirs("")
        self.assertEqual(data["path"], "")
        self.assertIsNone(data["parent"])
        self.assertTrue(data["roots"])
        self.assertTrue(data["home"])
        self.assertEqual(data["dirs"], [])

    def test_list_dirs_root_has_no_parent(self) -> None:
        """到根（Windows 的 `E:\`、POSIX 的 `/`）必须返回 null：
        界面据此把「上级」切回盘符列表，否则会在根上原地打转。"""
        anchor = Path(self.ws.anchor)
        if not anchor.is_dir():
            self.skipTest(f"根 {anchor} 不可读")
        data = list_dirs(str(anchor))
        self.assertIsNone(data["parent"])

    def test_list_dirs_rejects_non_directory(self) -> None:
        with self.assertRaises(ValueError):
            list_dirs(str(self.ws / "nope"))
        with self.assertRaises(ValueError):
            list_dirs(str(self.ws / "crlf.txt"))  # 存在，但是文件不是目录


class SessionTests(unittest.TestCase):
    """审批会合点与中断语义。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "a.txt").write_text("keep me\n", encoding="utf-8", newline="")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _session(self, steps, **kwargs) -> Session:
        config = Config.load(self.ws)
        return Session(config, client=FakeClient(steps), **kwargs)

    def _wait_idle(self, session: Session, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if session.state == "idle":
                return
            time.sleep(0.02)
        raise AssertionError("会话没有回到空闲状态")

    def _wait_approval(self, session: Session, timeout: float = 6.0) -> dict:
        """等审批请求出现。

        必须先等到它 —— 直接读 pending_approval 会和 Agent 线程赛跑，
        读早了就永远等不到，测试会以「卡住直到超时」这种很难看的方式失败。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            pending = session.pending_approval
            if pending is not None:
                return pending
            time.sleep(0.02)
        raise AssertionError("没有等到审批请求")

    # ---- 命令流式输出（run_command 边跑边回显）-----------------------------
    def test_run_command_streams_tool_output_event(self) -> None:
        """后端把子进程输出转成 run.tool_output 事件（UI 流式回显的数据源）。"""
        (self.ws / "emit.py").write_text(
            "print('hello-stream')\n", encoding="utf-8", newline=""
        )
        session = self._session(
            [[("run_command", {"command": "python emit.py"})], "跑完了。"]
        )
        sub = session.bus.subscribe()  # 有界面连着：审批会合点才会真正抛给用户
        session.submit("跑一下")
        pending = self._wait_approval(session)
        self.assertEqual(pending["tool"], "run_command")
        session.resolve_approval(pending["approvalId"], "approve")
        self._wait_idle(session)

        types: list[str] = []
        stdout_text = ""
        while True:
            event = sub.get(timeout=0.05)
            if event is None:
                break
            types.append(event["type"])
            if event["type"] == "run.tool_output" and event.get("stream") == "stdout":
                stdout_text += event["text"]
        sub.close()

        self.assertIn("run.tool_output", types)
        self.assertIn("hello-stream", stdout_text)

    # ---- 审批 -------------------------------------------------------------
    def test_approve_applies_edit(self) -> None:
        session = self._session(
            [
                [("edit_file", {"path": "a.txt", "search": "keep me", "replace": "changed"})],
                "改好了。",
            ]
        )
        session.bus.subscribe()  # 有界面连着

        session.submit("改一下")
        pending = self._wait_approval(session)

        self.assertEqual(pending["tool"], "edit_file")
        self.assertEqual(pending["preview"]["kind"], "diff")
        self.assertIn("-keep me", pending["preview"]["body"])
        self.assertIn("+changed", pending["preview"]["body"])

        self.assertTrue(session.resolve_approval(pending["approvalId"], "approve"))
        self._wait_idle(session)

        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "changed\n")

    def test_deny_leaves_file_untouched(self) -> None:
        session = self._session(
            [
                [("edit_file", {"path": "a.txt", "search": "keep me", "replace": "changed"})],
                "好的，不改了。",
            ]
        )
        session.bus.subscribe()
        session.submit("改一下")

        pending = self._wait_approval(session)
        session.resolve_approval(pending["approvalId"], "deny")
        self._wait_idle(session)

        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(session.agent.stats.denials, 1)

    def test_no_ui_connected_denies_instead_of_hanging(self) -> None:
        """失效安全：没人能按按钮时，绝不能干等 —— 默认拒绝。"""
        session = self._session(
            [
                [("edit_file", {"path": "a.txt", "search": "keep me", "replace": "changed"})],
                "改不了。",
            ],
            approval_timeout=30,  # 若走超时路径，这个测试会明显变慢
        )
        # 刻意不订阅总线
        started = time.time()
        session.submit("改一下")
        self._wait_idle(session, timeout=8)

        self.assertLess(time.time() - started, 5, "应当立即拒绝而不是等超时")
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_resolve_unknown_approval_returns_false(self) -> None:
        session = self._session(["完成。"])
        self.assertFalse(session.resolve_approval("no-such-id", "approve"))

    def test_invalid_decision_rejected(self) -> None:
        session = self._session(["完成。"])
        with self.assertRaises(ValueError):
            session.resolve_approval("x", "maybe")

    # ---- 预演闸门 ---------------------------------------------------------
    def test_failing_patch_skips_approval_entirely(self) -> None:
        """注定失败的补丁不该弹窗打扰用户，直接把诊断回灌给模型。"""
        session = self._session(
            [
                [("edit_file", {"path": "a.txt", "search": "这段文字不存在", "replace": "x"})],
                "换个做法。",
            ]
        )
        session.bus.subscribe()
        session.submit("改一下")
        self._wait_idle(session)

        self.assertIsNone(session.pending_approval, "不该产生审批请求")
        self.assertEqual(session.agent.stats.approvals, 0)
        self.assertGreaterEqual(session.agent.stats.tool_failures, 1)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "keep me\n")

    # ---- 会话管理 ---------------------------------------------------------
    def test_second_run_while_busy_rejected(self) -> None:
        session = self._session(
            [
                [("edit_file", {"path": "a.txt", "search": "keep me", "replace": "x"})],
                "好了。",
            ]
        )
        session.bus.subscribe()
        session.submit("第一个任务")
        with self.assertRaises(BusyError):
            session.submit("第二个任务")
        pending = self._wait_approval(session)
        session.resolve_approval(pending["approvalId"], "deny")
        self._wait_idle(session)

    def test_empty_prompt_rejected(self) -> None:
        session = self._session(["完成。"])
        with self.assertRaises(ValueError):
            session.submit("   ")

    def test_reset_clears_history_but_keeps_audit(self) -> None:
        session = self._session(["第一次完成。"])
        session.submit("任务一")
        self._wait_idle(session)
        turns_before = len(session.agent.messages)
        self.assertGreater(turns_before, 2)

        session.reset()
        self.assertEqual(len(session.agent.messages), 1)
        self.assertTrue(session.agent.config.audit_path.exists(), "审计不该被清空")

    def test_interrupt_at_iteration_boundary(self) -> None:
        """中断在轮次边界生效，因此消息序列始终保持自洽。"""
        session = self._session(
            [
                [("list_dir", {})],
                [("list_dir", {})],
                [("list_dir", {})],
                "完成。",
            ]
        )
        session.bus.subscribe()
        session.submit("列目录")
        session.interrupt()
        self._wait_idle(session, timeout=8)

        self.assertEqual(session.state, "idle")
        # 历史必须以 assistant 收尾，不能残留配不上对的 tool 消息
        self.assertEqual(session.agent.messages[-1]["role"], "assistant")

    def test_interrupt_when_idle_is_noop(self) -> None:
        session = self._session(["完成。"])
        self.assertFalse(session.interrupt())

    def test_describe_shape(self) -> None:
        session = self._session(["完成。"])
        info = session.describe()
        for key in ("sessionId", "workspace", "model", "tools", "config", "state"):
            self.assertIn(key, info)
        self.assertEqual(info["state"], "idle")
        # 核心 10 个必到；`lsp_diagnostics` 是条件注册（本机装没装语言服务器而定），
        # 所以断言「核心名字齐全 + 无重复」，而不是钉死总数 —— 否则装了 pyright
        # 的机器上这条会莫名其妙变红。
        names = [tool["name"] for tool in info["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(len(CORE_TOOLS), len(names))
        self.assertEqual(CORE_TOOLS - set(names), set())
        self.assertLessEqual(len(names), len(CORE_TOOLS) + 1)

    def test_diffs_are_grouped_by_file(self) -> None:
        """路径与 diff 必须成对，不能被下标错位拆散。"""
        session = self._session(
            [
                [("edit_file", {"path": "a.txt", "search": "keep me", "replace": "one"})],
                [("write_file", {"path": "b.txt", "content": "hello\n"})],
                "好了。",
            ]
        )
        session.bus.subscribe()
        session.submit("改两个文件")

        deadline = time.time() + 12
        while time.time() < deadline and session.state != "idle":
            pending = session.pending_approval
            if pending:
                session.resolve_approval(pending["approvalId"], "approve")
            time.sleep(0.05)
        self._wait_idle(session)

        changes = session.diffs()
        by_path = {c["path"]: c for c in changes}
        self.assertIn("a.txt", by_path)
        self.assertIn("b.txt", by_path)
        self.assertIn("+one", by_path["a.txt"]["diff"])
        self.assertIn("+hello", by_path["b.txt"]["diff"])
        self.assertFalse(by_path["a.txt"]["created"])
        self.assertTrue(by_path["b.txt"]["created"])
        # 关键回归：不能因为 write_file 不写 applied_diffs 就把两边的下标错位
        self.assertNotIn("+one", by_path["b.txt"]["diff"])
        self.assertNotIn("+hello", by_path["a.txt"]["diff"])


class HttpTests(unittest.TestCase):
    """真实 HTTP 层：鉴权、静态资源、事件流。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "a.txt").write_text("hi\n", encoding="utf-8", newline="")
        config = Config.load(self.ws)
        self.host = HostServer(
            config,
            port=0,
            client=FakeClient(
                [
                    [("list_dir", {})],
                    "看完了。",
                ]
            ),
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._tmp.cleanup()

    def get(self, path: str, token: str | None = None, headers: dict | None = None):
        request = urllib.request.Request(path if path.startswith("http") else self.base + path)
        if token is not None:
            request.add_header("X-Vibe-Token", token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return self._opener.open(request, timeout=10)

    def test_state_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state")
        self.assertEqual(ctx.exception.code, 403)

    def test_state_with_token(self) -> None:
        with self.get("/api/state", token=self.token) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["workspace"], str(self.ws))
        names = [tool["name"] for tool in data["tools"]]
        self.assertEqual(CORE_TOOLS - set(names), set())

    def test_lowercase_headers_accepted_over_http(self) -> None:
        """Electron 的窗口用 Node 的 fetch 同款栈，头名是小写的。"""
        request = urllib.request.Request(self.base + "/api/state")
        request.add_header("host", "127.0.0.1:" + str(self.host.port))
        request.add_header("x-vibe-token", self.token)
        with self._opener.open(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["workspace"], str(self.ws))

    def test_wrong_token_forbidden(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state", token="nope")
        self.assertEqual(ctx.exception.code, 403)

    def test_foreign_origin_forbidden(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state", token=self.token, headers={"Origin": "http://evil.example.com"})
        self.assertEqual(ctx.exception.code, 403)

    def test_health_needs_no_token(self) -> None:
        with self.get("/api/health") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["ok"])

    def test_index_served(self) -> None:
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        self.assertIn("Vibe Coding Agent", body)

    def test_layout_mode_switch_contract(self) -> None:
        """IDE / SOLO 布局切换靠 body[data-mode] 驱动，这里锁住 app.js 依赖的 DOM 契约。

        只断言「改一处名字就会立刻静默失效」的部分：默认模式、两个模式按钮、
        以及 setMode 按 id 清列宽的三根分栏（editor-stack 曾缺 id，导致切换抛异常且不持久化）。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        self.assertIn('<body data-mode="ide">', body)
        self.assertIn('class="mode-btn is-active" data-mode="ide"', body)
        self.assertIn('data-mode="solo"', body)
        for element_id in ("chatpane", "editor-stack", "sidebar", "sb-resizer", "chat-resizer"):
            self.assertIn(f'id="{element_id}"', body)

    def test_solo_layout_rules_served(self) -> None:
        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        # 两种布局共用一套 DOM，SOLO 只靠 CSS 重排 —— 规则丢了界面会退回 IDE 排布
        self.assertIn('body[data-mode="solo"]', css)
        self.assertIn('has-open-tab', css)

    def test_styles_and_scripts_served(self) -> None:
        for path in ("/styles.css", "/app.js", "/diff.js"):
            with self.get(path, token=self.token) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())

    def test_static_traversal_blocked(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/../server.py", token=self.token)
        self.assertIn(ctx.exception.code, (403, 404))

    def test_workspace_switch_dom_contract(self) -> None:
        """W1：工作区弹窗的入口、代价说明与「从磁盘选择」按钮必须都在。

        锁的是「改个 id 或删一句话就静默失效」的部分：入口按钮、弹窗、路径输入，
        以及那句**必须在点之前看到**的代价说明（验收要求）。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        for element_id in (
            "tb-workspace", "ws-modal", "ws-list", "ws-path", "ws-error",
            "btn-ws-open", "btn-ws-close", "btn-ws-pick",
            "ws-dirs", "ws-cwd", "btn-ws-up",
        ):
            self.assertIn(f'id="{element_id}"', body)
        self.assertIn("重启后端上下文", body)
        # 目录对话框按钮**默认隐藏**：只有桌面壳（preload 注入 window.vibeShell）才露出来。
        # 上一版这里放的是个纯文本标签，看着像按钮却点不动 —— 不许再退回那个形态。
        self.assertIn('id="btn-ws-pick" hidden', body)

        with self.get("/app.js", token=self.token) as response:
            script = response.read().decode("utf-8")
        self.assertIn("/api/workspaces/open", script)
        # 浏览器侧那条独立的目录选择路径：没有它就只剩「手输绝对路径」
        self.assertIn("/api/fs/dirs", script)

    def test_refs_dom_contract(self) -> None:
        """P0-A：`@` 引用浮层与 chips 的 DOM 契约，以及界面**只提交意图**这条红线。

        锁三件「改个 id 或换个字段名就静默失效」的事：
        1. 浮层与 chips 容器都在，且浮层默认隐藏；
        2. 发送时带上 `refs`，选中后从正文里删掉 `@词`（留正文会变成重复信息）；
        3. 界面**不自己读文件** —— 候选一律来自 `/api/refs`，正文由后端经 PathGuard 解析。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        for element_id in ("refs-bar", "mention-pop", "mention-head", "mention-list"):
            self.assertIn(f'id="{element_id}"', body)
        self.assertIn('id="mention-pop" hidden', body)
        self.assertIn('id="refs-bar" hidden', body)

        with self.get("/app.js", token=self.token) as response:
            script = response.read().decode("utf-8")
        self.assertIn("/api/refs", script)
        self.assertIn("api.post('/api/run', { text, refs })", script)
        # 抹掉正文里的 `@词`：acceptMention 必须真的改写 input.value
        accept = _slice(script, "function acceptMention", "\n}")
        self.assertIn("input.value = input.value.slice", accept)
        # 清会话时引用必须一起清（否则换工作区后旧引用全是解析失败）
        reset = _slice(script, "function clearSessionView", "\n}")
        self.assertIn("S.refs = []", reset)

    def test_command_palette_contract(self) -> None:
        """P2-13：快捷键收敛成 KEYMAP + 命令面板，锁住「改个名字就静默失效」的部分。

        三件必须同时成立：
        1. 面板 DOM（遮罩/输入框/列表）都在，且默认隐藏、工具栏有入口；
        2. 旧的快捷键全部登记进 KEYMAP —— 漏一条就是「按了没反应」的静默回归；
        3. 关键实现钩子在：查表、模糊过滤、执行前关面板（否则动作里再开浮层会被自己盖住）。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        for element_id in ("palette", "palette-input", "palette-list", "btn-palette"):
            self.assertIn(f'id="{element_id}"', body)
        self.assertIn('id="palette" hidden', body)

        with self.get("/app.js", token=self.token) as response:
            script = response.read().decode("utf-8")
        self.assertIn("const KEYMAP = [", script)
        self.assertIn("function matchCommand", script)
        self.assertIn("function buildCommandList", script)
        self.assertIn("function fuzzyMatch", script)
        self.assertIn("function openPalette", script)
        # 旧快捷键逐条登记（chord 串写死，改名即红）
        for chord in ("Ctrl+1", "Ctrl+6", "Ctrl+`", "Ctrl+k", "Ctrl+s", "F12", "F2",
                      "Esc", "Ctrl+Enter", "Ctrl+Shift+p"):
            self.assertIn(f"chord: '{chord}'", script)
        # 执行前先关面板再跑动作
        run = _slice(script, "function runCommand", "\n}")
        self.assertIn("closePalette()", run)
        # 过滤是「子序列真匹配」而不是「恒匹配」——把过滤改成 return items 这句就红。
        # （反向验证：临时让 filterCommands 恒返回全部 → 本断言必须失败。）
        self.assertIn(
            "palette.items.filter((c) => fuzzyMatch(q, c.label) || fuzzyMatch(q, c.id))",
            script,
        )
        self.assertIn("if (t[k] === q[i]) i++;", script)   # 子序列推进
        self.assertIn("return i === q.length;", script)    # 全部命中才算匹配

    def test_tests_panel_contract(self) -> None:
        """P2-14：IDE 内测试树的 DOM 与关键接线，锁住「改个名字就静默失效」的部分。

        四件：
        1. 活动栏第 7 项与侧栏面板都在，且面板默认隐藏（只有切到它才拉用例）；
        2. `Ctrl+7` 登记进 KEYMAP —— 漏一条就是「按了没反应」的静默回归；
        3. 收集 / 执行 / 最近结果三个端点都在用；
        4. 失败项点击 → 开文件并**定位到 failureLine**（复用 openFile + placeCaret）。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        self.assertIn('data-panel="tests"', body)
        self.assertIn('data-body="tests" hidden', body)
        for element_id in (
            "btn-tests-run-all", "btn-tests-run-failed", "btn-tests-refresh",
            "tests-summary", "tests-tree", "act-badge-tests",
        ):
            self.assertIn(f'id="{element_id}"', body)

        with self.get("/app.js", token=self.token) as response:
            script = response.read().decode("utf-8")
        self.assertIn("chord: 'Ctrl+7'", script)
        self.assertIn("switchPanel('tests')", script)
        for endpoint in ("/api/tests", "/api/tests/run", "/api/tests/last"):
            self.assertIn(endpoint, script)
        # 事件流接线：跑测试的结果走这两条事件，缺一条界面就不更新
        self.assertIn("'test.started'", script)
        self.assertIn("'test.finished'", script)
        # 失败项点击定位到失败行：openTestLocation 必须真把行号交给 placeCaret
        # （反向验证：删掉这行调用，本断言即失败。）
        locate = _slice(script, "async function openTestLocation", "\n}")
        self.assertIn("await openFile(file)", locate)
        self.assertIn("placeCaret(entry, line, 0)", locate)
        # 行内「▶ 跑这一个」按 node id 起一次单跑
        row = _slice(script, "function renderTestRow", "\n}")
        self.assertIn("runTests([item.nodeId])", row)

    def test_fs_dirs_endpoint_guarded_and_validated(self) -> None:
        """列目录接口：令牌闸门与路径校验都在服务端，不靠界面自觉。

        这个接口是全仓唯一一个**故意绕过 PathGuard** 的读路径（挑新工作区时
        候选目录必然在当前 jail 之外），所以它的边界必须由闸门 + 输入校验兜住。
        """
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/fs/dirs")
        self.assertEqual(ctx.exception.code, 403)

        with self.get("/api/fs/dirs", token=self.token) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["roots"])  # 空 path = 盘符列表

        quoted = urllib.parse.quote(str(self.ws))
        with self.get(f"/api/fs/dirs?path={quoted}", token=self.token) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["path"], str(self.ws))

        missing = urllib.parse.quote(str(self.ws / "nope"))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(f"/api/fs/dirs?path={missing}", token=self.token)
        self.assertEqual(ctx.exception.code, 400)  # 客户端给的路径不合法

    def test_shell_bridge_contract(self) -> None:
        """W1：壳与界面之间那条唯一 IPC 的契约两边必须对得上。

        通道名写错是**静默失败** —— 按钮点下去毫无反应，且只有真跑 Electron 才暴露。
        （浏览器里没有 window.vibeShell，界面会隐藏该按钮，所以这条只能靠读文件钉住。）
        """
        desktop = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "src"
        preload_js = (desktop / "preload.js").read_text(encoding="utf-8")
        main_js = (desktop / "main.js").read_text(encoding="utf-8")
        self.assertIn("ipcRenderer.invoke('vibe:pick-folder')", preload_js)
        self.assertIn("ipcMain.handle('vibe:pick-folder'", main_js)
        # 只弹目录、只回传路径字符串：不得把 Node 能力交给渲染进程
        # （按 require 形式断言，不按词匹配 —— 注释里正说着"没有 child_process"）
        self.assertIn("properties: ['openDirectory']", main_js)
        for forbidden in (
            "require('fs')", "require('node:fs')",
            "require('child_process')", "require('node:child_process')",
            "require('node:path')",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, preload_js)

        with self.get("/app.js", token=self.token) as response:
            script = response.read().decode("utf-8")
        self.assertIn("pickFolder", script)  # 界面确实在用这座桥

    def test_approval_buttons_stay_live_after_session_reset(self) -> None:
        """审批浮层必须和 `S.awaiting` 同生共死（用户报的「按钮点不动」）。

        现象：弹窗明明在，「批准并执行 / 全部放行 / 拒绝 / 拒绝并禁用」全部没反应，
        只有不查状态的「先看看」能动 —— 因为 `clearSessionView()` 把 `S.awaiting`
        清成了 null 却留着浮层，`decide()` 一进门就 return（它不报错、不发请求，
        所以从控制台和网络面板都看不出问题）。

        触发路径是**每次刷新都会走的那条**：页面加载 → `applySession()` 打开待审批
        浮层并设好状态 → 事件流重放 `session.ready` → 旧顺序下 `clearSessionView()`
        紧跟 `applySession()` 之后把状态抹掉。所以这里钉两件事：
        1. `clearSessionView()` 清状态时必须一并关掉浮层；
        2. `session.ready` 里必须**先 clearSessionView 再 applySession**。
        """
        with self.get("/app.js", token=self.token) as response:
            script = response.read().decode("utf-8")

        reset = _slice(script, "function clearSessionView", "\n}")
        self.assertIn("S.awaiting = null", reset)
        self.assertIn("$('modal').hidden = true", reset)
        self.assertIn("$('approval-banner').hidden = true", reset)

        ready = _strip_comments(_slice(script, "'session.ready':", "\n  },\n"))
        self.assertLess(
            ready.index("clearSessionView("),
            ready.index("applySession(ev)"),
            "session.ready 必须先清视图再铺状态，否则会把刚打开的审批浮层清成死键",
        )

        finished = _slice(script, "'run.finished':", "\n  },\n")
        self.assertIn("closeApproval()", finished)
        self.assertNotIn("S.awaiting = null", finished)

    def test_tree_endpoint(self) -> None:
        with self.get("/api/tree", token=self.token) as response:
            data = json.loads(response.read().decode("utf-8"))
        names = {node["name"] for node in data["nodes"]}
        self.assertIn("a.txt", names)

    def test_file_endpoint_rejects_sensitive(self) -> None:
        (self.ws / ".env").write_text("X=1\n", encoding="utf-8", newline="")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/file?path=.env", token=self.token)
        self.assertEqual(ctx.exception.code, 403)

    def test_run_and_stream_events(self) -> None:
        events: list[dict] = []
        ready = threading.Event()

        def reader() -> None:
            # 服务关闭时这条长连接会被切断，抛异常属正常收尾，不必污染测试输出
            try:
                request = urllib.request.Request(
                    f"{self.base}/api/events?token={self.token}"
                )
                request.add_header("Accept", "text/event-stream")
                with self._opener.open(request, timeout=15) as response:
                    event_type = None
                    for raw in response:
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("event: "):
                            event_type = line[7:]
                            if event_type == "session.ready":
                                ready.set()
                        elif line.startswith("data: "):
                            events.append(json.loads(line[6:]))
            except Exception:
                pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5), "SSE 没有及时建立")

        request = urllib.request.Request(
            f"{self.base}/api/run",
            data=json.dumps({"text": "看看目录"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Vibe-Token": self.token},
            method="POST",
        )
        with self._opener.open(request, timeout=10) as response:
            self.assertEqual(response.status, 200)

        deadline = time.time() + 10
        while time.time() < deadline:
            if any(e.get("type") == "run.finished" for e in events):
                break
            time.sleep(0.05)

        types = [e["type"] for e in events]
        self.assertIn("run.started", types)
        self.assertIn("run.tool_call", types)
        self.assertIn("run.tool_result", types)
        self.assertIn("run.finished", types)
        # 携带 id 的事件必须单调递增，否则断线补齐会错乱。
        # session.ready 是欢迎帧，刻意不带 id（不该推进 Last-Event-ID），因此排除。
        ids = [e["id"] for e in events if "id" in e]
        self.assertTrue(ids)
        self.assertEqual(ids, sorted(ids))

    def test_approval_over_http(self) -> None:
        """完整走一遍：任务 → 审批请求 → HTTP 送达决定 → 落盘。"""
        config = Config.load(self.ws)
        host = HostServer(
            config,
            port=0,
            client=FakeClient(
                [
                    [("edit_file", {"path": "a.txt", "search": "hi", "replace": "bye"})],
                    "改好了。",
                ]
            ),
        )
        host.start_background()
        try:
            base = f"http://127.0.0.1:{host.port}"
            token = host.guard.token

            got: list[dict] = []
            ready = threading.Event()

            def reader() -> None:
                try:
                    request = urllib.request.Request(f"{base}/api/events?token={token}")
                    with self._opener.open(request, timeout=15) as response:
                        event_type = None
                        for raw in response:
                            line = raw.decode("utf-8", "replace").rstrip("\n")
                            if line.startswith("event: "):
                                event_type = line[7:]
                                if event_type == "session.ready":
                                    ready.set()
                            elif line.startswith("data: ") and event_type == "run.approval":
                                got.append(json.loads(line[6:]))
                except Exception:
                    pass

            threading.Thread(target=reader, daemon=True).start()
            self.assertTrue(ready.wait(5))

            request = urllib.request.Request(
                f"{base}/api/run",
                data=json.dumps({"text": "改一下"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Vibe-Token": token},
                method="POST",
            )
            self._opener.open(request, timeout=10).read()

            deadline = time.time() + 8
            while not got and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(got, "没有收到审批事件")

            payload = got[0]
            self.assertEqual(payload["preview"]["kind"], "diff")
            self.assertIn("+bye", payload["preview"]["body"])

            decision = urllib.request.Request(
                f"{base}/api/approval",
                data=json.dumps(
                    {"approvalId": payload["approvalId"], "decision": "approve"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Vibe-Token": token},
                method="POST",
            )
            with self._opener.open(decision, timeout=10) as response:
                self.assertTrue(json.loads(response.read().decode("utf-8"))["delivered"])

            deadline = time.time() + 8
            while time.time() < deadline:
                if host.session.state == "idle":
                    break
                time.sleep(0.05)

            self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "bye\n")
        finally:
            host.shutdown()


class _BlockingClient:
    """卡在 chat() 上不返回的假模型：把会话钉在 running 状态。

    用来验证「任务执行中不许切工作区」—— 换成会立刻返回的假模型，
    这个用例就只能靠赛跑，会间歇性通过。
    """

    supports_native_tools = True

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools=None, temperature=None):  # noqa: ANN001
        self.entered.set()
        self.release.wait(timeout=5)
        return ChatResponse(content="完成。")


class WorkspaceSwitchTests(unittest.TestCase):
    """W1：浏览器里「打开其他文件夹作为工作区」—— 切换 = 优雅重启一套上下文。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.ws1 = self.root / "proj-a"
        self.ws2 = self.root / "proj-b"
        for ws in (self.ws1, self.ws2):
            ws.mkdir()
            (ws / "readme.md").write_text("hi\n", encoding="utf-8", newline="")
        self.store = SessionStore(self.root / "sessions.db")
        self.host = HostServer(
            Config.load(self.ws1),
            port=0,
            store=self.store,
            client=FakeClient([[("list_dir", {})], "看完了。"]),
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._tmp.cleanup()

    def get_json(self, path: str) -> dict:
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        with self._opener.open(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Vibe-Token": self.token},
            method="POST",
        )
        with self._opener.open(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _events(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ---- 列表 -------------------------------------------------------------
    def test_workspace_view_lists_current_first(self) -> None:
        data = self.get_json("/api/workspaces")
        self.assertEqual(data["current"], str(self.ws1))
        self.assertTrue(data["workspaces"])
        self.assertEqual(data["workspaces"][0]["path"], str(self.ws1))
        self.assertTrue(data["workspaces"][0]["current"])

    def test_workspace_view_recent_comes_from_session_store(self) -> None:
        """「最近打开」的真源是会话库 —— 壳与浏览器因此天然一致。"""
        self.store.append_message(
            "seed-1", workspace=str(self.ws2), role="user", content="在 b 里干过活", title="b"
        )
        self.store.append_message(
            "seed-2", workspace=str(self.root / "已删除的目录"), role="user", content="x", title="x"
        )
        paths = [item["path"] for item in self.get_json("/api/workspaces")["workspaces"]]
        self.assertIn(str(self.ws1), paths)  # 当前工作区恒在
        self.assertIn(str(self.ws2), paths)
        # 已不存在的目录不给选（选了也打不开，留着只是噪音）
        self.assertNotIn(str(self.root / "已删除的目录"), paths)

    # ---- 切换 -------------------------------------------------------------
    def test_switch_rebuilds_context_and_moves_audit_chain(self) -> None:
        old_session_id = self.host.session.describe()["sessionId"]
        result = self.post_json("/api/workspaces/open", {"path": str(self.ws2)})

        self.assertTrue(result["ok"])
        self.assertTrue(result["switched"])
        self.assertNotEqual(result["sessionId"], old_session_id)

        info = self.host.session.describe()
        self.assertEqual(info["workspace"], str(self.ws2))
        # 审计链、检查点目录都跟着换根 —— 这三者必须一起动，不能只换一个
        self.assertTrue(info["auditPath"].startswith(str(self.ws2)))

        old_events = self._events(self.ws1 / ".vibe" / "audit.jsonl")
        ends = [e for e in old_events if e["event"] == "session.end"]
        self.assertTrue(ends, "旧工作区的链上要留下一次收尾")
        self.assertEqual(ends[-1]["reason"], "workspace_switch")

        new_events = [e["event"] for e in self._events(self.ws2 / ".vibe" / "audit.jsonl")]
        self.assertIn("session.start", new_events)

    def test_switch_moves_file_tree_and_rejects_old_paths(self) -> None:
        """切换后新 API 全部指向新工作区：文件树换了，旧路径不再可读。"""
        self.post_json("/api/workspaces/open", {"path": str(self.ws2)})
        names = {node["name"] for node in self.get_json("/api/tree")["nodes"]}
        self.assertIn("readme.md", names)
        (self.ws2 / ".env").write_text("X=1\n", encoding="utf-8", newline="")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_json("/api/file?path=.env")
        self.assertEqual(ctx.exception.code, 403)

    def test_switch_same_workspace_is_noop(self) -> None:
        result = self.post_json("/api/workspaces/open", {"path": str(self.ws1)})
        self.assertTrue(result["ok"])
        self.assertFalse(result["switched"])

    def test_switch_rejects_missing_directory(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_json("/api/workspaces/open", {"path": str(self.root / "nope")})
        self.assertEqual(ctx.exception.code, 400)

    def test_switch_rejects_empty_path(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_json("/api/workspaces/open", {"path": "   "})
        self.assertEqual(ctx.exception.code, 400)

    def test_switch_rejected_while_busy(self) -> None:
        """任务执行中不许切 —— 换上下文会把正在跑的那套东西连根拔掉。"""
        client = _BlockingClient()
        host = HostServer(Config.load(self.ws1), port=0, client=client)
        host.start_background()
        try:
            base = f"http://127.0.0.1:{host.port}"
            token = host.guard.token

            run = urllib.request.Request(
                f"{base}/api/run",
                data=json.dumps({"text": "干件长活"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Vibe-Token": token},
                method="POST",
            )
            self._opener.open(run, timeout=10).read()
            self.assertTrue(client.entered.wait(5), "任务没有进入执行状态")

            switch = urllib.request.Request(
                f"{base}/api/workspaces/open",
                data=json.dumps({"path": str(self.ws2)}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Vibe-Token": token},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._opener.open(switch, timeout=10)
            self.assertEqual(ctx.exception.code, 409)
            self.assertEqual(str(host.session.config.workspace), str(self.ws1))
        finally:
            client.release.set()
            host.shutdown()

    def test_switch_publishes_session_ready_on_same_bus(self) -> None:
        """已连着的界面不掉线：切换后由同一条总线推一条 session.ready。"""
        bus = self.host.session.bus
        sub = bus.subscribe(since=bus.last_id)
        self.post_json("/api/workspaces/open", {"path": str(self.ws2)})

        ready = None
        while True:
            event = sub.get(timeout=0.2)
            if event is None:
                break
            if event.get("type") == "session.ready":
                ready = event
        sub.close()
        self.assertIsNotNone(ready, "切换后没有推 session.ready")
        self.assertEqual(ready["workspace"], str(self.ws2))


if __name__ == "__main__":
    unittest.main()
