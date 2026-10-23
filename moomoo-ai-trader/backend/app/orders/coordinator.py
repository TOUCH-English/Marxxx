"""退出订单协调器：所有卖出（止损、止盈、持有期限、AI 卖出、基准反向信号、手动清仓）
都经过这里，保证「在途卖单剩余数量 + 新卖单数量 ≤ 实际持仓」，防止超卖。

保护性退出优先：触发时先撤销同一证券的非保护性卖单，撤单确认后再补足剩余数量。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.audit import audit
from ..core.enums import PROTECTIVE_REASONS, IntentReason, OrderStatus, Side
from ..core.money import round_lot_down, round_to_tick
from ..db.models import OrderIntent, Position
from ..db.types import D

_OPEN = [OrderStatus.CREATED, OrderStatus.AWAITING_APPROVAL, OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED,
         OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN]


def open_sells(s: Session, pid: int, symbol: str) -> list[OrderIntent]:
    return list(s.execute(select(OrderIntent).where(
        OrderIntent.portfolio_id == pid, OrderIntent.symbol == symbol, OrderIntent.side == Side.SELL,
        OrderIntent.status.in_(_OPEN))).scalars())


def available_to_sell(s: Session, pos: Position) -> int:
    pending = sum(it.qty - it.filled_qty for it in open_sells(s, pos.portfolio_id, pos.symbol))
    return max(0, pos.qty - pending)


def exit_limit_price(market: str, bid: Decimal, params: dict) -> Decimal:
    """可成交的卖出限价：略低于买一价（止损限价不保证成交，跳空时可能更差或无法成交）。"""
    return round_to_tick(market, D(bid) * (1 - D(params["exit_limit_discount_bps"]) / 10000), "down")


def request_exit(s: Session, *, pos: Position, reason: str, bid: Decimal | None, params: dict, mode: str,
                 account_id: str, auth_id: int | None, now: datetime, fraction: Decimal | None = None,
                 lot: int = 1, decision_id: str | None = None, key_suffix: str = "", strategy_version: str = "",
                 min_price: Decimal | None = None, status: str = OrderStatus.CREATED) -> tuple[OrderIntent | None, str]:
    if bid is None or bid <= 0:
        return None, "没有可用买一价，暂不能生成卖单（保护规则会在下一次行情更新时再试）"
    avail = available_to_sell(s, pos)
    if fraction is None or fraction >= 1:
        qty = avail
    else:
        want = round_lot_down(int(Decimal(pos.qty) * D(fraction)), lot)
        if want == 0:
            return None, f"减仓比例 {fraction} 不足一手（每手 {lot}）"
        qty = min(want, avail)
    if qty <= 0:
        return None, "可卖数量为 0（已有在途卖单覆盖全部持仓）"
    limit = exit_limit_price(pos.market, bid, params)
    if min_price is not None and reason == IntentReason.EXIT_AI:
        if bid < D(min_price):
            return None, f"买一价 {bid} 低于 AI 设定的最低卖出价 {min_price}，等待价格条件"
        limit = max(limit, round_to_tick(pos.market, D(min_price), "up"))
    key = f"EXIT:{pos.id}:{reason}:{decision_id or ''}:{key_suffix}"
    if s.execute(select(OrderIntent.id).where(OrderIntent.intent_key == key)).first():
        return None, "相同退出信号已生成过卖单"
    iid = str(uuid.uuid4())
    it = OrderIntent(
        id=iid, intent_key=key, portfolio_id=pos.portfolio_id, authorization_id=auth_id, mode=mode,
        account_id=account_id, decision_id=decision_id, position_id=pos.id,
        strategy_version=strategy_version or pos.strategy_version, symbol=pos.symbol, market=pos.market,
        currency=pos.currency, side=Side.SELL, reason=reason, qty=qty, limit_price=limit, status=status,
        ref_price=D(bid), remark=iid, as_of=now,
        expires_at=now + timedelta(minutes=int(params["exit_reprice_minutes"])),
        risk_check={"ok": True, "checks": [{"name": "防超卖", "ok": True, "detail": f"持仓 {pos.qty}，在途卖出后可卖 {avail}，本次 {qty}"}]},
    )
    s.add(it)
    s.flush()
    labels = {IntentReason.EXIT_STOP: "止损", IntentReason.EXIT_TAKE_PROFIT: "止盈", IntentReason.EXIT_TIME: "持有期限到期",
              IntentReason.EXIT_AI: "AI 卖出决定", IntentReason.EXIT_SIGNAL: "基准策略反向信号",
              IntentReason.MANUAL_LIQUIDATE: "用户手动清仓"}
    audit(s, "ORDER", "EXIT_INTENT", f"{pos.symbol} {labels.get(reason, reason)}：卖出 {qty} @≥{limit}",
          actor="worker" if reason != IntentReason.MANUAL_LIQUIDATE else "user", portfolio_id=pos.portfolio_id,
          symbol=pos.symbol, decision_id=decision_id or "", intent_id=iid, as_of=now)
    return it, "ok"


def non_protective_sells(s: Session, pos: Position) -> list[OrderIntent]:
    return [it for it in open_sells(s, pos.portfolio_id, pos.symbol) if it.reason not in PROTECTIVE_REASONS]
