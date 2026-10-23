"""固定规则基准策略（对照测试，不声称有盈利优势）。

- 只用白名单/股票池内证券、已完整收盘的日 K（复权价计算均线）。
- 短期均线上穿长期均线（默认 20/60）在 K 线收盘后成立；在下一个正常交易时段按当时报价提交限价委托，
  不会按信号当日收盘价假装成交。
- 每个信号只产生一个订单意图（去重键 = 策略版本 + 证券 + 信号日期）。
- 退出：反向交叉 + 已批准的保护规则（止损/持有期限）。不摊平、不补仓。
- 可选「AI 仅解释」或「AI 可否决新开仓」：AI 只能对规则已产生的信号说不，不能批准规则不允许的交易。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.enums import DecisionAction, DecisionOutcome, IntentReason
from ..core.money import round_to_tick
from ..db.session import write_session
from ..db.types import D
from ..market_data.features import atr, sma
from ..portfolio import ledger
from . import market_context as mctx
from .common import TickState, execute_buy, execute_sell, record_decision

DEFAULT_BASELINE_PARAMS: dict[str, Any] = {
    "short": 20, "long": 60, "atr_stop_mult": "2.0", "max_hold_days": 30, "ai_overlay": "none",
}


def signal(bars, short: int, long: int) -> dict:
    """返回基于已收盘 K 线的交叉信号。bars 必须全部是已收盘日 K。"""
    closes = [float(b.adj_close) for b in bars if not b.suspended]
    if len(closes) < long + 1:
        return {"ready": False, "reason": f"预热数据不足：需要 {long + 1} 根，已有 {len(closes)} 根"}
    s_now, l_now = sma(closes, short), sma(closes, long)
    s_prev, l_prev = sma(closes[:-1], short), sma(closes[:-1], long)
    out = {"ready": True, "sma_short": round(s_now, 4), "sma_long": round(l_now, 4),
           "sma_short_prev": round(s_prev, 4), "sma_long_prev": round(l_prev, 4),
           "signal_day": bars[-1].day.isoformat()}
    out["cross_up"] = s_prev <= l_prev and s_now > l_now
    out["cross_down"] = s_prev >= l_prev and s_now < l_now
    return out


def run(rt, ts: TickState, wall_now: datetime) -> None:
    if not ts.auth_ok or ts.strategy is None:
        return
    p = dict(DEFAULT_BASELINE_PARAMS) | (ts.strategy.params or {})
    short, long = int(p["short"]), int(p["long"])
    now = rt.now()
    sv = f"{ts.strategy.key}:{ts.strategy.version}"
    with write_session(rt.ns) as s:
        held = {x.symbol: x for x in ledger.open_positions(s, ts.portfolio.id)}
        for sym in sorted(ts.scope | set(held)):
            inst = rt.provider.instrument(sym)
            if inst is None or not rt.calendar.is_open(inst.market, now):
                continue
            last_closed = rt.calendar.last_closed_session(inst.market, now)
            if last_closed is None:
                continue
            key = f"{sym}|{last_closed.isoformat()}"
            if rt.last_baseline_eval.get(sym) == key:
                continue
            rt.last_baseline_eval[sym] = key
            bars = rt.provider.daily_bars(sym, now, long + 5)
            if bars and bars[-1].day != last_closed:
                continue  # 数据未更新到最近收盘日，不产生信号
            sig = signal(bars, short, long)
            base = dict(source="BASELINE", symbol=sym, as_of=now, facts={"signal": sig},
                        market=inst.market, currency=inst.currency)
            if not sig["ready"]:
                record_decision(s, rt, ts, action=DecisionAction.NO_TRADE, outcome=DecisionOutcome.NO_ACTION,
                                reason=sig["reason"], **base)
                continue
            pos = held.get(sym)
            if pos is not None:
                if sig["cross_down"]:
                    d = record_decision(s, rt, ts, action=DecisionAction.SELL_PROPOSAL, outcome=DecisionOutcome.NO_ACTION,
                                        reason="", proposal={"sell_fraction": "1", "rule": "短均线下穿长均线"}, **base)
                    d.outcome, d.outcome_reason = execute_sell(s, rt, ts, d, fraction=Decimal(1), min_price=None,
                                                               reason=IntentReason.EXIT_SIGNAL,
                                                               key_suffix=sig["signal_day"])
                continue
            if not sig["cross_up"]:
                record_decision(s, rt, ts, action=DecisionAction.NO_TRADE, outcome=DecisionOutcome.NO_ACTION,
                                reason=f"无上穿信号（MA{short}={sig['sma_short']}，MA{long}={sig['sma_long']}）", **base)
                continue
            q = rt.quote(sym)
            a = atr(bars, 14)
            if q is None or q.ask is None or a is None:
                record_decision(s, rt, ts, action=DecisionAction.BUY_PROPOSAL, outcome=DecisionOutcome.RISK_BLOCKED,
                                reason="出现上穿信号，但当前缺少可用报价或 ATR，不下单", **base)
                continue
            atr_raw = Decimal(str(a)) / bars[-1].adj_factor
            stop = round_to_tick(inst.market, q.ask - D(p["atr_stop_mult"]) * atr_raw, "down")
            close_t = rt.calendar.session_close(inst.market, rt.calendar.local_date(inst.market, now))
            d = record_decision(s, rt, ts, action=DecisionAction.BUY_PROPOSAL, outcome=DecisionOutcome.NO_ACTION,
                                reason="", expires_at=close_t,
                                proposal={"rule": f"MA{short} 上穿 MA{long}（信号日 {sig['signal_day']}）",
                                          "stop_loss_price": str(stop), "max_holding_days": int(p["max_hold_days"])},
                                **base)
            overlay = p.get("ai_overlay", "none")
            if overlay in ("explain", "veto"):
                agrees, why = _ai_overlay(rt, ts, sym)
                d.model_judgment = {"overlay": overlay, "ai_agrees": agrees, "rationale": why,
                                    "note": "AI 叠加实验：AI 只能解释或否决规则已产生的信号，不能批准规则外的交易"}
                if overlay == "veto" and not agrees:
                    d.outcome, d.outcome_reason = DecisionOutcome.NO_ACTION, f"AI 否决了本次新开仓（实验模式）：{why[:120]}"
                    continue
            why = mctx.entry_block_reason(s, rt, ts.portfolio.id, sym) if inst.market == "US" else None
            if why:  # 与 AI 策略同样的大盘、财报、同行业检查
                d.outcome, d.outcome_reason = DecisionOutcome.RISK_BLOCKED, why
                continue
            d.outcome, d.outcome_reason = execute_buy(
                s, rt, ts, wall_now, d, stop=stop, take_profit=None, max_hold_days=int(p["max_hold_days"]),
                target_pct=None, max_price=None, intent_key=f"BASE:{sv}:{sym}:{sig['signal_day']}:BUY")


def _ai_overlay(rt, ts: TickState, sym: str) -> tuple[bool, str]:
    """把规则信号的证券单独交给 AI 判断。AI 不同意（或无效/超时）→ 视为否决；AI 永远不能产生规则外的买单。"""
    from ..ai.validate import ValidationContext, parse_and_validate
    from . import ai_autonomous as aa

    p = aa._params(ts)
    ts1 = TickState(portfolio=ts.portfolio, auth=ts.auth, auth_ok=ts.auth_ok, auth_reason=ts.auth_reason,
                    params=ts.params, risk_policy_version=ts.risk_policy_version, strategy=ts.strategy, scope={sym},
                    risk_policy_version_id=ts.risk_policy_version_id)
    inst = rt.provider.instrument(sym)
    payload, meta = aa.build_payload(rt, ts1, inst.market, p | {"max_candidates": 1})
    if sym not in meta["candidates"]:
        return False, "AI 输入中没有该证券（数据不足）"
    try:
        res = rt.ai_provider.complete(payload)
    except Exception as e:
        return False, f"AI 调用异常：{e}"
    if res.status != "OK":
        return False, f"AI 未给出结果（{res.status}）"
    from ..db.types import D

    v = parse_and_validate(res.text, ValidationContext(
        as_of=datetime.fromisoformat(payload["as_of_utc"]),  # 与发给模型的时点一致
        strategy_version=payload["strategy_version"], candidate_symbols={sym}, held_symbols=set(),
        evidence_ids={k: set(x) for k, x in meta["evidence"].items()},
        last_prices={k: D(x) for k, x in meta["last_prices"].items() if x is not None},
        max_target_position_pct=D(meta["max_target_position_pct"]), max_holding_days=meta["max_holding_days"],
        max_valid_minutes=meta["max_valid_minutes"], now=rt.now()))
    d = next((x for x in v.decisions if x.raw.symbol == sym), None)
    if not v.ok or d is None:
        return False, "AI 输出无效"
    return d.raw.action == "BUY_PROPOSAL", d.raw.rationale
