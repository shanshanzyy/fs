"""真实交互式终端（阶段 M / P1-7）：Unix PTY / Windows ConPTY / 管道降级。

## 定位与边界

终端是**用户在 IDE 里敲命令**（ADR-011 范式）：不走审批浮层，但记审计、受路径
沙箱约束，且**与 Agent 通道彻底隔离** —— Agent 仍只能走受审批的 `run_command`。

三条硬约束：

1. **零强制依赖** —— Unix 用标准库 `pty`，Windows 用 `ctypes` 调 ConPTY，
   `dependencies` 仍为 `[]`；
2. **不可用即降级** —— ConPTY 探不到就退到管道模式（`mode="pipe"`），
   如实回原因，绝不半启动；
3. **进程生命周期有收尾** —— 上限 / 空闲回收 / `atexit` 兜底 kill，
   Windows 走 `taskkill /T /F` 口径，避免孤儿进程（见 manager.py）。
"""

from .manager import (
    DEFAULT_IDLE_SECONDS,
    DEFAULT_MAX_SESSIONS,
    TerminalError,
    TerminalManager,
    TerminalSession,
    get_manager,
    set_manager,
)

__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_SESSIONS",
    "TerminalError",
    "TerminalManager",
    "TerminalSession",
    "get_manager",
    "set_manager",
]