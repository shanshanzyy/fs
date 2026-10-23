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

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.remote.errors import RemoteConfigError  # noqa: E402
from agent_core.terminal.manager import (  # noqa: E402
    TerminalError,
    TerminalManager,
    default_shell_argv,
    shell_label,
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
        """显式配了上限就按上限拦（`VIBE_TERM_MAX` 仍然有效）。"""
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

    def test_default_is_unlimited(self) -> None:
        """**默认不设上限**：能同时开 4 个以上终端（原来第 4 个会被拒）。

        为什么这条要真起进程而不是只断言 `_max == 0`：上限是「拦在 open 里」的，
        只查字段发现不了「字段是 0 但判定写成了 `>= 0`」这类错。
        """
        manager = self._manager()  # 用默认值
        self.assertEqual(manager.status()["maxSessions"], 0)
        ids: list[str] = []
        try:
            for _ in range(5):
                session = manager.open(
                    argv=[sys.executable, "-u", "-c", "import time; time.sleep(300)"],
                    cwd=self.dir.name,
                    env=dict(os.environ),
                )
                ids.append(session.id)
            self.assertEqual(len(manager.status()["sessions"]), 5)
        finally:
            for term_id in ids:
                manager.close(term_id)

    def test_unlimited_zero_also_from_config_setter(self) -> None:
        """`set_max_sessions(0)` = 解除上限；非法值也按「不限」处理，不把入口闩死。"""
        manager = self._manager(max_sessions=1)
        manager.set_max_sessions(0)
        self.assertEqual(manager.status()["maxSessions"], 0)
        manager.set_max_sessions("这不是数字")
        self.assertEqual(manager.status()["maxSessions"], 0)

    def test_shell_label_uses_basename_only(self) -> None:
        """标签名只取 argv[0] 的 basename —— 参数里的路径不该被显示出去。"""
        self.assertEqual(shell_label(["/usr/bin/zsh", "-l"]), "zsh")
        self.assertEqual(shell_label([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]),
                         "powershell")
        self.assertEqual(shell_label([]), "shell")


class RemoteTerminalArgvTest(unittest.TestCase):
    """远程工作区里的终端：argv 只能是 `ssh -tt …`，并且落在连接 root（+子目录）上。

    这一层要防的是**错配**：文件操作已经路由到远端，终端却还在本机跑 ——
    用户以为在改远端的服务，实际命令全在本机执行。所以这里钉住三件事：
    ① argv 首项是 `ssh` 而不是本地 shell；② `-tt` 强制远端 TTY（否则
    `vim`/`top` 退化）；③ 起始目录是「连接 root + `@remote/<连接>/<子目录>`」。
    """

    class _FakeTransport:
        def __init__(self) -> None:
            self.extra: list[str] = []

        def base_argv(self, *, extra_options=()):
            self.extra = list(extra_options)
            return ["ssh", *extra_options, "dev@10.0.0.5"]

    class _FakeWorkspace:
        def __init__(self, transport) -> None:
            self.transport = transport

    class _FakeSessions:
        def __init__(self, conn, workspace) -> None:
            self._conn = conn
            self._workspace = workspace

        def active(self):
            return self._conn

        def require_workspace(self):
            return self._workspace

    def _sessions(self, *, root: str = "/home/dev", name: str = "devbox"):
        from types import SimpleNamespace

        conn = SimpleNamespace(root_normalized=root, name=name)
        transport = self._FakeTransport()
        return self._FakeSessions(conn, self._FakeWorkspace(transport)), transport

    def test_argv_is_ssh_with_remote_tty(self) -> None:
        from agent_core.host.session import remote_terminal_argv

        sessions, transport = self._sessions()
        argv, connection = remote_terminal_argv(sessions, "@remote/devbox")
        self.assertEqual(connection, "devbox")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-tt", transport.extra)   # 常量由代码拼出，不经用户输入
        self.assertIn("-tt", argv)
        self.assertEqual(argv[-1], 'cd /home/dev && exec "$SHELL" -l')

    def test_sub_directory_is_quoted(self) -> None:
        from agent_core.host.session import remote_terminal_argv

        sessions, _transport = self._sessions()
        argv, _connection = remote_terminal_argv(sessions, "@remote/devbox/项目/src")
        # 单引号包裹：目录名带空格/中文时不会被远端 shell 拆成两段。
        self.assertEqual(argv[-1], "cd '/home/dev/项目/src' && exec \"$SHELL\" -l")

    def test_root_slash_starts_in_home(self) -> None:
        from agent_core.host.session import remote_terminal_argv

        sessions, _transport = self._sessions(root="/")
        argv, _connection = remote_terminal_argv(sessions, "@remote/devbox")
        # 根是 `/` 时不做 cd：直接交给登录 shell，落到它自己的家目录。
        self.assertEqual(argv[-1], "dev@10.0.0.5")

    def test_unconnected_raises(self) -> None:
        from agent_core.host.session import remote_terminal_argv

        sessions, _transport = self._sessions()
        sessions._conn = None
        with self.assertRaises(ValueError):
            remote_terminal_argv(sessions, "@remote/devbox")


class ManifestTerminalArgvTest(unittest.TestCase):
    """清单驱动工作区（M11 §7）里的终端：同样必须落在远端设备上。

    这类工作区的目录里只有 `.vibe/remote.json`，代码全在设备上。若终端仍在本机
    起 powershell，用户就会对着一个空的本机目录（如
    `C:\\Users\\...\\VibeWorkspaces\\gdhd-10.0.1.50`）敲命令 —— 看着像项目、
    实际一行代码都不在。所以钉住三件事：
    ① 有清单 → argv 是 `ssh -tt …`，起始目录为连接 root，回传连接名；
    ② 没有清单 → 返回 None（调用方按本机终端处理，行为一字不变）；
    ③ 多条连接又没设默认 → 抛错而不是猜（猜错就是把命令打到错的机器上）。
    """

    class _FakeTransport:
        def __init__(self) -> None:
            self.extra: list[str] = []

        def base_argv(self, *, extra_options=()):
            self.extra = list(extra_options)
            return ["ssh", *extra_options, "gdhd@10.0.1.50"]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, payload: dict) -> None:
        (self.ws / ".vibe" / "remote.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _call(self):
        from types import SimpleNamespace

        from agent_core.host import session as session_mod

        transport = self._FakeTransport()
        config = SimpleNamespace(workspace=str(self.ws), remote_config="")
        with mock.patch.object(session_mod, "transport_for", lambda _conn: transport):
            return session_mod.manifest_terminal_argv(config), transport

    def _connection(self, **overrides) -> dict:
        entry = {
            "name": "gdhd-10.0.1.50",
            "host": "10.0.1.50",
            "user": "gdhd",
            "root": "/home/gdhd",
        }
        entry.update(overrides)
        return entry

    def test_manifest_connection_starts_ssh_at_root(self) -> None:
        self._write({
            "version": 1,
            "default": "gdhd-10.0.1.50",
            "connections": [self._connection()],
        })
        result, transport = self._call()
        self.assertIsNotNone(result)
        argv, connection = result
        self.assertEqual(connection, "gdhd-10.0.1.50")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-tt", transport.extra)   # 常量由代码拼出，不经用户输入
        self.assertEqual(argv[-1], 'cd /home/gdhd && exec "$SHELL" -l')

    def test_root_slash_starts_in_home(self) -> None:
        self._write({
            "version": 1,
            "default": "gdhd-10.0.1.50",
            "connections": [self._connection(root="/")],
        })
        result, _transport = self._call()
        self.assertIsNotNone(result)
        argv, _connection = result
        # 根是 `/` 时不加 cd：交给登录 shell，落到它自己的家目录。
        self.assertEqual(argv[-1], "gdhd@10.0.1.50")

    def test_no_manifest_returns_none(self) -> None:
        result, _transport = self._call()
        self.assertIsNone(result)

    def test_local_project_with_connections_stays_local(self) -> None:
        """本地项目只是碰巧配了远端连接 → 终端仍在本机（别把本地 shell 换成 ssh）。

        判据是「除 `.vibe/` 外没有任何本地内容」：有项目文件就说明这是本地工作区，
        远端连接只是用来浏览/操作设备的。
        """
        (self.ws / "main.py").write_text("print(1)\n", encoding="utf-8")
        self._write({
            "version": 1,
            "default": "gdhd-10.0.1.50",
            "connections": [self._connection()],
        })
        result, _transport = self._call()
        self.assertIsNone(result)

    def test_multiple_connections_without_default_refuses_to_guess(self) -> None:
        self._write({
            "version": 1,
            "connections": [
                self._connection(name="a", host="1.1.1.1"),
                self._connection(name="b", host="2.2.2.2"),
            ],
        })
        with self.assertRaises(RemoteConfigError):
            self._call()


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


@unittest.skipUnless(os.name == "nt", "ConPTY 只在 Windows 上有")
class ConPtyAttributeTest(unittest.TestCase):
    """`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 的 lpValue 必须是 HPCON 值本身。

    这是实测踩过的坑：传 `&hpc` 时 UpdateProcThreadAttribute 照样返回成功，
    但 CreateProcess 把伪控制台当成没绑上 —— 子进程被当成普通控制台程序，
    **弹出一个原生终端窗口**（用户反馈「点开启终端跳出本机的终端」），
    同时输出全写进那个真控制台、管道一个字节都收不到（随后被判「哑终端」降级）。
    """

    class _FakeKernel:
        """只记录参数、不做真实 Win32 调用的假 kernel32。"""

        def __init__(self) -> None:
            self.updates: list[object] = []

        def InitializeProcThreadAttributeList(self, buf, count, flags, size_ptr):
            if buf is None:
                size_ptr._obj.value = 48  # 假装「所需尺寸 = 48」
                return 0
            return 1

        def UpdateProcThreadAttribute(self, buf, flags, attr, value, size, reserved, prev):
            self.updates.append(value)
            return 1

        def DeleteProcThreadAttributeList(self, buf):
            return None

    def test_lpvalue_is_hpcon_value_itself(self) -> None:
        import ctypes

        from agent_core.terminal import pty_windows

        fake = self._FakeKernel()
        original = pty_windows._load_kernel32
        pty_windows._load_kernel32 = lambda: fake  # type: ignore[assignment]
        try:
            session = pty_windows.WindowsConPtySession.__new__(
                pty_windows.WindowsConPtySession
            )
            hpc = pty_windows.wintypes.HANDLE(0x1234)
            session._build_attr_list(hpc)
        finally:
            pty_windows._load_kernel32 = original  # type: ignore[assignment]

        self.assertEqual(len(fake.updates), 1)
        value = fake.updates[0]
        self.assertIsInstance(value, ctypes.c_void_p)
        # 关键断言：传的是 HPCON 值本身，不是「指向它的指针」。
        self.assertEqual(value.value, 0x1234)


class ConPtyFlagContractTest(unittest.TestCase):
    """ConPTY 子进程的启动约定 —— 两条都是实测踩出来的，缺一条就退化成管道模式。

    1. **不能带 `CREATE_NO_WINDOW`**：它与伪控制台属性冲突。带上它 CreateProcess
       照样成功，但子进程拿不到控制台，伪控制台连开屏序列（清屏/标题/光标）都收不到；
    2. **必须显式声明 `STARTF_USESTDHANDLES` 且三个标准句柄置 NULL**：桌面壳用
       `stdio: ['ignore','pipe','pipe']` 拉起内核，不声明时 shell 会继承内核被重定向的
       stdio，把提示符和回显写进内核管道 —— 伪控制台只收到开屏那 89 字节。
    """

    def setUp(self) -> None:
        self.source = (
            Path(__file__).resolve().parents[1]
            / "agent_core"
            / "terminal"
            / "pty_windows.py"
        ).read_text(encoding="utf-8")

    def test_child_process_flags_without_create_no_window(self) -> None:
        self.assertIn(
            "flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT\n",
            self.source,
        )
        self.assertNotIn("| CREATE_NO_WINDOW", self.source)

    def test_std_handles_explicitly_null(self) -> None:
        self.assertIn("si.StartupInfo.dwFlags |= STARTF_USESTDHANDLES", self.source)
        for handle in ("hStdInput", "hStdOutput", "hStdError"):
            self.assertIn(f"si.StartupInfo.{handle} = None", self.source)


if __name__ == "__main__":
    unittest.main()