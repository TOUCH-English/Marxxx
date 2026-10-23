"""交易日历。

- 交易日与开/收市时间来自 exchange_calendars（XKLS = 马来西亚交易所，XNYS = 纽约证交所），
  已处理美股夏令时与半日市。
- 马股午休（12:30–14:30 马来西亚时间）由本模块补充；连续竞价按 09:00–12:30、14:30–16:45 处理，
  16:45 之后的收市竞价阶段不提交新委托。
- 可在 data/calendar_overrides.json 增补临时休市日（例如交易所临时公告）。
- 日历数据需每年对照交易所公告核对，见 docs/capabilities.md。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

MYT = ZoneInfo("Asia/Kuala_Lumpur")
ET = ZoneInfo("America/New_York")

MARKET_TZ = {"MY": MYT, "US": ET}
_XC_NAME = {"MY": "XKLS", "US": "XNYS"}

# 马股连续竞价时段（当地时间）
MY_WINDOWS = [(time(9, 0), time(12, 30)), (time(14, 30), time(16, 45))]


@lru_cache(maxsize=4)
def _xc(market: str):
    import exchange_calendars as xc

    return xc.get_calendar(_XC_NAME[market])


class TradingCalendar:
    def __init__(self, overrides_path: Path | None = None):
        self.extra_holidays: dict[str, set[date]] = {"MY": set(), "US": set()}
        if overrides_path and overrides_path.exists():
            data = json.loads(overrides_path.read_text(encoding="utf-8"))
            for m, days in data.get("extra_holidays", {}).items():
                self.extra_holidays.setdefault(m, set()).update(date.fromisoformat(d) for d in days)
        self._cache: dict[tuple[str, date], list[tuple[datetime, datetime]]] = {}

    # ------------------------------------------------------------ 基础查询
    def is_session_day(self, market: str, d: date) -> bool:
        if d in self.extra_holidays.get(market, set()):
            return False
        cal = _xc(market)
        ts = pd.Timestamp(d)
        if ts < cal.first_session or ts > cal.last_session:
            # 超出日历库覆盖范围：仅按周一至周五估算，并由调用方标记「日历未覆盖」
            return d.weekday() < 5
        return bool(cal.is_session(ts))

    def windows(self, market: str, d: date) -> list[tuple[datetime, datetime]]:
        """返回某交易日的连续交易时段（UTC）。休市日返回空列表。"""
        key = (market, d)
        if key in self._cache:
            return self._cache[key]
        out: list[tuple[datetime, datetime]] = []
        if self.is_session_day(market, d):
            tz = MARKET_TZ[market]
            if market == "MY":
                for a, b in MY_WINDOWS:
                    out.append(
                        (
                            datetime.combine(d, a, tz).astimezone(timezone.utc),
                            datetime.combine(d, b, tz).astimezone(timezone.utc),
                        )
                    )
            else:
                cal = _xc(market)
                ts = pd.Timestamp(d)
                if cal.first_session <= ts <= cal.last_session:
                    o = cal.session_open(ts).to_pydatetime()
                    c = cal.session_close(ts).to_pydatetime()
                else:
                    o = datetime.combine(d, time(9, 30), tz).astimezone(timezone.utc)
                    c = datetime.combine(d, time(16, 0), tz).astimezone(timezone.utc)
                out.append((o.astimezone(timezone.utc), c.astimezone(timezone.utc)))
        self._cache[key] = out
        return out

    def local_date(self, market: str, t: datetime) -> date:
        return t.astimezone(MARKET_TZ[market]).date()

    def is_open(self, market: str, t: datetime) -> bool:
        for a, b in self.windows(market, self.local_date(market, t)):
            if a <= t < b:
                return True
        return False

    def session_close(self, market: str, d: date) -> datetime | None:
        w = self.windows(market, d)
        return w[-1][1] if w else None

    def session_open(self, market: str, d: date) -> datetime | None:
        w = self.windows(market, d)
        return w[0][0] if w else None

    def next_open(self, market: str, t: datetime) -> datetime:
        d = self.local_date(market, t)
        for _ in range(30):
            for a, _b in self.windows(market, d):
                if a > t:
                    return a
            d = d + timedelta(days=1)
        raise RuntimeError("30 天内找不到开市时间，请检查日历配置")

    def minutes_to_close(self, market: str, t: datetime) -> float | None:
        c = self.session_close(market, self.local_date(market, t))
        if c is None or t >= c:
            return None
        return (c - t).total_seconds() / 60

    def sessions_between(self, market: str, start: date, end: date) -> list[date]:
        out = []
        d = start
        while d <= end:
            if self.is_session_day(market, d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def last_closed_session(self, market: str, t: datetime) -> date | None:
        """t 时刻已经完整收盘的最近一个交易日（用于只使用已收盘日 K）。"""
        d = self.local_date(market, t)
        for _ in range(30):
            c = self.session_close(market, d)
            if c is not None and c <= t:
                return d
            d -= timedelta(days=1)
        return None

    def market_status(self, market: str, t: datetime) -> str:
        if self.is_open(market, t):
            return "OPEN"
        d = self.local_date(market, t)
        w = self.windows(market, d)
        if not w:
            return "HOLIDAY_OR_WEEKEND"
        if t < w[0][0]:
            return "PRE_OPEN"
        if market == "MY" and len(w) > 1 and w[0][1] <= t < w[1][0]:
            return "LUNCH_BREAK"
        return "CLOSED"

    def in_coverage(self, market: str, d: date) -> bool:
        cal = _xc(market)
        return cal.first_session <= pd.Timestamp(d) <= cal.last_session


def myt_day(t: datetime) -> str:
    """日损益边界：马来西亚时间 00:00。"""
    return t.astimezone(MYT).date().isoformat()


def next_myt_midnight(t: datetime) -> datetime:
    d = t.astimezone(MYT).date() + timedelta(days=1)
    return datetime.combine(d, time(0, 0), MYT).astimezone(timezone.utc)


CCY_MARKET = {"USD": "US", "MYR": "MY"}


def risk_day(ccy_or_market: str, t: datetime) -> str:
    """风控日界：按该币种所在市场的当地日期（美股按美东日期，马股按马来西亚日期）。

    美股交易时段跨越马来西亚午夜；若按马来西亚日期重置日亏损和每日开仓次数，同一个美股交易日会被切成两天。
    """
    mk = CCY_MARKET.get(ccy_or_market, ccy_or_market)
    return t.astimezone(MARKET_TZ.get(mk, MYT)).date().isoformat()


def next_risk_day_start(ccy_or_market: str, t: datetime) -> datetime:
    mk = CCY_MARKET.get(ccy_or_market, ccy_or_market)
    tz = MARKET_TZ.get(mk, MYT)
    d = t.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(d, time(0, 0), tz).astimezone(timezone.utc)


_default: TradingCalendar | None = None


def get_calendar() -> TradingCalendar:
    global _default
    if _default is None:
        from ..config import get_settings

        _default = TradingCalendar(get_settings().data_dir / "calendar_overrides.json")
    return _default
