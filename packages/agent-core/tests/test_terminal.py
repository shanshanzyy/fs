"""交互式终端（M/P1-7）测试。

这一层的风险同样不在「能不能连上某个终端」，而在三件**静默错**：

1. **写了没反应**。终端接口最容易坏的地方是「起得来、写不进、读不出」——
   界面看着正常，敲键毫无回显。所以这里起一个**真子进程**（`sys.executable`
   的交互式 REPL），喂一条命令、断言它的输出真的回来了。
2. **关了不干净**。PTY 会派生整棵进程树；只杀引导进程会留下一串孤儿
   （Windows 尤其明显）。所以「关掉后子进程不残留」必须被断言，而不是
   「看起来退出了」。
3. **缺能力就崩**。Windows 上没有 ConPTY 时必须**降级为管道模式**并继续可用，
   而不是把界面搞崩。所以这里注入一个「必抛错」的 spawn，钉住降级路径。

假终端用 `sys.executable` 启动，因此**任何装了 Python 的机器都能跑**；
真伪终端（PTY/ConPTY）不可用时对「回显」用例显式 skip —— 不是测试空洞，
因为降级路径另有专门用例覆盖。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.terminal.manager import (  # noqa: E402
    TerminalError,
    TerminalManager,
    default_shell_argv,
)

MARK = "VIBE_TERM_MARK"


def _process_alive(pid: int) -> bool:
    """跨平台判断进程是否还在。

    刻意不用 `os.kill(pid, 0)`：在 Windows 上它的语义是 `TerminateProcess`
    —— 拿它做存活探测会**顺手把进程杀掉**，测出来永远是「没了」。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Collector:
    """收集读线程推来的输出（回调可能是从别的线程调的）。"""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def __call__(self, term_id: str, chunk: bytes) -> None:
        self.buffer.extend(chunk)

    def wait_for(self, needle: str, timeout: float = 8.0) -> bool:
        target = needle.encode("utf-8")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if target in bytes(self.buffer):
                return True
            time.sleep(0.05)
        return target in bytes(self.buffer)

    def text(self) -> str:
        return bytes(self.buffer).decode("utf-8", "replace")


class TerminalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.managers: list[TerminalManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()
        self.dir.cleanup()

    def _manager(self, **kwargs) -> TerminalManager:
        # 空闲回收设 0 = 关掉，避免用例在等回显时被回收线程提前摘掉会话。
        manager = TerminalManager(idle_seconds=0, **kwargs)
        self.managers.append(manager)
        return manager

    # ---------------------------------------------------------------- ① 起/写/读
    def test_open_write_read_echo(self) -> None:
        """起终端 → 写一条命令 → 读到它的输出（真 PTY 才有互动回显）。"""
        collector = _Collector()
        manager = self._manager()
        session = manager.open(
            argv=[sys.executable, "-u", "-i", "-q"],
            cwd=self.dir.name,
            env=dict(os.environ),
            cols=80,
            rows=24,
            on_data=collector,
        )
        if session.mode != "pty":
            self.skipTest(
                f"本机无伪终端（{manager.fallback_reason[:80]}）；降级路径见用例④"
            )
        self.assertTrue(session.pid > 0)
        manager.write(session.id, f"print('{MARK}')\n".encode("utf-8"))
        self.assertTrue(
            collector.wait_for(MARK),
            f"未读到子进程输出；已收：{collector.text()[:200]!r}",
        )
        # 真 PTY 会话在关闭后进程应立刻消失。
        pid = session.pid
        manager.close(session.id)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _process_alive(pid):
            time.sleep(0.05)
        self.assertFalse(_process_alive(pid), "关闭后子进程仍在运行")

    # ---------------------------------------------------------------- ② resize
    def test_resize_does_not_crash(self) -> None:
        """resize 不崩、且会话尺寸如实更新（管道模式下是 no-op 但也不报错）。"""
        collector = _Collector()
        manager = self._manager()
        session = manager.open(
            argv=[sys.executable, "-u", "-i", "-q"],
            cwd=self.dir.name,
            env=dict(os.environ),
            cols=80,
            rows=24,
            on_data=collector,
        )
        manager.resize(session.id, 120, 40)
        self.assertEqual(manager.get(session.id).cols, 120)
        self.assertEqual(manager.get(session.id).rows, 40)
        # 缩放后终端仍可用。
        manager.write(session.id, f"print('{MARK}')\n".encode("utf-8"))
        self.assertTrue(
            collector.wait_for(MARK),
            f"缩放后读不到输出；已收：{collector.text()[:200]!r}",
        )

    # ---------------------------------------------------------------- ③ 无残留
    def test_close_leaves_no_process(self) -> None:
        """close 收掉整棵进程树 —— 断言子进程真的没了（不是「应该没了」）。"""
        manager = self._manager()
        session = manager.open(
            argv=[sys.executable, "-u", "-c", "import time; time.sleep(300)"],
            cwd=self.dir.name,
            env=dict(os.environ),
        )
        pid = session.pid
        self.assertTrue(_process_alive(pid), "会话进程未起来")
        manager.close(session.id)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _process_alive(pid):
            time.sleep(0.05)
        self.assertFalse(_process_alive(pid), "关闭后子进程仍残留")

    def test_close_unknown_id_raises(self) -> None:
        """关不存在的终端要报业务错误，而不是静默成功。"""
        manager = self._manager()
        with self.assertRaises(TerminalError):
            manager.close("term_nonexistent")

    # ---------------------------------------------------------------- ④ 降级
    def test_fallback_to_pipe_when_pty_unavailable(self) -> None:
        """伪终端起不来时降级为管道模式：功能不崩、如实报原因。"""

        def boom(*args, **kwargs):
            raise RuntimeError("ConPTY 不可用（模拟）")

        collector = _Collector()
        manager = self._manager(spawn=boom)
        session = manager.open(
            argv=[sys.executable, "-u", "-i", "-q"],
            cwd=self.dir.name,
            env=dict(os.environ),
            cols=80,
            rows=24,
            on_data=collector,
        )
        self.assertEqual(session.mode, "pipe")
        self.assertIn("ConPTY", manager.fallback_reason)
        # 降级后照样能写能读。
        manager.write(session.id, f"print('{MARK}')\n".encode("utf-8"))
        self.assertTrue(
            collector.wait_for(MARK),
            f"降级模式下读不到输出；已收：{collector.text()[:200]!r}",
        )
        # resize 在管道模式下不报错。
        manager.resize(session.id, 100, 30)

    def test_max_sessions_limit(self) -> None:
        """超过上限明确拒绝，而不是无限起进程。"""
        manager = self._manager(max_sessions=1)
        first = manager.open(
            argv=[sys.executable, "-u", "-c", "import time; time.sleep(300)"],
            cwd=self.dir.name,
            env=dict(os.environ),
        )
        with self.assertRaises(TerminalError):
            manager.open(
                argv=[sys.executable, "-u", "-c", "import time; time.sleep(300)"],
                cwd=self.dir.name,
                env=dict(os.environ),
            )
        manager.close(first.id)


class ShellArgvTest(unittest.TestCase):
    def test_default_shell_argv_respects_config(self) -> None:
        self.assertEqual(default_shell_argv("zsh"), ["zsh"])

    def test_default_shell_argv_platform(self) -> None:
        argv = default_shell_argv("")
        self.assertTrue(argv)
        if os.name == "nt":
            self.assertEqual(argv[0], "powershell.exe")
        else:
            self.assertIn("sh", argv[0])


if __name__ == "__main__":
    unittest.main()