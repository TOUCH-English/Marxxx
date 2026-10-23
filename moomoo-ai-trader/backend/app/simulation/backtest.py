"""日 K 回测器（基准策略 / 固定规则模拟的 AI 政策）。

真实性约束：
- 在第 d 日收盘后，只用截至 d 日（含）已收盘的 K 线计算信号；委托在下一个交易日才可能成交，
  绝不按信号当日收盘价成交。
- 买入：下一交易日开盘价若不高于限价，按开盘价 + 滑点成交（不超过限价）；否则若当日最低价触及限价，
  按限价成交；否则不成交（「触及」只表示可能成交，这里作乐观假设并在报告中说明）。
- 同一根 K 线内同时触及止损与止盈时，保守假设先触发止损。跳空低开穿过止损时按开盘价 − 滑点成交。
- 指标用复权价，成交与持仓用原始价；拆股调整数量与止损价，股息计入现金。
- 不同币种分开记账，不做换汇。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from ..core.money import calc_fees, round_lot_down, round_to_tick
from ..db.types import D
from ..market_data.base import Bar
from ..market_data.features import atr, compute_features, momentum_score, sma

ASSUMPTIONS = [
    "信号只使用信号日（含）之前已收盘的日 K；委托最早在下一个交易日成交。",
    "买入限价 = 下一交易日开盘价 ×(1+入场溢价)；开盘价不高于限价时按开盘价+滑点成交，否则最低价触及限价时按限价成交（乐观假设：触及即全部成交）。",
    "只有 OHLC 数据，无法得知同一根 K 线内止损与止盈的先后，保守假设先触发止损。",
    "跳空穿过止损时按开盘价减滑点成交，实际亏损可能大于计划。",
    "费用按费用表计算（测试费用表并非官方费率），不含汇率与税务影响。",
    "指标用复权价，成交用原始价；拆股调整数量/止损价，现金股息入账。",
    "样本来自演示或导入数据，存在选择偏差；短样本或几十笔交易不能证明策略有效。",
]


@dataclass
class BTPosition:
    symbol: str
    qty: int
    avg_cost: Decimal
    entry_day: date
    stop: Decimal | None
    take_profit: Decimal | None
    max_hold_days: int
    fees: Decimal
    reduced: bool = False
    entry_price: Decimal = Decimal(0)
    max_qty: int = 0


@dataclass
class BTResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _decision_baseline(bars: list[Bar], params: dict, held: BTPosition | None) -> tuple[str, dict]:
    short, long = int(params.get("short", 20)), int(params.get("long", 60))
    closes = [float(b.adj_close) for b in bars if not b.suspended]
    if len(closes) < long + 1:
        return "NONE", {}
    s_now, l_now = sma(closes, short), sma(closes, long)
    s_prev, l_prev = sma(closes[:-1], short), sma(closes[:-1], long)
    if held is None and s_prev <= l_prev and s_now > l_now:
        a = atr(bars, 14)
        if a is None:
            return "NONE", {}
        a_raw = Decimal(str(a)) / bars[-1].adj_factor
        return "BUY", {"stop_dist": D(params.get("atr_stop_mult", "2.0")) * a_raw, "tp_dist": None,
                       "max_hold": int(params.get("max_hold_days", 30)), "target_pct": None}
    if held is not None and s_prev >= l_prev and s_now < l_now:
        return "SELL", {"fraction": Decimal(1)}
    return "NONE", {}


def _decision_ai_fixture(bars: list[Bar], params: dict, held: BTPosition | None) -> tuple[str, dict]:
    p = params.get("fixture_params", params)
    f = compute_features(bars, None)
    if not f.get("atr_14") or not f.get("sma_20"):
        return "NONE", {}
    last = float(bars[-1].close)
    a = D(f["atr_14"])
    if held is None:
        score = momentum_score(f)
        if (f.get("ret_20d") or 0) > 0 and float(bars[-1].adj_close) > f["sma_20"] and score >= float(p.get("entry_min_score", 1.5)):
            return "BUY", {"stop_dist": D(p.get("stop_atr_mult", "2.0")) * a, "tp_dist": D(p.get("take_profit_atr_mult", "4.0")) * a,
                           "max_hold": int(p.get("max_holding_days", 10)), "target_pct": D(p.get("target_position_pct", "0.08")),
                           "score": score}
        return "NONE", {}
    gain = (D(last) - held.avg_cost) / a if a else Decimal(0)
    if gain >= D(p.get("reduce_gain_atr", "1.5")) and not held.reduced:
        return "SELL", {"fraction": D(p.get("reduce_fraction", "0.5"))}
    if (f.get("ret_5d") is not None and f["ret_5d"] <= float(p.get("exit_ret_5d_below", -0.04))) or \
            float(bars[-1].adj_close) < f["sma_20"] * 0.99:
        return "SELL", {"fraction": Decimal(1)}
    return "NONE", {}


def _ratios(pnls: list[Decimal]) -> dict:
    """盈亏比与盈利因子只在同一币种内计算。"""
    w = [x for x in pnls if x > 0]
    lo = [x for x in pnls if x <= 0]
    return {
        "payoff_ratio": str((sum(w) / len(w) / abs(sum(lo) / len(lo))).quantize(Decimal("0.01")))
        if w and lo and sum(lo) != 0 else None,
        "profit_factor": str((sum(w) / abs(sum(lo))).quantize(Decimal("0.01"))) if lo and sum(lo) != 0 else None,
    }


def run_backtest(provider, calendar, *, symbols: list[str], start: date, end: date, capital: dict[str, Decimal],
                 fee_schedules: dict[str, dict], strategy_key: str, strategy_params: dict, risk_params: dict,
                 slippage_bps: int = 10, entry_premium_bps: int = 30) -> BTResult:
    decide = _decision_baseline if strategy_key == "BASELINE_MA" else _decision_ai_fixture
    insts = {s: provider.instrument(s) for s in symbols if provider.instrument(s)}
    cash = {c: D(v) for c, v in capital.items()}
    initial = dict(cash)
    positions: dict[str, BTPosition] = {}
    pending: dict[str, tuple[str, dict, date]] = {}
    trades: list[dict] = []
    curve: list[dict] = []
    slip = Decimal(slippage_bps) / 10000
    prem = Decimal(entry_premium_bps) / 10000
    # 只加载截至回测结束日的已收盘 K 线；循环中再按日期截取，绝不读取当日之后的数据
    all_bars = {s: provider.daily_bars(s, datetime.combine(end, time(23, 59), timezone.utc), 100000)
                for s in insts}
    by_day = {s: {b.day: i for i, b in enumerate(bs)} for s, bs in all_bars.items()}
    days = sorted({b.day for bs in all_bars.values() for b in bs if start <= b.day <= end})
    actions = {}
    for s in insts:
        for a in getattr(provider, "corporate_actions", lambda *_: [])(s, start, end):
            actions.setdefault((s, a.ex_date), []).append(a)
    last_close: dict[str, Decimal] = {}
    peak = {c: v for c, v in cash.items()}
    max_dd = {c: Decimal(0) for c in cash}
    bench_start: dict[str, Decimal] = {}

    def sell(sym: str, qty: int, price: Decimal, day: date, why: str):
        pos = positions[sym]
        inst = insts[sym]
        fee = calc_fees(fee_schedules.get(inst.market), "SELL", qty, price) or Decimal(0)
        cash[inst.currency] += price * qty - fee
        pnl = (price * qty - fee) - pos.avg_cost * qty
        trades.append({"symbol": sym, "side": "SELL", "day": day.isoformat(), "qty": qty, "price": str(price),
                       "fee": str(fee), "pnl": str(pnl.quantize(Decimal("0.01"))), "reason": why,
                       "holding_days": (day - pos.entry_day).days, "currency": inst.currency})
        pos.qty -= qty
        pos.fees += fee
        if pos.qty == 0:
            del positions[sym]
        else:
            pos.reduced = True

    for d in days:
        # 公司行动
        for sym in list(positions):
            for a in actions.get((sym, d), []):
                pos = positions[sym]
                if a.kind == "SPLIT":
                    pos.qty = int(pos.qty * a.split_ratio)
                    pos.avg_cost /= a.split_ratio
                    pos.stop = pos.stop / a.split_ratio if pos.stop else None
                    pos.take_profit = pos.take_profit / a.split_ratio if pos.take_profit else None
                elif a.kind == "DIVIDEND":
                    cash[insts[sym].currency] += a.cash_per_share * pos.qty
        for sym, inst in insts.items():
            i = by_day[sym].get(d)
            if i is None:
                continue
            bar = all_bars[sym][i]
            if sym not in bench_start:
                bench_start[sym] = bar.open
            if bar.suspended:
                continue
            # 1) 执行昨日收盘后产生的委托（只用今日 K 线）
            if sym in pending:
                act, info, sig_day = pending.pop(sym)
                if act == "BUY" and sym not in positions:
                    eq = cash[inst.currency] + sum(p.qty * last_close.get(s, p.avg_cost) for s, p in positions.items()
                                                   if insts[s].currency == inst.currency)
                    limit = bar.open * (1 + prem)
                    if bar.open <= limit:
                        px = min(limit, bar.open * (1 + slip))
                    elif bar.low <= limit:
                        px = limit
                    else:
                        px = None
                    if px is not None:
                        px = round_to_tick(inst.market, px, "up")
                        stop = px - info["stop_dist"]
                        per_share = px - stop + px * slip
                        budget = eq * D(risk_params.get("risk_per_trade_pct", "0.0025"))
                        q_risk = int(budget / per_share) if per_share > 0 else 0
                        cap_pct = D(risk_params.get("max_position_pct", "0.10"))
                        if info.get("target_pct"):
                            cap_pct = min(cap_pct, info["target_pct"])
                        q_cap = int(eq * cap_pct / px)
                        exposure = sum(p.qty * last_close.get(s, p.avg_cost) for s, p in positions.items()
                                       if insts[s].currency == inst.currency)
                        q_tot = int(max(Decimal(0), eq * D(risk_params.get("max_total_exposure_pct", "0.30")) - exposure) / px)
                        q_cash = int(cash[inst.currency] / (px * Decimal("1.01")))
                        qty = round_lot_down(max(0, min(q_risk, q_cap, q_tot, q_cash)), inst.lot_size)
                        nheld = len([s for s in positions if insts[s].currency == inst.currency])
                        if qty >= inst.lot_size and nheld < int(risk_params.get("max_positions", 3)) and stop > 0:
                            fee = calc_fees(fee_schedules.get(inst.market), "BUY", qty, px) or Decimal(0)
                            cash[inst.currency] -= px * qty + fee
                            positions[sym] = BTPosition(sym, qty, (px * qty + fee) / qty, d, stop,
                                                        px + info["tp_dist"] if info.get("tp_dist") else None,
                                                        info["max_hold"], fee, entry_price=px, max_qty=qty)
                            trades.append({"symbol": sym, "side": "BUY", "day": d.isoformat(), "qty": qty,
                                           "price": str(px), "fee": str(fee), "pnl": "", "reason": f"信号日 {sig_day}",
                                           "holding_days": "", "currency": inst.currency})
                elif act == "SELL" and sym in positions:
                    pos = positions[sym]
                    qty = pos.qty if info["fraction"] >= 1 else round_lot_down(int(pos.qty * info["fraction"]), inst.lot_size)
                    if qty > 0:
                        sell(sym, qty, round_to_tick(inst.market, bar.open * (1 - slip), "down"), d,
                             info.get("why", "策略卖出（次日开盘）"))
            # 2) 当日保护规则（保守：先止损）
            pos = positions.get(sym)
            if pos is not None and pos.entry_day < d:
                if pos.stop is not None and bar.low <= pos.stop:
                    px = (bar.open if bar.open < pos.stop else pos.stop) * (1 - slip)
                    sell(sym, pos.qty, round_to_tick(inst.market, px, "down"), d, "止损" + ("（跳空）" if bar.open < pos.stop else ""))
                elif pos.take_profit is not None and bar.high >= pos.take_profit:
                    px = max(bar.open, pos.take_profit) * (1 - slip)
                    sell(sym, pos.qty, round_to_tick(inst.market, px, "down"), d, "止盈")
                elif (d - pos.entry_day).days >= pos.max_hold_days:
                    sell(sym, pos.qty, round_to_tick(inst.market, bar.close * (1 - slip), "down"), d, "持有期限")
            last_close[sym] = bar.close
            # 3) 收盘后决策（只用截至今日的 K 线）
            hist = all_bars[sym][max(0, i - 300): i + 1]
            act, info = decide(hist, strategy_params, positions.get(sym))
            if act != "NONE":
                pending[sym] = (act, info, d)
        # 净值
        row = {"day": d.isoformat()}
        for c in cash:
            eq = cash[c] + sum(p.qty * last_close.get(s, p.avg_cost) for s, p in positions.items() if insts[s].currency == c)
            peak[c] = max(peak[c], eq)
            if peak[c] > 0:
                max_dd[c] = max(max_dd[c], (peak[c] - eq) / peak[c])
            row[c] = str(eq.quantize(Decimal("0.01")))
        curve.append(row)
    # 指标
    sells = [t for t in trades if t["side"] == "SELL"]
    pnls = [D(t["pnl"]) for t in sells]
    wins = [x for x in pnls if x > 0]
    metrics: dict[str, Any] = {"assumptions": ASSUMPTIONS, "per_currency": {}}
    for c in cash:
        end_eq = D(curve[-1][c]) if curve else initial[c]
        c_pnls = [D(t["pnl"]) for t in sells if t["currency"] == c]
        c_fees = sum((D(t["fee"]) for t in trades if t["currency"] == c), Decimal(0))
        bh = []
        for s, inst in insts.items():
            if inst.currency != c or s not in bench_start or s not in last_close:
                continue
            bh.append(last_close[s] / bench_start[s] - 1)
        metrics["per_currency"][c] = {
            "initial": str(initial[c]), "final_equity": str(end_eq.quantize(Decimal("0.01"))),
            "net_return_pct": str(((end_eq / initial[c] - 1) * 100).quantize(Decimal("0.01"))) if initial[c] else "0",
            "max_drawdown_pct": str((max_dd[c] * 100).quantize(Decimal("0.01"))),
            "benchmark_buy_hold_equal_weight_pct": str((sum(bh) / len(bh) * 100).quantize(Decimal("0.01"))) if bh else None,
            "benchmark_cash_pct": "0.00",
            "round_trips": len(c_pnls), "net_pnl": str(sum(c_pnls, Decimal(0)).quantize(Decimal("0.01"))),
            "fees": str(c_fees.quantize(Decimal("0.01"))),
            **_ratios(c_pnls),
        }
    metrics.update({
        "round_trips": len(sells), "buys": len([t for t in trades if t["side"] == "BUY"]),
        "net_pnl_note": "盈亏与费用按币种见 per_currency；不同币种不相加",
        "win_rate_pct": str((Decimal(len(wins)) / len(pnls) * 100).quantize(Decimal("0.1"))) if pnls else None,
        "avg_holding_days": str(round(sum(t["holding_days"] for t in sells) / len(sells), 1)) if sells else None,
        "fees_by_currency": {c: str(sum((D(t["fee"]) for t in trades if t["currency"] == c), Decimal(0)).quantize(Decimal("0.01")))
                             for c in cash},
        "fee_share_of_gross_pct": None,
        "sample_days": len(days), "open_positions_at_end": len(positions),
        "sample_warning": "样本量过小，结果不具统计意义" if len(sells) < 30 else "样本仍有限，需前向验证",
    })
    shares = {}
    for c in cash:
        g = sum((abs(D(t["pnl"])) for t in sells if t["currency"] == c), Decimal(0))
        f = sum((D(t["fee"]) for t in trades if t["currency"] == c), Decimal(0))
        if g > 0:
            shares[c] = str((f / g * 100).quantize(Decimal("0.1")))
    metrics["fee_share_of_gross_pct"] = shares
    return BTResult(trades=trades, equity_curve=curve, metrics=metrics)
