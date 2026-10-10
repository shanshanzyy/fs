"""语言服务器（E1–E6）测试。

这一层的风险不在「能不能连上 pyright」——本机装没装是环境问题。
真正的风险是三件**静默错**，都不报错、只出错：

1. **分帧**。LSP 的正文里含换行是常态。按行读会把一条消息拆成好几截，
   而症状是「偶尔解析失败」——最难复现的那类 bug。所以这里起一个**假语言
   服务器**（同进程的 Python 子进程），它按 `Content-Length` 精确回帧，
   把「客户端有没有按字节数读满」钉死。
2. **生命周期**。常驻进程 + 空闲不回收 = 用户机器上多出一串孤儿进程。
   假服务器把 `initialize`/`didOpen`/`didChange`/`shutdown`/`exit` 逐条写进
   日志文件，回收、重启、退出的行为因此可断言，而不是「看起来没崩」。
3. **降级**。没有语言服务器、或 `lsp_mode=off` 时必须返回
   `{available: false, reason}` 而不是抛异常 —— 界面要显示原因，不是 500。

假服务器用 `sys.executable` 启动，因此**任何装了 Python 的机器都能跑**，
不需要真的语言服务器，也就不存在「CI 上被跳过」的测试空洞。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.lsp import servers  # noqa: E402
from agent_core.lsp.manager import LspManager, _uri_to_path, set_manager  # noqa: E402
from agent_core.lsp.servers import ServerInfo  # noqa: E402
from agent_core.tools import build_default_registry  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

# --------------------------------------------------------------------------- 假语言服务器
# 只做我们实际用到的这几件事；每收一条消息写一行 JSONL 日志，
# 于是「有没有发 didOpen / didChange」「有没有优雅退出」都能被断言。
FAKE_SERVER = r'''
import json, os, sys

LOG, MODE, DEF_URI, RENAME_URI, MARKER = sys.argv[1:6]


def log(event, **extra):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": event, **extra}, ensure_ascii=False) + "\n")


def send(message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read_exactly(stream, size):
    chunks = []
    while size > 0:
        chunk = stream.read(size)
        if not chunk:
            return None
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def read_message():
    length = -1
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", "replace").partition(":")
        if key.strip().lower() == "content-length":
            length = int(value.strip())
    if length <= 0:
        return None
    body = read_exactly(sys.stdin.buffer, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def diagnostics_for(text):
    items = []
    for index, line in enumerate(text.split("\n")):
        if "bad" in line:
            items.append({
                "range": {"start": {"line": index, "character": 2},
                          "end": {"line": index, "character": 5}},
                "severity": 1, "message": "调用了不存在的名字", "source": "fake", "code": "E001",
            })
        if "TODO" in line:
            items.append({
                "range": {"start": {"line": index, "character": 0},
                          "end": {"line": index, "character": 4}},
                "severity": 2, "message": "遗留 TODO", "source": "fake",
            })
    return items


def handle(message):
    method = message.get("method")
    rid = message.get("id")
    params = message.get("params") or {}
    doc = params.get("textDocument") or {}

    if method == "initialize":
        log("initialize")
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "capabilities": {"textDocumentSync": 1, "definitionProvider": True,
                             "renameProvider": True},
            "serverInfo": {"name": "fake-lsp", "version": "0.1"},
        }})
    elif method == "initialized":
        log("initialized")
    elif method == "shutdown":
        log("shutdown")
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif method == "exit":
        log("exit")
        sys.exit(0)
    elif method == "textDocument/didOpen":
        log("didOpen", uri=doc.get("uri"), languageId=doc.get("languageId"),
            version=doc.get("version"))
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": doc.get("uri") or "",
                         "diagnostics": diagnostics_for(doc.get("text") or "")}})
    elif method == "textDocument/didChange":
        changes = params.get("contentChanges") or []
        text = (changes[0] or {}).get("text") if changes else ""
        log("didChange", uri=doc.get("uri"), version=doc.get("version"))
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": doc.get("uri") or "",
                         "diagnostics": diagnostics_for(text or "")}})
    elif method == "textDocument/definition":
        log("definition", uri=doc.get("uri"), position=params.get("position"))
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "uri": DEF_URI,
            "range": {"start": {"line": 4, "character": 1},
                      "end": {"line": 4, "character": 8}},
        }})
    elif method == "textDocument/rename":
        log("rename", newName=params.get("newName"))
        send({"jsonrpc": "2.0", "id": rid, "result": {"changes": {RENAME_URI: [
            {"range": {"start": {"line": 2, "character": 8},
                       "end": {"line": 2, "character": 13}}, "newText": params.get("newName")},
            {"range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 0, "character": 5}}, "newText": params.get("newName")},
        ]}}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": None})


# 崩溃模拟：crash_once 首次退出（用来验证「重启一次」），always_crash 每次都退。
if MODE == "always_crash" or (MODE == "crash_once" and not os.path.exists(MARKER)):
    if MODE == "crash_once":
        open(MARKER, "a").close()
    log("crash")
    sys.exit(3)

while True:
    message = read_message()
    if message is None:
        log("eof")
        break
    handle(message)
'''


class _Clock:
    """可拨动的单调时钟。空闲回收是按时间判定的，真实 sleep 太慢也不稳定。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LspTestCase(unittest.TestCase):
    """共同夹具：临时工作区 + 假语言服务器脚本 + 注入式 manager。"""

    MODE = "ok"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self._managers: list[LspManager] = []
        self.script = self.ws / "fake_lsp.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8", newline="")
        self.log = self.ws / "fake_lsp.log"
        self.marker = self.ws / "fake_lsp.crash"
        self.target = self.ws / "target.py"
        self.target.write_text("def helper():\n    pass\n", encoding="utf-8", newline="")

        (self.ws / "diag.py").write_text("bad = 1\nTODO fix\nok = 2\n", encoding="utf-8", newline="")
        (self.ws / "defs.py").write_text("import target\ntarget.helper()\n", encoding="utf-8", newline="")
        (self.ws / "ren.py").write_text(
            "alpha = 1\nbeta = 2\ngamma = alpha\n", encoding="utf-8", newline=""
        )

    def tearDown(self) -> None:
        # 顺序要紧：unittest 的 tearDown **先于** addCleanup 执行，而假语言服务器
        # 子进程的 cwd 就在临时工作区里。若先 rmtree，会因目录被占用而失败。
        # 所以这里显式收尾：先停子进程，再删目录。
        host = getattr(self, "host", None)
        if host is not None:
            host.shutdown()
        for manager in getattr(self, "_managers", []):
            manager.shutdown()
        set_manager(None)
        self._tmp.cleanup()

    # ---- 夹具工具 ---------------------------------------------------------
    def server_info(self) -> ServerInfo:
        return ServerInfo(
            language="python",
            available=True,
            argv=(
                sys.executable,
                str(self.script),
                str(self.log),
                self.MODE,
                self.target.as_uri(),
                (self.ws / "ren.py").as_uri(),
                str(self.marker),
            ),
            command="fake-lsp",
        )

    def resolver(self, path, *, language: str = "") -> ServerInfo:
        """永远返回假服务器 —— 探测本身归 `LspServersTests` 管。"""
        return self.server_info()

    def manager(self, *, idle_seconds: int = 300, clock=None, timeout: float = 10.0) -> LspManager:
        manager = LspManager(
            idle_seconds=idle_seconds,
            resolver=self.resolver,
            clock=clock or time.monotonic,
            request_timeout=timeout,
        )
        # 由 tearDown 统一收尾（见那里的顺序说明），不用 addCleanup
        self._managers.append(manager)
        return manager

    def events(self) -> list[str]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)["event"]
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def records(self, event: str) -> list[dict]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["event"] == event
        ]

    def config(self, *, mode: str = "on", **overrides) -> Config:
        base = Config.load(self.ws)
        return replace(base, lsp_mode=mode, **overrides)


# --------------------------------------------------------------------------- E2 协议帧
class ProtocolFrameTests(LspTestCase):
    """JSON-RPC over stdio：分帧、配对、通知切分。"""

    def test_diagnostics_parsed_from_published_notification(self) -> None:
        """诊断是「服务器主动推」的通知，必须在没有请求等待时也能被读到。"""
        manager = self.manager()
        result = manager.diagnostics(self.config(), self.ws / "diag.py", "diag.py")

        self.assertTrue(result["available"])
        self.assertEqual(result["language"], "python")
        self.assertEqual(result["counts"]["error"], 1)
        self.assertEqual(result["counts"]["warning"], 1)
        first, second = result["diagnostics"][:2]
        self.assertEqual((first["line"], first["char"]), (0, 2))
        self.assertEqual(first["severity"], "error")
        self.assertEqual(first["message"], "调用了不存在的名字")
        self.assertEqual(first["code"], "E001")
        self.assertEqual((second["line"], second["severity"]), (1, "warning"))

    def test_frames_survive_multiline_payload(self) -> None:
        """正文里含换行/中文（UTF-8 多字节）时长度按**字节**算，不是字符数。

        源文件内容整段塞在 `didOpen` 的 text 里，含换行与中文；假服务器按
        `len(body)` 回帧，客户端只有按字节读满才不会切歪。
        """
        (self.ws / "diag.py").write_text(
            "bad = 1\n# 中文注释换行\n中文 = '值'\n", encoding="utf-8", newline=""
        )
        manager = self.manager()
        result = manager.diagnostics(self.config(), self.ws / "diag.py", "diag.py")

        self.assertTrue(result["available"])
        self.assertEqual(result["diagnostics"][0]["line"], 0)
        opened = self.records("didOpen")
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0]["uri"].endswith("diag.py"))

    def test_did_change_on_second_query(self) -> None:
        """同一文件内容变了走 didChange；没变才不该重复发。"""
        manager = self.manager()
        config = self.config()
        manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertEqual(self.events().count("didOpen"), 1)

        manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertEqual(self.events().count("didChange"), 0, "内容未变不该发 didChange")

        (self.ws / "diag.py").write_text("bad = 2\n", encoding="utf-8", newline="")
        manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertEqual(self.events().count("didChange"), 1)

    def test_definition_and_rename_requests_are_paired(self) -> None:
        """请求要按 id 配对回来 —— 两个连续请求各自拿到自己的结果。"""
        manager = self.manager()
        config = self.config()

        definition = manager.definition(config, self.ws / "defs.py", "defs.py", 1, 7)
        self.assertTrue(definition["available"])
        location = definition["locations"][0]
        self.assertEqual(location["path"], "target.py")
        self.assertFalse(location["outside"])
        self.assertEqual((location["line"], location["char"]), (4, 1))

        rename = manager.rename(config, self.ws / "ren.py", "ren.py", 0, 3, "delta")
        self.assertTrue(rename["available"])
        edits = rename["edits"][0]["edits"]
        self.assertEqual(len(edits), 2)
        # 落盘侧依赖「从后往前」的顺序，这里把它钉住
        self.assertGreater(edits[0]["startLine"], edits[1]["startLine"])

    def test_uri_to_path_rejects_foreign_scheme(self) -> None:
        self.assertEqual(_uri_to_path("untitled:Untitled-1"), None)
        self.assertEqual(_uri_to_path(""), None)
        path = _uri_to_path((self.ws / "diag.py").as_uri())
        self.assertIsNotNone(path)


# --------------------------------------------------------------------------- E3 生命周期
class LifecycleTests(LspTestCase):
    """启停：握手、空闲回收、崩溃重启一次、退出收尾。"""

    def test_handshake_and_graceful_shutdown(self) -> None:
        manager = self.manager()
        manager.diagnostics(self.config(), self.ws / "diag.py", "diag.py")
        events = self.events()
        self.assertEqual(events[0], "initialize")
        self.assertIn("initialized", events)
        self.assertIn("didOpen", events)
        self.assertEqual(len(manager.running()), 1)

        manager.shutdown()
        events = self.events()
        self.assertIn("shutdown", events)
        self.assertEqual(events[-1], "exit")
        self.assertEqual(manager.running(), [])

    def test_instance_reused_across_queries(self) -> None:
        """同一 `(工作区, 语言)` 复用同一个进程 —— 不能每问一次起一个。"""
        manager = self.manager()
        config = self.config()
        manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        manager.diagnostics(config, self.ws / "defs.py", "defs.py")
        self.assertEqual(self.events().count("initialize"), 1)
        self.assertEqual(len(manager.running()), 1)

    def test_idle_instance_is_reaped(self) -> None:
        """空闲超过 `lsp_idle_seconds` 就 shutdown —— 不常驻。"""
        clock = _Clock()
        manager = self.manager(idle_seconds=1, clock=clock)
        config = self.config(lsp_idle_seconds=1)

        manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertEqual(len(manager.running()), 1)

        clock.advance(5)
        manager.diagnostics(config, self.ws / "diag.py", "diag.py")

        self.assertEqual(self.events().count("exit"), 1, "空闲实例必须被回收")
        self.assertEqual(self.events().count("initialize"), 2, "回收后按需重起")
        self.assertEqual(len(manager.running()), 1)

    def test_restart_once_after_crash(self) -> None:
        """崩溃后自动重启一次：多数情况是首次索引把进程搞挂了。"""
        self.MODE = "crash_once"
        manager = self.manager()
        result = manager.diagnostics(self.config(), self.ws / "diag.py", "diag.py")

        self.assertTrue(result["available"], "重启一次后应当恢复")
        self.assertEqual(self.events().count("crash"), 1, "只崩了第一个进程")
        self.assertEqual(self.events().count("initialize"), 1)
        again = manager.diagnostics(self.config(), self.ws / "diag.py", "diag.py")
        self.assertTrue(again["available"], "恢复后不该被标记为不可用")

    def test_broken_after_two_crashes_and_no_more_spawn(self) -> None:
        """重启一次仍失败 → 标记不可用，且**不再**spawn（否则每次请求都卡）。"""
        self.MODE = "always_crash"
        manager = self.manager()
        config = self.config()

        first = manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertFalse(first["available"])
        self.assertIn("调用失败", first["reason"])
        spawned = self.events().count("crash")
        self.assertEqual(spawned, 2, "首次请求只重试一次")

        second = manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertFalse(second["available"])
        self.assertIn("已标记不可用", second["reason"])
        self.assertEqual(self.events().count("crash"), spawned, "标记后不该再起进程")

        manager.reset_availability()
        third = manager.diagnostics(config, self.ws / "diag.py", "diag.py")
        self.assertIn("调用失败", third["reason"], "重置后应当重新尝试")
        self.assertEqual(self.events().count("crash"), spawned + 2)


# --------------------------------------------------------------------------- E1 探测
class LspServersTests(unittest.TestCase):
    """探测表：`shutil.which` 命中就用、没命中给原因；argv 里不许有用户输入。"""

    def test_language_buckets_by_suffix(self) -> None:
        self.assertEqual(servers.language_for("a.py"), "python")
        self.assertEqual(servers.language_for("a.pyi"), "python")
        self.assertEqual(servers.language_for("a.ts"), "typescript")
        self.assertEqual(servers.language_for("a.js"), "javascript")
        self.assertEqual(servers.language_for("a.go"), "go")
        self.assertEqual(servers.language_for("a.rs"), "rust")
        self.assertEqual(servers.language_for("a.md"), "")
        self.assertEqual(servers.language_for("noext"), "")

    def test_resolve_uses_which_result_verbatim(self) -> None:
        """命中的绝对路径直接进 argv —— 不拼接、不改写。"""
        with mock.patch("shutil.which", return_value=r"C:\tools\gopls.exe"):
            info = servers.resolve("main.go")
        self.assertTrue(info.available)
        self.assertEqual(info.language, "go")
        self.assertEqual(info.command, "gopls")
        self.assertEqual(info.argv, (r"C:\tools\gopls.exe",))

    def test_resolve_prefers_first_candidate(self) -> None:
        """python 桶按 pyright → pylsp 的优先级取第一个命中的。"""
        def fake_which(name: str):
            return "/usr/bin/pylsp" if name == "pylsp" else None

        with mock.patch("shutil.which", side_effect=fake_which):
            info = servers.resolve("a.py")
        self.assertTrue(info.available)
        self.assertEqual(info.argv, ("/usr/bin/pylsp",))

    def test_resolve_fallback_carries_args(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/pyright-langserver"):
            info = servers.resolve("a.py")
        self.assertEqual(info.argv, ("/usr/bin/pyright-langserver", "--stdio"))

    def test_missing_server_reports_human_reason(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            info = servers.resolve("a.unknownlang")
            self.assertFalse(info.available)
            self.assertTrue(info.reason)

            missing = servers.resolve("a.go")
        self.assertFalse(missing.available)
        self.assertIn("gopls", missing.reason)
        self.assertIn("未安装或不在 PATH 中", missing.reason)

    def test_unknown_suffix_never_touches_path(self) -> None:
        """不认识的后缀连 which 都不该查，更不该进 argv。"""
        with mock.patch("shutil.which") as which:
            info = servers.resolve("notes.txt")
        which.assert_not_called()
        self.assertFalse(info.available)
        self.assertEqual(info.argv, ())
        self.assertTrue(info.reason)

    def test_user_path_never_leaks_into_argv(self) -> None:
        """用户名/路径里的怪字符串不能出现在命令里（argv 只来自常量+which）。"""
        weird = "…; rm -rf / ;.py"
        with mock.patch("shutil.which", return_value="/usr/bin/pyright-langserver"):
            info = servers.resolve(weird)
        self.assertTrue(info.available)
        self.assertEqual(info.argv, ("/usr/bin/pyright-langserver", "--stdio"))
        self.assertNotIn(weird, " ".join(info.argv))

    def test_detect_all_covers_every_language(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            listing = servers.detect_all()
            self.assertFalse(servers.any_available())
        self.assertEqual(
            {item.language for item in listing},
            {"python", "javascript", "typescript", "go", "rust"},
        )
        self.assertTrue(all(item.reason for item in listing))


# --------------------------------------------------------------------------- E4 配置
class ConfigTests(unittest.TestCase):
    """`VIBE_LSP` / `VIBE_LSP_IDLE` 的接线与校验。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults(self) -> None:
        config = Config.load(self.ws)
        self.assertEqual(config.lsp_mode, "auto")
        self.assertEqual(config.lsp_idle_seconds, 300)

    def test_env_overrides(self) -> None:
        with mock.patch.dict(
            "os.environ", {"VIBE_LSP": "OFF", "VIBE_LSP_IDLE": "45"}, clear=False
        ):
            config = Config.load(self.ws)
        self.assertEqual(config.lsp_mode, "off")
        self.assertEqual(config.lsp_idle_seconds, 45)

    def test_bad_mode_is_rejected(self) -> None:
        with mock.patch.dict("os.environ", {"VIBE_LSP": "maybe"}, clear=False):
            with self.assertRaises(Exception) as ctx:
                Config.load(self.ws)
        self.assertIn("VIBE_LSP", str(ctx.exception))


# --------------------------------------------------------------------------- E5 会话层
class SessionLspTests(LspTestCase):
    """五个端点的会话层：诊断/定义/重命名预览与落盘、降级。"""

    def setUp(self) -> None:
        super().setUp()
        self.manager_ = self.manager()
        set_manager(self.manager_)

    def _session(self, **overrides) -> Session:
        return Session(self.config(**overrides), client=FakeClient([]))

    def test_status_payload(self) -> None:
        with mock.patch("agent_core.lsp.servers.shutil.which", return_value="/usr/bin/fake"):
            status = self._session().lsp_status()
        self.assertEqual(status["mode"], "on")
        self.assertEqual(status["idleSeconds"], 300)
        self.assertTrue(status["available"])
        self.assertEqual(
            {item["language"] for item in status["servers"]},
            {"python", "javascript", "typescript", "go", "rust"},
        )

    def test_diagnostics_line_is_one_based(self) -> None:
        """LSP 是 0 基行号，接口对前端是 1 基 —— 与 gutter / rewrite 同一口径。"""
        data = self._session().lsp_diagnostics("diag.py")
        self.assertTrue(data["available"])
        self.assertEqual(data["path"], "diag.py")
        self.assertEqual(data["server"], "fake-lsp")
        self.assertEqual(data["counts"]["error"], 1)
        self.assertEqual(data["diagnostics"][0]["line"], 1)
        self.assertEqual(data["diagnostics"][1]["line"], 2)

    def test_definition_off_by_one_translated(self) -> None:
        data = self._session().lsp_definition("defs.py", 2, 7)
        self.assertTrue(data["available"])
        location = data["locations"][0]
        self.assertEqual(location["path"], "target.py")
        self.assertEqual(location["line"], 5, "内部 0 基 +1 回到前端 1 基")
        self.assertFalse(location["outside"])
        # 发给服务器的是 0 基行号
        self.assertEqual(self.records("definition")[0]["position"]["line"], 1)

    def test_rename_preview_then_apply_writes_files(self) -> None:
        session = self._session()
        preview = session.lsp_rename_preview("ren.py", 1, 3, "delta")
        self.assertTrue(preview["available"])
        self.assertEqual(preview["edits"], 2)
        self.assertFalse(preview["noChange"])
        self.assertEqual(len(preview["files"]), 1)
        self.assertEqual(preview["files"][0]["path"], "ren.py")
        self.assertIn("+gamma = delta", preview["files"][0]["diff"])
        # 预览绝不落盘
        self.assertEqual(
            (self.ws / "ren.py").read_text(encoding="utf-8"), "alpha = 1\nbeta = 2\ngamma = alpha\n"
        )

        applied = session.lsp_rename_apply(preview["token"])
        self.assertTrue(applied["changed"])
        self.assertEqual(applied["paths"], ["ren.py"])
        self.assertEqual(
            (self.ws / "ren.py").read_text(encoding="utf-8"),
            "delta = 1\nbeta = 2\ngamma = delta\n",
        )
        # 同一 token 只能落一次
        with self.assertRaises(ValueError):
            session.lsp_rename_apply(preview["token"])

    def test_rename_token_is_required_and_single_use(self) -> None:
        session = self._session()
        with self.assertRaises(ValueError):
            session.lsp_rename_apply("nonexistent")

    def test_rename_preview_rejects_bad_names(self) -> None:
        session = self._session()
        for bad in ("", "   ", "has space", "x" * 80):
            with self.assertRaises(ValueError):
                session.lsp_rename_preview("ren.py", 1, 3, bad)

    def test_rename_records_audit_without_name(self) -> None:
        """审计只记元数据（ADR-006）：新名称留指纹与长度，不留正文。"""
        session = self._session()
        preview = session.lsp_rename_preview("ren.py", 1, 3, "delta")
        session.lsp_rename_apply(preview["token"])

        lines = [
            json.loads(line)
            for line in session.agent.audit.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = [entry["event"] for entry in lines]
        self.assertIn("lsp.rename.preview", events)
        self.assertIn("lsp.rename.apply", events)
        entries = [entry for entry in lines if entry["event"].startswith("lsp.rename.")]
        self.assertTrue(entries)
        for entry in entries:
            self.assertNotIn("delta", json.dumps(entry, ensure_ascii=False))
        preview_entry = next(e for e in entries if e["event"] == "lsp.rename.preview")
        self.assertEqual(preview_entry["new_name_chars"], 5)
        self.assertTrue(preview_entry["new_name_digest"])
        apply_entry = next(e for e in entries if e["event"] == "lsp.rename.apply")
        self.assertEqual(apply_entry["strategy"], "lsp-rename")

    def test_rename_apply_conflicts_when_stale(self) -> None:
        """预览之后文件被改过 → 整批拒收（409 语义），不写一半。"""
        session = self._session()
        preview = session.lsp_rename_preview("ren.py", 1, 3, "delta")
        (self.ws / "ren.py").write_text("someone else\n", encoding="utf-8", newline="")
        with self.assertRaises(Exception) as ctx:
            session.lsp_rename_apply(preview["token"])
        self.assertIn("已被改动", str(ctx.exception))
        self.assertEqual(
            (self.ws / "ren.py").read_text(encoding="utf-8"), "someone else\n"
        )

    def test_mode_off_degrades_without_spawning(self) -> None:
        """`lsp_mode=off`：三个查询都返回 `{available: false, reason}`，不起进程。"""
        session = self._session(mode="off")
        data = session.lsp_diagnostics("diag.py")
        self.assertFalse(data["available"])
        self.assertIn("关闭", data["reason"])
        self.assertEqual(data["diagnostics"], [])

        definition = session.lsp_definition("defs.py", 1, 0)
        self.assertFalse(definition["available"])
        self.assertIn("关闭", definition["reason"])

        rename = session.lsp_rename_preview("ren.py", 1, 3, "delta")
        self.assertFalse(rename["available"])
        self.assertEqual(rename["files"], [])

        self.assertFalse(session.lsp_status()["available"])
        self.assertEqual(self.events(), [], "off 模式下不该起任何语言服务器")

    def test_unavailable_server_degrades(self) -> None:
        """探测不到服务器时同样只降级不报错。"""
        self.manager_._resolver = lambda path, **kwargs: ServerInfo(
            language="go", available=False, reason="未找到 go 语言服务器（gopls）"
        )
        data = self._session().lsp_diagnostics("diag.py")
        self.assertFalse(data["available"])
        self.assertIn("gopls", data["reason"])

    def test_path_outside_workspace_is_rejected(self) -> None:
        session = self._session()
        with self.assertRaises(Exception):
            session.lsp_diagnostics("../outside.py")


# --------------------------------------------------------------------------- E6 工具注册
class DiagnosticsToolTests(unittest.TestCase):
    """`lsp_diagnostics` 只在真的有语言服务器时进工具清单。"""

    def _names(self, registry) -> set[str]:
        return set(registry.names())

    def test_registered_only_when_available(self) -> None:
        with mock.patch("agent_core.lsp.servers.any_available", return_value=True):
            names = self._names(build_default_registry())
        self.assertIn("lsp_diagnostics", names)

    def test_absent_when_no_server(self) -> None:
        with mock.patch("agent_core.lsp.servers.any_available", return_value=False):
            names = self._names(build_default_registry())
        self.assertNotIn("lsp_diagnostics", names)

    def test_absent_when_mode_off(self) -> None:
        with mock.patch("agent_core.lsp.servers.any_available", return_value=True):
            names = self._names(build_default_registry(lsp=False))
        self.assertNotIn("lsp_diagnostics", names)

    def test_spec_is_safe_and_needs_path(self) -> None:
        from agent_core.sandbox import Risk
        from agent_core.tools.diagnostics import LSP_DIAGNOSTICS_SPEC

        self.assertEqual(LSP_DIAGNOSTICS_SPEC.risk, Risk.SAFE)
        self.assertEqual(LSP_DIAGNOSTICS_SPEC.parameters["required"], ["path"])


# --------------------------------------------------------------------------- E5 HTTP 路由
class LspHttpTests(LspTestCase):
    """`/api/lsp/*` 的路由与状态码（逻辑在会话层，这里只钉接线）。"""

    def setUp(self) -> None:
        super().setUp()
        self.manager_ = self.manager()
        set_manager(self.manager_)
        self.host = HostServer(
            self.config(), port=0, client=FakeClient([["list_dir", {}], "看完了。"])
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _get(self, path: str):
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        return self._opener.open(request, timeout=15)

    def _post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )
        request.add_header("X-Vibe-Token", self.token)
        request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=15)

    def _code(self, method: str, path: str, body: dict | None = None) -> int:
        try:
            if method == "GET":
                with self._get(path) as response:
                    return response.status
            with self._post(path, body or {}) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_status_endpoint(self) -> None:
        with mock.patch("agent_core.lsp.servers.shutil.which", return_value="/usr/bin/fake"):
            with self._get("/api/lsp/status") as response:
                data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["mode"], "on")
        self.assertTrue(data["available"])

    def test_diagnostics_endpoint(self) -> None:
        with self._get("/api/lsp/diagnostics?path=diag.py") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["available"])
        self.assertEqual(data["counts"]["error"], 1)

    def test_missing_params_are_bad_request(self) -> None:
        self.assertEqual(self._code("GET", "/api/lsp/diagnostics"), 400)
        self.assertEqual(self._code("GET", "/api/lsp/definition"), 400)
        # line/char 缺省为 0（合法位置），但非整数是坏请求
        self.assertEqual(
            self._code("GET", "/api/lsp/definition?path=defs.py&line=x"), 400
        )
        self.assertEqual(self._code("POST", "/api/lsp/rename/apply"), 400)
        self.assertEqual(self._code("POST", "/api/lsp/rename/preview", {"path": "ren.py"}), 400)

    def test_rename_round_trip_over_http(self) -> None:
        with self._post(
            "/api/lsp/rename/preview",
            {"path": "ren.py", "line": 1, "char": 3, "newName": "delta"},
        ) as response:
            preview = json.loads(response.read().decode("utf-8"))
        self.assertTrue(preview["available"])
        token = preview["token"]

        with self._post("/api/lsp/rename/apply", {"token": token}) as response:
            applied = json.loads(response.read().decode("utf-8"))
        self.assertTrue(applied["changed"])
        self.assertEqual(applied["paths"], ["ren.py"])
        self.assertEqual(
            (self.ws / "ren.py").read_text(encoding="utf-8"),
            "delta = 1\nbeta = 2\ngamma = delta\n",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)