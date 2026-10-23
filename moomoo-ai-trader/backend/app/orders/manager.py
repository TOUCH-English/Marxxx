"""订单生命周期。

规则：
1. 订单意图先持久化（CREATED → SUBMITTING 已提交到数据库）再调用券商。
2. 提交超时/断线/异常 → UNKNOWN，暂停新开仓；绝不简单重试 place_order。
3. 券商回调可能先于下单函数返回：按券商订单号或 remark（= intent_id）归并，顺序无关。
4. 成交按券商成交编号去重；账本只按实际成交记账。
5. 撤单请求成功 ≠ 撤单完成：保持 CANCEL_PENDING 直到券商确认；撤单期间的成交照常记账。
6. 终态时只释放未成交部分的资金预留。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..broker.base import FillEvent, OrderUpdate, PlaceResult
from ..core.audit import audit, notify
from ..core.enums import TERMINAL_ORDER_STATUSES, LockKind, OrderStatus
from ..core.money import calc_fees
from ..db.models import BrokerOrder, OrderEvent, OrderIntent
from ..db.session import write_session
from ..portfolio import ledger
from ..risk.engine import release_reservation
from ..risk.locks import active_locks, add_lock, release_lock

log = logging.getLogger(__name__)

_BROKER_TO_STATUS = {
    "SUBMITTED": OrderStatus.ACKNOWLEDGED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCEL_PENDING": OrderStatus.CANCEL_PENDING,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "UNKNOWN": OrderStatus.UNKNOWN,
}


def _event(s: Session, intent: OrderIntent, event: str, to: str | None, detail: str = "", payload: dict | None = None):
    frm = intent.status
    if to is not None and to != intent.status:
        intent.status = to
    intent.updated_at_utc = datetime.now(tz=intent.created_at_utc.tzinfo)
    s.add(OrderEvent(intent_id=intent.id, event=event, from_status=frm, to_status=intent.status, detail=detail,
                     payload=payload or {}))


class OrderManager:
    def __init__(self, ns: str, adapter, fee_schedules: dict[str, dict], now_fn: Callable[[], datetime]):
        self.ns = ns
        self.adapter = adapter
        self.fee_schedules = fee_schedules
        self.now = now_fn
        self._mismatch_seen: dict[str, int] = {}

    # ------------------------------------------------------------ 查找
    @staticmethod
    def find_intent(s: Session, broker_order_id: str | None, remark: str | None) -> OrderIntent | None:
        if broker_order_id:
            it = s.execute(select(OrderIntent).where(OrderIntent.broker_order_id == broker_order_id)).scalar_one_or_none()
            if it:
                return it
        if remark:
            return s.get(OrderIntent, remark)
        return None

    def _attach(self, s: Session, intent: OrderIntent, broker_order_id: str) -> None:
        if intent.broker_order_id == broker_order_id:
            return
        if intent.broker_order_id and intent.broker_order_id != broker_order_id:
            audit(s, "ORDER", "BROKER_ID_CONFLICT", f"同一意图出现两个券商订单号 {intent.broker_order_id}/{broker_order_id}",
                  actor="worker", portfolio_id=intent.portfolio_id, intent_id=intent.id)
            intent.needs_manual_review = True
            return
        intent.broker_order_id = broker_order_id
        if not s.execute(select(BrokerOrder.id).where(BrokerOrder.intent_id == intent.id)).first():
            s.add(BrokerOrder(intent_id=intent.id, broker=self.adapter.broker, trd_env=self.adapter.trd_env,
                              broker_order_id=broker_order_id))

    # ------------------------------------------------------------ 提交
    def submit(self, intent_id: str) -> str:
        """提交一个 CREATED 状态的意图。返回最终本地状态。"""
        with write_session(self.ns) as s:
            intent = s.get(OrderIntent, intent_id)
            if intent is None or intent.status != OrderStatus.CREATED:
                return intent.status if intent else "MISSING"
            intent.submitted_at = self.now()
            _event(s, intent, "SUBMIT_START", OrderStatus.SUBMITTING, "意图已持久化，开始提交券商")
            args = dict(symbol=intent.symbol, side=intent.side, qty=intent.qty, limit_price=intent.limit_price,
                        remark=intent.remark)
        try:
            res: PlaceResult = self.adapter.place_order(now=self.now(), **args)
        except Exception as e:  # 超时、断线、进程异常
            res = PlaceResult("UNKNOWN", message=f"提交异常：{type(e).__name__}: {e}")
        with write_session(self.ns) as s:
            intent = s.get(OrderIntent, intent_id)
            if res.status == "ACCEPTED" and res.broker_order_id:
                self._attach(s, intent, res.broker_order_id)
                if intent.status in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN):
                    _event(s, intent, "ACK", OrderStatus.ACKNOWLEDGED, f"券商订单号 {res.broker_order_id}")
                else:
                    _event(s, intent, "ACK_LATE", None, f"下单返回晚于回调，当前 {intent.status}")
            elif res.status == "REJECTED":
                if intent.status == OrderStatus.SUBMITTING:
                    _event(s, intent, "REJECTED", OrderStatus.REJECTED, res.message)
                    intent.status_detail = res.message
                    release_reservation(s, intent.id, "rejected")
                    audit(s, "ORDER", "REJECTED", f"{intent.symbol} 委托被拒绝：{res.message}", actor="broker",
                          portfolio_id=intent.portfolio_id, symbol=intent.symbol, intent_id=intent.id,
                          decision_id=intent.decision_id or "", as_of=self.now())
            else:
                if intent.status == OrderStatus.SUBMITTING:
                    self._mark_unknown(s, intent, res.message or "提交结果未知")
            return intent.status

    def _mark_unknown(self, s: Session, intent: OrderIntent, why: str) -> None:
        _event(s, intent, "UNKNOWN", OrderStatus.UNKNOWN, why)
        intent.status_detail = why
        add_lock(s, intent.portfolio_id, LockKind.UNKNOWN_ORDER,
                 f"订单状态未知（{intent.symbol}）：暂停新开仓，等待对账/人工核对", self.now())
        audit(s, "ORDER", "UNKNOWN", f"{intent.symbol} 订单状态未知：{why}。不会自动重发。", actor="worker",
              portfolio_id=intent.portfolio_id, symbol=intent.symbol, intent_id=intent.id, as_of=self.now())

    # ------------------------------------------------------------ 推送处理
    def process_events(self, max_events: int = 1000) -> int:
        n = 0
        while n < max_events:
            try:
                ev = self.adapter.events.get_nowait()
            except Exception:
                break
            n += 1
            with write_session(self.ns) as s:
                if isinstance(ev, FillEvent):
                    self.apply_fill(s, ev)
                elif isinstance(ev, OrderUpdate):
                    self.apply_update(s, ev)
        return n

    def apply_update(self, s: Session, upd: OrderUpdate) -> None:
        intent = self.find_intent(s, upd.broker_order_id, upd.remark)
        if intent is None:
            return  # 非本系统订单（例如用户在 App 手动下单）
        self._attach(s, intent, upd.broker_order_id)
        bo = s.execute(select(BrokerOrder).where(BrokerOrder.intent_id == intent.id)).scalar_one_or_none()
        if bo:
            bo.raw_status = upd.raw_status
            bo.dealt_qty = max(bo.dealt_qty, upd.dealt_qty)
            bo.last_update = datetime.now(tz=bo.last_update.tzinfo)
        target = _BROKER_TO_STATUS.get(upd.status)
        cur = OrderStatus(intent.status)
        if target is None:
            return
        if cur in TERMINAL_ORDER_STATUSES:
            if target != cur:
                _event(s, intent, "LATE_UPDATE_IGNORED", None, f"终态 {cur} 后收到 {upd.status}")
            return
        new = target
        if cur == OrderStatus.CANCEL_PENDING and target in (OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED):
            new = OrderStatus.CANCEL_PENDING  # 撤单中继续成交：保持撤单中
        if target == OrderStatus.ACKNOWLEDGED and upd.dealt_qty > 0:
            new = OrderStatus.PARTIALLY_FILLED
        if new != cur:
            _event(s, intent, f"BROKER_{upd.status}", new, upd.message, {"dealt_qty": upd.dealt_qty})
            if upd.message and new in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                intent.status_detail = upd.message
        self._maybe_finalize(s, intent, upd.dealt_qty)

    def _maybe_finalize(self, s: Session, intent: OrderIntent, broker_dealt: int | None = None) -> None:
        if intent.status in TERMINAL_ORDER_STATUSES:
            if broker_dealt is None:
                broker_dealt = s.execute(select(BrokerOrder.dealt_qty).where(BrokerOrder.intent_id == intent.id)).scalar()
            if broker_dealt is not None and intent.filled_qty < broker_dealt:
                return  # 成交回报尚未到齐，暂不释放预留
            release_reservation(s, intent.id, intent.status)
            if intent.status == OrderStatus.CANCELLED and intent.filled_qty > 0:
                audit(s, "ORDER", "PARTIAL_CANCELLED",
                      f"{intent.symbol} 部分成交 {intent.filled_qty}/{intent.qty} 后撤销剩余，已释放未成交部分预留",
                      actor="worker", portfolio_id=intent.portfolio_id, symbol=intent.symbol, intent_id=intent.id)

    def apply_fill(self, s: Session, fe: FillEvent) -> None:
        intent = self.find_intent(s, fe.broker_order_id, fe.remark)
        if intent is None:
            return
        self._attach(s, intent, fe.broker_order_id)
        fee = fe.fee
        est = fee is None
        if est:
            fee = calc_fees(self.fee_schedules.get(intent.market), intent.side, fe.qty, fe.price) or Decimal(0)
        fill = ledger.apply_fill(s, intent, broker=self.adapter.broker, broker_fill_id=fe.broker_fill_id, qty=fe.qty,
                                 price=fe.price, fee=fee, fee_estimated=est, filled_at=fe.ts)
        if fill is None:
            return  # 重复回调
        if intent.status in (OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.UNKNOWN):
            _event(s, intent, "FILL", OrderStatus.PARTIALLY_FILLED if intent.filled_qty < intent.qty else OrderStatus.FILLED)
        elif intent.status == OrderStatus.PARTIALLY_FILLED and intent.filled_qty >= intent.qty:
            _event(s, intent, "FILL", OrderStatus.FILLED)
        elif intent.status == OrderStatus.CANCEL_PENDING and intent.filled_qty >= intent.qty:
            _event(s, intent, "FILL_DURING_CANCEL", OrderStatus.FILLED, "撤单过程中全部成交")
        else:
            _event(s, intent, "FILL", None, f"成交 {fe.qty}@{fe.price}")
        audit(s, "ORDER", "FILL", f"{intent.symbol} {'买入' if intent.side == 'BUY' else '卖出'}成交 {fe.qty} @ {fe.price}，费用 {fee}"
              + ("（预估）" if est else ""), actor="broker", portfolio_id=intent.portfolio_id, symbol=intent.symbol,
              intent_id=intent.id, decision_id=intent.decision_id or "", as_of=fe.ts)
        self._maybe_finalize(s, intent)

    # ------------------------------------------------------------ 撤单
    def cancel(self, intent_id: str, why: str) -> str:
        with write_session(self.ns) as s:
            intent = s.get(OrderIntent, intent_id)
            if intent is None:
                return "MISSING"
            if intent.status == OrderStatus.CREATED or intent.status == OrderStatus.AWAITING_APPROVAL:
                _event(s, intent, "CANCEL_LOCAL", OrderStatus.CANCELLED, why)
                release_reservation(s, intent.id, why)
                return intent.status
            if intent.status not in (OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED) or not intent.broker_order_id:
                return intent.status
            boid = intent.broker_order_id
            _event(s, intent, "CANCEL_REQUEST", OrderStatus.CANCEL_PENDING, why)
        try:
            r = self.adapter.cancel_order(boid, self.now())
        except Exception as e:
            r = None
            err = str(e)
        with write_session(self.ns) as s:
            intent = s.get(OrderIntent, intent_id)
            if r is None or r.status == "UNKNOWN":
                _event(s, intent, "CANCEL_UNKNOWN", None, "撤单请求结果未知，等待券商状态/对账")
            elif r.status == "REJECTED":
                # 撤单遭拒：通常是已成交或已撤销。以券商事实为准，等待推送/对账更新
                _event(s, intent, "CANCEL_REJECTED", None, r.message)
                audit(s, "ORDER", "CANCEL_REJECTED", f"{intent.symbol} 撤单被拒：{r.message}；以券商状态为准",
                      actor="broker", portfolio_id=intent.portfolio_id, intent_id=intent.id)
            return intent.status

    # ------------------------------------------------------------ 对账
    def reconcile(self, pid: int, managed_symbols_fn: Callable[[Session], set[str]], *, startup: bool = False) -> dict:
        now = self.now()
        report = {"orders_updated": 0, "fills_added": 0, "unknown_resolved": 0, "unknown_remaining": 0,
                  "position_mismatches": []}
        # 1. 崩溃遗留的 SUBMITTING → UNKNOWN
        with write_session(self.ns) as s:
            for it in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid,
                                                          OrderIntent.status == OrderStatus.SUBMITTING)).scalars():
                if startup or (it.submitted_at and now - it.submitted_at > timedelta(minutes=2)):
                    self._mark_unknown(s, it, "启动对账：发现提交中断的订单（进程可能在提交后崩溃）")
        # 2. 先处理推送，再用券商查询结果补齐
        self.process_events()
        try:
            orders = self.adapter.query_orders(now)
            fills = self.adapter.query_fills(now)
        except Exception as e:
            report["error"] = f"查询券商失败：{e}"
            return report
        with write_session(self.ns) as s:
            for fe in fills:
                before = s.execute(select(OrderIntent.filled_qty).where(OrderIntent.broker_order_id == fe.broker_order_id)).scalar()
                self.apply_fill(s, fe)
                after = s.execute(select(OrderIntent.filled_qty).where(OrderIntent.broker_order_id == fe.broker_order_id)).scalar()
                if after is not None and before != after:
                    report["fills_added"] += 1
            for upd in orders:
                intent = self.find_intent(s, upd.broker_order_id, upd.remark)
                if intent is None or intent.portfolio_id != pid:
                    continue
                was_unknown = intent.status == OrderStatus.UNKNOWN
                self.apply_update(s, upd)
                report["orders_updated"] += 1
                if was_unknown and intent.status != OrderStatus.UNKNOWN:
                    report["unknown_resolved"] += 1
                    audit(s, "ORDER", "UNKNOWN_RESOLVED", f"{intent.symbol} 未知订单经券商查询确认：{intent.status}",
                          actor="worker", portfolio_id=pid, intent_id=intent.id)
            remaining = list(s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid,
                                                                 OrderIntent.status == OrderStatus.UNKNOWN)).scalars())
            for it in remaining:
                # 券商查询中找不到：保留预留，要求人工核对，不自动重发
                if not it.needs_manual_review:
                    it.needs_manual_review = True
                    notify(s, "error", f"订单 {it.symbol} 状态未知，需要人工核对",
                           "券商查询未找到该订单。系统保留资金预留，不会自动重发。请在「订单与持仓」页面核对。",
                           dedupe_key=f"unknown:{it.id}")
            report["unknown_remaining"] = len(remaining)
            if not remaining:
                for lk in active_locks(s, pid):
                    if lk.kind == LockKind.UNKNOWN_ORDER:
                        release_lock(s, lk, "worker", "所有未知订单已对账确认", now)
            # 3. 持仓对账（只比较系统管理范围内的证券）
            managed = managed_symbols_fn(s)
            try:
                bpos = {p.symbol: p.qty for p in self.adapter.positions()}
            except Exception as e:
                report["error"] = f"查询券商持仓失败：{e}"
                return report
            lpos = {p.symbol: p.qty for p in ledger.open_positions(s, pid)}
            for sym in sorted(set(lpos) | (managed & set(bpos))):
                lq, bq = lpos.get(sym, 0), bpos.get(sym, 0)
                if lq == bq:
                    self._mismatch_seen.pop(sym, None)
                    continue
                if s.execute(select(OrderIntent.id).where(OrderIntent.portfolio_id == pid, OrderIntent.symbol == sym,
                                                          OrderIntent.status.in_([OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED,
                                                                                  OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING]))).first():
                    continue  # 有在途订单，下次再比
                self._mismatch_seen[sym] = self._mismatch_seen.get(sym, 0) + 1
                if self._mismatch_seen[sym] < 2 and not startup:
                    continue
                if bq < lq:
                    why = f"{sym} 券商持仓 {bq} 少于系统账本 {lq}（可能在 App 手动卖出）：已暂停该证券的自动交易，不自动修正"
                else:
                    why = f"{sym} 券商持仓 {bq} 多于系统账本 {lq}（可能有手动买入或原有持仓）：无法区分归属，禁止自动管理该证券"
                add_lock(s, pid, LockKind.RECONCILE, why, now, scope=sym, manual=True)
                report["position_mismatches"].append(why)
        return report

    # ------------------------------------------------------------ 人工核对
    def manual_resolve(self, intent_id: str, resolution: str, broker_order_id: str | None, note: str) -> str:
        """用户核对未知订单：NOT_SUBMITTED（券商确无此单）/ ATTACH（填写券商订单号，之后由对账同步）。"""
        with write_session(self.ns) as s:
            it = s.get(OrderIntent, intent_id)
            if it is None or it.status != OrderStatus.UNKNOWN:
                raise ValueError("只有状态未知的订单可以人工核对")
            if resolution == "NOT_SUBMITTED":
                _event(s, it, "MANUAL_NOT_SUBMITTED", OrderStatus.REJECTED, f"用户核对：券商无此订单。{note}")
                it.status_detail = "用户核对确认未提交"
                release_reservation(s, it.id, "manual")
            elif resolution == "ATTACH" and broker_order_id:
                self._attach(s, it, broker_order_id)
                _event(s, it, "MANUAL_ATTACH", None, f"用户填写券商订单号 {broker_order_id}，等待对账同步。{note}")
            else:
                raise ValueError("无效的核对方式")
            it.needs_manual_review = False
            audit(s, "ORDER", "MANUAL_RESOLVE", f"用户核对未知订单 {it.symbol}：{resolution}", actor="user",
                  portfolio_id=it.portfolio_id, intent_id=it.id)
            return it.status
