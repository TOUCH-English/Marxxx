"""独立审核（2026-10-23 夜，第 2 批复审）发现的问题的回归测试。"""
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.calendar import get_calendar
from app.db.models import DailyEquity, Decision, OrderIntent
from app.db.session import read_session, write_session
from app.portfolio import ledger
from app.strategies import market_context as mctx
from helpers import US_OPEN, trend
from integration.test_ai_safety import ScriptedAI, _buy, _one, _vctx
from integration.test_market_context import CtxProvider, _run, _spy


def test_reward_risk_uses_worst_entry_price_max_buy():
    from app.ai.validate import parse_and_validate

    # 最新价 100、止损 95、止盈 112：按 100 计 2.4；但最高买入价 104 时只有 (112-104)/(104-95)=0.89
    r = parse_and_validate(_one(max_buy_price="104.00"), _vctx())
    assert "盈亏比" in " ".join(e for d in r.decisions for e in d.errors)
    r = parse_and_validate(_one(stop_loss_price="100.00"), _vctx())
    assert "不低于入场价" in " ".join(e for d in r.decisions for e in d.errors)


def test_reward_risk_rechecked_at_actual_order_limit(env):
    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    # 按最新价 64：(68.2-64)/(64-61.9)=2.0 刚好合格；实际限价约 64.2 时低于 2
    ai = ScriptedAI(lambda p: _buy(p, stop_loss_price="61.90", take_profit_price="68.20") if p["candidates"] else _buy(p))
    pid, *_ = _run(prov, ["US.UPA"], ai)
    with read_session("sim") as s:
        d = s.execute(select(Decision).where(Decision.source == "AI_MODEL")).scalars().first()
        assert ledger.get_open_position(s, pid, "US.UPA") is None
    assert d.outcome == "RISK_BLOCKED" and "盈亏比（按实际限价）" in str(d.rule_calc)


def _rt(earn, now):
    return SimpleNamespace(provider=SimpleNamespace(earnings_calendar=lambda t, days=21: earn),
                           calendar=get_calendar(), now=lambda: now)


@pytest.mark.parametrize("earn_day,pub,expect", [
    (date(2026, 10, 24), "BEFORE", True),    # 周六公布（如伯克希尔）：周五收市前退出
    (date(2026, 10, 25), "N/A", True),       # 周日
    (date(2026, 10, 26), "BEFORE", True),    # 下一个交易日盘前
    (date(2026, 10, 27), "BEFORE", False),   # 还早
    (date(2026, 10, 23), "BEFORE", False),   # 今天盘前已公布
    (date(2026, 10, 23), "AFTER", True),     # 今天收市后
])
def test_exit_before_earnings_covers_weekends(earn_day, pub, expect):
    now = datetime(2026, 10, 23, 19, 45, tzinfo=timezone.utc)  # 周五 美东 15:45
    pos = SimpleNamespace(symbol="US.BRK", market="US")
    rt = _rt({"US.BRK": {"date": earn_day, "pub_type": pub}}, now)
    assert (mctx.exit_before_earnings(rt, pos, now) is not None) == expect


def test_regime_unknown_is_retried_not_cached_all_day(monkeypatch):
    calls = {"n": 0}
    bars = [SimpleNamespace(close=Decimal(500 + i), adj_factor=Decimal(1), day=date(2026, 1, 1)) for i in range(220)]

    def daily_bars(sym, now, n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("OpenD 暂时断开")
        return bars

    rt = SimpleNamespace(provider=SimpleNamespace(daily_bars=daily_bars), now=lambda: US_OPEN)
    assert mctx.market_regime(rt) is None
    assert mctx.market_regime(rt) is None and calls["n"] == 1  # 10 分钟内不重复请求
    rt.regime_try = time.monotonic() - mctx.UNKNOWN_RETRY_S - 1
    assert mctx.market_regime(rt)["risk_off"] is False and calls["n"] == 2


def test_error_in_earnings_rule_does_not_block_stop_loss(env, monkeypatch):
    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, eng, fb, clock = _run(prov, ["US.UPA"], ScriptedAI(lambda p: _buy(p)), n=4)
    with read_session("sim") as s:
        assert ledger.get_open_position(s, pid, "US.UPA") is not None

    def boom(*a, **k):
        raise KeyError("pub_type")

    monkeypatch.setattr(mctx, "exit_before_earnings", boom)
    prov.set_quote("US.UPA", "60.50")  # 跌破止损 61
    clock.advance(minutes=1)
    eng.tick()
    with read_session("sim") as s:
        sells = list(s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).scalars())
    assert sells and sells[0].reason == "EXIT_STOP"


def test_scorecard_counts_trading_days_and_does_not_overstate_drawdown(env):
    from app.learning import scorecard
    from helpers import setup_run

    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "1000"})
    start = date(2026, 10, 16)  # 周五
    with write_session("sim") as s:
        for r in s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == pid)).scalars():
            s.delete(r)
        s.flush()
        for i in range(7):  # 周五到下周四，含周末两天
            d = start + timedelta(days=i)
            s.add(DailyEquity(portfolio_id=pid, currency="USD", day_myt=d.isoformat(), start_equity=Decimal(1000),
                              last_equity=Decimal(1000), min_equity=Decimal(1000), flows=Decimal(0),
                              hwm_adjusted=Decimal(1000), updated_at=US_OPEN))
        # 某天先跌到 950 再涨到 1100：回撤是 5%，不是 (1100-950)/1100 = 13.6%
        row = s.execute(select(DailyEquity).where(DailyEquity.day_myt == "2026-10-20")).scalar_one()
        row.min_equity, row.last_equity, row.hwm_adjusted = Decimal(950), Decimal(1100), Decimal(1100)
        s.flush()
        for r in s.execute(select(DailyEquity).where(DailyEquity.day_myt > "2026-10-20")).scalars():
            r.start_equity = r.last_equity = r.min_equity = r.hwm_adjusted = Decimal(1100)
    with read_session("sim") as s:
        out = scorecard.build(s, pid, price_of=lambda sym: None, now=US_OPEN + timedelta(days=7))
    assert out["sessions"] == 5
    assert out["numbers"]["max_drawdown_pct"] == "5.00%"
