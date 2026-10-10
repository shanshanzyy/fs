"""Unix 伪终端后端（阶段 M / P1-7）。

用标准库 `os.openpty` + `subprocess` 把一个子进程挂到伪终端的从端上，
主端就是这个会话的「键盘 + 屏幕」：写主端 = 敲键盘，读主端 = 看屏幕。
`fcntl.TIOCSWINSZ` 设置窗口尺寸，子进程收到 `SIGWINCH` 后，交互程序
（top / vim / python REPL）会自己重排 —— 这是「真实终端」与管道回显的区别。

零第三方依赖（刻意不用 `ptyprocess` / `pexpect`）。

## 为什么子进程要 `start_new_session=True`

它让子进程成为一个**新会话/进程组的组长**。关闭终端时对**整个进程组**
发信号，才能连 shell 拉起的一串子进程一起收掉 —— 否则 `bash` 死了、
它里面的 `sleep 1000` 还挂在后台，界面退出后留下一串孤儿。
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import struct
import subprocess
import termios
import time

READ_CHUNK = 65536


class PtyError(RuntimeError):
    """PTY 创建 / 读写失败。"""


def _terminate_tree(pid: int) -> None:
    """对进程组依次 HUP → TERM → KILL，确保整棵树都被收掉。"""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.1)


class UnixPtySession:
    """一个挂在伪终端上的子进程。接口见 `terminal/manager.py` 的后端约定。"""

    mode = "pty"

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        *,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self.argv = list(argv)
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self._closed = False
        self._eof = False
        self._exit_code: int | None = None

        try:
            master, slave = os.openpty()
        except OSError as exc:  # pragma: no cover - 沙箱/容器里偶发
            raise PtyError(f"无法创建伪终端：{exc}") from None
        self._set_winsize(slave, self.rows, self.cols)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — argv 数组 + shell=False
                self.argv,
                cwd=cwd,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            os.close(master)
            os.close(slave)
            raise PtyError(f"终端程序启动失败：{exc}") from None
        os.close(slave)
        self.master = master
        # 非阻塞 + select：read 有超时，读线程不会因为一个卡住的子进程而悬住。
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # ------------------------------------------------------------------ 属性
    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return (not self._closed) and self._proc.poll() is None

    @property
    def eof(self) -> bool:
        return self._eof

    @property
    def exit_code(self) -> int | None:
        rc = self._proc.poll()
        return rc if rc is not None else self._exit_code

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    # ------------------------------------------------------------------ 读写
    def read(self, timeout: float = 0.2) -> bytes:
        if self._closed:
            return b""
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(self.master, READ_CHUNK)
        except OSError as exc:
            # EIO = 从端已全部关闭（子进程退出），按 EOF 处理即可。
            if exc.errno in (errno.EIO, errno.EBADF, errno.EAGAIN):
                self._eof = True
                return b""
            raise PtyError(f"读取终端输出失败：{exc}") from None

    def write(self, data: bytes) -> None:
        if not data or self._closed:
            return
        try:
            os.write(self.master, data)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                return
            raise PtyError(f"写入终端失败：{exc}") from None

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        if not self._closed:
            self._set_winsize(self.master, self.rows, self.cols)

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc.poll() is None:
            _terminate_tree(proc.pid)
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 — 收尸失败不影响关闭
            pass
        self._exit_code = proc.poll()
        try:
            os.close(self.master)
        except OSError:
            pass


def spawn(
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    *,
    cols: int = 80,
    rows: int = 24,
) -> UnixPtySession:
    """起一个 Unix 伪终端会话。失败抛 `PtyError`。"""
    return UnixPtySession(argv, cwd, env, cols=cols, rows=rows)


__all__ = ["PtyError", "UnixPtySession", "spawn"]