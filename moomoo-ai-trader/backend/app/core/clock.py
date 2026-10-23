"""时钟抽象。

实时模式使用真实 UTC 时间；演示/历史回放使用持久化的回放时钟，
回放时钟只在交易时段内前进，重启后从数据库保存的位置继续。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    @property
    def is_replay(self) -> bool: ...


class WallClock:
    is_replay = False

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ReplayClock:
    """回放时钟：按步长推进，跳过休市时间。"""

    is_replay = True

    def __init__(self, start: datetime, step: timedelta, markets: list[str], calendar):
        if start.tzinfo is None:
            raise ValueError("start must be tz-aware")
        self._now = start
        self.step = step
        self.markets = markets
        self.calendar = calendar

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        self._now = t

    def advance(self) -> datetime:
        """推进一个步长；若落在全部市场休市时段，则跳到最近的开市时刻。"""
        t = self._now + self.step
        if not any(self.calendar.is_open(m, t) for m in self.markets):
            nxt = min(self.calendar.next_open(m, t) for m in self.markets)
            # 开市后稍等一个步长，使开盘价格先出现
            t = max(t, nxt)
        self._now = t
        return t
