"""策略共用：决策记录、风控上下文、买入/卖出执行（AI 与基准策略走同一条链路）。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ..core.audit import audit
from ..core.enums import DecisionOutcome, IntentReason, Mode, OrderStatus
from ..db.models import Authorization, Decision, Portfolio, StrategyVersion
from ..db.types import D
from ..orders import coordinator
from ..portfolio import ledger
from ..risk.engine import EntryRequest, RiskContext, create_entry_intent


@dataclass
class TickState:
    portfolio: Portfolio
    auth: Authorization | None
    auth_ok: bool
    auth_reason: str
    params: dict[str, Any]
    risk_policy_version: int
    strategy: StrategyVersion | None
    scope: set[str]
    health_blocks: list[str] = field(default_factory=list)
    broker_cash: dict[str, Any] = field(default_factory=dict)


def risk_ctx(rt, ts: TickState, wall_now: datetime) -> RiskContext:
    return RiskContext(
        mode=rt.mode, portfolio=ts.portfolio, auth=ts.auth, params=ts.params,
        risk_policy_version=ts.risk_policy_version, now=rt.now(), wall_now=wall_now, calendar=rt.calendar,
        is_replay=rt.is_replay, fee_schedules=rt.fee_schedules, scope_symbols=ts.scope, account_id=rt.account_id,
        broker_cash=ts.broker_cash, health_blocks=ts.health_blocks, price_of=rt.price_of,
    )


def record_decision(s: Session, rt, ts: TickState, *, source: str, symbol: str, action: str, outcome: str,
                    reason: str, as_of: datetime, expires_at: datetime | None = None, proposal: dict | None = None,
                    facts: dict | None = None, rule_calc: dict | None = None, model_judgment: dict | None = None,
                    evidence: list | None = None, ai_call_id: int | None = None, decision_id: str | None = None,
                    market: str = "", currency: str = "") -> Decision:
    sv = ts.strategy
    d = Decision(
        id=decision_id or str(uuid.uuid4()), portfolio_id=ts.portfolio.id, authorization_id=ts.auth.id if ts.auth else None,
        mode=rt.mode.value, account_id=rt.account_id, strategy_version_id=sv.id if sv else 0,
        strategy_version=f"{sv.key}:{sv.version}" if sv else "", source=source, symbol=symbol, market=market,
        currency=currency, action=action, as_of=as_of, expires_at=expires_at, proposal=proposal or {},
        facts=facts or {}, rule_calc=rule_calc or {}, model_judgment=model_judgment or {}, evidence=evidence or [],
        ai_call_id=ai_call_id, outcome=outcome, outcome_reason=reason, data_source=rt.data_source,
    )
    s.add(d)
    s.flush()
    return d


def execute_buy(s: Session, rt, ts: TickState, wall_now: datetime, d: Decision, *, stop: Decimal | None,
                take_profit: Decimal | None, max_hold_days: int | None, target_pct: Decimal | None,
                max_price: Decimal | None, intent_key: str, reason: str = IntentReason.ENTRY) -> tuple[str, str]:
    inst = rt.provider.instrument(d.symbol)
    q = rt.quote(d.symbol)
    sv = ts.strategy
    req = EntryRequest(
        symbol=d.symbol, instrument=inst, quote=q, stop_price=stop, intent_key=intent_key,
        strategy_version=d.strategy_version, strategy_version_id=sv.id if sv else 0, decision_id=d.id,
        take_profit_price=take_profit, max_hold_days=max_hold_days, target_position_pct=target_pct,
        max_price=max_price, signal_time=d.as_of, signal_expires_at=d.expires_at, reason=reason,
    )
    intent, res = create_entry_intent(s, risk_ctx(rt, ts, wall_now), req)
    d.rule_calc = res.as_json()
    if intent is None:
        if res.duplicate:
            return DecisionOutcome.NO_ACTION, "相同信号已处理（去重）"
        return DecisionOutcome.RISK_BLOCKED, "；".join(res.reasons) or "风控未通过"
    if rt.mode == Mode.LIVE_REVIEW:
        intent.status = OrderStatus.AWAITING_APPROVAL
        return DecisionOutcome.AWAITING_APPROVAL, f"已生成买单 {intent.qty}@≤{intent.limit_price}，等待您确认（LIVE_REVIEW）"
    return DecisionOutcome.AUTO_EXECUTED, f"已自动生成买单 {intent.qty} 股 @≤{intent.limit_price}，提交券商"


def execute_sell(s: Session, rt, ts: TickState, d: Decision, *, fraction: Decimal | None, min_price: Decimal | None,
                 reason: str = IntentReason.EXIT_AI, key_suffix: str = "") -> tuple[str, str]:
    if ts.auth is not None and not ts.auth.permissions.get("sell", True):
        return DecisionOutcome.RISK_BLOCKED, "运行授权未允许卖出"
    pos = ledger.get_open_position(s, ts.portfolio.id, d.symbol)
    if pos is None:
        return DecisionOutcome.NO_ACTION, "没有持仓"
    from ..risk.locks import blocking_locks

    if any(lk.scope == d.symbol and lk.kind == "RECONCILE" for lk in blocking_locks(s, ts.portfolio.id, d.symbol)):
        return DecisionOutcome.RISK_BLOCKED, "该证券对账不一致，已暂停自动管理"
    q = rt.quote(d.symbol)
    if q is None or q.bid is None or q.suspended or not rt.calendar.is_open(pos.market, rt.now()):
        return DecisionOutcome.WAITING_PRICE, "当前无法交易（休市/停牌/无买价），等待下一次机会"
    inst = rt.provider.instrument(d.symbol)
    it, why = coordinator.request_exit(
        s, pos=pos, reason=reason, bid=q.bid, params=ts.params, mode=rt.mode.value, account_id=rt.account_id,
        auth_id=ts.auth.id if ts.auth else None, now=rt.now(), fraction=fraction, lot=inst.lot_size if inst else 1,
        decision_id=d.id, key_suffix=key_suffix, strategy_version=d.strategy_version, min_price=min_price,
        status=OrderStatus.AWAITING_APPROVAL if rt.mode == Mode.LIVE_REVIEW else OrderStatus.CREATED,
    )
    if it is None:
        return (DecisionOutcome.WAITING_PRICE if "等待价格" in why else DecisionOutcome.NO_ACTION), why
    if rt.mode == Mode.LIVE_REVIEW:
        return DecisionOutcome.AWAITING_APPROVAL, f"已生成卖单 {it.qty}，等待您确认（LIVE_REVIEW）"
    return DecisionOutcome.AUTO_EXECUTED, f"已自动生成卖单 {it.qty} 股 @≥{it.limit_price}"


def note(s: Session, rt, ts: TickState, msg: str, **kw) -> None:
    audit(s, "STRATEGY", kw.pop("action", "NOTE"), msg, actor="worker", portfolio_id=ts.portfolio.id,
          as_of=rt.now(), **kw)


def dd(x) -> Decimal | None:
    return D(x) if x is not None else None
