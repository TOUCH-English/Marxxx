"""历史回放行情（DEMO 固定数据 / 用户导入的历史数据）。

- 只返回在当前回放时刻之前已完整收盘的日 K，避免未来数据泄漏。
- 日内报价由当日 OHLC 按确定性路径插值得到（开盘 → 先触及高/低 → 收盘），
  并在模拟买卖价差内给出 bid/ask。这是撮合假设，不是真实盘口，界面会标示「历史回放」。
"""
from __future__ import annotations

import csv
import json
from bisect import bisect_left
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..core.calendar import TradingCalendar
from ..core.money import round_to_tick
from ..db.types import D
from .base import Bar, CorporateAction, InstrumentInfo, ProviderStatus, Quote


class ReplayProvider:
    is_realtime = False

    def __init__(self, fixture_dir: Path, calendar: TradingCalendar, *, name: str = "DEMO_FIXTURE",
                 prefix: str = "demo"):
        self.name = name
        self.calendar = calendar
        self.dir = fixture_dir
        self._bars: dict[str, list[Bar]] = {}
        self._days: dict[str, list[date]] = {}
        self._instruments: dict[str, InstrumentInfo] = {}
        self._actions: list[CorporateAction] = []
        self.meta: dict = {}
        self._load(prefix)

    # ------------------------------------------------------------ 加载
    def _load(self, prefix: str) -> None:
        inst = json.loads((self.dir / f"{prefix}_instruments.json").read_text(encoding="utf-8"))
        for i in inst:
            self._instruments[i["symbol"]] = InstrumentInfo(
                symbol=i["symbol"], market=i["market"], name=i["name"], sec_type=i["sec_type"],
                currency=i["currency"], lot_size=int(i["lot_size"]), is_demo=bool(i.get("is_demo")),
                is_leveraged=bool(i.get("is_leveraged")), spread_bps=int(i.get("spread_bps", 20)),
            )
        with (self.dir / f"{prefix}_daily.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                b = Bar(
                    symbol=r["symbol"], day=date.fromisoformat(r["date"]), open=D(r["open"]), high=D(r["high"]),
                    low=D(r["low"]), close=D(r["close"]), volume=int(r["volume"]),
                    adj_factor=D(r.get("adj_factor") or "1"), suspended=r.get("suspended") in ("1", "true", "True"),
                )
                self._bars.setdefault(b.symbol, []).append(b)
        for sym, bars in self._bars.items():
            bars.sort(key=lambda b: b.day)
            self._days[sym] = [b.day for b in bars]
        ap = self.dir / f"{prefix}_actions.json"
        if ap.exists():
            for a in json.loads(ap.read_text(encoding="utf-8")):
                self._actions.append(CorporateAction(
                    symbol=a["symbol"], ex_date=date.fromisoformat(a["ex_date"]), kind=a["kind"],
                    cash_per_share=D(a.get("cash_per_share", "0")), split_ratio=D(a.get("split_ratio", "1")),
                ))
        mp = self.dir / f"{prefix}_meta.json"
        if mp.exists():
            self.meta = json.loads(mp.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 查询
    def status(self) -> ProviderStatus:
        label = "演示固定数据（虚构证券）" if self.name == "DEMO_FIXTURE" else "历史回放（非实时）"
        return ProviderStatus(name=self.name, connected=True, is_realtime=False, label=label,
                              detail=self.meta.get("disclaimer", ""))

    def instruments(self) -> list[InstrumentInfo]:
        return list(self._instruments.values())

    def instrument(self, symbol: str) -> InstrumentInfo | None:
        return self._instruments.get(symbol)

    def data_end(self) -> date | None:
        ends = [d[-1] for d in self._days.values() if d]
        return min(ends) if ends else None

    def _bar_on(self, symbol: str, d: date) -> Bar | None:
        days = self._days.get(symbol)
        if not days:
            return None
        i = bisect_left(days, d)
        if i < len(days) and days[i] == d:
            return self._bars[symbol][i]
        return None

    def daily_bars(self, symbol: str, now: datetime, n: int) -> list[Bar]:
        inst = self._instruments.get(symbol)
        if inst is None:
            return []
        last_closed = self.calendar.last_closed_session(inst.market, now)
        if last_closed is None:
            return []
        days = self._days[symbol]
        i = bisect_left(days, last_closed)
        if i < len(days) and days[i] == last_closed:
            i += 1
        return self._bars[symbol][max(0, i - n): i]

    def _fraction(self, market: str, d: date, now: datetime) -> float | None:
        """当日已经过的交易时间比例；不在交易时段返回 None（午休时返回上午结束时的比例）。"""
        windows = self.calendar.windows(market, d)
        if not windows:
            return None
        total = sum((b - a).total_seconds() for a, b in windows)
        elapsed = 0.0
        for a, b in windows:
            if now >= b:
                elapsed += (b - a).total_seconds()
            elif now > a:
                elapsed += (now - a).total_seconds()
        if now < windows[0][0]:
            return None
        return min(1.0, elapsed / total)

    @staticmethod
    def _path_price(bar: Bar, f: float) -> Decimal:
        o, h, l, c = (float(bar.open), float(bar.high), float(bar.low), float(bar.close))
        first, second = (l, h) if c >= o else (h, l)
        pts = [(0.0, o), (0.3, first), (0.7, second), (1.0, c)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if f <= x1:
                y = y0 + (y1 - y0) * (f - x0) / (x1 - x0)
                return Decimal(repr(round(y, 6)))
        return bar.close

    def quote(self, symbol: str, now: datetime) -> Quote | None:
        inst = self._instruments.get(symbol)
        if inst is None:
            return None
        d = self.calendar.local_date(inst.market, now)
        bar = self._bar_on(symbol, d)
        prev = self.daily_bars(symbol, now, 1)
        prev_close = prev[-1].close if prev else None
        src = self.name
        f = self._fraction(inst.market, d, now) if bar else None
        if bar is None or f is None:
            # 非交易时段：只有上一收盘价，标记为过期报价（不可用于开仓）
            if not prev:
                return None
            close_t = self.calendar.session_close(inst.market, prev[-1].day)
            return Quote(symbol=symbol, last=prev[-1].close, bid=None, ask=None, volume=0, prev_close=prev_close,
                         source=src, is_realtime=False, source_ts=close_t, recv_ts=now, suspended=prev[-1].suspended,
                         note="休市：仅显示上一收盘价")
        if bar.suspended:
            return Quote(symbol=symbol, last=prev_close, bid=None, ask=None, volume=0, prev_close=prev_close,
                         source=src, is_realtime=False, source_ts=now, recv_ts=now, suspended=True, note="停牌")
        last = round_to_tick(inst.market, self._path_price(bar, f), "down")
        # 已经走过的高低点
        steps = [i / 20 for i in range(0, 21) if i / 20 <= f] + [f]
        seen = [self._path_price(bar, x) for x in steps]
        hi = round_to_tick(inst.market, max(seen), "up")
        lo = round_to_tick(inst.market, min(seen), "down")
        half = last * Decimal(inst.spread_bps) / Decimal(20000)
        bid = round_to_tick(inst.market, last - half, "down")
        ask = round_to_tick(inst.market, last + half, "up")
        if ask <= bid:
            ask = round_to_tick(inst.market, bid + Decimal("0.0001"), "up")
        return Quote(symbol=symbol, last=last, bid=bid, ask=ask, volume=int(bar.volume * f), prev_close=prev_close,
                     source=src, is_realtime=False, source_ts=now, recv_ts=now, suspended=False,
                     day_high=hi, day_low=lo, day_open=bar.open, note="历史回放（按日 K 插值的模拟盘口）")

    def corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        return [a for a in self._actions if a.symbol == symbol and start <= a.ex_date <= end]

    def all_corporate_actions(self) -> list[CorporateAction]:
        return list(self._actions)
