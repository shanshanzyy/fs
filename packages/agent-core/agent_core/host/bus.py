"""事件总线：把 Agent 的内部动作广播给所有已连接的界面。

两个设计要点：

1. **带历史回放。** 界面刷新或断线重连后不应该丢掉当前会话 ——
   总线保留最近 N 条事件，新订阅者可以指定 `since` 把漏掉的补齐。
   没有这个，用户按一下 F5 就得从头再来一遍。
2. **队列满时丢最旧的，绝不阻塞生产者。** 生产者是 Agent 的工作线程，
   如果因为它要等一个卡住的浏览器而停下，整个任务就悬住了。
   宁可让某个慢客户端漏掉中间事件，也不能让 Agent 停摆。
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any

DEFAULT_HISTORY = 2_000
DEFAULT_QUEUE_SIZE = 512


class Subscription:
    """一个 SSE 连接的收件箱。"""

    def __init__(self, bus: "EventBus", inbox: queue.Queue[dict[str, Any]]) -> None:
        self._bus = bus
        self._inbox = inbox
        self._closed = False

    def get(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """取一条事件；超时返回 None（让调用方有机会发心跳）。"""
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self._inbox)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """线程安全的广播器。Agent 线程写，HTTP 线程读。"""

    def __init__(
        self,
        history: int = DEFAULT_HISTORY,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._inboxes: set[queue.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._seq = 0

    # ------------------------------------------------------------------ 写
    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        """广播一条事件并返回补好 id 的副本。"""
        with self._lock:
            self._seq += 1
            event = {**event, "id": self._seq}
            self._history.append(event)
            inboxes = list(self._inboxes)

        for inbox in inboxes:
            try:
                inbox.put_nowait(event)
            except queue.Full:
                # 该客户端跟不上：丢弃最旧的一条给它腾位置。
                # 不阻塞生产者是硬要求 —— 见模块头的说明。
                try:
                    inbox.get_nowait()
                    inbox.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        return event

    # ------------------------------------------------------------------ 读
    def subscribe(self, since: int = 0) -> Subscription:
        """订阅事件流。`since` 是已收到的最大事件 id，用于补齐断线期间的事件。"""
        inbox: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            if since > 0:
                for event in self._history:
                    if event["id"] > since:
                        try:
                            inbox.put_nowait(event)
                        except queue.Full:
                            break
            self._inboxes.add(inbox)
        return Subscription(self, inbox)

    def _unsubscribe(self, inbox: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._inboxes.discard(inbox)

    def snapshot(self, limit: int = 60) -> list[dict[str, Any]]:
        """最近的事件，供界面初次加载时快速铺出当前会话。"""
        with self._lock:
            events = list(self._history)
        return events[-limit:]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._seq

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._inboxes)
