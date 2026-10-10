"""Windows ConPTY 后端（阶段 M / P1-7）—— 尽力而为，不可用即抛「不支持」。

Windows 10 1809 起提供 ConPTY（`CreatePseudoConsole`）。这里用 `ctypes` 直接调
Win32，**不引第三方**（如 `pywinpty` / `winpty`）—— 核心链路零强制依赖。

## 设计立场：宁可保守

任一步失败（API 不存在、结构体/管道创建失败、CreateProcess 返回 0）都抛
`TerminalUnsupported`，由 `terminal/manager.py` 降级为管道模式。**绝不半启动** ——
一个「看着起来了其实收不到输出」的终端，比明确降级更糟。

## 已知局限

- 只在 ConPTY 可用时启用；老系统自动降级（见 manager 的探测）。
- 不做鼠标事件 / 焦点事件上报（交互式终端够用即可，见计划「明确不做」）。
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import threading
from ctypes import wintypes

# ---- 常量 ------------------------------------------------------------------
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STILL_ACTIVE = 259
READ_CHUNK = 65536


class TerminalUnsupported(RuntimeError):
    """本机无法建立伪终端（ConPTY 不可用或调用失败）。"""


# ---- Win32 结构体 ----------------------------------------------------------
class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


_INVALID_HANDLE = ctypes.c_void_p(-1).value
_NEEDED = (
    "CreatePseudoConsole",
    "ResizePseudoConsole",
    "ClosePseudoConsole",
    "InitializeProcThreadAttributeList",
    "UpdateProcThreadAttribute",
    "DeleteProcThreadAttributeList",
    "CreatePipe",
    "CreateProcessW",
    "ReadFile",
    "WriteFile",
    "CloseHandle",
    "GetExitCodeProcess",
)

_kernel32: object = None  # False = 已探测但不可用；None = 未探测
_probe_lock = threading.Lock()


def _load_kernel32():
    global _kernel32
    with _probe_lock:
        if _kernel32 is not None:
            return _kernel32 or None
        if os.name != "nt":
            _kernel32 = False
            return None
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            _kernel32 = False
            return None
        if any(not hasattr(k, name) for name in _NEEDED):
            _kernel32 = False
            return None

        k.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.DWORD,
        ]
        k.CreatePipe.restype = wintypes.BOOL
        k.CreatePseudoConsole.argtypes = [
            COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        k.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT
        k.ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
        k.ResizePseudoConsole.restype = ctypes.c_long
        k.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        k.ClosePseudoConsole.restype = None
        k.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        k.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        k.CreateProcessW.restype = wintypes.BOOL
        k.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k.ReadFile.restype = wintypes.BOOL
        k.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k.WriteFile.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k.GetExitCodeProcess.restype = wintypes.BOOL

        _kernel32 = k
        return k


def available() -> bool:
    """ConPTY 是否可用（只探测 API 是否存在，不起进程）。"""
    return _load_kernel32() is not None


def _env_block(env: dict[str, str]) -> ctypes.Array:
    """构造 CreateProcessW 需要的 UTF-16 环境块。

    键按不区分大小写排序（Windows 的约定），双 \\0 结尾。
    """
    items = [f"{k}={v}" for k, v in env.items() if "=" not in k]
    items.sort(key=lambda s: s.split("=", 1)[0].upper())
    return ctypes.create_unicode_buffer("\0".join(items) + "\0\0")


class WindowsConPtySession:
    """一个挂在 ConPTY 上的子进程。接口同 `UnixPtySession`。"""

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
        k = _load_kernel32()
        if k is None:
            raise TerminalUnsupported("ConPTY 不可用（kernel32 缺少所需 API）")

        self.argv = list(argv)
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self._closed = False
        self._eof = False
        self._exit_code: int | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._hpc = None
        self._h_process = None
        self._h_thread = None
        self._h_in_write = None
        self._h_out_read = None
        self._attr_list = None

        # --- 管道：输入（我们写→控制台读）、输出（控制台写→我们读）--------
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True

        in_read = wintypes.HANDLE()
        in_write = wintypes.HANDLE()
        out_read = wintypes.HANDLE()
        out_write = wintypes.HANDLE()
        for r, w in ((ctypes.byref(in_read), ctypes.byref(in_write)),
                     (ctypes.byref(out_read), ctypes.byref(out_write))):
            if not k.CreatePipe(r, w, ctypes.byref(sa), 0):
                self._close_handles()
                raise TerminalUnsupported(
                    f"创建终端管道失败（错误码 {ctypes.get_last_error()}）"
                )

        hpc = wintypes.HANDLE()
        size = COORD(self.cols, self.rows)
        hres = k.CreatePseudoConsole(
            size, in_read, out_write, 0, ctypes.byref(hpc)
        )
        # 控制台已复制走它的两端，我们手上的这两个可以关掉。
        k.CloseHandle(in_read)
        k.CloseHandle(out_write)
        if hres != 0 or not hpc.value or hpc.value == _INVALID_HANDLE:
            self._close_handles(in_write, out_read)
            raise TerminalUnsupported(f"CreatePseudoConsole 失败（HRESULT 0x{hres & 0xFFFFFFFF:08x}）")
        self._hpc = hpc
        self._h_in_write = in_write
        self._h_out_read = out_read

        # --- 属性列表：把伪控制台绑到子进程 -------------------------------
        attr = self._build_attr_list(hpc)
        self._attr_list = attr

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)

        cmdline = subprocess.list2cmdline(self.argv)
        env_block = _env_block(env)
        pi = PROCESS_INFORMATION()
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        ok = k.CreateProcessW(
            None,
            ctypes.create_unicode_buffer(cmdline),
            None,
            None,
            False,
            flags,
            ctypes.cast(env_block, ctypes.c_void_p),
            str(cwd) if cwd else None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        k.DeleteProcThreadAttributeList(attr)
        self._attr_list = None
        if not ok:
            err = ctypes.get_last_error()
            k.ClosePseudoConsole(hpc)
            self._hpc = None
            self._close_handles(self._h_in_write, self._h_out_read)
            self._h_in_write = self._h_out_read = None
            raise TerminalUnsupported(f"CreateProcess 失败（错误码 {err}）")

        self._h_process = pi.hProcess
        self._h_thread = pi.hThread
        self._pid = int(pi.dwProcessId)

        self._reader = threading.Thread(
            target=self._read_loop, name="conpty-reader", daemon=True
        )
        self._reader.start()

    # ------------------------------------------------------------------ 内部
    def _build_attr_list(self, hpc):
        k = _load_kernel32()
        size = ctypes.c_size_t(0)
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if size.value == 0:
            k.ClosePseudoConsole(hpc)
            self._hpc = None
            self._close_handles(self._h_in_write, self._h_out_read)
            self._h_in_write = self._h_out_read = None
            raise TerminalUnsupported("InitializeProcThreadAttributeList 取尺寸失败")
        buf = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size)):
            raise TerminalUnsupported("InitializeProcThreadAttributeList 失败")
        hpc_ref = ctypes.c_void_p(hpc.value)
        if not k.UpdateProcThreadAttribute(
            buf,
            0,
            ctypes.byref(ctypes.c_size_t(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE)),
            ctypes.cast(ctypes.byref(hpc_ref), ctypes.c_void_p),
            ctypes.sizeof(wintypes.HANDLE),
            None,
            None,
        ):
            k.DeleteProcThreadAttributeList(buf)
            raise TerminalUnsupported("UpdateProcThreadAttribute 失败")
        return buf

    def _read_loop(self) -> None:
        k = _load_kernel32()
        buf = ctypes.create_string_buffer(READ_CHUNK)
        read = wintypes.DWORD(0)
        while True:
            ok = k.ReadFile(
                self._h_out_read, buf, READ_CHUNK, ctypes.byref(read), None
            )
            if not ok or read.value == 0:
                self._eof = True
                self._queue.put(None)
                return
            self._queue.put(bytes(buf.raw[: read.value]))

    def _close_handles(self, *extra) -> None:
        k = _load_kernel32()
        if k is None:
            return
        handles = [
            self._h_in_write,
            self._h_out_read,
            self._h_process,
            self._h_thread,
            *extra,
        ]
        for handle in handles:
            if handle:
                try:
                    k.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
        self._h_in_write = self._h_out_read = None
        self._h_process = self._h_thread = None

    # ------------------------------------------------------------------ 属性
    @property
    def pid(self) -> int:
        return self._pid

    @property
    def eof(self) -> bool:
        return self._eof

    @property
    def alive(self) -> bool:
        if self._closed or self._eof:
            return False
        k = _load_kernel32()
        if k is None or not self._h_process:
            return False
        code = wintypes.DWORD(0)
        if not k.GetExitCodeProcess(self._h_process, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE

    @property
    def exit_code(self) -> int | None:
        k = _load_kernel32()
        if k is None or not self._h_process:
            return self._exit_code
        code = wintypes.DWORD(0)
        if not k.GetExitCodeProcess(self._h_process, ctypes.byref(code)):
            return self._exit_code
        return None if code.value == STILL_ACTIVE else int(code.value)

    # ------------------------------------------------------------------ 读写
    def read(self, timeout: float = 0.2) -> bytes:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return b""
        if item is None:
            return b""
        return item

    def write(self, data: bytes) -> None:
        if not data or self._closed or not self._h_in_write:
            return
        k = _load_kernel32()
        written = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(data, len(data))
        k.WriteFile(self._h_in_write, buf, len(data), ctypes.byref(written), None)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        if self._closed or not self._hpc:
            return
        k = _load_kernel32()
        try:
            k.ResizePseudoConsole(self._hpc, COORD(self.cols, self.rows))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # 先按进程树杀（taskkill /T /F）：ConPTY 子进程常拉起一串子进程，
        # 只 TerminateProcess 主进程会留下孤儿（见记忆教训）。
        if self._pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self._pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            except OSError:
                pass
        k = _load_kernel32()
        if k is not None and self._hpc:
            try:
                k.ClosePseudoConsole(self._hpc)
            except Exception:  # noqa: BLE001
                pass
            self._hpc = None
        self._exit_code = self.exit_code
        self._close_handles()


def spawn(
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    *,
    cols: int = 80,
    rows: int = 24,
) -> WindowsConPtySession:
    """起一个 ConPTY 会话。不可用 / 失败一律抛 `TerminalUnsupported`。"""
    return WindowsConPtySession(argv, cwd, env, cols=cols, rows=rows)


__all__ = [
    "TerminalUnsupported",
    "WindowsConPtySession",
    "available",
    "spawn",
]