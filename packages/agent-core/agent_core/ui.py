"""终端渲染：颜色、分级输出、Diff 着色、审批交互。

刻意不引入 rich / colorama：M0 要求零依赖，且内网环境 pip 源不一定可用。
Windows 下通过 ctypes 打开虚拟终端序列（VT）支持，让 ANSI 颜色生效。
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Sequence

# --- ANSI 色板 -------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GRAY = "\033[90m"

_COLOR_ENABLED = True


def _enable_windows_vt() -> None:
    """在 Windows 控制台启用 ANSI 转义序列。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # STDOUT, STDERR
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        # 拿不到控制台（重定向到文件、IDE 捕获等）时静默降级
        pass


def init_console() -> None:
    """初始化控制台：UTF-8 输出 + ANSI 支持。CLI 启动时调用一次。"""
    global _COLOR_ENABLED
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    _enable_windows_vt()
    if os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
    if not sys.stdout.isatty():
        _COLOR_ENABLED = False


def paint(text: str, *styles: str) -> str:
    if not _COLOR_ENABLED or not styles:
        return text
    return "".join(styles) + text + RESET


def info(message: str) -> None:
    print(paint("· ", GRAY) + message)


def ok(message: str) -> None:
    print(paint("✓ ", GREEN) + message)


def warn(message: str) -> None:
    print(paint("! ", YELLOW) + message, file=sys.stderr)


def error(message: str) -> None:
    print(paint("✗ ", RED) + message, file=sys.stderr)


def step(prefix: str, message: str) -> None:
    print(paint(f"{prefix} ", CYAN) + message)


def rule(title: str = "") -> None:
    width = min(shutil_width(), 78)
    if title:
        line = f"── {title} " + "─" * max(0, width - len(title) - 4)
    else:
        line = "─" * width
    print(paint(line, GRAY))


def shutil_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def print_diff(diff: str, max_lines: int = 200) -> None:
    """按 unified diff 语义着色输出。"""
    lines = diff.splitlines()
    shown = lines[:max_lines]
    for line in shown:
        if line.startswith("+++") or line.startswith("---"):
            print(paint(line, BOLD))
        elif line.startswith("@@"):
            print(paint(line, CYAN))
        elif line.startswith("+"):
            print(paint(line, GREEN))
        elif line.startswith("-"):
            print(paint(line, RED))
        else:
            print(paint(line, GRAY))
    if len(lines) > max_lines:
        print(paint(f"... 省略 {len(lines) - max_lines} 行", GRAY))


def print_block(text: str, limit: int = 4000) -> None:
    if len(text) > limit:
        print(text[:limit] + paint(f"\n... 已截断（共 {len(text)} 字符）", GRAY))
    else:
        print(text)


def confirm(question: str, options: Sequence[str], default: str) -> str:
    """从 options 中读一个单字符选择。EOF / 非交互时返回 default。"""
    rendered = "/".join(o.upper() if o == default else o for o in options)
    while True:
        try:
            raw = input(paint(f"? {question} [{rendered}] ", MAGENTA + BOLD))
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        choice = raw.strip().lower()
        if choice == "":
            return default
        if choice in options:
            return choice
        # 允许输入完整单词
        for opt in options:
            if opt.startswith(choice):
                return opt
        warn(f"无效输入，可选：{', '.join(options)}")


def bullet_list(items: Iterable[str], marker: str = "  ") -> None:
    for item in items:
        print(marker + item)
