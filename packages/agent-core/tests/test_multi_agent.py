"""M14-A：工作区级智能体档案（校验、原子落盘、跨工作区隔离、门控）。

A 段的验收口径（`docs/M14-PLAN.md` §2）：

- 坏档案拒绝且**不落盘**；
- 原子写无 `.vibetmp` 残渣；
- 跨工作区**不串档案**；
- `VIBE_MULTI_AGENT=0` 时端点一律 400（fail-closed，不返回空列表）。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("VIBE_MEMORY", "0")

from agent_core.agents.profile import (  # noqa: E402
    AgentProfile,
    AgentProfileError,
    MAX_PROMPT_BYTES,
    readonly_tool_names,
    validate_profile,
)
from agent_core.agents.store import (  # noqa: E402
    AgentStore,
    default_store_path,
    store_path_for,
    write_text_atomic,
)
from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.store import SessionStore  # noqa: E402


def _profile(**overrides) -> AgentProfile:
    base = dict(
        id="agent1",
        name="安全审查员",
        role="只挑安全问题",
        system_prompt="你是安全审查员。",
        tools=(),
        enabled=True,
    )
    base.update(overrides)
    return AgentProfile(**base)


class ProfileValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = readonly_tool_names()

    def _validate(self, profile: AgentProfile, existing=None) -> None:
        validate_profile(
            profile,
            existing=existing or {},
            readonly_tools=self.tools,
        )

    def test_accepts_minimal_profile(self) -> None:
        self._validate(_profile())

    def test_empty_name_rejected(self) -> None:
        with self.assertRaises(AgentProfileError):
            self._validate(_profile(name=""))

    def test_name_too_long_rejected(self) -> None:
        with self.assertRaises(AgentProfileError):
            self._validate(_profile(name="名" * 25))

    def test_name_control_char_rejected(self) -> None:
        with self.assertRaises(AgentProfileError):
            self._validate(_profile(name="审查\n员"))

    def test_duplicate_name_rejected_except_self(self) -> None:
        existing = {"agent2": _profile(id="agent2", name="安全审查员")}
        with self.assertRaises(AgentProfileError):
            self._validate(_profile(name="安全审查员"), existing=existing)
        # 自己改自己不算重名
        self._validate(_profile(), existing={"agent1": _profile()})

    def test_unknown_tool_rejected(self) -> None:
        with self.assertRaises(AgentProfileError):
            self._validate(_profile(tools=("no_such_tool",)))

    def test_write_tool_rejected(self) -> None:
        """写盘 / 执行类工具结构上进不来。"""
        for name in ("write_file", "edit_file", "run_command"):
            with self.subTest(tool=name):
                with self.assertRaises(AgentProfileError):
                    self._validate(_profile(tools=(name,)))

    def test_prompt_size_capped(self) -> None:
        huge = "x" * (MAX_PROMPT_BYTES + 1)
        with self.assertRaises(AgentProfileError):
            self._validate(_profile(system_prompt=huge))

    def test_tools_are_deduped(self) -> None:
        sample = self.tools[0]
        profile = AgentProfile.from_dict(
            {"name": "a", "tools": [sample, sample, sample]}
        )
        self.assertEqual(profile.tools, (sample,))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "proj"
        self.ws.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_path_is_workspace_scoped(self) -> None:
        self.assertEqual(
            default_store_path(self.ws),
            self.ws / ".vibe" / "agents.json",
        )
        cfg = Config.load(self.ws)
        self.assertEqual(store_path_for(cfg), self.ws / ".vibe" / "agents.json")

    def test_roundtrip_and_no_temp_residue(self) -> None:
        store = AgentStore.load(default_store_path(self.ws))
        store.upsert(_profile())
        store.save()

        reloaded = AgentStore.load(default_store_path(self.ws))
        self.assertEqual([p.to_dict() for p in reloaded.list()], [_profile().to_dict()])

        vibe_dir = self.ws / ".vibe"
        residues = [n for n in os.listdir(vibe_dir) if n.endswith(".vibetmp")]
        self.assertEqual(residues, [])

    def test_file_is_private_on_posix(self) -> None:
        store = AgentStore.load(default_store_path(self.ws))
        store.upsert(_profile())
        store.save()
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(default_store_path(self.ws)).st_mode)
            self.assertEqual(mode, 0o600)

    def test_corrupt_file_raises_instead_of_empty(self) -> None:
        path = default_store_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(AgentProfileError):
            AgentStore.load(path)

    def test_future_version_raises(self) -> None:
        path = default_store_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "agents": []}), encoding="utf-8")
        with self.assertRaises(AgentProfileError):
            AgentStore.load(path)

    def test_file_size_cap(self) -> None:
        store = AgentStore.load(default_store_path(self.ws))
        for index in range(40):
            store.upsert(
                _profile(
                    id=f"agent{index}",
                    name=f"agent-{index}",
                    system_prompt="x" * (MAX_PROMPT_BYTES - 16),
                )
            )
        with self.assertRaises(AgentProfileError):
            store.save()

    def test_remove(self) -> None:
        store = AgentStore.load(default_store_path(self.ws))
        store.upsert(_profile())
        store.save()
        self.assertTrue(store.remove("agent1"))
        self.assertFalse(store.remove("agent1"))

    def test_profiles_do_not_follow_another_workspace(self) -> None:
        """跨工作区不串档案（与远程根节点同一条教训）。"""
        other = self.root / "other"
        other.mkdir()
        cfg_a = Config.load(self.ws)
        cfg_b = Config.load(other)

        store_a = AgentStore.load(store_path_for(cfg_a))
        store_a.upsert(_profile())
        store_a.save()

        store_b = AgentStore.load(store_path_for(cfg_b))
        self.assertEqual(store_b.list(), [])
        self.assertFalse((other / ".vibe" / "agents.json").exists())

    def test_atomic_write_retries_transient_permission_error(self) -> None:
        """Windows 上「读者正打开着目标文件」会让 os.replace 抛 PermissionError。

        这是**正常并发**（界面在刷新列表、引擎在逐 Turn 落盘），不是权限问题：
        必须退让重试，否则一次偶发冲突就会把后台讨论线程打挂。
        """
        path = self.ws / ".vibe" / "race.json"
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "拒绝访问")
            return real_replace(src, dst)

        with mock.patch("agent_core.agents.store.os.replace", side_effect=flaky):
            write_text_atomic(path, '{"ok": true}')

        self.assertEqual(calls["n"], 3)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"ok": true}')
        residues = [n for n in os.listdir(path.parent) if n.endswith(".vibetmp")]
        self.assertEqual(residues, [])


class _NoopClient:
    """档案端点不应触达模型；真被调用就说明串线了。"""

    def chat(self, *args, **kwargs):  # pragma: no cover - 仅防串线
        raise AssertionError("智能体档案端点不应调用模型")


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "proj"
        self.ws.mkdir()
        (self.ws / "readme.md").write_text("hi\n", encoding="utf-8")
        self.store = SessionStore(self.root / "sessions.db")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        if getattr(self, "host", None) is not None:
            self.host.shutdown()
        self._tmp.cleanup()

    def _start(self, *, enabled: bool) -> None:
        self.host = HostServer(
            Config.load(self.ws, multi_agent_enabled=enabled),
            port=0,
            store=self.store,
            client=_NoopClient(),
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", "X-Vibe-Token": self.token},
            method=method,
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    # ---- 门控 -------------------------------------------------------------
    def test_gate_off_returns_400_for_all_endpoints(self) -> None:
        self._start(enabled=False)
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 400)
        self.assertIn("VIBE_MULTI_AGENT", payload["error"])

        status, _ = self._request("POST", "/api/agents", {"profile": {"name": "a"}})
        self.assertEqual(status, 400)
        status, _ = self._request("POST", "/api/agents/delete", {"id": "x"})
        self.assertEqual(status, 400)
        # fail-closed：不返回空列表，也不落盘
        self.assertFalse((self.ws / ".vibe" / "agents.json").exists())

    # ---- 正常路径 ---------------------------------------------------------
    def test_list_is_empty_before_creation(self) -> None:
        self._start(enabled=True)
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["agents"], [])
        self.assertTrue(payload["tools"])

    def test_create_list_update_delete(self) -> None:
        self._start(enabled=True)
        status, payload = self._request(
            "POST",
            "/api/agents",
            {"profile": {"name": "安全审查员", "role": "只挑安全问题"}},
        )
        self.assertEqual(status, 200)
        agent = payload["agent"]
        self.assertEqual(agent["name"], "安全审查员")
        # tools 省略 = 全部只读工具
        self.assertEqual(agent["tools"], readonly_tool_names())
        agent_id = agent["id"]

        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual([a["id"] for a in payload["agents"]], [agent_id])
        # 落盘位置恒随工作区
        self.assertTrue((self.ws / ".vibe" / "agents.json").exists())

        status, payload = self._request(
            "POST",
            "/api/agents",
            {"profile": {"id": agent_id, "name": "安全审查员", "systemPrompt": "新提示词"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["agent"]["systemPrompt"], "新提示词")
        self.assertEqual(len(AgentStore.load(default_store_path(self.ws)).list()), 1)

        status, payload = self._request("POST", "/api/agents/delete", {"id": agent_id})
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])
        self.assertEqual(AgentStore.load(default_store_path(self.ws)).list(), [])

    def test_bad_profiles_rejected_and_not_persisted(self) -> None:
        self._start(enabled=True)
        cases = [
            {"name": ""},
            {"name": "名" * 25},
            {"name": "a", "tools": ["write_file"]},
            {"name": "a", "tools": ["no_such_tool"]},
            {"name": "a", "systemPrompt": "x" * (MAX_PROMPT_BYTES + 1)},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, _ = self._request("POST", "/api/agents", {"profile": body})
                self.assertEqual(status, 400)
        self.assertFalse((self.ws / ".vibe" / "agents.json").exists())

    def test_duplicate_name_rejected(self) -> None:
        self._start(enabled=True)
        self._request("POST", "/api/agents", {"profile": {"name": "甲"}})
        status, payload = self._request(
            "POST", "/api/agents", {"profile": {"name": "甲"}}
        )
        self.assertEqual(status, 400)
        self.assertIn("已存在", payload["error"])
        self.assertEqual(len(AgentStore.load(default_store_path(self.ws)).list()), 1)

    def test_update_unknown_id_rejected(self) -> None:
        self._start(enabled=True)
        status, _ = self._request(
            "POST", "/api/agents", {"profile": {"id": "nope", "name": "甲"}}
        )
        self.assertEqual(status, 400)

    def test_delete_unknown_rejected(self) -> None:
        self._start(enabled=True)
        status, _ = self._request("POST", "/api/agents/delete", {"id": "nope"})
        self.assertEqual(status, 400)

    def test_update_keeps_omitted_fields(self) -> None:
        """省略字段 = 保留原值；改个名字不能把裁剪过的工具面静默放大。"""
        self._start(enabled=True)
        sample = readonly_tool_names()[0]
        _, payload = self._request(
            "POST",
            "/api/agents",
            {"profile": {"name": "甲", "tools": [sample]}},
        )
        agent_id = payload["agent"]["id"]
        status, payload = self._request(
            "POST", "/api/agents", {"profile": {"id": agent_id, "name": "乙"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["agent"]["name"], "乙")
        self.assertEqual(payload["agent"]["tools"], [sample])
        self.assertTrue(payload["agent"]["enabled"])

    def test_missing_profile_object_rejected(self) -> None:
        self._start(enabled=True)
        status, _ = self._request("POST", "/api/agents", {"name": "a"})
        self.assertEqual(status, 400)

    def test_corrupt_file_returns_400_with_reason(self) -> None:
        """坏文件要报错，不能静默当空 —— 否则用户以为档案凭空消失。"""
        self._start(enabled=True)
        path = self.ws / ".vibe" / "agents.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 400)
        self.assertIn("JSON", payload["error"])


if __name__ == "__main__":
    unittest.main()
