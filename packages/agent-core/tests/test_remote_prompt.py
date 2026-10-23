"""远端工作面注入系统提示（M11 补强）测试。

验收要求：

1. **未启用远端时逐字节一致**（回归保护）：`VIBE_REMOTE` 为 0/未设、或清单里
   一条连接都没有时，系统提示必须与改造前**逐字节相同**；
2. 启用且清单里有一条可自动解析的连接（清单 `default` 或唯一那条）时，注入
   「## 远端工作面」小节，含连接名 / 远端根目录 / 远端工具名与能力位；
3. 多条连接且没设默认连接 → **不猜**（与 `RemoteStore.resolve` 同款纪律），
   只列名字并指引去 `remote_connections`；
4. 清单缺失 / 损坏**不阻断启动**：退化为不注入，而不是把会话起不来。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.loop.agent import Agent  # noqa: E402
from agent_core.loop.prompts import (  # noqa: E402
    build_remote_section,
    build_system_prompt,
)
from agent_core.remote.connection import RemoteConnection  # noqa: E402
from agent_core.remote.store import RemoteStore, store_path_for  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

MARK = "## 远端工作面"


def make_conn(**overrides) -> RemoteConnection:
    fields = {
        "name": "devbox",
        "host": "10.0.0.12",
        "user": "dev",
        "root": "/srv/app",
    }
    fields.update(overrides)
    return RemoteConnection(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 段落文本（纯函数）
# --------------------------------------------------------------------------- #
class SectionTextTest(unittest.TestCase):
    def test_no_inputs_yields_empty_section(self) -> None:
        self.assertEqual(build_remote_section(), "")

    def test_single_connection_names_tools_and_root(self) -> None:
        text = build_remote_section(
            name="devbox",
            target="dev@10.0.0.12",
            root="/srv/app",
            allow_write=True,
            allow_shell=True,
            available=("devbox",),
        )
        self.assertIn(MARK, text)
        self.assertIn("`devbox`", text)
        self.assertIn("dev@10.0.0.12", text)
        self.assertIn("/srv/app", text)
        for tool in (
            "remote_read_file",
            "remote_list_dir",
            "remote_grep",
            "remote_edit",
            "remote_write_file",
            "remote_run_command",
        ):
            self.assertIn(tool, text)

    def test_read_only_connection_is_declared(self) -> None:
        """能力位必须如实写进提示词 —— 否则模型会去试注定被拒的写入。"""
        text = build_remote_section(
            name="devbox", target="dev@h", root="/srv", available=("devbox",)
        )
        self.assertIn("只读", text)
        self.assertIn("allow_write=off", text)
        self.assertNotIn("可在远端执行命令", text)

    def test_multiple_without_default_does_not_guess(self) -> None:
        text = build_remote_section(available=("a", "b"))
        self.assertIn(MARK, text)
        self.assertIn("`a`", text)
        self.assertIn("`b`", text)
        self.assertIn("没有设默认连接", text)
        self.assertIn("remote_connections", text)

    # ---- M12：已加入工作区的远端根 ----------------------------------------
    def test_roots_section_lists_namespace_path(self) -> None:
        text = build_remote_section(
            name="devbox",
            target="dev@10.0.0.12",
            root="/srv/app",
            available=("devbox",),
            roots=(
                {
                    "ns_path": "@remote/devbox/项目/smart-writer",
                    "connection": "devbox",
                    "target": "dev@10.0.0.12",
                },
            ),
        )
        self.assertIn("已加入工作区的远端目录", text)
        self.assertIn("@remote/devbox/项目/smart-writer", text)

    def test_empty_roots_yields_zero_characters(self) -> None:
        """无根时 `_remote_roots_section` 必须**逐字节**返回空串（反向锚点 1 的 M12 部分）。

        这是「无根时提示词与 M12 之前逐字节一致」的回归保护 —— 直接钉住
        **空输入 → 零字符**这个契约（含 `roots=({"ns_path": ""},)` 这种
        「有元素但不可展示」的边界）。若只比较两次带空 roots 的调用，两者会
        一起变化，断言就失去了牙齿。
        """
        from agent_core.loop.prompts import _remote_roots_section

        self.assertEqual(_remote_roots_section(()), "")
        self.assertEqual(_remote_roots_section(({},)), "")
        self.assertEqual(_remote_roots_section(({"ns_path": ""},)), "")
        # 整段提示词：无根时不出现 M12 那段
        without_roots = build_remote_section(
            name="devbox", target="dev@10.0.0.12", root="/srv/app", available=("devbox",)
        )
        self.assertIn(MARK, without_roots)
        self.assertNotIn("已加入工作区的远端目录", without_roots)


# --------------------------------------------------------------------------- #
# Agent 装配（门控 + 注入）
# --------------------------------------------------------------------------- #
class AgentInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent(self) -> Agent:
        config = Config.load(self.ws)
        return Agent(config, client=FakeClient(["好的。"]), approver=lambda r: "approve")

    def _plain_prompt(self, agent: Agent) -> str:
        return build_system_prompt(
            workspace=str(self.ws),
            tool_descriptions=agent.registry.describe(),
            native_tools=True,
        )

    def _write_store(self, connections: list[RemoteConnection], default: str = "") -> None:
        """写一份清单。

        `upsert` 会让**第一条**连接自动成为默认（见 `RemoteStore.upsert`），
        所以这里在落盘前显式改写 `default_name`：`default` 参数说了算，
        传空串就是「多条、且明确没有默认连接」这个场景。
        """
        path = store_path_for(Config.load(self.ws))
        store = RemoteStore.load(path)
        for conn in connections:
            store.upsert(conn)
        store.default_name = default
        store.save()

    def test_remote_off_keeps_prompt_byte_identical(self) -> None:
        """**反向验证的锚点**：若哪天改成无条件注入，这条立刻变红。"""
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "0"}):
            self._write_store([make_conn()], default="devbox")
            agent = self._agent()
            self.assertEqual(agent.messages[0]["content"], self._plain_prompt(agent))
            self.assertNotIn(MARK, agent.messages[0]["content"])

    def test_remote_on_without_store_keeps_prompt_identical(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            agent = self._agent()
            self.assertEqual(agent.messages[0]["content"], self._plain_prompt(agent))
            self.assertNotIn(MARK, agent.messages[0]["content"])

    def test_remote_on_with_default_connection_injects_section(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn()], default="devbox")
            content = self._agent().messages[0]["content"]
            self.assertIn(MARK, content)
            self.assertIn("`devbox`", content)
            self.assertIn("/srv/app", content)
            self.assertIn("remote_read_file", content)

    def test_sole_connection_without_default_still_resolves(self) -> None:
        """只配了一条时连默认都不用设 —— 与 `RemoteStore.resolve` 的口径一致。"""
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn(name="only")], default="")
            content = self._agent().messages[0]["content"]
            self.assertIn(MARK, content)
            self.assertIn("`only`", content)

    def test_multiple_without_default_lists_names_only(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn(name="a"), make_conn(name="b")], default="")
            content = self._agent().messages[0]["content"]
            self.assertIn(MARK, content)
            self.assertIn("`a`", content)
            self.assertIn("`b`", content)
            self.assertNotIn("远端根目录 `/srv/app`", content)

    def test_corrupt_store_does_not_block_startup(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            path = store_path_for(Config.load(self.ws))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ 这不是合法 JSON", encoding="utf-8", newline="")
            agent = self._agent()
            self.assertNotIn(MARK, agent.messages[0]["content"])

    def test_subagent_prompt_never_gets_remote_section(self) -> None:
        """子 Agent 调不到远端工具（不在只读注册表里），不能给它这段指引。"""
        from agent_core.loop.prompts import build_subagent_prompt

        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn()], default="devbox")
            prompt = build_subagent_prompt(
                workspace=str(self.ws), tool_descriptions="- t: x", native_tools=True
            )
            self.assertNotIn(MARK, prompt)

    # ---- M12：远端根进提示词 + 加根后当轮重算 ----------------------------
    def test_root_appears_in_prompt_after_joining_workspace(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn()], default="devbox")
            path = store_path_for(Config.load(self.ws))
            store = RemoteStore.load(path)
            store.add_root("devbox", "项目/smart-writer")
            content = self._agent().messages[0]["content"]
            self.assertIn("已加入工作区的远端目录", content)
            self.assertIn("@remote/devbox/项目/smart-writer", content)

    def test_no_root_keeps_prompt_without_m12_section(self) -> None:
        """只配了连接、没加根 —— 提示词里不能出现 M12 那段（锚点 1）。"""
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn()], default="devbox")
            content = self._agent().messages[0]["content"]
            self.assertIn(MARK, content)
            self.assertNotIn("已加入工作区的远端目录", content)
            self.assertNotIn("@remote/", content)

    def test_refresh_system_prompt_picks_up_new_root(self) -> None:
        """加根后必须能**当轮**让模型看见新根，否则用户加完就被模型说「没加」。

        走的是 host 端点里的 `router.invalidate()` + `refresh_system_prompt()`
        同一条链路（这里直接调门面，验证重算本身有效）。
        """
        with mock.patch.dict(os.environ, {"VIBE_REMOTE": "1"}):
            self._write_store([make_conn()], default="devbox")
            agent = self._agent()
            self.assertNotIn("@remote/devbox", agent.messages[0]["content"])
            store = RemoteStore.load(store_path_for(Config.load(self.ws)))
            store.add_root("devbox", "")
            self.assertTrue(agent.refresh_system_prompt())
            self.assertIn("@remote/devbox", agent.messages[0]["content"])
            # 根没变时重算是 no-op（不白白改写系统段）
            self.assertFalse(agent.refresh_system_prompt())

    def test_refresh_system_prompt_is_noop_for_explicit_override(self) -> None:
        """子 Agent 的显式提示词**不重算**（M12 决策 3）—— 保持子 Agent 行为不变。"""
        config = Config.load(self.ws)
        agent = Agent(
            config,
            client=FakeClient(["好的。"]),
            approver=lambda r: "approve",
            system_prompt="只读探索取向的提示词",
        )
        self.assertFalse(agent.refresh_system_prompt())
        self.assertEqual(agent.messages[0]["content"], "只读探索取向的提示词")


if __name__ == "__main__":
    unittest.main()
