"""讨论事件总线（M14-B2）：单写者 / 多读者，只追加。

为什么不是「传话」而是「知情」：讨论里的模型调用是**串行**的，B 不可能在 A
生成到一半时插进来。所以「实时」的准确含义是 —— 事件实时可见，反应发生在
对端下一次迭代。编排器把 A 的增量、工具动作、完成发言写进总线；每个 Agent
持一个 `WatchCursor`，在**自己下一次模型调用前**取走尚未看过的增量。

约定：

- 只有编排器（`DiscussionEngine`）调用 `publish()`；Agent 与 UI 只读。
- `seq` 单调递增，读者各自记自己的位置，不回溯、不重复。
- 事件只是观测素材，**不进会话历史**；拼接与包裹见 `observer.py`。
- 缓冲区有上限（默认 2000 条）。超出后最旧的事件被丢弃 —— 观测只关心最近
  发生了什么，丢旧事件不影响正确性；读者游标落在更早位置时会拿到缓冲区里
  仍存在的全部事件。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

EVENT_KINDS = (
    "turn.delta",
    "turn.point",
    "tool.call",
    "tool.result",
    "turn.done",
    "interjection",
)

MAX_EVENTS = 2000


@dataclass(frozen=True)
class BusEvent:
    seq: int
    kind: str
    agent: str
    turn_id: str = ""
    round: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "agent": self.agent,
            "turnId": self.turn_id,
            "round": self.round,
            "data": dict(self.data),
        }


class WatchCursor:
    """一个读者的位置。`drain()` 后位置前移，重复调用不会重复拿到同一事件。"""

    def __init__(self, bus: "DiscussionBus", owner: str = "") -> None:
        self.bus = bus
        self.owner = owner
        self.seq = 0

    def drain(self) -> list[BusEvent]:
        events = self.bus.since(self.seq)
        if events:
            self.seq = events[-1].seq
        return events

    def pending(self) -> int:
        return max(0, self.bus.last_seq - self.seq)


class DiscussionBus:
    """只追加的事件缓冲区。`publish()` 只该由编排器调用。"""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._events: deque[BusEvent] = deque(maxlen=max(1, int(max_events)))
        self._seq = 0

    @property
    def last_seq(self) -> int:
        return self._seq

    def publish(
        self,
        kind: str,
        *,
        agent: str,
        turn_id: str = "",
        round_no: int = 0,
        **data: Any,
    ) -> BusEvent:
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知的讨论事件类型：{kind}")
        self._seq += 1
        event = BusEvent(
            seq=self._seq,
            kind=kind,
            agent=str(agent),
            turn_id=str(turn_id),
            round=int(round_no),
            data=dict(data),
        )
        self._events.append(event)
        return event

    def since(self, seq: int) -> list[BusEvent]:
        """返回 `seq` 之后的事件。缓冲区已丢弃的更早事件不会再出现。"""
        return [event for event in self._events if event.seq > seq]

    def cursor(self, owner: str = "") -> WatchCursor:
        return WatchCursor(self, owner)

    def events(self) -> list[BusEvent]:
        return list(self._events)
