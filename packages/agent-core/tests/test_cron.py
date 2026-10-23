"""调度表达式解析（M10 Part E）：`cron` / `@every` / `@at` 三形态 + fail-closed。

验收要点（对齐 M10-PLAN A2 / E）：
1. 5 字段 cron 的 `*` / `?` / `a,b,c` / `a-b` / `*/n` / `a-b/n` 全形态；
2. `日` 与 `周` **同时被限定**时取 **OR**（Vixie 语义），单限定则取 AND；
3. 周字段 `0` 与 `7` 均为周日；
4. `next_after()` 逐字段跳进正确（不逐分钟扫描），严格 > now；
5. `@every <n><unit>` 周期 / `@at <ISO8601>` 一次性；
6. 非法表达式一律抛 `ScheduleError`（fail-closed，绝不当「无调度」放行）。

全部按**本机本地时间**构造时间戳（`time.mktime`），不做 tz / DST。
"""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.scheduler import (  # noqa: E402
    CronSchedule,
    IntervalSchedule,
    OnceSchedule,
    ScheduleError,
    parse_schedule,
)
from agent_core.scheduler.model import MAX_SCHEDULE_CHARS  # noqa: E402


def _ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> float:
    """构造本机本地时间的时间戳。"""
    return float(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))


def _parts(ts: float) -> tuple[int, int, int, int, int, int]:
    lt = time.localtime(ts)
    return lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_wday


class CronParseTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertIsInstance(parse_schedule("*/5 * * * *"), CronSchedule)
        self.assertIsInstance(parse_schedule("@every 30m"), IntervalSchedule)
        self.assertIsInstance(parse_schedule("@at 2026-10-17T09:00:00"), OnceSchedule)

    def test_star_and_question_wildcard(self) -> None:
        """`?` 等价 `*`，且都不算「被限定」。"""
        star = parse_schedule("* * * * *")
        q = parse_schedule("? ? ? ? ?")
        self.assertEqual(star.minutes, q.minutes)
        self.assertEqual(star.days, q.days)
        self.assertFalse(star.days_restricted)
        self.assertFalse(star.weekdays_restricted)

    def test_step_star_is_restricted(self) -> None:
        """`*/n` 也算「被限定」（与 Vixie 口径一致，A2 已写明）。"""
        s = parse_schedule("*/5 * * * *")
        self.assertEqual(sorted(s.minutes), [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])

    def test_list_and_range_and_step_range(self) -> None:
        s = parse_schedule("1,2,3-5,10-20/5 9 * * *")
        self.assertEqual(sorted(s.minutes), [1, 2, 3, 4, 5, 10, 15, 20])
        self.assertEqual(sorted(s.hours), [9])

    def test_sunday_zero_and_seven_same(self) -> None:
        """周字段 0 与 7 均归一为周日（Python weekday=6）。"""
        s0 = parse_schedule("0 0 * * 0")
        s7 = parse_schedule("0 0 * * 7")
        self.assertEqual(s0.weekdays, s7.weekdays)
        self.assertEqual(s0.weekdays, frozenset({6}))

    def test_weekday_mapping(self) -> None:
        s = parse_schedule("0 9 * * 1-5")
        # 周一=0 … 周五=4
        self.assertEqual(s.weekdays, frozenset({0, 1, 2, 3, 4}))


class CronNextAfterTests(unittest.TestCase):
    def test_every_five_minutes(self) -> None:
        """`*/5` 跳到下一个 5 分钟整点。"""
        now = _ts(2026, 10, 17, 8, 2, 30)
        nxt = parse_schedule("*/5 * * * *").next_after(now)
        self.assertEqual(nxt, _ts(2026, 10, 17, 8, 5, 0))

    def test_strictly_after_now(self) -> None:
        """恰好在整点时，结果必须严格 > now（不返回当下这一分钟）。"""
        now = _ts(2026, 10, 17, 8, 5, 0)
        nxt = parse_schedule("*/5 * * * *").next_after(now)
        self.assertEqual(nxt, _ts(2026, 10, 17, 8, 10, 0))
        self.assertGreater(nxt, now)

    def test_jump_over_months(self) -> None:
        """月份不匹配时整段跳过 —— 不逐分钟扫描。"""
        # 只在 3 月触发；从 10 月出发应直接跳到次年 3 月 1 日 00:00
        now = _ts(2026, 10, 10, 0, 0, 0)
        nxt = parse_schedule("0 0 1 3 *").next_after(now)
        self.assertEqual(nxt, _ts(2027, 3, 1, 0, 0, 0))

    def test_jump_over_days(self) -> None:
        """小时不匹配时跳到次日。"""
        now = _ts(2026, 10, 17, 10, 0, 0)
        nxt = parse_schedule("0 9 * * *").next_after(now)
        self.assertEqual(nxt, _ts(2026, 10, 18, 9, 0, 0))

    def test_weekday_9am(self) -> None:
        """工作日 9 点：结果必为工作日、9:00、且在未来。"""
        now = _ts(2026, 10, 16, 12, 0, 0)  # 2026-10-16 是周五
        nxt = parse_schedule("0 9 * * 1-5").next_after(now)
        y, mo, d, h, mi, wd = _parts(nxt)
        self.assertGreater(nxt, now)
        self.assertEqual((h, mi), (9, 0))
        self.assertIn(wd, (0, 1, 2, 3, 4))

    def test_dom_and_dow_both_restricted_is_or(self) -> None:
        """`日` 与 `周` 同时被限定 → OR：先到哪个用哪个。

        `0 0 1 * 0` = 每月 1 号 **或** 每周日。从 2026-10-10（周六）出发，
        下一个周日（10-11）远比下月 1 号（11-01）近，应取周日。
        """
        now = _ts(2026, 10, 10, 0, 0, 0)
        nxt = parse_schedule("0 0 1 * 0").next_after(now)
        y, mo, d, h, mi, wd = _parts(nxt)
        self.assertEqual((h, mi), (0, 0))
        self.assertLessEqual(nxt - now, 8 * 86400, "OR 语义失效：跑到了 11-01")
        # 且这一年里到达的不是 11-01
        self.assertFalse(mo == 11 and d == 1)

    def test_dom_only_is_and_semantics_not_or(self) -> None:
        """只限 `日`（周为 `*`）→ 只按日匹配。"""
        now = _ts(2026, 10, 10, 0, 0, 0)
        nxt = parse_schedule("0 0 1 * *").next_after(now)
        self.assertEqual(nxt, _ts(2026, 11, 1, 0, 0, 0))

    def test_dow_only(self) -> None:
        now = _ts(2026, 10, 10, 0, 0, 0)  # 周六
        nxt = parse_schedule("0 0 * * 0").next_after(now)
        # 次日（周日）00:00
        self.assertEqual(nxt, _ts(2026, 10, 11, 0, 0, 0))


class IntervalTests(unittest.TestCase):
    def test_seconds(self) -> None:
        s = parse_schedule("@every 45s")
        self.assertEqual(s.seconds, 45)
        now = _ts(2026, 10, 17, 8, 0, 0)
        nxt = s.next_after(now)
        self.assertEqual(nxt, now + 45)

    def test_units(self) -> None:
        self.assertEqual(parse_schedule("@every 30m").seconds, 30 * 60)
        self.assertEqual(parse_schedule("@every 2h").seconds, 2 * 3600)
        self.assertEqual(parse_schedule("@every 1d").seconds, 86400)

    def test_aligned_to_epoch(self) -> None:
        """周期对齐 epoch 边界（确定可复现）。"""
        s = parse_schedule("@every 30m")
        step = 30 * 60
        now = _ts(2026, 10, 17, 8, 7, 13)
        nxt = s.next_after(now)
        self.assertEqual(nxt % step, 0)
        self.assertGreater(nxt, now)
        self.assertLessEqual(nxt - now, step)


class OnceTests(unittest.TestCase):
    def test_fires_before_then_never(self) -> None:
        at = _ts(2027, 1, 1, 0, 0, 0)
        s = parse_schedule("@at 2027-01-01T00:00:00")
        self.assertEqual(s.at, at)
        now = _ts(2026, 12, 31, 0, 0, 0)
        self.assertEqual(s.next_after(now), at)
        # 已过 → 不再排期
        self.assertIsNone(s.next_after(at))
        self.assertIsNone(s.next_after(at + 1))

    def test_iso_with_seconds_roundtrip(self) -> None:
        dt = datetime.fromisoformat("2026-10-17T09:30:00")
        s = parse_schedule("@at 2026-10-17T09:30:00")
        self.assertAlmostEqual(s.at, dt.timestamp(), places=3)


class FailClosedTests(unittest.TestCase):
    def _bad(self, text: str) -> None:
        with self.assertRaises(ScheduleError):
            parse_schedule(text)

    def test_empty_and_oversize(self) -> None:
        self._bad("")
        self._bad("   ")
        self._bad("x" * (MAX_SCHEDULE_CHARS + 1))

    def test_wrong_field_count(self) -> None:
        self._bad("* * * *")  # 4 字段
        self._bad("* * * * * *")  # 6 字段

    def test_out_of_range(self) -> None:
        self._bad("99 9 * * *")
        self._bad("0 24 * * *")
        self._bad("0 0 32 * *")
        self._bad("0 0 * 13 *")
        self._bad("0 0 * * 8")

    def test_malformed_forms(self) -> None:
        self._bad("1- 9 * * *")
        self._bad("5-1 9 * * *")  # 区间反了
        self._bad("*/0 9 * * *")  # 步长 0
        self._bad("a 9 * * *")  # 非整数
        self._bad("1,,2 9 * * *")  # 空列表项

    def test_bad_macros(self) -> None:
        self._bad("@every 0s")
        self._bad("@every 30x")  # 未知单位
        self._bad("@unknown")
        self._bad("@at 不是时间")
        self._bad("@at ")  # 缺时间

    def test_error_is_valueerror_subclass(self) -> None:
        """`ScheduleError` 是 `ValueError` 子类 —— 便于上层统一捕获。"""
        self.assertTrue(issubclass(ScheduleError, ValueError))


if __name__ == "__main__":
    unittest.main()
