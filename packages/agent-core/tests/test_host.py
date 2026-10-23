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

    def test_pause_button_contract(self) -> None:
        """交互增强一：暂停按钮常驻在发送旁，且与顶部中断共用同一个动作。

        锁住三处：DOM 位置（.composer-box 段内）、状态机（syncPauseButton 依据
        S.interrupting 置灰 + 文案「停止中…」）、绑定到同一个 interrupt()。
        少任何一处，用户在执行中都会找不到可点的停止入口。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        start = body.index('class="composer-box"')
        send_at = body.index('id="btn-send"', start)
        segment = body[send_at:send_at + 900]  # 从发送按钮往后一段，覆盖按钮组
        self.assertIn('id="btn-pause"', segment)
        self.assertIn('aria-label="暂停"', segment)
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function syncPauseButton(", js)
        self.assertIn("S.interrupting = true;", js)
        self.assertIn("btn.textContent = S.interrupting ? '停止中…' : '⏸';", js)
        self.assertIn("$('btn-pause').addEventListener('click', interrupt);", js)
        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".btn-pause {", css)

    def test_terminal_resize_reflow_contract(self) -> None:
        """终端实测二：放大再缩小后内容显示不全。

        根因是缩小时每行多出来的格子被 `termRow(... c < t.cols)` 直接裁掉，
        以及行数变少后光标所在的底部行落到可视区之外。锁住四处修复：
        ① 列宽变化前先 `termReflow`（软折行，缓冲与历史一起重排）—— 多终端后
           它带会话参数（`t`），不再读模块级单例；
        ② 行数变少时把顶部溢出的行推进 history 并同步光标；
        ③ 隐藏 / 折叠（尺寸为 0）时不做重排 —— 按 0 宽折出来的行不可还原
           （多终端后这一条同时保护「非激活标签」：量不到尺寸就不动）。
        ④ 尺寸监听挂在 `#term-stage` 上（所有终端共用一个可视区），不是单个终端。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function termReflow(t, newCols)", js)
        self.assertIn("if (size.cols !== t.cols) termReflow(t, size.cols);", js)
        self.assertIn("if (host.clientWidth < 40 || host.clientHeight < 20) return null;", js)
        self.assertIn("while (t.buf.length > t.rows) {", js)
        self.assertIn("t.curRow = Math.max(0, t.curRow - 1);", js)
        self.assertIn("new ResizeObserver(() => termResizeActive()).observe(termStage);", js)

    def test_terminal_multi_session_contract(self) -> None:
        """多终端（不设上限）：锁四处，缺一就会退回「单终端」或「资源失控」。

        ① 后端默认不限（0 = 不限，显式配 VIBE_TERM_MAX 才拦）；
        ② 前端是会话注册表而不是单例，事件按 termId 分发 —— 否则第二个终端
           收不到任何输出；
        ③ 切标签只换显示不重建 DOM（缓冲/滚动位置都在）；
        ④ 刷新后跟后端对账（恢复活着的、撤掉已不在的）—— 否则要么丢标签、
           要么留下点进去没反应的死标签。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        with self.get("/index.html", token=self.token) as response:
            html = response.read().decode("utf-8")
        self.assertIn('id="term-tabs"', html)          # 标签条
        self.assertIn('id="term-stage"', html)          # 所有终端共用的可视区
        self.assertNotIn("id=\"term\"", html)           # 旧的单终端宿主已移除
        self.assertIn("const TERMS = new Map();", js)
        self.assertIn("'term.data': (ev) => {", js)     # 事件入口按 termId 分发
        self.assertIn("function termResizeActive()", js)
        self.assertIn("async function loadTermSessions()", js)
        self.assertIn("now - t.createdAt < 3000", js)   # 对账保护窗口（防误删新建的）
        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".term-tab {", css)
        self.assertIn(".term-interactive[hidden] { display: none; }", css)

    def test_terminal_follows_remote_workspace_contract(self) -> None:
        """远程工作区里终端跑在远端 —— 三条缺一条就会「文件在远端、终端却在本机」。

        ① `/api/term/open` 的远程判定与文件路由**同源**（`remote_workspace`），
           否则会出现最危险的错配：改的是远端、敲的命令却在本机执行；
        ② ssh 必须带 `-tt` 强制分配远端 TTY，否则 `vim`/`top` 直接退化；
        ③ 界面按 `kind="remote"` 说明这是远程终端，而不是把 `ssh` 当 shell 名显示。
        """
        source = (
            Path(__file__).resolve().parents[1] / "agent_core" / "host" / "session.py"
        ).read_text(encoding="utf-8")
        self.assertIn("remote_terminal_argv(", source)
        self.assertIn('extra_options=["-tt"]', source)
        self.assertIn(
            'cfg.remote_enabled and str(getattr(cfg, "remote_workspace", "") or "")', source
        )
        # 远程终端的自检宽限期必须比本地宽（ssh 握手 + 远端登录 > 2.5s）。
        self.assertIn("probe_seconds=REMOTE_TERM_PROBE_SECONDS if kind == \"remote\"", source)
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("res.kind === 'remote'", js)

    def test_terminal_follows_manifest_remote_workspace(self) -> None:
        """清单驱动的远端项目工作区（M11 §7）里，终端也必须落在设备上。

        这类工作区是「远端项目的门牌号」：目录里只有 `.vibe/remote.json`，
        代码全在设备上。只按 `remote_workspace`（M13 命名空间）判定远程会漏掉
        它 —— 用户会看到终端提示符停在 `C:\\Users\\...\\VibeWorkspaces\\<连接>`
        这种本机空目录里（用户实测反馈），敲下的命令全跑在本机。
        所以：① 有独立的清单解析入口；② 它接在 `remote_workspace` 分支之后
        作为 `elif`（两者不会互相覆盖）；③ 仍然复用同一套 ssh 加固 argv。
        """
        source = (
            Path(__file__).resolve().parents[1] / "agent_core" / "host" / "session.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def manifest_terminal_argv(", source)
        self.assertIn("manifest = manifest_terminal_argv(cfg)", source)
        self.assertIn("elif cfg.remote_enabled:", source)
        self.assertIn('extra_options=["-tt"]', source)

    def test_terminal_context_menu_and_clipboard_contract(self) -> None:
        """终端右键菜单 + Ctrl+C/X/V 剪贴板 —— 锁住二义性与降级链。

        ① 右键菜单复用 `openTreeMenu`（不另造一套浮层），条目带快捷键提示；
        ② `Ctrl+C` 有选区才复制、无选区交回原来的 `\\x03` 中断（终端里 Ctrl+C
           的第一语义永远是「中断」，不能因为加了剪贴板就把中断弄丢）；
        ③ 粘贴走「桌面壳窄桥 → navigator.clipboard → 隐藏 textarea」三级降级，
           否则浏览器里会时灵时不灵；
        ④ 数据进入 PTY 必须走 `termInsertText`（管道模式沿用本地行编辑）。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function openTermMenu(t, ev) {", js)
        self.assertIn("openTreeMenu(ev.clientX, ev.clientY, [", js)
        self.assertIn("function termSelectionText(t) {", js)
        self.assertIn("function termClipboardKey(t, event) {", js)
        self.assertIn("if (!termSelectionText(t)) return false;", js)
        self.assertIn("function termClipboardWrite(text) {", js)
        self.assertIn("window.vibeShell", js)
        self.assertIn("shell.clipboardWrite", js)
        self.assertIn("shell.clipboardRead", js)
        self.assertIn("navigator.clipboard.writeText", js)
        self.assertIn("document.execCommand('copy')", js)
        self.assertIn("function termInsertText(t, text) {", js)
        # 快捷键拦截必须排在「通用 ctrl 字符转换」之前，否则 Ctrl+V 会发 \x16。
        key_index = js.index("if (termClipboardKey(t, event)) return;")
        self.assertLess(key_index, js.index("event.ctrlKey", key_index))
        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".tree-menu-hint {", css)
        self.assertIn(".tree-menu-item:disabled {", css)

    def test_terminal_ime_contract(self) -> None:
        """终端中文输入 —— 锁住输入法组合链路。

        普通 div 收不到 composition 事件，所以每个终端宿主里必须有一个隐藏
        textarea 持有焦点；组合期间的按键让给 IME，组合结束后整段送 PTY。
        缺任一处，中文都只能「打不进去」。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("const ime = el('textarea', 'term-ime');", js)
        self.assertIn("function termFocus(t) {", js)
        self.assertIn("function termImeCommit(t, text) {", js)
        self.assertIn("addEventListener('compositionstart'", js)
        self.assertIn("addEventListener('compositionend'", js)
        # 组合期间的按键必须原样交给 IME，且要在管道/PTY 分支之前返回
        guard = js.index("event.isComposing || event.keyCode === 229")
        self.assertLess(guard, js.index("termPipeKey(t, event)", guard))
        self.assertLess(guard, js.index("termInput(t, '\\r')", guard))
        # 焦点落在 textarea 上时宿主仍要高亮
        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".term-ime {", css)
        self.assertIn(".term-interactive:focus-within {", css)

    def test_chat_markdown_contract(self) -> None:
        """对话区必须渲染 Markdown，而不是把 ** 之类的记号原样吐出来。

        ① 助手正文走 markdown.js 的 render()（与编辑器预览同一套零依赖渲染器）；
        ② 流式输出也渲染（节流重排），不能只在收口时才排版；
        ③ 围栏代码要过 highlight() 产出 tok-* span，样式里必须有对应颜色。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn(
            "import { render, isMarkdown, highlight, langForPath } from './markdown.js';", js
        )
        self.assertIn("function renderAnswerBody(node, text) {", js)
        self.assertIn("node.replaceChildren(render(text || ''));", js)
        self.assertIn("renderAnswerBody(entry.body, entry.text);", js)
        self.assertIn("S.streaming = { node, body, text: '', timer: 0 };", js)

        with self.get("/markdown.js", token=self.token) as response:
            md = response.read().decode("utf-8")
        self.assertIn("function highlight(codeEl, source, lang) {", md)
        self.assertIn("highlight(c, body.join('\\n'), lang);", md)
        self.assertIn("token('tok-kw'", md)

        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".tok-str {", css)
        self.assertIn(".block-answer .msg-md .md-preview {", css)
        self.assertIn(".block-answer .msg-md .md-preview pre code {", css)

    def test_code_view_highlight_contract(self) -> None:
        """文件代码视图也要着色（像 IDE 一样），而不是一整片同色文本。

        只读代码视图按后缀选语言，用与对话区同一套 highlighter 着色后再按行
        切开（保留行号 gutter 与 LSP 诊断装饰）；超大文件跳过着色防止卡顿。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn(
            "import { render, isMarkdown, highlight, langForPath } from './markdown.js';", js
        )
        self.assertIn("function highlightedRows(text, lang) {", js)
        self.assertIn("highlight(code, text, lang);", js)
        # 一行被切成多个 token span 后，Ctrl+Click 的列号仍须按整行偏移算
        self.assertIn("span.setStartAfter(gutter);", js)
        self.assertIn(
            "return { line: Number(gutter.dataset.line), char: span.toString().length };", js
        )
        self.assertIn(
            "text.length <= MAX_HIGHLIGHT_CHARS ? langForPath(data.path) : ''", js
        )

        with self.get("/markdown.js", token=self.token) as response:
            md = response.read().decode("utf-8")
        self.assertIn("export function highlight(codeEl, source, lang) {", md)
        self.assertIn("export function langForPath(path) {", md)
        self.assertIn("py: 'python'", md)

        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        # 着色类名不挂在 .md-preview 下，文件代码视图才能共用
        self.assertIn(".tok-kw {", css)

    def test_remote_workbench_tab_contract(self) -> None:
        """交互增强三：远程设备是可开可关的真页签，连接管理收进按需抽屉。

        锁四处缺一就会退回「只能打开」的旧形态：
        ① 页签名与关闭入口（#remote-close / 抽屉 / 面包屑）；
        ② app.js 有 openRemotePanel / closeRemotePanel，且 switchPanel 里的
           「已打开则关闭」「切其他面板则关闭」两条分支都在；
        ③ 页签只在 openRemotePanel 里创建一次（门控关闭时页签不存在）；
        ④ 样式类存在，否则新结构会挤成一团。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        for token in (
            'class="remote-head"',
            'id="remote-close"',
            'id="remote-conn-drawer"',
            'id="remote-drawer-backdrop"',
            'id="remote-path-crumb"',
            'id="remote-left"',
            'id="remote-resizer"',
            'id="remote-degraded-toggle"',
        ):
            self.assertIn(token, body)
        # 静态视图默认不激活：门控关闭时页签根本不存在，也不该抢先占住编辑器区
        self.assertIn('class="editor remote-wrap" id="remote-view"', body)

        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function openRemotePanel(", js)
        self.assertIn("function closeRemotePanel(", js)
        self.assertIn("S.editors.has('remote') && S.activeEditor === 'remote'", js)
        self.assertIn("$('remote-close').addEventListener('click', closeRemotePanel);", js)
        self.assertIn("$('remote-drawer-backdrop').addEventListener('click', closeRemoteDrawer);", js)
        self.assertIn("bindResizer('remote-resizer'", js)
        self.assertIn("function renderRemoteCrumb(", js)
        self.assertIn("openRemoteDrawer(", js)
        self.assertIn("ui.close-remote-panel", js)
        # 页签只在一个地方创建：重复创建会让关闭后重开出现两个「远程设备」页签
        self.assertEqual(js.count("makeTab('remote'"), 1)
        # 门控关闭（VIBE_REMOTE=0）：活动栏按钮隐藏，switchPanel 直接返回
        self.assertIn("remoteAct.hidden = !remoteOn;", js)
        self.assertIn("if (!S.remote.enabled) return;", js)

        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        for rule in (".remote-head {", ".remote-path-crumb {", ".remote-drawer {", ".remote-left {", ".remote-resizer {"):
            self.assertIn(rule, css)
        # 旧的三栏常驻结构已废弃，留着只会误导后续改动
        self.assertNotIn(".remote-conn-col {", css)
        self.assertNotIn(".remote-path {", css)

    def test_rollback_ui_contract(self) -> None:
        """P3：每条用户提问下的「回退到这条之前」+ 确认框 + 重铺视图。

        锁住四个缺一就会「点了没反应」的点：按钮在哪渲染（addUser 的 seq 参数）、
        确认框 DOM、确认后重铺（messages 带 seq）、执行中置灰（syncRollbackButtons）。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        for token in ('id="rb-modal"', 'id="rb-files"', 'id="btn-rb-confirm"'):
            self.assertIn(token, body)

        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        for token in (
            "function addUser(text, seq = 0)",
            "function syncRollbackButtons(",
            "function openRollbackConfirm(",
            "function confirmRollback(",
            "api.post('/api/rollback', { userSeq: seq })",
            "function addRemoteRollbackNote(",
            "addRemoteRollbackNote(res.remote);",
            "addUser(ev.prompt, ev.userSeq || 0)",
            "addUser(m.content, m.seq || 0)",
            "ui.close-rollback-confirm",
            "$('btn-rb-confirm').addEventListener('click', confirmRollback);",
        ):
            self.assertIn(token, js)

        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        for rule in (".btn-rollback-inline {", ".rb-files {", ".rb-notes {"):
            self.assertIn(rule, css)

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

    def test_tree_ref_menu_contract(self) -> None:
        """工作区树右键「加入对话」：三处契约缺一就会静默失效。

        1) 文件与目录节点都挂 contextmenu（旧逻辑只给带 index.html 的目录开菜单，
           文件右键会被浏览器原生菜单接管）；
        2) 目录映射成 `folder`、文件映射成 `file` —— 类型错了后端会按错误口径解析；
        3) 复用既有 `addRef` 引用通道，不新开一条数据通路。

        远端节点（M12/M13）走 `addRemoteRef`，值是 `@remote/<连接>/<路径>`：
        用户当前工作区常常是远端工作区，只覆盖本地树等于「右键还是原生菜单」。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function addTreeRef(", js)
        self.assertIn("label: '加入对话'", js)
        self.assertIn("node.type === 'dir' ? 'folder' : 'file'", js)
        self.assertIn("addRef(item);", js)
        # 远端三处入口：工作区树的远端子节点、远端根、远程设备面板
        self.assertIn("function addRemoteRef(", js)
        self.assertIn("`@remote/${connection}", js)
        self.assertEqual(js.count("addRemoteRef("), 4)  # 定义 1 + 调用点 3

    def test_ws_modal_remote_tab_contract(self) -> None:
        """切换工作区弹层的「远程目录」页签（M13 / ADR-042）锁住三层契约。

        这三段任何一处改名都会让页签静默失效：DOM 里没有 id、前端误把文件当候选、
        样式丢了弱化 —— 用户会以为文件也能切过去，切的时候才发现不行。
        """
        with self.get("/", token=self.token) as response:
            body = response.read().decode("utf-8")
        for element_id in (
            "ws-tab-local",
            "ws-tab-remote",
            "ws-local-body",
            "ws-remote-body",
            "ws-conn",
            "ws-remote-cwd",
            "ws-remote-dirs",
            "ws-remote-list",
        ):
            self.assertIn(f'id="{element_id}"', body)

        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        # 目录做成按钮（可下钻）；文件只列出行（同一层里两类同屏，但只有目录是候选）
        self.assertIn("/api/remote/tree", js)
        self.assertIn("entry.type === 'dir'", js)
        self.assertIn("function remoteDirRow(", js)
        self.assertIn("el('button', 'ws-dir')", js)
        self.assertIn("function remoteFileRow(", js)

        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".ws-file {", css)

    def test_remote_root_sub_normalized_contract(self) -> None:
        """M12 收尾：连接根层的 `./名称` 必须压平后再比较与传参（回归锁）。

        后端列目录在连接根层回的 `entry.path` 是 `./名称`（那一层的 rel 就是 `.`），
        而落库的 sub 已被 `store.normalize_sub()` 压平成 `名称`。前端若拿原样值比较，
        已加入的目录在「远程设备」右键菜单里仍显示「加入工作区」，点下去才报
        「该目录已在工作区」—— 实机实测就是这么暴露的。这条锁住：判「在不在工作区」、
        加入、移出三处都经过同一个压平函数，别再退回原样比较。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function normalizeRootSub(", js)
        self.assertIn("const wanted = normalizeRootSub(sub);", js)
        self.assertIn("sub: normalized", js)
        self.assertIn("sub: normalizeRootSub(sub)", js)

    def test_ws_remote_file_opens_in_editor_contract(self) -> None:
        """点工作区树里的远端文件要开在中间编辑区，不能把人切到远程设备面板。

        用户要的是「和本地文件一样」：同一套标签与代码视图，内容直接出现在中间。
        编辑/保存走 `/api/remote/save`（乐观锁），本机 LSP / 补全 / 静态预览
        这些只认本机路径的能力必须显式让位，否则会读到同名的本地文件。
        """
        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        # 工作区树远端文件节点 → 编辑器，而不是 openRemoteInPanel
        self.assertIn("else openRemoteInEditor(connection, e.path, !!e.link, row);", js)
        self.assertNotIn("openRemoteInPanel", js)
        self.assertIn("async function openRemoteInEditor(connection, path, isLink, row) {", js)
        # 标签路径用命名空间写法，与本地同名文件不冲突
        self.assertIn("const ns = `@remote/${connection}/${path}`;", js)
        self.assertIn("const id = `file:${ns}`;", js)
        self.assertIn("const data = await api.get('/api/remote/file', { path });", js)
        # 编辑/保存落远端，本机独占能力对远端文件让位
        self.assertIn("const data = await api.post('/api/remote/save', {", js)
        self.assertIn("if (entry.remote) return;", js)
        self.assertIn("if (!entry || entry.remote || !lspSupported(entry.path))", js)
        self.assertIn("previewBtn.hidden = !!entry.remote ||", js)

    def test_middle_stage_tabs_contract(self) -> None:
        """中间区三视图：浏览器 / 终端 / 编辑器都能在同一列里显示。

        浏览器视图内嵌本机跑起来的界面，地址校验仍走服务端
        `/api/preview/external`（门控 VIBE_PREVIEW_EXTERNAL）；终端视图复用
        底部那块终端并展开到全高，不复制第二份终端 DOM。
        """
        with self.get("/", token=self.token) as response:
            html = response.read().decode("utf-8")
        for chunk in (
            'id="stage-tabs"',
            'data-stage="browser"',
            'data-stage="terminal"',
            'data-stage="editor"',
            'id="browser-pane"',
            'id="browser-url"',
            'id="btn-browser-detected"',
        ):
            self.assertIn(chunk, html)

        with self.get("/app.js", token=self.token) as response:
            js = response.read().decode("utf-8")
        self.assertIn("function switchStage(name) {", js)
        self.assertIn("function browserOpen(raw) {", js)
        self.assertIn("function maybeLocalUrl(text) {", js)
        self.assertIn("api.get('/api/preview/external')", js)
        self.assertIn("api.post('/api/preview/external', { url })", js)
        # 终端视图 = 底部面板展开 + 页签切到终端，不新建第二份终端 DOM
        self.assertIn("toggleBottom(true);", js)
        self.assertIn("switchBottom('terminal');", js)
        # 只认本机回环 + 显式端口，与服务端的校验口径一致
        self.assertIn(r"/http:\/\/(?:localhost|127\.0\.0\.1):\d{1,5}", js)
        # 桌面壳里改用 webview 承载本机应用：X-Frame-Options / frame-ancestors 只
        # 约束 iframe，webview 是独立 WebContents 的顶层导航，能正常显示被拒嵌入的页面
        self.assertIn("customElements.get('webview')", js)
        self.assertIn("document.createElement('webview')", js)
        self.assertIn("did-fail-load", js)

        with self.get("/styles.css", token=self.token) as response:
            css = response.read().decode("utf-8")
        self.assertIn(".stage-tabs {", css)
        self.assertIn('.editor-stack[data-stage="terminal"] .bottom-panel {', css)
        self.assertIn("webview.browser-frame {", css)

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


class RollbackTests(unittest.TestCase):
    """P3 逐条回退：文件 + 对话一起回到某条用户提问之前。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "proj"
        self.ws.mkdir()
        (self.ws / "a.txt").write_text("one\n", encoding="utf-8", newline="")
        self.store = SessionStore(self.root / "sessions.db")
        self.host = HostServer(
            Config.load(self.ws),
            port=0,
            store=self.store,
            client=FakeClient(
                [
                    [("edit_file", {"path": "a.txt", "search": "one", "replace": "ONE"})],
                    "改好了。",
                    [("edit_file", {"path": "a.txt", "search": "ONE", "replace": "two"})],
                    "又改好了。",
                ]
            ),
        )
        self.host.start_background()
        # REVIEW 级编辑需要审批：夹具替人按下「批准」，聚焦回退链路本身
        self.host.session.agent.approver = lambda request: "approve"
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self.sid = self.host.session.config.session_id
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self.store.close()
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Vibe-Token": self.token},
            method="POST",
        )
        with self._opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        with self._opener.open(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _run(self, text: str) -> None:
        self._post("/api/run", {"text": text})
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.host.session.state == "idle":
                return
            time.sleep(0.02)
        raise AssertionError("会话没有回到空闲状态")

    def _ui_messages(self) -> list[dict]:
        return self._get(f"/api/sessions/{self.sid}/messages")["messages"]

    # ---- tests ---------------------------------------------------------
    def test_rollback_restores_files_and_messages(self) -> None:
        self._run("把 one 改成 ONE")
        self._run("再改成 two")
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "two\n")

        users = [m for m in self._ui_messages() if m["role"] == "user"]
        self.assertEqual([m["content"] for m in users], ["把 one 改成 ONE", "再改成 two"])
        # UI 通路必须带 seq（锚点）；默认形态不带
        self.assertEqual([m["seq"] for m in users], [1, 5])
        self.assertTrue(all("seq" not in m for m in self.store.get_messages(self.sid)))

        res = self._post("/api/rollback", {"userSeq": users[1]["seq"]})
        self.assertTrue(res["ok"])
        # 第二轮改动撤回，第一轮成果保留
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "ONE\n")
        self.assertEqual(res["truncated"], 4)  # 第二条提问 + 工具往返 + 回答
        self.assertEqual(
            [m["content"] for m in res["messages"] if m["role"] == "user"],
            ["把 one 改成 ONE"],
        )
        # 重建后的 Agent 不带 seq（界面字段不能跟着消息发给模型）
        self.assertTrue(all("seq" not in m for m in self.host.session.agent.messages))
        self.assertEqual(self.host.session.config.session_id, self.sid)

        # 再回退到第一条：第一轮的改动也撤掉，对话清空
        res2 = self._post("/api/rollback", {"userSeq": 1})
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "one\n")
        self.assertEqual([m for m in res2["messages"] if m["role"] == "user"], [])

    def test_rollback_conflicts_while_running(self) -> None:
        self._run("把 one 改成 ONE")
        self.host.session._state = "running"
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/rollback", {"userSeq": 1})
            self.assertEqual(ctx.exception.code, 409)
        finally:
            self.host.session._state = "idle"
        # 409 = 什么都没动
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "ONE\n")
        self.assertEqual(len(self.store.get_messages(self.sid)), 4)

    def test_rollback_rejects_bad_anchor(self) -> None:
        self._run("把 one 改成 ONE")
        for seq in (0, 999):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/rollback", {"userSeq": seq})
            self.assertEqual(ctx.exception.code, 400)
        # 非用户消息（assistant / tool）不能当锚点
        assistant_seq = next(
            m["seq"] for m in self._ui_messages() if m["role"] == "assistant"
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/rollback", {"userSeq": assistant_seq})
        self.assertEqual(ctx.exception.code, 400)
        # 三次拒绝之后文件与消息都原样
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "ONE\n")
        self.assertEqual(len(self.store.get_messages(self.sid)), 4)


if __name__ == "__main__":
    unittest.main()
