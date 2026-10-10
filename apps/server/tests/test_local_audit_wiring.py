"""本地审计的**真实产出**必须能被上传契约原样接受。

## 为什么单独立一组

`test_chain_compat._sample_locals()` 是**手写**的「我以为客户端会写这些字段」。
手写样本与真实产出之间必然漂移 —— 这一轮修掉的缺陷全部长在这个缝里：

| 缺陷 | 形态 |
|---|---|
| `session.start` / `session.end` | 词表里有规则，代码里从来没人发 |
| `sandbox.block` / `sandbox.deny_command` / `tool.error` / `approval.request` | 服务端登记了，客户端从不产出 |
| `verify.run` | 代码在发，词表里没登记（静默不上传） |

更隐蔽的一种是**接在不可达的位置**：`sandbox.deny_command` 最初被我接在
`CommandGuard.ensure_allowed`（工具处理器里），但循环的闸门 1 会先按
`_assess_command` 的结论拦下整个调用，处理器根本不会执行 ——
那条审计在真机上永远不会产生，而代码看起来「写好了」。
只有真跑一遍才能发现，静态检查看不出来。

所以这一组**不写样本**：跑一遍真代码，把产出的本地 JSONL 直接喂给
`vocabulary.translate` + 服务端 `redaction.validate_event`。
样本只要还是人写的，就还会漂。
"""

from __future__ import annotations

import json
import sys
import unittest

from tests.support import TempDirTestCase

from agent_core.audit import record_session_end, record_session_start
from agent_core.config import Config
from agent_core.llm import ToolCall
from agent_core.loop import Agent
from agent_core.sandbox import Risk
from agent_core.tools import ToolResult, ToolSpec
from agent_core.governance import vocabulary
from vibe_server import redaction


class _NoopClient:
    """只用来满足 `Agent` 的构造参数。

    这一组测的是工具调用的**周边接线**（沙箱拦截、审批、审计），与模型无关，
    所以不造假模型剧本，直接调 `_execute_call`（单次工具执行入口）。
    用假模型驱动整个 `run()` 会把「模型剧本」也变成被测对象，
    那样这条测试就同时在测三件事，失败时定位不到是哪一件。
    """

    supports_native_tools = True

    def chat(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("这一组测试不应调用模型")


_ECHO_UNSUPPORTED_ON_WINDOWS = (
    "Windows 上 echo 不是可执行文件，而沙箱刻意不使用 shell=True —— "
    "命令会以 WinError 2 失败。这是平台缺口，不是这几条断言的对象。"
)


def _probe_tool_spec() -> ToolSpec:
    """一个**内部会抛异常**的工具，用来逼出 `tool.error`。

    真实工具都把异常处理得很好，所以这条路径在正常使用中几乎不出现 ——
    而「几乎不出现」正是它需要被测试的原因：真出现时（某个工具被改坏），
    没有测试的话没人知道审计有没有记下来。
    """

    def boom(ctx: object, args: dict) -> ToolResult:  # noqa: ARG001
        raise ValueError("probe: 故意抛出的内部错误")

    return ToolSpec(
        name="probe_boom",
        description="测试专用：handler 抛 ValueError",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )


class LocalAuditWiringTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.tmp / "ws"
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        # 敏感文件：用来逼出路径沙箱的拒绝
        (self.workspace / ".env").write_text("SECRET=should-never-be-uploaded\n", encoding="utf-8")
        # 验证命令用 echo：在白名单里（SAFE 级），且不产生副作用
        self.client_config = Config.load(self.workspace, verify_command="echo verify-ok")
        # 审批人一律放行：默认审批人是「一律拒绝」，那样写操作根本不会执行，
        # 也就产不出 `tool.write_file` —— 我们要测的是「写完之后记了什么」。
        # 「拒绝」那条分支由 agent-core 自己的测试覆盖。
        self.agent = Agent(
            self.client_config, client=_NoopClient(), approver=lambda req: "approve"
        )
        self.agent.registry.register(_probe_tool_spec())

    # ------------------------------------------------------------------ 工具
    def _events(self) -> list[dict]:
        path = self.client_config.audit_path
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _named(self, name: str) -> list[dict]:
        return [e for e in self._events() if e.get("event") == name]

    def _call(self, name: str, arguments: dict, call_id: str = "c1") -> str:
        return self.agent._execute_call(ToolCall(id=call_id, name=name, arguments=arguments))

    # ------------------------------------------------------------------ 路径拦截
    def test_path_block_is_recorded_with_structured_kind(self) -> None:
        """读敏感文件被拦 —— 安全审计里最该被聚合的一类。"""
        self._call("read_file", {"path": ".env"})

        blocks = self._named("sandbox.block")
        self.assertEqual(
            len(blocks), 1,
            f"未记录 sandbox.block，实际事件：{[e.get('event') for e in self._events()]}",
        )
        entry = blocks[0]
        # kind 是结构化的，不需要去解析异常消息 —— 消息文案会改
        self.assertEqual(entry.get("kind"), "sensitive")
        self.assertEqual(entry.get("tool"), "read_file")
        self.assertTrue(entry.get("path"), "没带上被拒的路径，事后无法定位")
        self.assertTrue(entry.get("reason"), "本地应保留原文以便排障")

    # ------------------------------------------------------------------ 命令拒绝
    def test_deny_command_is_recorded_on_the_reachable_path(self) -> None:
        """DENY 级命令必须留下一条审计。

        这条曾经是**不可达**的：闸门 1 先按 `_assess_command` 的结论拦下调用，
        工具处理器不执行，于是接在处理器里的那处记录永远不会触发。
        """
        self._call("run_command", {"command": "sudo rm -rf /"}, call_id="c2")

        denied = self._named("sandbox.deny_command")
        self.assertEqual(
            len(denied), 1,
            f"未记录 sandbox.deny_command，实际事件：{[e.get('event') for e in self._events()]}",
        )
        entry = denied[0]
        self.assertEqual(entry.get("executable"), "sudo")
        self.assertEqual(entry.get("risk"), "deny")
        # 被拒的命令不会真的执行
        self.assertNotIn("tool.run_command", {e.get("event") for e in self._events()})

    # ------------------------------------------------------------------ 工具内部异常
    def test_tool_internal_error_is_recorded(self) -> None:
        """「工具内部炸了」必须与「工具跑了没成功」区分开。

        两者之前都只表现为 tool.execute 的 ok=False，于是
        「哪个工具在崩」在审计里永远看不见。
        """
        self._call("probe_boom", {}, call_id="c3")

        errors = self._named("tool.error")
        self.assertEqual(
            len(errors), 1,
            f"未记录 tool.error，实际事件：{[e.get('event') for e in self._events()]}",
        )
        self.assertEqual(errors[0].get("tool"), "probe_boom")
        self.assertEqual(errors[0].get("kind"), "ValueError")

        # 对照组：正常的工具失败**不应**产生 tool.error
        self._call("read_file", {"path": "src/does-not-exist.py"}, call_id="c4")
        self.assertEqual(
            len(self._named("tool.error")), 1,
            "正常失败（文件不存在）被误记成工具内部异常 —— 那会让这条信号失去意义",
        )

    # ------------------------------------------------------------------ 审批
    def test_approval_request_and_decision_are_recorded_paired(self) -> None:
        """审批的「请求」与「决定」必须成对出现。

        只记决定的话，「有多少次请求压根没人处理」（界面没连上、进程被掐）
        就无从统计 —— 而那恰恰是审批闸门最需要被监督的地方。
        """
        self._call("write_file", {"path": "src/new.py", "content": "x = 1\n"}, call_id="c5")

        requests = self._named("approval.request")
        decisions = self._named("approval.decision")
        self.assertEqual(len(requests), 1, "未记录 approval.request")
        self.assertEqual(len(decisions), 1, "未记录 approval.decision")
        self.assertEqual(requests[0].get("tool"), "write_file")
        self.assertIn(requests[0].get("risk"), (Risk.REVIEW.value, Risk.SAFE.value))
        # 请求必须与决定配对：同一工具、同一决定
        self.assertEqual(decisions[0].get("tool"), requests[0].get("tool"))
        self.assertEqual(decisions[0].get("decision"), "approve")

    # ------------------------------------------------------------------ 会话边界
    def test_session_boundaries_are_recorded_and_translatable(self) -> None:
        """会话边界事件必须真的有人发 —— 词表里有规则不等于代码里写了。"""
        record_session_start(self.agent.audit, self.client_config)
        record_session_end(self.agent.audit, reason="exit", iterations=2, duration_ms=1500)

        names = {e.get("event") for e in self._events()}
        self.assertIn("session.start", names)
        self.assertIn("session.end", names)

        # 上传时 workspace 只留目录名：完整本地路径会连用户名一起送出去
        translated = vocabulary.translate(
            self._named("session.start")[0],
            session_id="s", sequence=1, fallback_ts=0,
            schema=redaction.event_schema(),
        )
        assert translated is not None
        self.assertEqual(translated.event["payload"]["workspace"], self.workspace.name)

    # ------------------------------------------------------------------ 自动验证
    @unittest.skipIf(sys.platform == "win32", _ECHO_UNSUPPORTED_ON_WINDOWS)
    def test_verify_run_is_recorded(self) -> None:
        """自动验证的退出码是「这次改动到底成没成」的最高信号。"""
        self.agent._run_verify()
        runs = self._named("verify.run")
        self.assertEqual(len(runs), 1, f"未记录 verify.run：{[e.get('event') for e in self._events()]}")
        self.assertIn("exit_code", runs[0])

    # ================================================================ 端到端
    @unittest.skipIf(sys.platform == "win32", _ECHO_UNSUPPORTED_ON_WINDOWS)
    def test_every_produced_event_passes_the_server_contract(self) -> None:
        """**本组的主断言**：真代码产出的每一条记录，都必须能被服务端收下。

        被服务端拒收的后果比看起来重：客户端是把事件算进上传链**之后**才发出去的，
        一旦被拒那条事件就永久丢了（本地水位线已推进，重传不会补上）。
        所以「服务端会兜底拒绝」不是安全网，是数据丢失的通道。
        """
        self._call("read_file", {"path": ".env"}, call_id="e1")
        self._call("read_file", {"path": "src/main.py"}, call_id="e2")
        self._call("run_command", {"command": "sudo rm -rf /"}, call_id="e3")
        self._call("probe_boom", {}, call_id="e4")
        self._call("write_file", {"path": "src/new.py", "content": "x = 1\n"}, call_id="e5")
        self._call("list_dir", {"path": "src"}, call_id="e6")
        self._call("grep", {"pattern": "print", "path": "src"}, call_id="e7")
        # 被允许的命令（SAFE 级 echo）：DENY 那条走不到处理器，产不出 tool.run_command
        self._call("run_command", {"command": "echo ok"}, call_id="e8")
        self._agent_verify_and_session()

        schema = redaction.event_schema()
        entries = self._events()
        self.assertGreater(len(entries), 5)

        rejected: list[tuple[str, str]] = []
        skipped: list[str] = []
        produced: set[str] = set()
        for entry in entries:
            name = str(entry.get("event") or "")
            translated = vocabulary.translate(
                entry,
                session_id="sess_wiring",
                sequence=int(entry.get("seq") or 0),
                fallback_ts=0,
                schema=schema,
            )
            if translated is None:
                # 纯本地的那些（agent.llm.error 之类）不算问题；
                # 但没有分类的事件名必须报出来，否则审计覆盖会悄悄变窄。
                if vocabulary.rule_for(name) is None:
                    skipped.append(name)
                continue
            produced.add(translated.upload_event)
            verdict = redaction.validate_event(translated.event)
            if not verdict.ok:
                rejected.append((name, verdict.reason))

        self.assertEqual(
            rejected, [],
            "真实产出的记录被服务端拒收 —— 这些事件会永久丢失："
            + "\n".join(f"  {name}: {reason}" for name, reason in rejected),
        )
        self.assertEqual(skipped, [], f"这些事件发了但没有分类（会被静默丢弃）：{skipped}")

        # 这几类必须真的产出过，否则上面「没有拒绝」只是一个空集上的真命题
        for expected in (
            "sandbox.block",
            "sandbox.deny_command",
            "tool.error",
            "approval.request",
            "approval.decision",
            "verify.run",
            "session.start",
            "session.end",
            "tool.read_file",
            "tool.write_file",
            "tool.run_command",
        ):
            self.assertIn(expected, produced, f"整轮跑下来没有产出 {expected}")

    def _agent_verify_and_session(self) -> None:
        record_session_start(self.agent.audit, self.client_config)
        self.agent._run_verify()
        record_session_end(
            self.agent.audit, reason="exit", iterations=self.agent.stats.iterations
        )


if __name__ == "__main__":
    unittest.main()
