"""本地模拟撮合器（LOCAL_SIM）。

它扮演一家「外部券商」：有自己独立持久化的账户、订单与成交，
交易引擎只能通过执行适配器接口与它交互，和真实券商一样需要对账。

撮合假设（会显示在报告中）：
- 买入限价 ≥ 卖一价才成交，成交价 = 卖一价 + 滑点（不超过限价）；卖出对称。
- 限价仅被触及不保证全部成交：每个撮合步最多成交该步新增成交量的 participation 比例，
  不足一手则本步不成交，因此会出现部分成交与限价未成交。
- 当日有效单在当日收市后由「券商」撤销；停牌、休市不撮合。
- 撤单请求先进入「撤单中」，下一撮合步先尝试撮合（可能继续成交），再完成撤单。
"""
from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.money import calc_fees, is_valid_tick, round_to_tick
from ..db.types import D
from ..broker.base import (
    AdapterHealth,
    BrokerPosition,
    CancelResult,
    CashInfo,
    FillEvent,
    OrderUpdate,
    PlaceResult,
)

OPEN = ("SUBMITTED", "PARTIALLY_FILLED", "CANCEL_PENDING")


class LocalSimBroker:
    broker = "LOCAL_SIM"
    trd_env = "LOCAL"

    def __init__(self, state_path: Path, *, acc_id: str, fee_schedules: dict[str, dict], calendar,
                 slippage_bps: int = 5, participation: Decimal = Decimal("0.10"),
                 reject_symbols: list[str] | None = None):
        self.state_path = state_path
        self.acc_id = acc_id
        self.fee_schedules = fee_schedules
        self.calendar = calendar
        self.slippage_bps = slippage_bps
        self.participation = participation
        self.reject_symbols = set(reject_symbols or [])
        self.events: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self.state: dict[str, Any] = {
            "cash": {}, "positions": {}, "orders": {}, "fills": [], "seq": 0, "fill_seq": 0,
            "seen_volume": {}, "applied_actions": [], "instruments": {},
        }
        if state_path.exists():
            self.state = json.loads(state_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 持久化
    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ 账户（模拟入金）
    def fund(self, currency: str, amount: Decimal) -> None:
        with self._lock:
            cur = D(self.state["cash"].get(currency, "0"))
            self.state["cash"][currency] = str(cur + D(amount))
            self._save()

    def register_instrument(self, symbol: str, market: str, currency: str, lot_size: int) -> None:
        with self._lock:
            self.state["instruments"][symbol] = {"market": market, "currency": currency, "lot": lot_size}
            self._save()

    # ------------------------------------------------------------ 下单/撤单
    def place_order(self, *, symbol: str, side: str, qty: int, limit_price: Decimal, remark: str,
                    now: datetime) -> PlaceResult:
        with self._lock:
            inst = self.state["instruments"].get(symbol)
            if inst is None:
                return PlaceResult("REJECTED", message="模拟券商：未知证券")
            if symbol in self.reject_symbols:
                return PlaceResult("REJECTED", message="模拟券商：该证券当前不可交易（故障注入）")
            if qty <= 0 or qty % inst["lot"] != 0:
                return PlaceResult("REJECTED", message=f"模拟券商：数量必须为 {inst['lot']} 的整数倍")
            if not is_valid_tick(inst["market"], limit_price):
                return PlaceResult("REJECTED", message="模拟券商：价格不符合最小变动单位")
            if side == "SELL":
                pos = self.state["positions"].get(symbol, {"qty": 0})
                pending = sum(o["qty"] - o["dealt_qty"] for o in self.state["orders"].values()
                              if o["symbol"] == symbol and o["side"] == "SELL" and o["status"] in OPEN)
                if qty > int(pos["qty"]) - pending:
                    return PlaceResult("REJECTED", message="模拟券商：可卖数量不足")
            else:
                cash = D(self.state["cash"].get(inst["currency"], "0"))
                need = D(limit_price) * qty
                held = sum(D(o["price"]) * (o["qty"] - o["dealt_qty"]) for o in self.state["orders"].values()
                           if o["side"] == "BUY" and o["status"] in OPEN and o["currency"] == inst["currency"])
                if need > cash - held:
                    return PlaceResult("REJECTED", message="模拟券商：现金不足（不使用融资）")
            self.state["seq"] += 1
            oid = f"SIM{self.state['seq']:08d}"
            self.state["orders"][oid] = {
                "id": oid, "symbol": symbol, "side": side, "qty": int(qty), "price": str(limit_price),
                "dealt_qty": 0, "dealt_value": "0", "status": "SUBMITTED", "remark": remark,
                "currency": inst["currency"], "market": inst["market"], "created": now.isoformat(),
                "day": self.calendar.local_date(inst["market"], now).isoformat(), "cancel_requested": False,
            }
            self._save()
            self._push(oid, now)
            return PlaceResult("ACCEPTED", broker_order_id=oid)

    def cancel_order(self, broker_order_id: str, now: datetime) -> CancelResult:
        with self._lock:
            o = self.state["orders"].get(broker_order_id)
            if o is None:
                return CancelResult("REJECTED", "模拟券商：订单不存在")
            if o["status"] not in OPEN:
                return CancelResult("REJECTED", f"模拟券商：订单已是终态 {o['status']}，无法撤单")
            o["cancel_requested"] = True
            o["status"] = "CANCEL_PENDING"
            self._save()
            self._push(broker_order_id, now)
            return CancelResult("REQUESTED")

    # ------------------------------------------------------------ 撮合
    def on_market_tick(self, now: datetime, provider) -> None:
        with self._lock:
            self._apply_corporate_actions(now, provider)
            for oid, o in list(self.state["orders"].items()):
                if o["status"] not in OPEN:
                    continue
                market = o["market"]
                today = self.calendar.local_date(market, now).isoformat()
                close = self.calendar.session_close(market, self.calendar.local_date(market, now))
                expired = today > o["day"] or (close is not None and now >= close and today == o["day"])
                if self.calendar.is_open(market, now) and not expired:
                    self._match(oid, o, now, provider)
                if o["status"] in OPEN and (o["cancel_requested"] or expired):
                    o["status"] = "CANCELLED"
                    o["message"] = "已撤单" if o["cancel_requested"] else "当日有效单到期，券商撤销剩余数量"
                    self._push(oid, now)
            self._save()

    def _match(self, oid: str, o: dict, now: datetime, provider) -> None:
        q = provider.quote(o["symbol"], now)
        if q is None or q.suspended or q.bid is None or q.ask is None:
            return
        inst = self.state["instruments"][o["symbol"]]
        lot = inst["lot"]
        key = f"{o['symbol']}|{self.calendar.local_date(inst['market'], now).isoformat()}"
        seen = int(self.state["seen_volume"].get(key, 0))
        new_vol = max(0, int(q.volume) - seen)
        self.state["seen_volume"][key] = max(seen, int(q.volume))
        cap = int(Decimal(new_vol) * self.participation) // lot * lot
        remaining = o["qty"] - o["dealt_qty"]
        fill_qty = min(remaining, cap)
        if fill_qty <= 0:
            return
        limit = D(o["price"])
        slip = Decimal(self.slippage_bps) / Decimal(10000)
        if o["side"] == "BUY":
            if q.ask > limit:
                return
            px = min(limit, round_to_tick(inst["market"], q.ask * (1 + slip), "up"))
            fee = calc_fees(self.fee_schedules.get(inst["market"]), "BUY", fill_qty, px) or Decimal(0)
            cash = D(self.state["cash"].get(inst["currency"], "0"))
            if px * fill_qty + fee > cash:
                return
            self.state["cash"][inst["currency"]] = str(cash - px * fill_qty - fee)
            pos = self.state["positions"].setdefault(o["symbol"], {"qty": 0, "cost": "0"})
            pos["cost"] = str(D(pos["cost"]) + px * fill_qty + fee)
            pos["qty"] = int(pos["qty"]) + fill_qty
        else:
            if q.bid < limit:
                return
            px = max(limit, round_to_tick(inst["market"], q.bid * (1 - slip), "down"))
            pos = self.state["positions"].get(o["symbol"], {"qty": 0, "cost": "0"})
            fill_qty = min(fill_qty, int(pos["qty"]))
            if fill_qty <= 0:
                return
            fee = calc_fees(self.fee_schedules.get(inst["market"]), "SELL", fill_qty, px) or Decimal(0)
            cash = D(self.state["cash"].get(inst["currency"], "0"))
            self.state["cash"][inst["currency"]] = str(cash + px * fill_qty - fee)
            avg = D(pos["cost"]) / pos["qty"] if pos["qty"] else Decimal(0)
            pos["qty"] = int(pos["qty"]) - fill_qty
            pos["cost"] = str(avg * pos["qty"])
            if pos["qty"] == 0:
                self.state["positions"].pop(o["symbol"], None)
        o["dealt_qty"] += fill_qty
        o["dealt_value"] = str(D(o["dealt_value"]) + px * fill_qty)
        if o["dealt_qty"] >= o["qty"]:
            o["status"] = "FILLED"
        elif o["status"] != "CANCEL_PENDING":
            o["status"] = "PARTIALLY_FILLED"
        self.state["fill_seq"] += 1
        fid = f"SIMF{self.state['fill_seq']:09d}"
        f = {"id": fid, "order_id": oid, "symbol": o["symbol"], "side": o["side"], "qty": fill_qty,
             "price": str(px), "fee": str(fee), "ts": now.isoformat(), "remark": o["remark"]}
        self.state["fills"].append(f)
        self.events.put(self._fill_event(f))
        self._push(oid, now)

    def _apply_corporate_actions(self, now: datetime, provider) -> None:
        actions = getattr(provider, "all_corporate_actions", lambda: [])()
        for a in actions:
            inst = self.state["instruments"].get(a.symbol)
            if inst is None:
                continue
            key = f"{a.symbol}|{a.ex_date.isoformat()}|{a.kind}"
            if key in self.state["applied_actions"]:
                continue
            open_t = self.calendar.session_open(inst["market"], a.ex_date)
            if open_t is None or now < open_t:
                continue
            pos = self.state["positions"].get(a.symbol)
            if pos and int(pos["qty"]) > 0:
                if a.kind == "SPLIT":
                    pos["qty"] = int(Decimal(pos["qty"]) * a.split_ratio)
                    for o in self.state["orders"].values():
                        if o["symbol"] == a.symbol and o["status"] in OPEN:
                            o["status"] = "CANCELLED"
                            o["message"] = "公司行动（拆股）券商撤销未成交委托"
                            self._push(o["id"], now)
                elif a.kind == "DIVIDEND":
                    cash = D(self.state["cash"].get(inst["currency"], "0"))
                    self.state["cash"][inst["currency"]] = str(cash + a.cash_per_share * int(pos["qty"]))
            self.state["applied_actions"].append(key)

    # ------------------------------------------------------------ 查询
    def _update(self, o: dict, now: datetime | None) -> OrderUpdate:
        avg = (D(o["dealt_value"]) / o["dealt_qty"]) if o["dealt_qty"] else None
        return OrderUpdate(
            broker_order_id=o["id"], status=o["status"], dealt_qty=o["dealt_qty"], dealt_avg_price=avg,
            remark=o["remark"], raw_status=o["status"], message=o.get("message", ""), ts=now,
            symbol=o["symbol"], side=o["side"], qty=o["qty"], price=D(o["price"]),
        )

    @staticmethod
    def _fill_event(f: dict) -> FillEvent:
        return FillEvent(broker_fill_id=f["id"], broker_order_id=f["order_id"], symbol=f["symbol"], side=f["side"],
                         qty=int(f["qty"]), price=D(f["price"]), ts=datetime.fromisoformat(f["ts"]),
                         fee=D(f["fee"]), remark=f.get("remark", ""))

    def _push(self, oid: str, now: datetime | None) -> None:
        self.events.put(self._update(self.state["orders"][oid], now))

    def query_orders(self, now: datetime) -> list[OrderUpdate]:
        with self._lock:
            return [self._update(o, now) for o in self.state["orders"].values()]

    def query_fills(self, now: datetime) -> list[FillEvent]:
        with self._lock:
            return [self._fill_event(f) for f in self.state["fills"]]

    def positions(self) -> list[BrokerPosition]:
        with self._lock:
            out = []
            for sym, p in self.state["positions"].items():
                pending = sum(o["qty"] - o["dealt_qty"] for o in self.state["orders"].values()
                              if o["symbol"] == sym and o["side"] == "SELL" and o["status"] in OPEN)
                q = int(p["qty"])
                out.append(BrokerPosition(symbol=sym, qty=q, can_sell_qty=q - pending,
                                          cost_price=(D(p["cost"]) / q) if q else None))
            return out

    def cash(self) -> list[CashInfo]:
        with self._lock:
            return [CashInfo(currency=c, cash=D(v), withdrawable=D(v), buying_power=D(v), reliable=True,
                             note="本地模拟账户：只有现金，无融资")
                    for c, v in self.state["cash"].items()]

    def health(self) -> AdapterHealth:
        return AdapterHealth(connected=True, detail="本地模拟撮合器运行中")

    # 用于测试：直接修改模拟券商持仓（模拟用户在 App 手动交易）
    def _test_set_position(self, symbol: str, qty: int) -> None:
        with self._lock:
            if qty == 0:
                self.state["positions"].pop(symbol, None)
            else:
                p = self.state["positions"].setdefault(symbol, {"qty": 0, "cost": "0"})
                p["qty"] = qty
            self._save()
