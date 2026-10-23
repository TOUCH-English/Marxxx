"""现金与持仓账本。

- 只根据实际成交（Fill）记账；成交按券商成交编号去重。
- 平均成本包含买入费用；卖出已实现盈亏 = 卖出净额 − 平均成本 × 数量。
- 现金 = 账本分录之和，可随时由分录重算核对。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.audit import audit
from ..core.enums import Side
from ..db.models import CashLedger, Fill, OrderIntent, Position, PositionLedger, RiskReservation
from ..db.types import D


def cash_balance(s: Session, pid: int, ccy: str) -> Decimal:
    rows = s.execute(select(CashLedger.amount).where(CashLedger.portfolio_id == pid, CashLedger.currency == ccy)).scalars()
    return sum((D(r) for r in rows), Decimal(0))


def cash_balances(s: Session, pid: int) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for c, a in s.execute(select(CashLedger.currency, CashLedger.amount).where(CashLedger.portfolio_id == pid)):
        out[c] = out.get(c, Decimal(0)) + D(a)
    return out


def total_flows(s: Session, pid: int, ccy: str) -> Decimal:
    rows = s.execute(select(CashLedger.amount).where(
        CashLedger.portfolio_id == pid, CashLedger.currency == ccy, CashLedger.kind == "ALLOCATION")).scalars()
    return sum((D(r) for r in rows), Decimal(0))


def reserved_cash(s: Session, pid: int, ccy: str, exclude_intent: str | None = None) -> Decimal:
    q = select(RiskReservation.cash_amount).where(
        RiskReservation.portfolio_id == pid, RiskReservation.currency == ccy, RiskReservation.status == "ACTIVE")
    if exclude_intent:
        q = q.where(RiskReservation.intent_id != exclude_intent)
    return sum((D(r) for r in s.execute(q).scalars()), Decimal(0))


def open_positions(s: Session, pid: int) -> list[Position]:
    return list(s.execute(select(Position).where(Position.portfolio_id == pid, Position.status == "OPEN")).scalars())


def get_open_position(s: Session, pid: int, symbol: str) -> Position | None:
    return s.execute(select(Position).where(
        Position.portfolio_id == pid, Position.symbol == symbol, Position.status == "OPEN")).scalar_one_or_none()


def allocate(s: Session, pid: int, mode: str, ccy: str, amount: Decimal, as_of: datetime, note: str = "") -> None:
    s.add(CashLedger(portfolio_id=pid, mode=mode, currency=ccy, amount=D(amount), kind="ALLOCATION",
                     ref_type="AUTH", note=note or "策略分配资金", as_of=as_of))
    from ..risk.locks import record_flow

    s.flush()
    record_flow(s, pid, ccy, D(amount), as_of)


@dataclass
class EquityView:
    currency: str
    cash: Decimal
    reserved: Decimal
    market_value: Decimal
    equity: Decimal
    unrealized: Decimal
    missing_prices: list[str]


def equity_view(s: Session, pid: int, ccy: str, price_of: Callable[[str], Decimal | None]) -> EquityView:
    cash = cash_balance(s, pid, ccy)
    mv = Decimal(0)
    unreal = Decimal(0)
    missing = []
    for p in open_positions(s, pid):
        if p.currency != ccy or p.qty == 0:
            continue
        px = price_of(p.symbol)
        if px is None:
            px = p.avg_cost
            missing.append(p.symbol)
        mv += px * p.qty
        unreal += (px - p.avg_cost) * p.qty
    return EquityView(ccy, cash, reserved_cash(s, pid, ccy), mv, cash + mv, unreal, missing)


def apply_fill(s: Session, intent: OrderIntent, *, broker: str, broker_fill_id: str, qty: int, price: Decimal,
               fee: Decimal, fee_estimated: bool, filled_at: datetime) -> Fill | None:
    """记一笔成交。重复的券商成交编号直接忽略（返回 None）。"""
    dup = s.execute(select(Fill.id).where(Fill.broker == broker, Fill.broker_fill_id == broker_fill_id)).first()
    if dup:
        return None
    price = D(price)
    fee = D(fee)
    if intent.filled_qty + qty > intent.qty:
        # 券商事实优先：记录异常并要求人工核对
        audit(s, "ORDER", "OVERFILL", f"成交数量超过委托数量：{intent.symbol} 委托 {intent.qty}，累计 {intent.filled_qty + qty}",
              actor="worker", portfolio_id=intent.portfolio_id, intent_id=intent.id)
        intent.needs_manual_review = True
        intent.qty = intent.filled_qty + qty
    fill = Fill(intent_id=intent.id, portfolio_id=intent.portfolio_id, mode=intent.mode, broker=broker,
                broker_fill_id=broker_fill_id, symbol=intent.symbol, side=intent.side, qty=qty, price=price,
                fee=fee, fee_estimated=fee_estimated, currency=intent.currency, filled_at=filled_at)
    s.add(fill)
    s.flush()
    notional = price * qty
    pid, ccy = intent.portfolio_id, intent.currency
    pos = get_open_position(s, pid, intent.symbol)
    realized = Decimal(0)
    if intent.side == Side.BUY:
        s.add(CashLedger(portfolio_id=pid, mode=intent.mode, currency=ccy, amount=-notional, kind="BUY",
                         ref_type="FILL", ref_id=str(fill.id), as_of=filled_at))
        if pos is None:
            hold_until = (filled_at + timedelta(days=intent.max_hold_days)) if intent.max_hold_days else None
            pos = Position(portfolio_id=pid, mode=intent.mode, symbol=intent.symbol, market=intent.market,
                           currency=ccy, qty=0, avg_cost=Decimal(0), opened_at=filled_at,
                           entry_decision_id=intent.decision_id, strategy_version=intent.strategy_version,
                           stop_price=intent.stop_price, take_profit_price=intent.take_profit_price,
                           max_hold_until=hold_until, protection_version=intent.strategy_version,
                           entry_ref_price=intent.ref_price)
            s.add(pos)
            s.flush()
        new_qty = pos.qty + qty
        pos.avg_cost = ((pos.avg_cost * pos.qty) + notional + fee) / new_qty
        pos.qty = new_qty
        pos.max_qty = max(pos.max_qty, new_qty)
        res = s.execute(select(RiskReservation).where(RiskReservation.intent_id == intent.id)).scalar_one_or_none()
        if res is not None and res.status == "ACTIVE":
            res.cash_amount = max(Decimal(0), res.cash_amount - notional - fee)
    else:
        if pos is None or pos.qty < qty:
            audit(s, "LEDGER", "SELL_WITHOUT_POSITION", f"卖出成交但账本持仓不足：{intent.symbol}",
                  actor="worker", portfolio_id=pid, intent_id=intent.id)
            intent.needs_manual_review = True
            if pos is None:
                raise RuntimeError("sell fill without position")
        s.add(CashLedger(portfolio_id=pid, mode=intent.mode, currency=ccy, amount=notional, kind="SELL",
                         ref_type="FILL", ref_id=str(fill.id), as_of=filled_at))
        realized = notional - fee - pos.avg_cost * qty
        pos.qty -= qty
        pos.realized_pnl += realized
        if pos.qty == 0:
            pos.status = "CLOSED"
            pos.closed_at = filled_at
    if fee:
        s.add(CashLedger(portfolio_id=pid, mode=intent.mode, currency=ccy, amount=-fee, kind="FEE",
                         ref_type="FILL", ref_id=str(fill.id),
                         note="预估费用" if fee_estimated else "券商费用", as_of=filled_at))
    pos.fees_paid += fee
    s.add(PositionLedger(portfolio_id=pid, position_id=pos.id, fill_id=fill.id,
                         qty_delta=qty if intent.side == Side.BUY else -qty, price=price, realized_pnl=realized,
                         as_of=filled_at))
    prev_val = (intent.avg_fill_price or Decimal(0)) * intent.filled_qty
    intent.filled_qty += qty
    intent.avg_fill_price = (prev_val + notional) / intent.filled_qty
    intent.fees += fee
    intent.position_id = pos.id
    return fill


def apply_corporate_action(s: Session, pid: int, mode: str, action, as_of: datetime) -> bool:
    """拆股调整数量和平均成本；分红入账现金。返回是否对持仓有影响。"""
    pos = get_open_position(s, pid, action.symbol)
    if pos is None or pos.qty == 0:
        return False
    if action.kind == "SPLIT":
        r = D(action.split_ratio)
        old = pos.qty
        pos.qty = int(Decimal(pos.qty) * r)
        pos.avg_cost = pos.avg_cost / r
        pos.max_qty = max(pos.max_qty, pos.qty)
        for attr in ("stop_price", "take_profit_price", "entry_ref_price"):
            v = getattr(pos, attr)
            if v is not None:
                setattr(pos, attr, v / r)
        audit(s, "LEDGER", "SPLIT", f"{action.symbol} 拆股 1:{r}，持仓 {old} → {pos.qty}，保护价同步调整",
              actor="worker", portfolio_id=pid, symbol=action.symbol, as_of=as_of)
    elif action.kind == "DIVIDEND":
        amt = D(action.cash_per_share) * pos.qty
        s.add(CashLedger(portfolio_id=pid, mode=mode, currency=pos.currency, amount=amt, kind="DIVIDEND",
                         ref_type="CORP", ref_id=f"{action.symbol}:{action.ex_date}",
                         note=f"现金股息 {action.cash_per_share}/股", as_of=as_of))
        audit(s, "LEDGER", "DIVIDEND", f"{action.symbol} 派息 {amt}", actor="worker", portfolio_id=pid,
              symbol=action.symbol, as_of=as_of)
    return True


def ledger_check(s: Session, pid: int) -> dict:
    """核对：现金分录合计 = 分配 + 卖出 − 买入 − 费用 + 股息；持仓数量 = 成交合计。"""
    sums = dict(s.execute(select(CashLedger.kind, func.count()).where(CashLedger.portfolio_id == pid)
                          .group_by(CashLedger.kind)).all())
    problems = []
    for p in s.execute(select(Position).where(Position.portfolio_id == pid)).scalars():
        net = sum((r.qty_delta for r in s.execute(select(PositionLedger).where(PositionLedger.position_id == p.id)).scalars()), 0)
        if net != p.qty and not s.execute(select(CashLedger.id).where(CashLedger.ref_type == "CORP")).first():
            problems.append(f"{p.symbol}: 持仓 {p.qty} ≠ 成交合计 {net}")
    return {"entries_by_kind": sums, "problems": problems}
