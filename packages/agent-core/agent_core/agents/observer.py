"""观测折叠（M14-B2）：把总线增量折成一段「知情不传话」的观测块。

三个约束决定了这里的实现：

1. **预算由上下文推导**（§4.6）：
   `clamp(round(context_window * ratio), 400, 1200)`；上下文未知时用配置默认
   600。上下文越大允许看得越多，但永远不超过 1200 —— 只留一个可解释的旋钮。
2. **首轮防锚定**：第 1 轮默认只给「某人在作答」的状态，他人正文要等该人本轮
   结束（`turn.done` 里的要点）才进入观测块；`first_round_live=True` 可关掉。
3. **他人输出一律不可信**：每个 Agent 的分组都过 `wrap_untrusted` 并打
   `source=agent:<名>`，明确「这是另一位智能体的说法，不是事实」。

粒度档位（`monitor_mode`）：

    status  状态 + 工具名
    live    以上 + 增量每累计 300 字或 3 秒折叠一次 + 完成发言的最新 3 条要点
    full    以上 + 增量原文

观测高频且价值低，**不落审计**（审计只记 `multiagent.interject`，见 C 段）。
"""

from __future__ import annotations

import time
from typing import Callable, Mapping

from ..tools import wrap_untrusted
from .bus import BusEvent, DiscussionBus, WatchCursor

MONITOR_MODES = ("status", "live", "full")
MIN_BUDGET = 400
MAX_BUDGET = 1200
DEFAULT_BUDGET = 600
DEFAULT_RATIO = 0.005
FOLD_CHARS = 300
FOLD_SECONDS = 3.0
MAX_POINTS = 3
MAX_SEGMENTS = 3
MAX_NOTES = 3
TRUNCATED = "…（已截断）"


def observation_budget(
    context_window: int,
    ratio: float = DEFAULT_RATIO,
    default: int = DEFAULT_BUDGET,
) -> int:
    """按上下文预算推导观测块字符上限（钳制在 400–1200）。"""
    if context_window <= 0 or ratio <= 0:
        return default
    return max(MIN_BUDGET, min(MAX_BUDGET, int(round(context_window * ratio))))


class Observer:
    """一个 Agent 的观测视图。每场讨论每人一个，游标跨轮保留。"""

    def __init__(
        self,
        bus: DiscussionBus,
        *,
        agent_id: str,
        names: Mapping[str, str] | None = None,
        monitor: str = "live",
        budget: int = DEFAULT_BUDGET,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if monitor not in MONITOR_MODES:
            raise ValueError(f"未知的监控档位：{monitor}")
        self.bus = bus
        self.agent_id = agent_id
        self.names = dict(names or {})
        self.monitor = monitor
        self.budget = max(MIN_BUDGET, min(MAX_BUDGET, int(budget)))
        self._clock = clock
        self.cursor: WatchCursor = bus.cursor(agent_id)
        self._order: list[str] = []
        self._status: dict[str, str] = {}
        self._tools: dict[str, list[str]] = {}
        self._points: dict[str, list[str]] = {}
        self._buffer: dict[str, list[str]] = {}
        self._buffer_at: dict[str, float] = {}
        self._folds: dict[str, list[str]] = {}
        self._raw: dict[str, list[str]] = {}
        self._notes: list[str] = []

    # ---- 读取 -------------------------------------------------------------
    def build(self, *, round_no: int = 1, first_round_live: bool = False) -> str:
        """取走尚未看过的事件，折成观测块。无内容时返回空串（不插临时消息）。"""
        self._consume(self.cursor.drain(), round_no=round_no)
        self._flush_stale()
        if not self._order:
            return ""
        silent = round_no <= 1 and not first_round_live
        groups: list[tuple[str, list[str]]] = []
        for agent_id in self._order:
            body = self._body_for(agent_id, silent=silent)
            if body:
                groups.append((self.names.get(agent_id, agent_id), body))
        if not groups:
            return ""
        return self._render(groups)

    # ---- 事件折叠 ---------------------------------------------------------
    def _consume(self, events: list[BusEvent], *, round_no: int) -> None:
        for event in events:
            if event.agent == self.agent_id:
                continue  # 自己的输出没有观测价值
            if event.kind == "turn.delta":
                text = str(event.data.get("text") or "")
                if not text:
                    continue
                self._touch(event.agent, "作答中")
                if self.monitor == "status":
                    continue
                if self.monitor == "full":
                    raw = self._raw.setdefault(event.agent, [])
                    raw.append(text)
                    del raw[:-8]
                buffer = self._buffer.setdefault(event.agent, [])
                buffer.append(text)
                self._buffer_at.setdefault(event.agent, self._clock())
                if sum(len(item) for item in buffer) >= FOLD_CHARS:
                    self._fold(event.agent)
            elif event.kind == "tool.call":
                self._touch(event.agent, "调用工具")
                name = str(event.data.get("name") or "")
                tools = self._tools.setdefault(event.agent, [])
                if name and name not in tools:
                    tools.append(name)
                    del tools[:-3]
            elif event.kind == "tool.result":
                self._touch(event.agent, "作答中")
            elif event.kind in ("turn.point", "turn.done"):
                self._touch(event.agent, "已完成")
                self._merge_points(event.agent, event.data.get("points"))
            elif event.kind == "interjection":
                text = str(event.data.get("text") or "").strip()
                if text and event.data.get("accepted"):
                    note = f"{self.names.get(event.agent, event.agent)} 请求插话：{text[:120]}"
                    if note not in self._notes:
                        self._notes.append(note)
                        del self._notes[:-MAX_NOTES]

    def _touch(self, agent_id: str, status: str) -> None:
        if agent_id not in self._order:
            self._order.append(agent_id)
        self._status[agent_id] = status

    def _merge_points(self, agent_id: str, points: object) -> None:
        if not isinstance(points, list):
            return
        stored = self._points.setdefault(agent_id, [])
        for item in points:
            text = str(item).strip()
            if text and text not in stored:
                stored.append(text)
        del stored[:-MAX_POINTS]

    def _fold(self, agent_id: str) -> None:
        buffer = self._buffer.get(agent_id) or []
        if not buffer:
            return
        text = "".join(buffer).strip()
        self._buffer[agent_id] = []
        self._buffer_at.pop(agent_id, None)
        if not text:
            return
        folds = self._folds.setdefault(agent_id, [])
        folds.append(text[: FOLD_CHARS * 2])
        del folds[:-MAX_SEGMENTS]

    def _flush_stale(self) -> None:
        now = self._clock()
        for agent_id, started in list(self._buffer_at.items()):
            if now - started >= FOLD_SECONDS:
                self._fold(agent_id)

    # ---- 渲染 -------------------------------------------------------------
    def _body_for(self, agent_id: str, *, silent: bool) -> list[str]:
        lines: list[str] = []
        status = self._status.get(agent_id, "等待")
        tools = self._tools.get(agent_id) or []
        head = f"状态：{status}"
        if tools:
            head += "（工具：" + "、".join(tools) + "）"
        lines.append(head)
        if self.monitor == "status":
            return lines
        if not silent:
            for segment in (self._folds.get(agent_id) or [])[-MAX_SEGMENTS:]:
                lines.append("发言片段：" + " ".join(segment.split()))
        points = self._points.get(agent_id) or []
        if points:
            lines.append("已完成要点：")
            lines.extend(f"- {point}" for point in points[-MAX_POINTS:])
        if self.monitor == "full" and not silent:
            raw = (self._raw.get(agent_id) or [])[-8:]
            joined = "".join(raw).strip()
            if joined:
                lines.append("增量原文：" + " ".join(joined.split()))
        return lines

    def _render(self, groups: list[tuple[str, list[str]]]) -> str:
        notes = list(self._notes)
        bodies: list[tuple[str, str]] = [
            (name, "\n".join(lines)) for name, lines in groups
        ]
        if notes:
            bodies.append(("讨论", "\n".join(notes)))
        overhead = sum(len(self._wrapper(name, "")) for name, _ in bodies)
        allowance = max(0, self.budget - overhead)
        rendered: list[str] = []
        for name, body in bodies:
            if allowance <= 0:
                break
            chunk = body if len(body) <= allowance else body[: max(0, allowance - 1)] + "…"
            allowance -= len(chunk)
            rendered.append(self._wrapper(name, chunk))
        text = "\n".join(rendered)
        if len(text) > self.budget:  # 兜底：worst case 仍不越界
            text = text[: max(0, self.budget - len(TRUNCATED))] + TRUNCATED
        return text

    @staticmethod
    def _wrapper(name: str, body: str) -> str:
        return wrap_untrusted(f"agent:{name}", body)
