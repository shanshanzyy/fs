"""定时任务的数据模型（M10）：调度表达式 + 任务句柄。

设计纪律：
  - `Schedule.next_after(now)` 返回「下一次该跑的**本地**时间戳」；返回 `None`
    表示不再排期（一次性任务已过）。
  - `JobSpec.describe()` **只回元数据**（**不含 `prompt` 正文**），与
    `async_agent.AgentTask.describe()` 同纪律（ADR-006）。
  - 全程按**本机本地时间**（`time.localtime` / `time.mktime`）解析，**不做**
    时区 tz 与夏令时 DST 补偿（M10-PLAN「明确不做」）。
  - 零第三方依赖：只用 `calendar` / `datetime` 两个 stdlib 模块。
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any

# `next_after` 逐字段跳进的搜索上限：最多向前找 4 年，找不到即返回 None
#（防「永不匹配」的表达式把调度线程拖死）。
MAX_SEARCH_YEARS = 4

# job 名与调度表达式串的长度上限。
MAX_JOB_NAME_CHARS = 120
MAX_SCHEDULE_CHARS = 120

# 定时任务 prompt 的长度上限。与 `async_agent.pool.MAX_ASYNC_TASK_CHARS` 同量级：
# 定时任务的 prompt 也是「一整段要做的事」，不该无限长；在调度侧先拦一道，
# 免得攒到触发时才被任务池拒绝。
MAX_JOB_PROMPT_CHARS = 8000


def _next_match(field: frozenset[int], start: int) -> int | None:
    """返回 `field` 中 >= `start` 的最小值；没有则 None。"""
    candidates = [v for v in field if v >= start]
    return min(candidates) if candidates else None


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


class Schedule:
    """调度表达式抽象基类。"""

    kind: str = ""

    def next_after(self, now: float) -> float | None:
        """下一次触发时间戳（严格大于 `now`）；不再排期则 None。"""
        raise NotImplementedError

    def describe(self) -> str:
        """回显用的原始表达式。"""
        raise NotImplementedError


@dataclass(frozen=True)
class CronSchedule(Schedule):
    """5 字段 cron：`分 时 日 月 周`。

    `weekdays` 存 **Python weekday**（周一=0 … 周日=6）—— cron 的 `0` 与 `7`
    均归一为周日（Python 6）。
    """

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    # 字段是否被限定（原始文本不是 `*` / `?`）。`日` 与 `周` **同时被限定**时
    # 取 OR（Vixie cron 语义）。
    days_restricted: bool = False
    weekdays_restricted: bool = False

    kind = "cron"

    def next_after(self, now: float) -> float | None:
        # 从 now 之后的第一个整分钟开始（严格大于 now）。
        t = int(now // 60) * 60 + 60
        lt = time.localtime(t)
        year, month, day = lt.tm_year, lt.tm_mon, lt.tm_mday
        hour, minute = lt.tm_hour, lt.tm_min
        limit_year = year + MAX_SEARCH_YEARS

        while year < limit_year:
            if month not in self.months:
                # 跳到下月 1 号 00:00
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
                day, hour, minute = 1, 0, 0
                continue
            if not self._day_matches(year, month, day):
                nxt = day + 1
                if nxt > _days_in_month(year, month):
                    month, year = (1, year + 1) if month == 12 else (month + 1, year)
                    nxt = 1
                day, hour, minute = nxt, 0, 0
                continue
            h = _next_match(self.hours, hour)
            if h is None:
                nxt = day + 1
                if nxt > _days_in_month(year, month):
                    month, year = (1, year + 1) if month == 12 else (month + 1, year)
                    nxt = 1
                day, hour, minute = nxt, 0, 0
                continue
            if h > hour:
                hour, minute = h, 0
            m = _next_match(self.minutes, minute)
            if m is None:
                hour, minute = hour + 1, 0
                continue
            cand = time.mktime((year, month, day, hour, m, 0, 0, 0, -1))
            if cand > now:
                return float(cand)
            # 防御性推进：正常路径不会走到这里（候选必然 > now）。
            minute = m + 1
            if minute > 59:
                hour, minute = hour + 1, 0
        return None

    def _day_matches(self, year: int, month: int, day: int) -> bool:
        in_days = day in self.days
        wd = _cron_weekday_of(year, month, day)
        in_weekdays = wd in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return in_days or in_weekdays  # Vixie cron：两者都限定 → OR
        if self.days_restricted:
            return in_days
        if self.weekdays_restricted:
            return in_weekdays
        return True

    def describe(self) -> str:
        return self.expr


def _cron_weekday_of(year: int, month: int, day: int) -> int:
    """某天的 Python weekday（周一=0 … 周日=6）。"""
    # time.mktime 会做本地时区换算，但 weekday 不受影响；用它是为了零依赖。
    return time.localtime(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))).tm_wday


@dataclass(frozen=True)
class IntervalSchedule(Schedule):
    """`@every <n><unit>`：按固定周期触发（对齐 epoch 边界，确定可复现）。"""

    expr: str
    seconds: int

    kind = "interval"

    def next_after(self, now: float) -> float | None:
        step = max(1, int(self.seconds))
        return float(int(now // step) * step + step)

    def describe(self) -> str:
        return self.expr


@dataclass(frozen=True)
class OnceSchedule(Schedule):
    """`@at <ISO8601>`：一次性触发；触发后不再排期（None）。"""

    expr: str
    at: float

    kind = "once"

    def next_after(self, now: float) -> float | None:
        return self.at if self.at > now else None

    def describe(self) -> str:
        return self.expr


@dataclass(frozen=True)
class JobSpec:
    """一个定时任务的句柄。

    frozen —— 变更用 `dataclasses.replace()`（与 `Config` 同一纪律）。
    """

    job_id: str
    name: str
    schedule: str  # 调度表达式原文串（落库 / 回显用）
    prompt: str
    enabled: bool = True
    created_at: float = 0.0
    next_run_at: float = 0.0
    last_run_at: float = 0.0
    last_task_id: str = ""
    last_state: str = ""
    run_count: int = 0

    def describe(self) -> dict[str, Any]:
        """回传给界面 / API 的元数据视图。**不含 `prompt` 正文**（ADR-006）。"""
        return {
            "jobId": self.job_id,
            "name": self.name,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "nextRunAt": self.next_run_at,
            "lastRunAt": self.last_run_at,
            "lastTaskId": self.last_task_id,
            "lastState": self.last_state,
            "runCount": self.run_count,
        }


__all__ = [
    "Schedule",
    "CronSchedule",
    "IntervalSchedule",
    "OnceSchedule",
    "JobSpec",
    "MAX_JOB_NAME_CHARS",
    "MAX_SCHEDULE_CHARS",
    "MAX_JOB_PROMPT_CHARS",
    "MAX_SEARCH_YEARS",
]
