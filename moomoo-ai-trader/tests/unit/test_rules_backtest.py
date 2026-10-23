"""历史规则回测：无未来数据、大盘过滤、止损跳空、风险预算、同行业限制、费用。"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.core.money import TEST_FEE_SCHEDULES
from app.market_data.base import Bar
from app.simulation.rules_backtest import (
    BENCHMARK,
    RulesPolicy,
    load_history,
    report_markdown,
    run_rules_backtest,
)

RISK = {"max_position_pct": "0.15", "max_total_exposure_pct": "0.45", "risk_per_trade_pct": "0.0025",
        "max_positions": 3, "max_daily_new_orders": 3, "daily_loss_pct": "0.01", "assumed_slippage_bps": 30}
START = date(2025, 1, 6)


def _days(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars(sym: str, closes: list[float], *, opens: dict[int, float] | None = None, lows: dict[int, float] | None = None,
          volume: int = 2_000_000) -> list[Bar]:
    out = []
    for i, (d, c) in enumerate(zip(_days(len(closes)), closes)):
        c = Decimal(str(c))
        o = Decimal(str((opens or {}).get(i, closes[i - 1] if i else closes[0])))
        lo = Decimal(str((lows or {}).get(i, min(o, c) * Decimal("0.995"))))
        out.append(Bar(sym, d, o, max(o, c) * Decimal("1.005"), lo, c, volume))
    return out


def _up(n: int, start: float, step: float) -> list[float]:
    # 带一点起伏的上升趋势（让 ATR 和波动率不为 0）
    return [round(start + i * step + (0.4 if i % 2 else -0.4), 2) for i in range(n)]


def _run(history, **kw):
    days = [b.day for b in history[BENCHMARK]]
    args = dict(start=days[230], end=days[-1], capital=Decimal("2448"), max_loss=Decimal("122"),
                fee_schedule=TEST_FEE_SCHEDULES["US"], risk_params=RISK)
    return run_rules_backtest(history, **(args | kw))


def test_fills_only_after_signal_day_and_respect_risk_budget():
    h = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)), "US.AAA": _bars("US.AAA", _up(300, 50, 0.25))}
    res = _run(h)
    buys = [f for f in res.fills if f["side"] == "BUY"]
    assert buys, "上升趋势中应该至少买入一次"
    days = [b.day for b in h[BENCHMARK]]
    first = buys[0]
    i = days.index(date.fromisoformat(first["day"]))
    # 成交价不可能低于成交日开盘价（只用次日开盘成交，不按信号日收盘价）
    assert Decimal(first["price"]) >= h["US.AAA"][i].open
    assert date.fromisoformat(first["day"]) > days[230]
    # 每笔计划风险（入场价 − 止损）× 股数 不超过 净值 × 单笔风险；止损距离 ≥ 1.5%；盈亏比 = 2
    assert res.trades
    for t in res.trades:
        px, stop, tp = Decimal(t["entry_price"]), Decimal(t["initial_stop"]), Decimal(t["take_profit"])
        assert (px - stop) * t["qty"] <= Decimal("2448") * Decimal("0.0025") * Decimal("1.1")
        assert (px - stop) / px >= Decimal("0.0149")
        assert abs((tp - px) / (px - stop) - 2) < Decimal("0.05")


def test_regime_filter_blocks_new_entries_when_spy_below_200_day_average():
    spy = _up(240, 500, 0.0) [:200] + [round(500 - i * 3, 2) for i in range(100)]
    h = {BENCHMARK: _bars(BENCHMARK, spy), "US.AAA": _bars("US.AAA", _up(300, 50, 0.25))}
    res = _run(h)
    assert not [f for f in res.fills if f["side"] == "BUY"]
    assert res.metrics["skips"]["risk_off_days"] > 0
    res2 = _run(h, policy=RulesPolicy(regime_filter=False))
    assert [f for f in res2.fills if f["side"] == "BUY"]


def _gap_history():
    closes = _up(300, 50, 0.25)
    h0 = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)), "US.AAA": _bars("US.AAA", closes)}
    first_buy = next(f for f in _run(h0).fills if f["side"] == "BUY")
    days = _days(300)
    k = days.index(date.fromisoformat(first_buy["day"])) + 1
    gap = round(closes[k - 1] * 0.85, 2)
    closes2 = closes[:k] + [gap + i * 0.25 for i in range(300 - k)]
    return {BENCHMARK: h0[BENCHMARK], "US.AAA": _bars("US.AAA", closes2, opens={k: gap})}, days[k]


def test_gap_through_stop_exits_at_open_and_loses_more_than_1r():
    h, gap_day = _gap_history()
    t = _run(h).trades[0]
    assert t["exit_reason"] == "止损（跳空）"
    assert t["exit_day"] == gap_day.isoformat()
    assert Decimal(t["r"]) < Decimal("-1")


def test_one_position_per_industry_and_score_order():
    h = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)),
         "US.AAA": _bars("US.AAA", _up(300, 50, 0.25)), "US.BBB": _bars("US.BBB", _up(300, 50, 0.30))}
    both = _run(h)
    first_day = min(f["day"] for f in both.fills if f["side"] == "BUY")
    same_day = [f["symbol"] for f in both.fills if f["side"] == "BUY" and f["day"] == first_day]
    assert len(same_day) == 2
    one = _run(h, industries={"US.AAA": "半导体", "US.BBB": "半导体"})
    open_syms = [f["symbol"] for f in one.fills if f["side"] == "BUY" and f["day"] == first_day]
    assert len(open_syms) == 1
    assert one.metrics["skips"]["skipped_industry"] > 0


def test_fees_reduce_equity_and_metrics_are_complete():
    flat = [100 + (0.3 if i % 2 else -0.3) for i in range(300)]
    h = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)), "US.AAA": _bars("US.AAA", _up(300, 50, 0.25)),
         "US.FLT": _bars("US.FLT", flat)}
    res = _run(h)
    m = res.metrics
    assert Decimal(m["fees_usd"]) > 0
    assert m["gate_b"]["status"] == "INSUFFICIENT"  # 交易数不足 30 笔
    assert set(m["by_year"]) <= {"2025", "2026"}
    md = report_markdown(m)
    assert "关卡 B" in md and "幸存者偏差" in md


def test_stop_line_halts_new_entries():
    h, gap_day = _gap_history()
    # 停止线 8 美元：单笔风险预算仍可开仓，但跳空亏损超过停止线后不再开新仓
    res = _run(h, max_loss=Decimal("8"))
    assert res.metrics["stop_line_hit_on"] == gap_day.isoformat()
    assert not [f for f in res.fills if f["side"] == "BUY" and f["day"] > gap_day.isoformat()]
    assert [f for f in _run(h).fills if f["side"] == "BUY" and f["day"] > gap_day.isoformat()]


def test_load_history_applies_adjustment(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text("symbol,date,open,high,low,close,volume,adj_factor\n"
                 "US.X,2025-01-02,200,202,198,200,1000,0.5\nUS.X,2025-01-03,101,102,99,100,1000,1\n", encoding="utf-8")
    bars = load_history(p)["US.X"]
    assert [b.close for b in bars] == [Decimal("100.0"), Decimal("100")]
