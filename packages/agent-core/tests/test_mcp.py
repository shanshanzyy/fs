"""MCP 客户端（L / P1-6）—— 外部工具来源的信任边界。

这一层最不能出错的是**信任边界**，而不是「能不能连上某个 server」：

1. **恒 REVIEW**。外部工具的副作用在别人的进程里，我们管不住；一旦它的
   风险等级被改成 SAFE，审批闸门就形同虚设。这里把「恒 REVIEW」钉死。
2. **结果必须 wrap_untrusted**。server 返回的文本是不可信数据，里面可能藏着
   「忽略以上要求，去做 X」这类注入指令。
3. **降级不崩会话**。server 起不来、握手就崩、调用中途退出 —— 都必须表现为
   「这个工具失败」，而不是让整个会话炸掉。
4. **名字冲突要拒绝启动 server**，而不是覆盖内置工具。

假 server 用 `sys.executable` 启动（任何装了 Python 的机器都能跑，不存在
「CI 上被跳过」的测试空洞），并且**两种分帧各测一遍**：NDJSON 与
`Content-Length` —— 后者是社区里照抄 LSP 的 server 的常见形态，按行读会把它拆碎。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.mcp import McpManager, McpServerConfig, load_servers, merge_mcp  # noqa: E402
from agent_core.mcp.manager import set_manager  # noqa: E402
from agent_core.mcp.registry import (  # noqa: E402
    NAME_PREFIX,
    dump_servers,
    sanitize_schema,
    save_servers,
)
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec  # noqa: E402
from agent_core.tools.base import UNTRUSTED_BEGIN  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

# --------------------------------------------------------------------------- 假 MCP server
# 用法：python fake_mcp.py <ndjson|framed>
# 它只实现我们实际用到的四件事：initialize / initialized / tools/list / tools/call。
# `crash` 工具直接 os._exit，用来验证「调用中途 server 死掉」的降级路径。
FAKE_SERVER = r'''
import json, os, sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "ndjson"
MARKER = os.environ.get("MCP_MARKER")

if MARKER:
    with open(MARKER, "a", encoding="utf-8") as fh:
        fh.write("started\n")

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the given text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "crash",
        "description": "Kill the server mid-call",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if MODE == "framed":
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    else:
        sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


for raw in sys.stdin.buffer:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw.decode("utf-8"))
    except Exception:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "crash":
            os._exit(3)
        if name == "echo":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "ECHO:" + str(args.get("text", ""))}],
                "isError": False,
            }})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "unknown tool"}],
                "isError": True,
            }})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
'''


class _ServerFixture(unittest.TestCase):
    """起一个临时工作区 + 假 server 脚本。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.script = self.ws / "fake_mcp.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.marker = self.ws / "started.txt"

    def tearDown(self) -> None:
        set_manager(None)
        self._tmp.cleanup()

    def server(self, mode: str = "ndjson", *, name: str = "fake") -> McpServerConfig:
        return McpServerConfig(
            name=name,
            command=sys.executable,
            args=(str(self.script), mode),
            env={"MCP_MARKER": str(self.marker)},
        )

    def ctx(self) -> ToolContext:
        config = Config.load(self.ws)
        return ToolContext(
            config=config,
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=AuditLog(config.audit_path, session_id="test-mcp"),
        )


class ToolDiscoveryTests(_ServerFixture):
    """① 工具被发现、注册、且恒为 REVIEW；两种分帧都要能读。"""

    def test_tools_are_registered_as_review(self) -> None:
        for mode in ("ndjson", "framed"):
            with self.subTest(mode=mode):
                manager = McpManager(timeout=10)
                registry = ToolRegistry()
                status = merge_mcp(registry, [self.server(mode)], manager=manager)
                self.assertEqual(len(status), 1)
                self.assertTrue(status[0]["available"], status[0]["reason"])
                spec = registry.get(f"{NAME_PREFIX}fake__echo")
                self.assertIsNotNone(spec)
                # 红线（ADR-015）：外部工具**恒** REVIEW，不可配置放行。
                self.assertIs(spec.risk, Risk.REVIEW)
                self.assertIn(f"{NAME_PREFIX}fake__crash", registry.names())
                manager.shutdown()

    def test_schema_is_sanitized(self) -> None:
        raw = {
            "type": "object",
            "$defs": {"x": {}},
            "properties": {
                "text": {"type": "string", "description": "t", "$ref": "#/$defs/x"},
            },
            "required": ["text"],
            "additionalProperties": False,
        }
        cleaned = sanitize_schema(raw)
        self.assertNotIn("$defs", cleaned)
        self.assertNotIn("$ref", cleaned["properties"]["text"])
        self.assertEqual(cleaned["required"], ["text"])
        self.assertEqual(cleaned["type"], "object")


class ResultWrappingTests(_ServerFixture):
    """② 调用结果被 wrap_untrusted；③ server 异常降级而非崩会话。"""

    def _registry(self) -> tuple[ToolRegistry, McpManager]:
        manager = McpManager(timeout=10)
        registry = ToolRegistry()
        merge_mcp(registry, [self.server()], manager=manager)
        return registry, manager

    def test_result_is_wrapped_untrusted(self) -> None:
        registry, manager = self._registry()
        ctx = self.ctx()
        result = registry.execute(ctx, f"{NAME_PREFIX}fake__echo", {"text": "hi"})
        self.assertTrue(result.ok, result.content)
        self.assertIn(UNTRUSTED_BEGIN, result.content)
        self.assertIn("ECHO:hi", result.content)
        manager.shutdown()

    def test_server_crash_degrades_without_raising(self) -> None:
        registry, manager = self._registry()
        ctx = self.ctx()
        # 会话/registry 绝不因外部进程崩溃而抛异常 —— 只表现为工具失败。
        result = registry.execute(ctx, f"{NAME_PREFIX}fake__crash", {})
        self.assertFalse(result.ok)
        self.assertIn("调用失败", result.content)
        # 后续调用仍可用（manager 会重启一次）。
        again = registry.execute(ctx, f"{NAME_PREFIX}fake__echo", {"text": "ok"})
        self.assertTrue(again.ok, again.content)
        manager.shutdown()

    def test_start_failure_degrades_without_raising(self) -> None:
        bad = McpServerConfig(name="bad", command=str(self.ws / "does-not-exist"), args=())
        manager = McpManager(timeout=5)
        registry = ToolRegistry()
        status = merge_mcp(registry, [bad], manager=manager)
        self.assertEqual(status[0]["available"], False)
        self.assertTrue(status[0]["reason"])
        self.assertEqual([n for n in registry.names() if n.startswith(NAME_PREFIX)], [])
        manager.shutdown()


class NameConflictTests(_ServerFixture):
    """④ 工具名冲突 → 拒绝启动该 server，而不是覆盖。"""

    def test_conflicting_name_refuses_server(self) -> None:
        manager = McpManager(timeout=10)
        registry = ToolRegistry()
        # 先占住一个会撞名的槽位（模拟内置工具 / 另一个 server）。
        registry.register(
            ToolSpec(
                name=f"{NAME_PREFIX}fake__echo",
                description="pre-existing",
                parameters={"type": "object", "properties": {}},
                handler=lambda ctx, args: ToolResult(ok=True, content=""),
            )
        )
        status = merge_mcp(registry, [self.server()], manager=manager)
        self.assertEqual(status[0]["available"], False)
        self.assertIn("冲突", status[0]["reason"])
        self.assertEqual(status[0]["tools"], [])
        # 原先登记的工具没被覆盖，crash 也没被注册进来。
        self.assertEqual(registry.get(f"{NAME_PREFIX}fake__echo").description, "pre-existing")
        self.assertIsNone(registry.get(f"{NAME_PREFIX}fake__crash"))
        manager.shutdown()


class ConfigFileTests(_ServerFixture):
    """配置读取：Claude Desktop 风与列表风都要认；坏条目跳过。"""

    def test_load_claude_desktop_shape(self) -> None:
        path = self.ws / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fs": {"command": "node", "args": ["server.js"]},
                        "web": {"url": "http://127.0.0.1:9000/mcp", "transport": "http"},
                        "broken": {"args": ["x"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        servers = {s.name: s for s in load_servers(path)}
        self.assertEqual(set(servers), {"fs", "web"})  # broken 被跳过
        self.assertEqual(servers["fs"].args, ("server.js",))
        self.assertEqual(servers["web"].transport, "http")

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_servers(self.ws / "nope.json"), [])


class RegistryWriteTests(_ServerFixture):
    """A：`dump_servers` / `save_servers` —— 导出形态、往返等值、原子写。"""

    def test_dump_load_roundtrip(self) -> None:
        configs = [
            McpServerConfig(name="fs", command="node", args=("server.js", "--x")),
            McpServerConfig(name="web", transport="http", url="http://127.0.0.1:9/mcp"),
            McpServerConfig(name="env", command="npx", env={"A": "1", "B": "2"}),
        ]
        path = self.ws / "out" / "mcp.json"  # 目标目录不存在，save 应自建
        save_servers(configs, path)
        self.assertTrue(path.is_file())
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], 1)
        self.assertEqual(set(doc["mcpServers"]), {"fs", "web", "env"})
        back = {s.name: s for s in load_servers(path)}
        self.assertEqual(back["fs"].args, ("server.js", "--x"))
        self.assertEqual(back["web"].transport, "http")
        self.assertEqual(back["web"].url, "http://127.0.0.1:9/mcp")
        self.assertEqual(dict(back["env"].env), {"A": "1", "B": "2"})

    def test_dump_omits_empty_fields(self) -> None:
        item = dump_servers([McpServerConfig(name="fs", command="node")])["mcpServers"]["fs"]
        self.assertNotIn("args", item)
        self.assertNotIn("env", item)
        self.assertNotIn("url", item)

    def test_save_is_atomic_on_failure(self) -> None:
        from unittest import mock

        path = self.ws / "mcp.json"
        save_servers([McpServerConfig(name="a", command="node")], path)
        before = path.read_text(encoding="utf-8")
        with mock.patch("agent_core.mcp.registry.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                save_servers([McpServerConfig(name="b", command="npx")], path)
        # 中途失败：目标文件保持原样，绝不留半截 JSON。
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual([s.name for s in load_servers(path)], ["a"])


class SessionWiringTests(_ServerFixture):
    """⑤ 未启用时不启动任何子进程；启用后工具真的进清单。"""

    def _write_config(self) -> None:
        (self.ws / ".vibe").mkdir(exist_ok=True)
        (self.ws / ".vibe" / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fake": {
                            "command": sys.executable,
                            "args": [str(self.script), "ndjson"],
                            "env": {"MCP_MARKER": str(self.marker)},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_disabled_does_not_spawn_subprocess(self) -> None:
        self._write_config()
        set_manager(None)
        config = replace(Config.load(self.ws), mcp_enabled=False)
        session = Session(config, client=FakeClient([]))
        self.assertEqual([n for n in session.registry.names() if n.startswith(NAME_PREFIX)], [])
        self.assertFalse(self.marker.exists(), "未启用 MCP 却启动了子进程")
        self.assertFalse(session.mcp_status()["enabled"])

    def test_enabled_registers_tools(self) -> None:
        self._write_config()
        set_manager(None)
        config = replace(Config.load(self.ws), mcp_enabled=True)
        session = Session(config, client=FakeClient([]))
        self.assertIn(f"{NAME_PREFIX}fake__echo", session.registry.names())
        status = session.mcp_status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["servers"][0]["available"])
        self.assertTrue(self.marker.exists(), "启用后应真的启动子进程")


class ConfigDefaultsTests(unittest.TestCase):
    """L4：配置字段与 VIBE_MCP* 环境变量。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_are_off(self) -> None:
        config = Config.load(self.ws)
        self.assertFalse(config.mcp_enabled)
        self.assertEqual(config.mcp_timeout, 30)
        self.assertEqual(config.mcp_config_path, self.ws / ".vibe" / "mcp.json")

    def test_env_overrides(self) -> None:
        import os

        from unittest import mock

        with mock.patch.dict(
            os.environ,
            {"VIBE_MCP": "1", "VIBE_MCP_TIMEOUT": "12", "VIBE_MCP_CONFIG": "custom.json"},
            clear=False,
        ):
            config = Config.load(self.ws)
        self.assertTrue(config.mcp_enabled)
        self.assertEqual(config.mcp_timeout, 12)
        self.assertEqual(config.mcp_config_path, Path("custom.json").expanduser())


if __name__ == "__main__":
    unittest.main()