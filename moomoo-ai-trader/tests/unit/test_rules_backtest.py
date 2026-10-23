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
    args = dict(start=days[230], end=days[-1], capital=Decimal("10000"), max_loss=Decimal("500"),
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
        assert (px - stop) * t["qty"] <= Decimal("10000") * Decimal("0.0025") * Decimal("1.1")
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
    # 停止线 12 美元：单笔风险预算仍可开仓，但跳空亏损超过停止线后不再开新仓
    res = _run(h, max_loss=Decimal("12"))
    assert res.metrics["stop_line_hit_on"] == gap_day.isoformat()
    assert not [f for f in res.fills if f["side"] == "BUY" and f["day"] > gap_day.isoformat()]
    assert [f for f in _run(h).fills if f["side"] == "BUY" and f["day"] > gap_day.isoformat()]


def test_load_history_applies_adjustment(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text("symbol,date,open,high,low,close,volume,adj_factor\n"
                 "US.X,2025-01-02,200,202,198,200,1000,0.5\nUS.X,2025-01-03,101,102,99,100,1000,1\n", encoding="utf-8")
    bars = load_history(p)["US.X"]
    assert [b.close for b in bars] == [Decimal("100.0"), Decimal("100")]


def test_stop_checked_on_entry_day_after_open_fill():
    closes = _up(300, 50, 0.25)
    h0 = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)), "US.AAA": _bars("US.AAA", closes)}
    first = next(f for f in _run(h0).fills if f["side"] == "BUY")
    k = _days(300).index(date.fromisoformat(first["day"]))
    # 成交当天：开盘成交后，盘中跌到止损以下再收回
    h = {BENCHMARK: h0[BENCHMARK], "US.AAA": _bars("US.AAA", closes, lows={k: closes[k - 1] * 0.9})}
    t = _run(h).trades[0]
    assert t["entry_day"] == t["exit_day"] == first["day"]
    assert t["exit_reason"] == "止损"
    assert Decimal(t["r"]) < Decimal("-0.9")


def test_sizing_matches_live_fee_aware_budget_and_target():
    from app.simulation.rules_backtest import size_entry

    kw = dict(eq=Decimal("2448"), cash=Decimal("2448"), open_risk=Decimal(0), exposure=Decimal(0),
              drawdown_from_capital=Decimal(0), max_loss=Decimal("122"), fee_schedule=TEST_FEE_SCHEDULES["US"],
              per_trade=Decimal("0.0025"), daily_loss=Decimal("0.01"), pos_cap=Decimal("0.15"), tot_cap=Decimal("0.45"),
              target_pct=Decimal("0.08"), slip=Decimal("0.003"))
    # 每股风险 1.38、预算 6.12：不含费用可买 4 股，含买卖费用只能买 2 股（线上 evaluate_entry 同样做法）
    assert size_entry(px=Decimal("60"), stop=Decimal("58.8"), **kw) == 2
    # 价格 200、止损 1.5%：含费用一股都买不起风险预算
    assert size_entry(px=Decimal("200"), stop=Decimal("197"), **kw) == 0
    # 目标仓位 8%：预算很大时也不超过 8% 净值
    big = kw | {"per_trade": Decimal("0.05"), "daily_loss": Decimal("0.5"), "max_loss": None}
    assert size_entry(px=Decimal("20"), stop=Decimal("19"), **big) == int(Decimal("2448") * Decimal("0.08") / 20)


def test_no_lookahead_future_bars_do_not_change_earlier_fills():
    h = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)), "US.AAA": _bars("US.AAA", _up(300, 50, 0.25)),
         "US.BBB": _bars("US.BBB", _up(300, 80, 0.35))}
    base = _run(h)
    cut = _days(300)[270]
    changed = {s: [b if b.day <= cut else Bar(s, b.day, b.open / 2, b.high / 2, b.low / 2, b.close / 2, b.volume)
                   for b in bs] for s, bs in h.items()}
    other = _run(changed)
    before = lambda r: [f for f in r.fills if f["day"] <= cut.isoformat()]  # noqa: E731
    assert before(base) and before(base) == before(other)


def test_gate_b_benchmark_matches_live_scorecard_formula():
    h = {BENCHMARK: _bars(BENCHMARK, _up(300, 400, 0.5)), "US.AAA": _bars("US.AAA", _up(300, 50, 0.25))}
    m = _run(h).metrics
    item = next(i for i in m["gate_b"]["items"] if "SPY" in i["name"])
    assert m["spy_cap_matched_return_pct"] in item["value"]


def test_price_filter_uses_raw_price_before_split():
    from app.simulation.rules_backtest import pool_for_day
    from app.market_data.universe import AUTO_US_POOL_RULES

    bars = _bars("US.SPL", _up(80, 120, 0.3), volume=2_000_000)
    hist = {"US.SPL": bars}
    assert pool_for_day(hist, AUTO_US_POOL_RULES)  # 复权价 120+ 在 10–600 之间
    # 当时的原始价是复权价的 10 倍（后来 1 拆 10）：超过 600 美元上限，线上不会入选
    factors = {"US.SPL": {b.day: Decimal("0.1") for b in bars}}
    assert not pool_for_day(hist, AUTO_US_POOL_RULES, factors)
