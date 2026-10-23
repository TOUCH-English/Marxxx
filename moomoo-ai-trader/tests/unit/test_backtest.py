"""验收 15：回测不利用未来 K 线，也不按信号当日收盘价成交。"""
from datetime import date, timedelta
from decimal import Decimal

from app.core.calendar import TradingCalendar
from app.core.money import TEST_FEE_SCHEDULES
from app.market_data.base import Bar, InstrumentInfo
from app.risk.policy import SEED_RISK_PARAMS
from app.simulation.backtest import run_backtest

cal = TradingCalendar()


class BarsProvider:
    def __init__(self, bars_by_symbol):
        self.b = bars_by_symbol
        self.insts = {s: InstrumentInfo(s, "US", s, "STOCK", "USD", 1) for s in bars_by_symbol}

    def instrument(self, s):
        return self.insts.get(s)

    def instruments(self):
        return list(self.insts.values())

    def daily_bars(self, s, now, n):
        last = cal.last_closed_session("US", now)
        return [b for b in self.b[s] if b.day <= last][-n:]

    def corporate_actions(self, *a):
        return []


def _series(closes, start=date(2025, 1, 2)):
    days = cal.sessions_between("US", start, start + timedelta(days=int(len(closes) * 1.6) + 10))[: len(closes)]
    return [Bar("US.T", d, Decimal(str(c)), Decimal(str(c * 1.01)), Decimal(str(c * 0.99)), Decimal(str(c)), 1_000_000)
            for d, c in zip(days, closes)]


def _bt(bars, end):
    return run_backtest(BarsProvider({"US.T": bars}), cal, symbols=["US.T"], start=bars[0].day, end=end,
                        capital={"USD": Decimal(10000)}, fee_schedules=TEST_FEE_SCHEDULES, strategy_key="BASELINE_MA",
                        strategy_params={"short": 5, "long": 20, "atr_stop_mult": "2.0", "max_hold_days": 30},
                        risk_params=SEED_RISK_PARAMS | {"risk_per_trade_pct": "0.02"})


def test_fill_happens_next_session_not_at_signal_close():
    closes = [100 - i * 0.2 for i in range(40)] + [92 + i * 1.0 for i in range(30)]
    bars = _series(closes)
    res = _bt(bars, bars[-1].day)
    buys = [t for t in res.trades if t["side"] == "BUY"]
    assert buys, "应产生上穿买入"
    b = buys[0]
    sig_day = date.fromisoformat(b["reason"].replace("信号日 ", ""))
    fill_day = date.fromisoformat(b["day"])
    assert fill_day > sig_day, "成交必须在信号日之后的交易日"
    sig_close = next(x.close for x in bars if x.day == sig_day)
    assert Decimal(b["price"]) != sig_close


def test_future_bars_do_not_change_past_results():
    closes = [100 - i * 0.2 for i in range(40)] + [92 + i * 1.0 for i in range(30)] + [120 - i * 2 for i in range(20)]
    bars = _series(closes)
    cut = bars[60].day
    a = _bt(bars, cut)
    altered = bars[:61] + [Bar(b.symbol, b.day, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.volume) for b in bars[61:]]
    b = _bt(altered, cut)
    assert a.trades == b.trades, "截至 cut 的结果不应受之后 K 线影响"
    assert a.equity_curve == b.equity_curve


def test_same_bar_stop_and_target_assumes_stop_first():
    from app.simulation.backtest import ASSUMPTIONS

    assert any("先触发止损" in x for x in ASSUMPTIONS)
