"""执行适配器接口。

本地模拟器、伪券商（测试）与 moomoo 适配器都实现同一接口，
因此模拟与实盘走同一条「决策 → 风控 → 订单」链路，只有执行适配器不同。
"""
from __future__ import annotations

import queue
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass
class PlaceResult:
    status: str  # ACCEPTED / REJECTED / UNKNOWN
    broker_order_id: str | None = None
    message: str = ""


@dataclass
class CancelResult:
    status: str  # REQUESTED / REJECTED / UNKNOWN
    message: str = ""


@dataclass
class OrderUpdate:
    """券商订单状态推送 / 查询结果（已归一化）。"""

    broker_order_id: str
    status: str  # SUBMITTED / PARTIALLY_FILLED / FILLED / CANCEL_PENDING / CANCELLED / REJECTED / UNKNOWN
    dealt_qty: int
    dealt_avg_price: Decimal | None = None
    remark: str = ""
    raw_status: str = ""
    message: str = ""
    ts: datetime | None = None
    symbol: str = ""
    side: str = ""
    qty: int = 0
    price: Decimal | None = None


@dataclass
class FillEvent:
    broker_fill_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: int
    price: Decimal
    ts: datetime
    fee: Decimal | None = None  # 券商未提供时由费用表估算
    remark: str = ""


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    can_sell_qty: int
    cost_price: Decimal | None = None


@dataclass
class CashInfo:
    currency: str
    cash: Decimal | None  # 可用且可结算现金（不含融资）
    withdrawable: Decimal | None = None
    buying_power: Decimal | None = None  # 仅展示，不用于风控
    reliable: bool = False  # 能否可靠区分现金与借贷能力
    note: str = ""


@dataclass
class AdapterHealth:
    connected: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


class ExecutionAdapter(Protocol):
    broker: str  # LOCAL_SIM / FAKE / MOOMOO
    trd_env: str  # LOCAL / SIMULATE / REAL
    acc_id: str
    events: "queue.Queue[OrderUpdate | FillEvent]"

    def place_order(self, *, symbol: str, side: str, qty: int, limit_price: Decimal, remark: str,
                    now: datetime) -> PlaceResult: ...

    def cancel_order(self, broker_order_id: str, now: datetime) -> CancelResult: ...

    def query_orders(self, now: datetime) -> list[OrderUpdate]: ...

    def query_fills(self, now: datetime) -> list[FillEvent]: ...

    def positions(self) -> list[BrokerPosition]: ...

    def cash(self) -> list[CashInfo]: ...

    def health(self) -> AdapterHealth: ...

    def on_market_tick(self, now: datetime, provider) -> None:
        """本地模拟器在此撮合；真实券商适配器为空操作。"""
        ...
