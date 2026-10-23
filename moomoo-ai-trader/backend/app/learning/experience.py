"""经验记录：盈利、亏损、被风控拒绝、执行故障、主动不交易都会记录，避免只学习成功案例。

经验只作为「假设参考」（verified=False），不会直接改变任何运行中的政策或提示词。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import DecisionOutcome, OrderStatus
from ..db.models import Decision, ExperienceRecord, Fill, OrderIntent, Portfolio, Position


def _exists(s: Session, key: str) -> bool:
    return s.execute(select(ExperienceRecord.id).where(ExperienceRecord.dedupe_key == key)).first() is not None


def collect(s: Session, pid: int, data_source: str, now: datetime) -> int:
    p = s.get(Portfolio, pid)
    n = 0
    # 已平仓交易
    for pos in s.execute(select(Position).where(Position.portfolio_id == pid, Position.status == "CLOSED")).scalars():
        key = f"trade:{pid}:{pos.id}"
        if _exists(s, key):
            continue
        intents = list(s.execute(select(OrderIntent).where(OrderIntent.position_id == pos.id)).scalars())
        fills = list(s.execute(select(Fill).where(Fill.intent_id.in_([i.id for i in intents]))).scalars()) if intents else []
        slippage = Decimal(0)
        for f in fills:
            it = next(i for i in intents if i.id == f.intent_id)
            if it.ref_price is None:
                continue
            diff = (f.price - it.ref_price) if f.side == "BUY" else (it.ref_price - f.price)
            slippage += diff * f.qty
        exit_reasons = Counter(i.reason for i in intents if i.side == "SELL" and i.filled_qty > 0)
        exec_fault = any(i.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN) or i.needs_manual_review for i in intents)
        pnl = pos.realized_pnl
        gross = pnl + pos.fees_paid
        if exec_fault:
            cls = "EXECUTION_FAULT"
        elif pnl > 0:
            cls = "WIN"
        elif gross > 0 >= pnl:
            cls = "COST_IMPACT"  # 扣费前盈利、扣费后亏损
        elif exit_reasons.get("EXIT_STOP"):
            cls = "SIGNAL_ERROR"  # 入场判断错误，被止损
        else:
            cls = "LOSS"
        entry = s.get(Decision, pos.entry_decision_id) if pos.entry_decision_id else None
        s.add(ExperienceRecord(
            portfolio_id=pid, mode=p.mode, data_source=data_source, kind="CLOSED_TRADE", dedupe_key=key,
            symbol=pos.symbol, decision_id=pos.entry_decision_id or "", position_id=pos.id,
            strategy_version=pos.strategy_version, outcome_class=cls, realized_pnl=pnl, fees=pos.fees_paid,
            slippage=slippage, holding_hours=int((pos.closed_at - pos.opened_at).total_seconds() // 3600),
            details={"exit_reasons": dict(exit_reasons), "currency": pos.currency,
                     "entry_rationale": (entry.model_judgment or {}).get("rationale", "") if entry else "",
                     "entry_source": entry.source if entry else "",
                     "gross_before_fees": str(gross), "max_qty": pos.max_qty},
            as_of=pos.closed_at or now))
        n += 1
    # 被风控拒绝 / 无效输出
    for d in s.execute(select(Decision).where(Decision.portfolio_id == pid, Decision.outcome.in_(
            [DecisionOutcome.RISK_BLOCKED, DecisionOutcome.INVALID]))).scalars():
        key = f"rej:{d.id}"
        if _exists(s, key):
            continue
        s.add(ExperienceRecord(portfolio_id=pid, mode=p.mode, data_source=data_source,
                               kind="RISK_REJECTED" if d.outcome == DecisionOutcome.RISK_BLOCKED else "INVALID_OUTPUT",
                               dedupe_key=key, symbol=d.symbol, decision_id=d.id, strategy_version=d.strategy_version,
                               outcome_class=d.outcome, details={"reason": d.outcome_reason[:500], "action": d.action},
                               as_of=d.as_of))
        n += 1
    # 执行故障
    for it in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid, OrderIntent.status.in_(
            [OrderStatus.REJECTED, OrderStatus.UNKNOWN]))).scalars():
        key = f"exec:{it.id}"
        if _exists(s, key):
            continue
        s.add(ExperienceRecord(portfolio_id=pid, mode=p.mode, data_source=data_source, kind="EXECUTION_FAILURE",
                               dedupe_key=key, symbol=it.symbol, decision_id=it.decision_id or "",
                               strategy_version=it.strategy_version, outcome_class="EXECUTION_FAULT",
                               details={"status": it.status, "detail": it.status_detail[:300]}, as_of=it.as_of or now))
        n += 1
    return n


def record_no_trade_day(s: Session, pid: int, data_source: str, day: str, counts: dict, as_of: datetime) -> None:
    key = f"notrade:{pid}:{day}"
    if _exists(s, key):
        return
    p = s.get(Portfolio, pid)
    s.add(ExperienceRecord(portfolio_id=pid, mode=p.mode, data_source=data_source, kind="NO_TRADE", dedupe_key=key,
                           symbol="*", outcome_class="NO_TRADE", details={"day": day, "counts": counts}, as_of=as_of))
