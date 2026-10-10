"""可注入的时钟。

为什么值得单独抽一层：配额是**按自然日**结算的，令牌有**过期时间**。
这两件事都只有在「时间跨过某个边界」时才会暴露问题，
而靠 `sleep(86400)` 去测是不现实的。

把时间做成可注入的对象之后，「跨天配额重置」「令牌刚过期」
「刷新令牌在过期边界上被重用」这类用例都能在毫秒内测完。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Clock:
    """生产用时钟。"""

    def now(self) -> int:
        return int(time.time())

    def today(self) -> str:
        """自然日（本地时区），配额按这个分组。"""
        return time.strftime("%Y-%m-%d", time.localtime(self.now()))

    def next_midnight(self) -> int:
        """距离下一个自然日零点的秒数。

        配额超限时用它算 `Retry-After`，比硬编码 60 秒有诚意得多：
        用户能确切知道「还有多久恢复」。
        """
        now = self.now()
        local = time.localtime(now)
        midnight = time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
        ) + 86400
        return max(1, int(midnight - now))

    def iso(self, epoch: int | None = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.now() if epoch is None else epoch))


@dataclass
class FrozenClock(Clock):
    """测试用时钟：时间只在你显式推进时才走。

    起点的设置入口是 `FrozenClock.at(epoch)`。底层字段叫 `_now` 是 dataclass
    的私有命名，只有通过这个工厂才能读得懂 —— 跨天配额、令牌过期这类用例
    全都需要「从一个确定的时刻开始」，让它们每次写 `_now=...` 只会让
    测试读起来像在碰内部实现。
    """

    _now: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def at(cls, epoch: int) -> "FrozenClock":
        """从指定的 epoch 秒开始。"""
        return cls(_now=int(epoch))

    def __post_init__(self) -> None:
        if not self._now:
            self._now = int(time.time())

    def now(self) -> int:
        with self._lock:
            return self._now

    def advance(self, seconds: int) -> int:
        with self._lock:
            self._now += seconds
            return self._now

    def set(self, epoch: int) -> None:
        with self._lock:
            self._now = int(epoch)


__all__ = ["Clock", "FrozenClock"]
