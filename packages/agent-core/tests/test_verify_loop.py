"""验证闭环（C2）测试。

验收要求（M1.5-PLAN C2）：

1. 改完文件收尾前自动跑 verify；失败把裁剪输出回灌，模型带着报错继续修；
2. 单次 run() 最多自动重验 2 次，防死循环；
3. verify 命令来自配置，模型无法篡改；被安全策略拒绝时跳过不阻塞；
4. 优先级：环境变量 VIBE_VERIFY_COMMAND > VIBE.md `verify:` 约定行 > 空。

注意：verify 命令与 run_command 走同一套 CommandGuard 白名单 —— python -c
这类任意代码执行会被 DENY，所以测试一律用「脚本文件」形式（也是沙箱
对真实用户的建议用法）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.llm import ToolCall  # noqa: E402
from agent_core.loop import Agent, AgentHooks, ApprovalRequest  # noqa: E402
from agent_core.sandbox import Risk  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

# 退出码由 probe 文件是否存在决定：第一次验证失败（probe 缺失），
# 模型第二轮「创建 probe」后验证通过 —— 驱动回灌→继续修→转绿的完整闭环。
PROBE_SCRIPT = """import sys
from pathlib import Path
if Path("probe.txt").exists():
    sys.exit(0)
print("probe.txt missing; verification failed")
sys.exit(1)
"""

FAIL_SCRIPT = "import sys\nprint('boom')\nsys.exit(3)\n"

BIG_SCRIPT = "print('x' * 100000)\nsys.exit(1)\n"  # 超长失败输出，验证裁剪


class _RecorderHooks(AgentHooks):
    """记录工具卡片回调，验证合成 verify 卡片走既有渲染通道。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Risk]] = []
        self.results: list[tuple[str, bool]] = []
        self.notices: list[str] = []

    def on_tool_call(self, call: ToolCall, risk: Risk, reason: str) -> None:
        self.calls.append((call.name, risk))

    def on_tool_result(self, call: ToolCall, result) -> None:
        self.results.append((call.name, result.ok))

    def on_notice(self, text: str) -> None:
        self.notices.append(text)


class VerifyHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        # 隔离环境变量，避免宿主机注入干扰优先级断言
        env = {k: v for k, v in os.environ.items() if k != "VIBE_VERIFY_COMMAND"}
        self._env_patcher = mock.patch.dict(os.environ, env, clear=True)
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()
        self._tmp.cleanup()

    def _agent(self, steps, *, verify: str = "", hooks=None) -> Agent:
        # 审批不可配置关闭（ADR-008）；这里用显式 approver 表达「测试夹具代为批准」。
        config = Config.load(self.ws, verify_command=verify)
        return Agent(
            config,
            client=FakeClient(steps),
            hooks=hooks or AgentHooks(),
            approver=lambda r: "approve",
        )

    def _write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")


class VerifyLoopTests(VerifyHarness):
    def test_passing_verify_lets_run_finish(self) -> None:
        self._write("check.py", PROBE_SCRIPT)
        self._write("probe.txt", "1")  # 探针已在 → verify 直接通过
        self._write("a.txt", "hello\n")
        hooks = _RecorderHooks()
        agent = self._agent(
            [[("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})], "改完了。"],
            verify="python check.py",
            hooks=hooks,
        )
        answer = agent.run("改一下")
        self.assertEqual(answer, "改完了。")
        self.assertEqual(agent._verify_runs, 1)
        self.assertIn(("verify", Risk.SAFE), hooks.calls)
        self.assertEqual(
            [r for r in hooks.results if r[0] == "verify"], [("verify", True)]
        )
        # 验证通过不回灌：user 消息只有任务那一条
        user_msgs = [m for m in agent.messages if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        events = self._audit_events(agent)
        self.assertIn("verify.run", [e["event"] for e in events])
        verify_event = next(e for e in events if e["event"] == "verify.run")
        self.assertEqual(verify_event["exit_code"], 0)

    def test_failing_verify_feeds_output_back(self) -> None:
        self._write("check.py", PROBE_SCRIPT)  # probe.txt 缺失 → 第一次验证失败
        self._write("a.txt", "hello\n")
        steps = [
            [("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})],
            "应该没问题了。",
            [("write_file", {"path": "probe.txt", "content": "now ok\n", "overwrite": "true"})],
            "这次真的修好了。",
        ]
        agent = self._agent(steps, verify="python check.py")
        answer = agent.run("改一下")
        self.assertEqual(answer, "这次真的修好了。")
        self.assertEqual(agent._verify_runs, 2)
        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles.count("user"), 2)  # 任务 + 一次回灌
        backfeed = [
            m for m in agent.messages
            if m["role"] == "user" and "自动验证未通过" in m["content"]
        ]
        self.assertEqual(len(backfeed), 1)
        self.assertIn("退出码 1", backfeed[0]["content"])
        self.assertIn("probe.txt missing", backfeed[0]["content"])

    def test_verify_gives_up_after_two_attempts(self) -> None:
        self._write("check.py", FAIL_SCRIPT)
        self._write("a.txt", "hello\n")
        steps = [
            [("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})],
            "总结 1",
            "总结 2",
            "总结 3（放行）",
        ]
        agent = self._agent(steps, verify="python check.py")
        answer = agent.run("改一下")
        self.assertEqual(answer, "总结 3（放行）")
        self.assertEqual(agent._verify_runs, 2)  # 恰好两次，不无限循环
        events = [
            e for e in self._audit_events(agent) if e["event"] == "verify.run"
        ]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["exit_code"] == 3 for e in events))

    def test_no_edits_no_verify(self) -> None:
        self._write("check.py", PROBE_SCRIPT)
        agent = self._agent(["纯回答，不动文件。"], verify="python check.py")
        agent.run("打个招呼")
        self.assertEqual(agent._verify_runs, 0)

    def test_no_verify_configured_no_verify(self) -> None:
        self._write("a.txt", "hello\n")
        agent = self._agent(
            [[("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})], "改完了。"]
        )
        agent.run("改一下")
        self.assertEqual(agent._verify_runs, 0)

    def test_denied_verify_command_skips_without_blocking(self) -> None:
        self._write("a.txt", "hello\n")
        hooks = _RecorderHooks()
        agent = self._agent(
            [[("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})], "改完了。"],
            verify="rm -rf /",
            hooks=hooks,
        )
        answer = agent.run("改一下")
        self.assertEqual(answer, "改完了。")  # 不阻塞任务
        self.assertEqual(agent._verify_runs, 1)
        self.assertTrue(any("无法执行" in n for n in hooks.notices))
        events = [
            e for e in self._audit_events(agent) if e["event"] == "verify.run"
        ]
        self.assertEqual(events[0]["status"], "skipped")

    def test_verify_output_is_clipped_to_limit(self) -> None:
        self._write("check.py", BIG_SCRIPT)
        self._write("a.txt", "hello\n")
        steps = [
            [("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})],
            "总结 1",
            "总结 2（放行）",
        ]
        agent = self._agent(steps, verify="python check.py")
        agent.run("改一下")
        backfeed = [
            m for m in agent.messages
            if m["role"] == "user" and "自动验证未通过" in m["content"]
        ]
        self.assertEqual(len(backfeed), 2)  # 恒失败 → 两次都回灌
        self.assertTrue(
            all(len(m["content"]) < 50_000 for m in backfeed),
            "回灌内容必须被裁剪",
        )

    def test_audit_chain_stays_valid(self) -> None:
        self._write("check.py", FAIL_SCRIPT)
        self._write("a.txt", "hello\n")
        agent = self._agent(
            [[("edit_file", {"path": "a.txt", "search": "hello", "replace": "hi"})], "改完了。"],
            verify="python check.py",
        )
        agent.run("改一下")
        self.assertTrue(self._audit_events(agent))  # 有审计
        ok, detail = verify_chain(agent.config.audit_path)
        self.assertTrue(ok, detail)

    def _audit_events(self, agent: Agent) -> list[dict]:
        path = agent.config.audit_path
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class VerifyConfigTests(VerifyHarness):
    def test_vibe_md_verify_line_used_as_fallback(self) -> None:
        self._write("VIBE.md", "# 规则\n\nverify: python -m unittest discover -s tests\n")
        config = Config.load(self.ws)
        self.assertEqual(config.verify_command, "python -m unittest discover -s tests")

    def test_env_var_wins_over_vibe_md(self) -> None:
        self._write("VIBE.md", "verify: python --version\n")
        os.environ["VIBE_VERIFY_COMMAND"] = "python check.py"
        try:
            config = Config.load(self.ws)
        finally:
            os.environ.pop("VIBE_VERIFY_COMMAND", None)
        self.assertEqual(config.verify_command, "python check.py")

    def test_empty_by_default(self) -> None:
        config = Config.load(self.ws)
        self.assertEqual(config.verify_command, "")

    def test_list_prefix_verify_line_supported(self) -> None:
        self._write(".vibe/VIBE.md", "- verify: python -m pytest\n")
        config = Config.load(self.ws)
        self.assertEqual(config.verify_command, "python -m pytest")


if __name__ == "__main__":
    unittest.main()
