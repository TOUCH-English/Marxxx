"""伪券商：用于实盘执行路径的离线故障注入验证（不连接任何真实券商）。

可注入的故障：
- timeout_after_accept：券商已接收订单，但下单调用超时（本机不知道结果）
- callback_before_return：订单/成交回调先于下单函数返回
- reject_next：下一笔下单被拒绝
- cancel_reject：撤单被拒
- fill_on_cancel：撤单请求期间继续成交
- duplicate_callbacks：重复推送同一成交
- 手动 fill() / set_position()：模拟部分成交、用户在 App 手动交易
"""
from __future__ import annotations

import queue
from datetime import datetime, timezone
from decimal import Decimal

from ..broker.base import (
    AdapterHealth,
    BrokerPosition,
    CancelResult,
    CashInfo,
    FillEvent,
    OrderUpdate,
    PlaceResult,
)


class FakeBroker:
    broker = "FAKE"

    def __init__(self, trd_env: str = "REAL", acc_id: str = "FAKE-0001", cash: dict[str, Decimal] | None = None):
        self.trd_env = trd_env
        self.acc_id = acc_id
        self.events: queue.Queue = queue.Queue()
        self.orders: dict[str, dict] = {}
        self.fills: list[FillEvent] = []
        self.pos: dict[str, int] = {}
        self._cash = cash or {"USD": Decimal("100000"), "MYR": Decimal("100000")}
        self.place_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.timeout_after_accept = False
        self.callback_before_return = False
        self.fill_immediately = False
        self.reject_next: str | None = None
        self.cancel_reject: str | None = None
        self.fill_on_cancel = 0
        self.duplicate_callbacks = False
        self.connected = True
        self._seq = 0
        self._fseq = 0

    # ------------------------------------------------------------ 接口
    def place_order(self, *, symbol, side, qty, limit_price, remark, now) -> PlaceResult:
        self.place_calls.append({"symbol": symbol, "side": side, "qty": qty, "price": limit_price, "remark": remark,
                                 "trd_env": self.trd_env, "acc_id": self.acc_id})
        if self.reject_next:
            msg, self.reject_next = self.reject_next, None
            return PlaceResult("REJECTED", message=msg)
        self._seq += 1
        oid = f"FK{self._seq:06d}"
        self.orders[oid] = {"id": oid, "symbol": symbol, "side": side, "qty": qty, "price": Decimal(limit_price),
                            "dealt": 0, "status": "SUBMITTED", "remark": remark}
        if self.callback_before_return:
            self.events.put(self._upd(oid))
            if self.fill_immediately:
                self.fill(oid, qty, Decimal(limit_price))
        if self.timeout_after_accept:
            raise TimeoutError("模拟：券商已接收但本机请求超时")
        return PlaceResult("ACCEPTED", broker_order_id=oid)

    def cancel_order(self, broker_order_id, now) -> CancelResult:
        self.cancel_calls.append(broker_order_id)
        o = self.orders.get(broker_order_id)
        if self.cancel_reject:
            return CancelResult("REJECTED", self.cancel_reject)
        if o is None or o["status"] in ("FILLED", "CANCELLED", "REJECTED"):
            return CancelResult("REJECTED", "订单已是终态")
        o["status"] = "CANCEL_PENDING"
        self.events.put(self._upd(broker_order_id))
        if self.fill_on_cancel:
            self.fill(broker_order_id, min(self.fill_on_cancel, o["qty"] - o["dealt"]), o["price"])
        if o["status"] != "FILLED":
            o["status"] = "CANCELLED"
            self.events.put(self._upd(broker_order_id))
        return CancelResult("REQUESTED")

    def query_orders(self, now) -> list[OrderUpdate]:
        return [self._upd(oid) for oid in self.orders]

    def query_fills(self, now) -> list[FillEvent]:
        return list(self.fills)

    def positions(self) -> list[BrokerPosition]:
        return [BrokerPosition(symbol=s, qty=q, can_sell_qty=q) for s, q in self.pos.items() if q]

    def cash(self) -> list[CashInfo]:
        return [CashInfo(currency=c, cash=v, withdrawable=v, buying_power=v * 2, reliable=True) for c, v in self._cash.items()]

    def health(self) -> AdapterHealth:
        return AdapterHealth(self.connected, "伪券商（故障注入测试）")

    def on_market_tick(self, now, provider) -> None:
        return None

    # ------------------------------------------------------------ 测试控制
    def fill(self, oid: str, qty: int, price: Decimal, ts: datetime | None = None) -> FillEvent:
        o = self.orders[oid]
        self._fseq += 1
        fe = FillEvent(broker_fill_id=f"FKF{self._fseq:06d}", broker_order_id=oid, symbol=o["symbol"], side=o["side"],
                       qty=qty, price=Decimal(price), ts=ts or datetime.now(timezone.utc), fee=Decimal("1.00"),
                       remark=o["remark"])
        o["dealt"] += qty
        self.pos[o["symbol"]] = self.pos.get(o["symbol"], 0) + (qty if o["side"] == "BUY" else -qty)
        if o["dealt"] >= o["qty"]:
            o["status"] = "FILLED"
        elif o["status"] == "SUBMITTED":
            o["status"] = "PARTIALLY_FILLED"
        self.fills.append(fe)
        self.events.put(fe)
        if self.duplicate_callbacks:
            self.events.put(fe)
        self.events.put(self._upd(oid))
        return fe

    def set_position(self, symbol: str, qty: int) -> None:
        self.pos[symbol] = qty

    def _upd(self, oid: str) -> OrderUpdate:
        o = self.orders[oid]
        return OrderUpdate(broker_order_id=oid, status=o["status"], dealt_qty=o["dealt"], remark=o["remark"],
                           raw_status=o["status"], symbol=o["symbol"], side=o["side"], qty=o["qty"], price=o["price"])
