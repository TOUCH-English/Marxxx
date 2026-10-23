"""历史规则回测（第 3 批 A）：不用 AI，按和线上相同的选股、风控、止损与费用规则，在真实历史日 K 上整组合模拟。

它是成绩单关卡 B 的「规则对照组」：AI 要证明自己有用，至少要跑赢这套固定规则。

和线上一致的部分：
- 每日股票池：价格区间 + 20 日平均成交额下限，按 0.6×20日涨幅 + 0.4×60日涨幅 排名取前 N 名。
- 大盘过滤：SPY 收盘低于 200 日均线时不开新仓（已有持仓照常保护）。
- 同一行业最多 1 个持仓（数据里有行业时）。
- 入场：固定规则（20 日涨幅为正、站上 20 日均线、动量分达标），止损 = max(2×ATR, 1.5%)，止盈 = 2 倍止损距离（盈亏比 2）。
- 仓数：单笔风险预算 = min(单笔风险上限, 停止线剩余 − 在途风险, 当日亏损上限 − 在途风险)，再受单只仓位上限、
  总投入上限、持仓数、每日新单数和现金约束。
- 费用用传入的费用表（在 Mac 上读官方费用表）。

和线上不同（报告里会写明）：
- 只有日 K：信号在收盘后算，下一交易日开盘才成交；同一根 K 线同时触及止损与止盈时保守假设先止损。
- 用前复权价格（拆股、分红不会造成假涨跌）。
- 没有历史财报日，所以不做财报前回避；没有历史市值，所以股票清单固定为当前的大市值股票（幸存者偏差，结果偏乐观）。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.money import calc_fees, round_lot_down, round_to_tick
from ..db.types import D
from ..learning import scorecard as sc
from ..market_data.base import Bar
from ..market_data.features import compute_features, momentum_score, sma
from ..market_data.universe import AUTO_US_POOL_RULES

BENCHMARK = "US.SPY"

ASSUMPTIONS = [
    "信号只用当日（含）之前已收盘的日 K；委托最早在下一个交易日开盘成交。",
    "买入限价 = 开盘价 ×(1+入场溢价)，按开盘价 + 滑点成交；卖出按开盘价 − 滑点成交。",
    "同一根 K 线同时触及止损与止盈时，保守假设先止损；跳空低开穿过止损按开盘价 − 滑点成交，亏损可能超过 1R。买入当天也检查止损止盈。",
    "回撤和停止线按当日最低净值判断（保守假设所有持仓同时在当日最低价）。",
    "指标与成交用前复权价格（拆股、分红不会造成假涨跌）；价格区间、成交额和「买不买得起」按当时的原始价判断。",
    "股票清单固定为当前的大市值美股：历史上退市或跌出大盘股的公司不在里面（幸存者偏差，结果偏乐观）。",
    "没有历史财报日：不做「财报前不开仓 / 财报前卖出」，财报跳空风险没有被回避。",
    "没有历史盘口：不检查买卖价差；成交按日 K 估计，真实成交可能更差。",
    "历史结果不能保证未来；30 笔以下交易的结果不具统计意义。",
]


@dataclass
class RulesPolicy:
    entry_min_score: float = 1.5
    stop_atr_mult: Decimal = Decimal("2.0")
    min_stop_pct: Decimal = Decimal("0.015")
    reward_risk: Decimal = Decimal("2")
    max_holding_days: int = 10
    reduce_gain_atr: Decimal = Decimal("1.5")
    reduce_fraction: Decimal = Decimal("0.5")
    exit_ret_5d_below: float = -0.04
    target_position_pct: Decimal = Decimal("0.08")  # 固定测试响应给出的目标仓位，线上同样受它约束
    regime_filter: bool = True
    one_position_per_industry: bool = True


@dataclass
class _Pos:
    symbol: str
    qty: int
    max_qty: int
    entry_day: date
    entry_price: Decimal
    cost: Decimal          # 买入金额 + 买入费用
    stop: Decimal
    initial_stop: Decimal
    take_profit: Decimal
    atr: Decimal
    proceeds: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    reduced: bool = False
    industry: str | None = None


@dataclass
class RulesResult:
    trades: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 数据
def load_history(csv_path: Path, factors: dict[str, dict[date, Decimal]] | None = None) -> dict[str, list[Bar]]:
    """读取回放格式 CSV（symbol,date,open,high,low,close,volume,adj_factor[,suspended]），返回前复权后的日 K。

    传入 factors 时顺便记下每天的复权因子（原始价 = 复权价 ÷ 因子），用于价格区间筛选和「买不买得起」。
    """
    out: dict[str, list[Bar]] = {}
    with Path(csv_path).open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            fac = D(r.get("adj_factor") or "1")
            if factors is not None:
                factors.setdefault(r["symbol"], {})[date.fromisoformat(r["date"])] = fac
            out.setdefault(r["symbol"], []).append(Bar(
                symbol=r["symbol"], day=date.fromisoformat(r["date"]), open=D(r["open"]) * fac, high=D(r["high"]) * fac,
                low=D(r["low"]) * fac, close=D(r["close"]) * fac, volume=int(float(r["volume"])),
                suspended=r.get("suspended") in ("1", "true", "True")))
    for bars in out.values():
        bars.sort(key=lambda b: b.day)
    return out


# ------------------------------------------------------------------ 规则
def pool_for_day(hist: dict[str, list[Bar]], rules: dict, factors: dict[str, dict[date, Decimal]] | None = None
                 ) -> list[tuple[str, float]]:
    """线上每日自动股票池的复刻：价格区间 + 20 日平均成交额（都按当时的原始价），按 0.6×20日 + 0.4×60日 涨幅排名。"""
    lo, hi = D(rules["min_price"]["US"]), D(rules["max_price"]["US"])
    min_turn = D(rules["min_avg_turnover_20d"]["US"])
    factors = factors or {}
    ranked = []
    for sym, bars in hist.items():
        if sym == BENCHMARK or len(bars) < 61 or bars[-1].suspended:
            continue
        fac = factors.get(sym, {})
        last = bars[-1].close
        if not lo <= last / fac.get(bars[-1].day, Decimal(1)) <= hi:
            continue
        turnover = sum(b.close / fac.get(b.day, Decimal(1)) * b.volume for b in bars[-20:]) / 20
        if turnover < min_turn:
            continue
        r20 = float(last / bars[-21].close - 1)
        r60 = float(last / bars[-61].close - 1)
        ranked.append((sym, 0.6 * r20 + 0.4 * r60))
    ranked.sort(key=lambda x: -x[1])
    return ranked[: int(rules.get("max_members", 20))]


def risk_off(spy: list[Bar]) -> bool | None:
    """SPY 收盘低于 200 日均线 → True；数据不足 → None（不阻止）。"""
    if len(spy) < 200:
        return None
    closes = [float(b.close) for b in spy]
    return closes[-1] < sma(closes, 200)


def entry_signal(bars: list[Bar], pol: RulesPolicy) -> dict | None:
    f = compute_features(bars, None)
    if not f.get("atr_14") or not f.get("sma_20") or f.get("ret_20d") is None:
        return None
    score = momentum_score(f)
    if f["ret_20d"] <= 0 or float(bars[-1].close) <= f["sma_20"] or score < pol.entry_min_score:
        return None
    return {"score": score, "atr": D(f["atr_14"])}


def exit_signal(bars: list[Bar], pos: _Pos, pol: RulesPolicy) -> Decimal | None:
    """收盘后的卖出信号：返回卖出比例，或 None。"""
    f = compute_features(bars, None)
    last = bars[-1].close
    if pos.atr and not pos.reduced and (last - pos.entry_price) / pos.atr >= pol.reduce_gain_atr:
        return pol.reduce_fraction
    if f.get("sma_20") and ((f.get("ret_5d") is not None and f["ret_5d"] <= pol.exit_ret_5d_below)
                            or float(last) < f["sma_20"] * 0.99):
        return Decimal(1)
    return None


# ------------------------------------------------------------------ 仓数（与 risk/engine.evaluate_entry 相同的约束）
def size_entry(*, eq: Decimal, cash: Decimal, px: Decimal, stop: Decimal, open_risk: Decimal, exposure: Decimal,
               drawdown_from_capital: Decimal, max_loss: Decimal | None, fee_schedule: dict | None, per_trade: Decimal,
               daily_loss: Decimal, pos_cap: Decimal, tot_cap: Decimal, target_pct: Decimal | None, slip: Decimal) -> int:
    """风险预算 = min(单笔上限, 停止线剩余 − 在途风险, 当日亏损额度 − 在途风险)，且买卖手续费也算进风险；
    再受单只上限、目标仓位、总投入和现金（含买入费用）约束。美股每手 1 股。"""
    budget = eq * per_trade
    if max_loss is not None:
        budget = min(budget, max(Decimal(0), max_loss - drawdown_from_capital - open_risk))
    budget = min(budget, max(Decimal(0), eq * daily_loss - open_risk))

    def fees(q: int) -> tuple[Decimal, Decimal]:
        return (calc_fees(fee_schedule, "BUY", q, px) or Decimal(0), calc_fees(fee_schedule, "SELL", q, stop) or Decimal(0))

    per_share = px - stop + px * slip
    q_risk = int(budget / per_share) if per_share > 0 else 0
    while q_risk > 0 and per_share * q_risk + sum(fees(q_risk)) > budget:
        q_risk -= 1
    cap = min(pos_cap, target_pct) if target_pct is not None else pos_cap
    q_cash = int(cash / px)
    while q_cash > 0 and px * q_cash + fees(q_cash)[0] > cash:
        q_cash -= 1
    return max(0, min(q_risk, int(eq * cap / px), int(max(Decimal(0), eq * tot_cap - exposure) / px), q_cash))


# ------------------------------------------------------------------ 回测
def run_rules_backtest(history: dict[str, list[Bar]], *, start: date, end: date, capital: Decimal,
                       max_loss: Decimal | None, fee_schedule: dict | None, risk_params: dict,
                       policy: RulesPolicy | None = None, pool_rules: dict | None = None,
                       industries: dict[str, str] | None = None, entry_premium_bps: int = 30,
                       factors: dict[str, dict[date, Decimal]] | None = None) -> RulesResult:
    pol = policy or RulesPolicy()
    rules = pool_rules or AUTO_US_POOL_RULES
    industries = industries or {}
    if BENCHMARK not in history:
        raise ValueError("缺少 SPY 日 K，无法判断大盘与比较基准")
    slip = D(risk_params.get("assumed_slippage_bps", 10)) / 10000
    prem = Decimal(entry_premium_bps) / 10000
    per_trade = D(risk_params.get("risk_per_trade_pct", "0.0025"))
    pos_cap = D(risk_params.get("max_position_pct", "0.10"))
    tot_cap = D(risk_params.get("max_total_exposure_pct", "0.30"))
    max_positions = int(risk_params.get("max_positions", 3))
    max_new = int(risk_params.get("max_daily_new_orders", 3))
    daily_loss = D(risk_params.get("daily_loss_pct", "0.01"))
    dd_limit = D(risk_params.get("max_drawdown_pct", "0.05"))
    factors = factors or {}

    idx = {s: {b.day: i for i, b in enumerate(bs)} for s, bs in history.items()}
    days = [b.day for b in history[BENCHMARK] if start <= b.day <= end]
    if not days:
        raise ValueError("回测区间内没有 SPY 交易日")
    cash = D(capital)
    positions: dict[str, _Pos] = {}
    pending_buys: list[tuple[str, dict]] = []
    pending_sells: dict[str, Decimal] = {}
    last_close: dict[str, Decimal] = {}
    res = RulesResult()
    stats = {"risk_off_days": 0, "skipped_industry": 0, "skipped_size": 0, "skipped_slots": 0, "no_fill": 0}
    halted_on: date | None = None
    halt_reason = ""
    notes: list[str] = []
    if pol.regime_filter and len([b for b in history[BENCHMARK] if b.day < start]) < 200:
        notes.append("开始日之前 SPY 日 K 不足 200 根：前一段时间无法判断大盘，大盘过滤没有生效")
    peak = cash
    mdd = Decimal(0)
    prev_exposure_frac = Decimal(0)
    spy_curve = Decimal(1)
    matched = Decimal(1)
    spy_prev: Decimal | None = None

    def bar(sym: str, d: date) -> Bar | None:
        i = idx[sym].get(d)
        return history[sym][i] if i is not None else None

    def upto(sym: str, d: date, n: int = 300) -> list[Bar]:
        i = idx[sym].get(d)
        if i is None:
            return []
        return history[sym][max(0, i - n + 1): i + 1]

    def mark(sym: str) -> Decimal:
        return last_close.get(sym, positions[sym].entry_price)

    def equity() -> Decimal:
        return cash + sum(p.qty * mark(s) for s, p in positions.items())

    def open_risk() -> Decimal:
        return sum((max(Decimal(0), mark(s) - p.stop) * p.qty for s, p in positions.items()), Decimal(0))

    def sell(sym: str, qty: int, px: Decimal, d: date, why: str):
        nonlocal cash
        p = positions[sym]
        px = round_to_tick("US", px, "down")
        fee = calc_fees(fee_schedule, "SELL", qty, px) or Decimal(0)
        cash += px * qty - fee
        p.proceeds += px * qty - fee
        p.fees += fee
        p.qty -= qty
        res.fills.append({"day": d.isoformat(), "symbol": sym, "side": "SELL", "qty": qty, "price": str(px),
                          "fee": str(fee), "reason": why})
        if p.qty == 0:
            pnl = p.proceeds - p.cost
            risk = (p.entry_price - p.initial_stop) * p.max_qty
            res.trades.append({
                "symbol": sym, "entry_day": p.entry_day.isoformat(), "exit_day": d.isoformat(),
                "entry_price": str(p.entry_price), "initial_stop": str(p.initial_stop), "take_profit": str(p.take_profit),
                "exit_reason": why, "qty": p.max_qty,
                "pnl": str(pnl.quantize(Decimal("0.01"))), "fees": str(p.fees.quantize(Decimal("0.01"))),
                "r": str((pnl / risk).quantize(Decimal("0.01"))) if risk > 0 else None,
                "holding_days": (d - p.entry_day).days})
            del positions[sym]
        else:
            p.reduced = True

    for d in days:
        spy_bar = bar(BENCHMARK, d)
        # 1) 开盘：先卖后买
        for sym, frac in list(pending_sells.items()):
            b = bar(sym, d)
            if sym in positions and b is not None and not b.suspended:
                p = positions[sym]
                qty = p.qty if frac >= 1 else round_lot_down(int(p.qty * frac), 1)
                if qty > 0:
                    sell(sym, qty, b.open * (1 - slip), d, "规则卖出（次日开盘）")
                pending_sells.pop(sym)
        new_today = 0
        for sym, info in pending_buys:
            b = bar(sym, d)
            if sym in positions or b is None or b.suspended:
                continue
            if len(positions) >= max_positions or new_today >= max_new:
                stats["skipped_slots"] += 1
                continue
            ind = industries.get(sym)
            if pol.one_position_per_industry and ind and any(p.industry == ind for p in positions.values()):
                stats["skipped_industry"] += 1
                continue
            limit = info["ref"] * (1 + prem)
            if b.open > limit:
                stats["no_fill"] += 1
                continue
            px = round_to_tick("US", min(limit, b.open * (1 + slip)), "up")
            stop_dist = max(pol.stop_atr_mult * info["atr"], px * pol.min_stop_pct)
            stop = round_to_tick("US", px - stop_dist, "down")
            tp = round_to_tick("US", px + pol.reward_risk * stop_dist, "up")
            eq = equity()
            # 线上会跳过一股（按当时原始价）都超过单只上限的股票
            if px / factors.get(sym, {}).get(d, Decimal(1)) > eq * pos_cap:
                stats["skipped_size"] += 1
                continue
            q = size_entry(eq=eq, cash=cash, px=px, stop=stop, open_risk=open_risk(),
                           exposure=sum(p.qty * mark(s) for s, p in positions.items()),
                           drawdown_from_capital=max(Decimal(0), D(capital) - eq),
                           max_loss=None if max_loss is None else D(max_loss), fee_schedule=fee_schedule,
                           per_trade=per_trade, daily_loss=daily_loss, pos_cap=pos_cap, tot_cap=tot_cap,
                           target_pct=pol.target_position_pct, slip=slip)
            if q < 1 or stop <= 0:
                stats["skipped_size"] += 1
                continue
            fee = calc_fees(fee_schedule, "BUY", q, px) or Decimal(0)
            cash -= px * q + fee
            positions[sym] = _Pos(sym, q, q, d, px, px * q + fee, stop, stop, tp, info["atr"], fees=fee, industry=ind)
            new_today += 1
            res.fills.append({"day": d.isoformat(), "symbol": sym, "side": "BUY", "qty": q, "price": str(px),
                              "fee": str(fee), "reason": f"规则买入（动量分 {info['score']:.2f}）"})
        pending_buys = []
        # 2) 盘中保护：开盘买入后，当天余下的走势也会触发止损/止盈（线上止损在成交后立即生效）
        for sym in list(positions):
            p = positions[sym]
            b = bar(sym, d)
            if b is None or b.suspended:
                continue
            if b.low <= p.stop:
                gap = b.open < p.stop and p.entry_day < d
                sell(sym, p.qty, (b.open if gap else p.stop) * (1 - slip), d, "止损（跳空）" if gap else "止损")
            elif b.high >= p.take_profit:
                sell(sym, p.qty, (max(b.open, p.take_profit) if p.entry_day < d else p.take_profit) * (1 - slip), d, "止盈")
            elif p.entry_day < d and (d - p.entry_day).days >= pol.max_holding_days:
                sell(sym, p.qty, b.close * (1 - slip), d, "持有期限")
        # 3) 收盘：估值、基准、停止线
        for sym in history:
            b = bar(sym, d)
            if b is not None and not b.suspended:
                last_close[sym] = b.close
        eq = equity()
        if spy_bar is not None:
            if spy_prev is not None:
                r = spy_bar.close / spy_prev - 1
                spy_curve *= 1 + r
                matched *= 1 + prev_exposure_frac * r
            spy_prev = spy_bar.close
        exposure = sum(p.qty * mark(s) for s, p in positions.items())
        prev_exposure_frac = exposure / eq if eq > 0 else Decimal(0)
        # 当日最低净值（保守：所有持仓同时在当日最低价），线上的回撤、停止线按盘中净值判断
        low_eq = cash + sum(p.qty * (bar(s, d).low if bar(s, d) is not None else mark(s)) for s, p in positions.items())
        if peak > 0:
            dd_today = (peak - min(low_eq, eq)) / peak
            mdd = max(mdd, dd_today)
            if halted_on is None and dd_today >= dd_limit:
                halted_on, halt_reason = d, f"回撤达到 {dd_limit * 100:.0f}% 暂停线（线上需人工复核才恢复）"
        peak = max(peak, eq)
        res.equity_curve.append({"day": d.isoformat(), "equity": str(eq.quantize(Decimal("0.01"))),
                                 "exposure_pct": str((prev_exposure_frac * 100).quantize(Decimal("0.1"))),
                                 "spy_index": str(spy_curve.quantize(Decimal("0.0001")))})
        if max_loss is not None and halted_on is None and D(capital) - min(low_eq, eq) >= D(max_loss):
            halted_on, halt_reason = d, "累计亏损达到停止线（可承受亏损）"
        # 4) 收盘后决策（只用截至今日的 K 线）
        for sym, p in positions.items():
            frac = exit_signal(upto(sym, d), p, pol)
            if frac is not None:
                pending_sells[sym] = frac
        if halted_on is not None:
            continue
        off = risk_off(upto(BENCHMARK, d, 260)) if pol.regime_filter else False
        if off:
            stats["risk_off_days"] += 1
            continue
        hist_today = {s: upto(s, d, 300) for s in history if idx[s].get(d) is not None}
        cands = []
        for sym, _ in pool_for_day(hist_today, rules, factors):
            if sym in positions:
                continue
            sig = entry_signal(hist_today[sym], pol)
            if sig:
                cands.append((sym, sig | {"ref": hist_today[sym][-1].close}))
        pending_buys = sorted(cands, key=lambda x: -x[1]["score"])

    res.metrics = _metrics(res, capital=D(capital), end_equity=equity(), mdd=mdd, spy=spy_curve - 1,
                           spy_matched=matched - 1, tot_cap=tot_cap, stats=stats, halted_on=halted_on,
                           days=days, open_positions=len(positions))
    res.metrics["halt_reason"] = halt_reason
    res.metrics["warnings"] = notes
    return res


def _pct(x: Decimal | None) -> str:
    return f"{x * 100:.2f}%" if x is not None else "—"


def _metrics(res: RulesResult, *, capital: Decimal, end_equity: Decimal, mdd: Decimal, spy: Decimal, spy_matched: Decimal,
             tot_cap: Decimal, stats: dict, halted_on: date | None, days: list[date], open_positions: int) -> dict:
    trades = res.trades
    pnls = [D(t["pnl"]) for t in trades]
    rs = [D(t["r"]) for t in trades if t["r"] is not None]
    stop_rs = [D(t["r"]) for t in trades if t["r"] is not None and t["exit_reason"].startswith("止损")]
    gross_win = sum((x for x in pnls if x > 0), Decimal(0))
    gross_loss = -sum((x for x in pnls if x < 0), Decimal(0))
    fees = sum((D(t["fees"]) for t in trades), Decimal(0))  # 只算已平仓交易，和成绩单一致
    gross_before = sum((D(t["pnl"]) + D(t["fees"]) for t in trades if D(t["pnl"]) + D(t["fees"]) > 0), Decimal(0))
    n = len(trades)
    ret = end_equity / capital - 1
    pf = gross_win / gross_loss if gross_loss > 0 else None
    avg_r = sum(rs, Decimal(0)) / len(rs) if rs else None
    cost_share = fees / gross_before if gross_before > 0 else None
    st = lambda ok, enough=True: sc.INSUFFICIENT if not enough else (sc.PASS if ok else sc.FAIL)  # noqa: E731
    enough = n >= sc.GATE_B_MIN_TRADES
    gate_b = sc._gate([
        sc._item("已平仓交易数", f"{n} 笔", f"≥ {sc.GATE_B_MIN_TRADES} 笔", st(True, enough)),
        sc._item("扣费后收益 vs 按总投入上限折算的 SPY", f"{_pct(ret)} vs {_pct(spy * tot_cap)}", "不低于基准",
                 st(ret >= spy * tot_cap, enough), "和线上成绩单同一算法"),
        sc._item("平均每笔 R", f"{avg_r:.2f}R" if avg_r is not None else "—", f"≥ {sc.MIN_AVG_R}R",
                 st(avg_r is not None and avg_r >= sc.MIN_AVG_R, enough and avg_r is not None)),
        sc._item("盈亏比（总盈利 ÷ 总亏损）", f"{pf:.2f}" if pf is not None else "—", f"≥ {sc.MIN_PROFIT_FACTOR}",
                 st(pf is not None and pf >= sc.MIN_PROFIT_FACTOR, enough and pf is not None)),
        sc._item("最大回撤", _pct(mdd), f"≤ {sc.MAX_DRAWDOWN * 100:.0f}%", st(mdd <= sc.MAX_DRAWDOWN)),
        sc._item("费用占毛利", _pct(cost_share), f"≤ {sc.MAX_COST_SHARE * 100:.0f}%",
                 st(cost_share is not None and cost_share <= sc.MAX_COST_SHARE, enough and cost_share is not None)),
    ])
    by_year: dict[str, dict] = {}
    curve = res.equity_curve
    for i, row in enumerate(curve):
        y = row["day"][:4]
        first = by_year.setdefault(y, {"start_eq": D(curve[i - 1]["equity"]) if i else capital,
                                       "start_spy": D(curve[i - 1]["spy_index"]) if i else Decimal(1)})
        first["end_eq"], first["end_spy"] = D(row["equity"]), D(row["spy_index"])
    years = {y: {"strategy": _pct(v["end_eq"] / v["start_eq"] - 1), "spy": _pct(v["end_spy"] / v["start_spy"] - 1)}
             for y, v in by_year.items()}
    return {
        "label": "【历史规则回测｜不用 AI 的固定规则（对照组）】",
        "period": f"{days[0].isoformat()} ~ {days[-1].isoformat()}", "sessions": len(days),
        "capital": str(capital), "final_equity": str(end_equity.quantize(Decimal("0.01"))),
        "return_pct": _pct(ret), "spy_return_pct": _pct(spy), "spy_exposure_matched_return_pct": _pct(spy_matched),
        "spy_cap_matched_return_pct": _pct(spy * tot_cap),
        "max_drawdown_pct": _pct(mdd), "closed_trades": n,
        "win_rate_pct": _pct(Decimal(len([x for x in pnls if x > 0])) / n) if n else "—",
        "avg_r": f"{avg_r:.2f}" if avg_r is not None else None,
        "avg_stop_r": f"{sum(stop_rs, Decimal(0)) / len(stop_rs):.2f}" if stop_rs else None,
        "profit_factor": f"{pf:.2f}" if pf is not None else None,
        "fees_usd": str(fees.quantize(Decimal("0.01"))), "fee_share_of_gross_pct": _pct(cost_share),
        "avg_holding_days": f"{sum(t['holding_days'] for t in trades) / n:.1f}" if n else None,
        "avg_exposure_pct": _pct(sum((D(r["exposure_pct"]) for r in curve), Decimal(0)) / len(curve) / 100) if curve else "—",
        "stop_line_hit_on": halted_on.isoformat() if halted_on else None,
        "open_positions_at_end": open_positions, "by_year": years, "skips": stats,
        "gate_b": gate_b, "assumptions": ASSUMPTIONS,
    }


def report_markdown(m: dict, *, title: str = "历史规则回测报告") -> str:
    """给用户看的中文报告（白话）。"""
    lines = [f"# {title}", "", m["label"], "",
             f"- 区间：{m['period']}（{m['sessions']} 个交易日）",
             f"- 起始资金：{m['capital']} 美元 → 结束：{m['final_equity']} 美元（{m['return_pct']}，已扣手续费）",
             f"- 同期 SPY 买入持有：{m['spy_return_pct']}；按总投入上限折算的 SPY：{m['spy_cap_matched_return_pct']}"
             f"（关卡 B 用这个比）；按实际平均投入 {m['avg_exposure_pct']} 折算：{m['spy_exposure_matched_return_pct']}（仅供参考）",
             f"- 最大回撤：{m['max_drawdown_pct']}；已平仓 {m['closed_trades']} 笔，胜率 {m['win_rate_pct']}，"
             f"平均每笔 {m['avg_r'] or '—'}R，盈亏比 {m['profit_factor'] or '—'}",
             f"- 手续费：{m['fees_usd']} 美元（占毛利 {m['fee_share_of_gross_pct']}）"]
    if m.get("stop_line_hit_on"):
        lines.append(f"- ⚠️ {m['stop_line_hit_on']} {m.get('halt_reason') or '触发暂停'}，之后不再开新仓")
    lines += [f"- ⚠️ {w}" for w in m.get("warnings", [])]
    lines += ["", "## 每年对比", "", "| 年份 | 策略 | SPY |", "|---|---|---|"]
    lines += [f"| {y} | {v['strategy']} | {v['spy']} |" for y, v in m["by_year"].items()]
    lines += ["", f"## 关卡 B（有没有优势）：{m['gate_b']['status_cn']}（历史回测只作参考）", "",
              "股票清单是今天的大公司，历史回测偏乐观；最终以纸上交易的成绩单为准。", "", "| 项目 | 数值 | 标准 | 结果 |", "|---|---|---|---|"]
    lines += [f"| {i['name']} | {i['value']} | {i['target']} | {i['status_cn']} |" for i in m["gate_b"]["items"]]
    lines += ["", "## 重要假设与局限", ""] + [f"- {a}" for a in m["assumptions"]]
    return "\n".join(lines) + "\n"
