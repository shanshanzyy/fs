"""桌面端接入层。

这一层存在的唯一理由：把 M0 那个「只会在终端里自言自语」的 Agent，
变成一个可以被图形界面驱动、观察和中断的会话 ——
同时**不把界面代码混进 Agent 循环**。

分层是刻意的：

    Agent 循环（M0，未改一行逻辑）
        ↑ 通过 AgentHooks / Approver 这两个既有接缝接入
    会话层 Session —— 事件翻译 + 审批会合 + 中断语义
        ↑
    事件总线 EventBus —— 带历史回放的广播
        ↑
    HTTP/SSE 服务 —— 回到「界面只是个网页」

因此换掉 Electron 只影响最后一层，`agent-core` 本身一行都不用动。
"""

from .bus import EventBus, Subscription
from .security import Forbidden, HostGuard
from .server import HostServer, main
from .session import BusyError, Session

__all__ = [
    "BusyError",
    "EventBus",
    "Forbidden",
    "HostGuard",
    "HostServer",
    "Session",
    "Subscription",
    "main",
]
