"""MCP 管理面（L 管理界面）的 host 端点测试。

这一层要钉死的是**写入语义**，而不是「能不能连上某个 server」：

1. **只写配置文件、不拼命令** —— 界面提交的 server 配置只落到 `<ws>/.vibe/mcp.json`，
   argv 仍只来自受信配置（ADR-015）；写出的文档能被 `load_servers` 读回。
2. **闸门顺序**：未启用 400、执行中 409、治理面**接管运行时** 400、非法 400 —— 且**非法时不写文件**。
3. **改后自动重载** —— 新增后 `mcp__*` 出现在 registry、删除后消失。

假 server 复用 `test_mcp.py` 里那份（`sys.executable` 启动，任何装了 Python 的机器都能跑）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.settings import env_locked_fields  # noqa: E402
from agent_core.mcp.manager import set_manager  # noqa: E402
from agent_core.mcp.registry import NAME_PREFIX  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402
from tests.test_mcp import FAKE_SERVER  # noqa: E402


class _McpHostFixture(unittest.TestCase):
    """起一个临时工作区 + 假 server 脚本 + 真实 host（可选是否启用 MCP）。"""

    enabled = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = (root / "ws").resolve()
        self.ws.mkdir()
        self.script = self.ws / "fake_mcp.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.marker = self.ws / "started.txt"
        self.mcp_file = self.ws / ".vibe" / "mcp.json"

        # 设置文件指到临时目录，避免污染开发机的 ~/.vibe/settings.json。
        self._saved_settings = os.environ.get("VIBE_SETTINGS")
        os.environ["VIBE_SETTINGS"] = str(root / "settings.json")

        set_manager(None)  # 清掉可能残留的单例，确保本次从零起
        config = replace(Config.load(self.ws), mcp_enabled=self.enabled)
        self.host = HostServer(
            config, port=0, client=FakeClient([[("list_dir", {})], "好的。"])
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        set_manager(None)
        if self._saved_settings is None:
            os.environ.pop("VIBE_SETTINGS", None)
        else:
            os.environ["VIBE_SETTINGS"] = self._saved_settings
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _request(self, path: str, *, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-Vibe-Token", self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=15)

    def _server_body(self, name: str = "fake") -> dict:
        return {
            "server": {
                "name": name,
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(self.script), "ndjson"],
                "env": {"MCP_MARKER": str(self.marker)},
            }
        }

    def _mcp_tool_names(self) -> list[str]:
        return [n for n in self.host.session.registry.names() if n.startswith(NAME_PREFIX)]

    def _stored_servers(self) -> dict:
        return json.loads(self.mcp_file.read_text(encoding="utf-8"))["mcpServers"]


class McpStatusTests(_McpHostFixture):
    def test_status_shape_when_enabled(self) -> None:
        with self._request("/api/mcp/status") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["enabled"])
        self.assertEqual(data["configPath"], str(self.mcp_file))
        self.assertEqual(data["servers"], [])  # 尚未配 server
        self.assertEqual(data["configError"], "")


class McpUpsertTests(_McpHostFixture):
    def test_upsert_adds_server_and_registers_tools(self) -> None:
        with self._request("/api/mcp/servers", method="POST", body=self._server_body()) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["enabled"])
        names = {s["name"]: s for s in data["servers"]}
        self.assertIn("fake", names)
        self.assertTrue(names["fake"]["available"], names["fake"].get("reason"))
        self.assertIn(f"{NAME_PREFIX}fake__echo", self._mcp_tool_names())
        # 只写配置文件：命令落盘（argv 只来自受信配置）
        stored = self._stored_servers()
        self.assertEqual(stored["fake"]["command"], sys.executable)
        self.assertEqual(stored["fake"]["args"], [str(self.script), "ndjson"])

    def test_status_returns_full_config_for_editing(self) -> None:
        """status 回完整 command/args/env —— 否则「编辑」浮层无法回填、一保存就抹掉 argv。"""
        self._request("/api/mcp/servers", method="POST", body=self._server_body())
        with self._request("/api/mcp/status") as r:
            data = json.loads(r.read().decode("utf-8"))
        entry = data["servers"][0]
        self.assertEqual(entry["command"], sys.executable)  # 全路径，不是 basename
        self.assertEqual(entry["args"], [str(self.script), "ndjson"])
        self.assertEqual(entry["env"]["MCP_MARKER"], str(self.marker))

    def test_upsert_same_name_replaces(self) -> None:
        self._request("/api/mcp/servers", method="POST", body=self._server_body())
        body = self._server_body()
        body["server"]["args"] = [str(self.script), "framed"]
        with self._request("/api/mcp/servers", method="POST", body=body) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.assertEqual(len(data["servers"]), 1)  # 同名替换，不追加
        self.assertEqual(self._stored_servers()["fake"]["args"], [str(self.script), "framed"])

    def test_invalid_server_is_400_and_file_unchanged(self) -> None:
        self._request("/api/mcp/servers", method="POST", body=self._server_body())
        before = self.mcp_file.read_text(encoding="utf-8")
        bad = {"server": {"name": "bad", "transport": "stdio"}}  # 缺 command
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/mcp/servers", method="POST", body=bad)
        self.assertEqual(ctx.exception.code, 400)
        # 整份不写：文件与内容都原样
        self.assertEqual(self.mcp_file.read_text(encoding="utf-8"), before)
        self.assertNotIn("bad", self._stored_servers())


class McpRemoveTests(_McpHostFixture):
    def test_remove_deletes_server_and_unregisters_tools(self) -> None:
        self._request("/api/mcp/servers", method="POST", body=self._server_body())
        self.assertIn(f"{NAME_PREFIX}fake__echo", self._mcp_tool_names())
        with self._request(
            "/api/mcp/servers/remove", method="POST", body={"name": "fake"}
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.assertEqual(data["servers"], [])
        self.assertEqual(self._mcp_tool_names(), [])  # 工具真的从清单消失
        self.assertEqual(self._stored_servers(), {})

    def test_remove_missing_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/mcp/servers/remove", method="POST", body={"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)


class McpTestConnectionTests(_McpHostFixture):
    def test_test_endpoint_returns_tools(self) -> None:
        self._request("/api/mcp/servers", method="POST", body=self._server_body())
        with self._request("/api/mcp/test", method="POST", body={"name": "fake"}) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertIn(f"{NAME_PREFIX}fake__echo", data["tools"])
        self.assertGreaterEqual(data["count"], 2)

    def test_test_unknown_server_is_ok_false_not_500(self) -> None:
        with self._request("/api/mcp/test", method="POST", body={"name": "nope"}) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])


class _FakeRuntime:
    """`GovernanceRuntime` 的最小替身：只提供「是否接管运行时」这一个判据。"""

    def __init__(self, *, locked: bool) -> None:
        self.locks_local_config = locked


class McpGuardTests(_McpHostFixture):
    def test_busy_is_409(self) -> None:
        self.host.session._state = "running"
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._request("/api/mcp/servers", method="POST", body=self._server_body())
            self.assertEqual(ctx.exception.code, 409)
        finally:
            self.host.session._state = "idle"
        self.assertFalse(self.mcp_file.exists())  # 忙时绝不落盘

    def test_governance_is_400(self) -> None:
        self.host.session.governance = object()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._request("/api/mcp/servers", method="POST", body=self._server_body())
            self.assertEqual(ctx.exception.code, 400)
        finally:
            self.host.session.governance = None

    def test_governance_taking_over_is_400(self) -> None:
        """治理面真的接管了运行时（模型请求经云端网关）→ MCP 目录拒绝写入。"""
        self.host.session.governance = _FakeRuntime(locked=True)
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._request("/api/mcp/servers", method="POST", body=self._server_body())
            self.assertEqual(ctx.exception.code, 400)
        finally:
            self.host.session.governance = None
        self.assertFalse(self.mcp_file.exists())

    def test_governance_not_taking_over_allows_write(self) -> None:
        """治理面接入了但没接管运行时（禁云 / 降级）→ 本机 MCP 目录仍可写。

        治理面从不托管 MCP 目录；此时本机文件就是唯一真源，
        挡住保存只会让用户「明明能改却不给改」。
        """
        self.host.session.governance = _FakeRuntime(locked=False)
        try:
            with self._request(
                "/api/mcp/servers", method="POST", body=self._server_body()
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        finally:
            self.host.session.governance = None
        self.assertIn("fake", {s["name"] for s in data["servers"]})
        self.assertTrue(self.mcp_file.exists())

    def test_env_locks_mcp_field(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_MCP": "0"}, clear=False):
            self.assertTrue(env_locked_fields()["mcp_enabled"])


class McpDisabledTests(_McpHostFixture):
    enabled = False

    def test_upsert_when_disabled_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/mcp/servers", method="POST", body=self._server_body())
        self.assertEqual(ctx.exception.code, 400)
        self.assertFalse(self.mcp_file.exists())
        self.assertFalse(self.marker.exists(), "未启用 MCP 却启动了子进程")

    def test_status_when_disabled(self) -> None:
        with self._request("/api/mcp/status") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertFalse(data["enabled"])
        self.assertEqual(data["servers"], [])


if __name__ == "__main__":
    unittest.main()