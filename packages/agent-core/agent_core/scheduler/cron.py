"""调度表达式解析（M10）：**纯 stdlib、零依赖**。

支持三形态：
  1. **5 字段 cron** —— `分 时 日 月 周`；每字段支持 `*` / `?` / `a,b,c` / `a-b` /
     `*/n` / `a-b/n`。范围：分 `0-59`、时 `0-23`、日 `1-31`、月 `1-12`、
     周 `0-7`（**0 与 7 均为周日**）。`日` 与 `周` **同时被限定**（原始文本均
     不是 `*` / `?`）时取 **OR**（Vixie cron 语义）。
  2. **`@every <n><unit>`** —— `s` / `m` / `h` / `d`，如 `@every 30m`（周期）。
  3. **`@at <ISO8601>`** —— 如 `@at 2026-10-17T09:00:00`（**一次性**）。

**fail-closed**：任何非法表达式一律抛 `ScheduleError`，**绝不当「无调度」放行**
（与 M6–M9 的「不配置时逐字节一致 / 坏配置拒绝覆盖」同一纪律）。

**本机本地时间**：全部按 `time.localtime` / `time.mktime` 解析，不做 tz / DST。
"""

from __future__ import annotations

import re
from datetime import datetime

from .model import (
    MAX_SCHEDULE_CHARS,
    CronSchedule,
    IntervalSchedule,
    OnceSchedule,
    Schedule,
)

_CRON_FIELDS = 5
_EVERY_RE = re.compile(r"^@every\s+(\d+)\s*([smhd])$", re.IGNORECASE)
_AT_RE = re.compile(r"^@at\s+(.+)$", re.IGNORECASE)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ScheduleError(ValueError):
    """调度表达式非法（fail-closed）。"""


def parse_schedule(text: str) -> Schedule:
    """把一段调度表达式解析成 `Schedule`。非法即抛 `ScheduleError`。"""
    raw = (text or "").strip()
    if not raw:
        raise ScheduleError("调度表达式不能为空")
    if len(raw) > MAX_SCHEDULE_CHARS:
        raise ScheduleError(
            f"调度表达式过长（{len(raw)} 字符，上限 {MAX_SCHEDULE_CHARS}）"
        )
    if raw.startswith("@"):
        return _parse_macro(raw)
    return _parse_cron(raw)


# --------------------------------------------------------------------------- 宏


def _parse_macro(raw: str) -> Schedule:
    m = _EVERY_RE.match(raw)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = n * _UNIT_SECONDS[unit]
        if seconds < 1:
            raise ScheduleError("周期必须至少 1 秒")
        return IntervalSchedule(expr=raw, seconds=seconds)

    m = _AT_RE.match(raw)
    if m:
        body = m.group(1).strip()
        try:
            dt = datetime.fromisoformat(body)
        except ValueError as exc:
            raise ScheduleError(f"@at 需要一个 ISO8601 时间（如 2026-10-17T09:00:00）：{body}") from exc
        return OnceSchedule(expr=raw, at=dt.timestamp())

    raise ScheduleError(
        f"未知的调度宏 {raw!r}（支持 @every <n><s|m|h|d> / @at <ISO8601>）"
    )


# --------------------------------------------------------------------------- cron


def _parse_cron(raw: str) -> Schedule:
    parts = raw.split()
    if len(parts) != _CRON_FIELDS:
        raise ScheduleError(
            f"cron 表达式需要 5 个字段（分 时 日 月 周），实际 {len(parts)} 个：{raw!r}"
        )
    minute_s, hour_s, dom_s, mon_s, dow_s = parts

    minutes, _ = _parse_field(minute_s, 0, 59, "分")
    hours, _ = _parse_field(hour_s, 0, 23, "时")
    days, days_restricted = _parse_field(dom_s, 1, 31, "日")
    months, _ = _parse_field(mon_s, 1, 12, "月")
    # 周：cron 的 0/7 均为周日；先按 0-7 解析，再归一为 Python weekday。
    dow_values, weekdays_restricted = _parse_field(dow_s, 0, 7, "周")
    weekdays = frozenset((v % 7 - 1) % 7 for v in dow_values)

    return CronSchedule(
        expr=raw,
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        days_restricted=days_restricted,
        weekdays_restricted=weekdays_restricted,
    )


def _parse_field(text: str, lo: int, hi: int, label: str) -> tuple[frozenset[int], bool]:
    """解析单个 cron 字段 → (值集合, 是否被限定)。

    「被限定」= 原始文本不是 `*` / `?`（`*/n` 也算被限定，与 Vixie 的实现口径
    保持一致，已在 M10-PLAN A2 写明）。
    """
    text = text.strip()
    if not text:
        raise ScheduleError(f"{label} 字段为空")
    if text in ("*", "?"):
        return frozenset(range(lo, hi + 1)), False

    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"{label} 字段含空的列表项：{text!r}")

        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) <= 0:
                raise ScheduleError(f"{label} 字段的步长非法：{part!r}")
            step = int(step_s)
            part = base.strip()

        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = _to_int(a, lo, hi, label), _to_int(b, lo, hi, label)
        else:
            start = end = _to_int(part, lo, hi, label)

        if start > end:
            raise ScheduleError(f"{label} 字段的区间反了：{part!r}")
        values.update(range(start, end + 1, step))

    if not values:
        raise ScheduleError(f"{label} 字段没有匹配值：{text!r}")
    return frozenset(values), True


def _to_int(text: str, lo: int, hi: int, label: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise ScheduleError(f"{label} 字段不是整数：{text!r}")
    value = int(text)
    if value < lo or value > hi:
        raise ScheduleError(f"{label} 字段超出范围 {lo}-{hi}：{value}")
    return value


__all__ = ["ScheduleError", "parse_schedule"]
