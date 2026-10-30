from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    market: str
    name: str
    sec_type: str  # STOCK / ETF
    currency: str
    lot_size: int
    is_demo: bool = False
    is_leveraged: bool = False
    spread_bps: int = 20  # 仅回放用


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    volume: int
    prev_close: Decimal | None
    source: str  # DEMO_FIXTURE / HISTORICAL_REPLAY / MOOMOO_OPEND
    is_realtime: bool
    source_ts: datetime | None
    recv_ts: datetime
    suspended: bool = False
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    day_open: Decimal | None = None
    note: str = ""
    # 盘前 / 盘后成交价（moomoo 快照另给的字段）。last 在休市时通常是上一个常规时段的收盘价
    pre_price: Decimal | None = None
    after_price: Decimal | None = None


@dataclass(frozen=True)
class Bar:
    symbol: str
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adj_factor: Decimal = Decimal(1)  # 前复权因子：adj_close = close * adj_factor
    suspended: bool = False

    @property
    def adj_close(self) -> Decimal:
        return self.close * self.adj_factor


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    kind: str  # DIVIDEND / SPLIT
    cash_per_share: Decimal = Decimal(0)
    split_ratio: Decimal = Decimal(1)  # 2 表示 1 拆 2
    note: str = ""


@dataclass
class ProviderStatus:
    name: str
    connected: bool
    is_realtime: bool
    label: str
    detail: str = ""
    extra: dict = field(default_factory=dict)


class MarketDataProvider(Protocol):
    name: str
    is_realtime: bool

    def status(self) -> ProviderStatus: ...

    def instruments(self) -> list[InstrumentInfo]: ...

    def instrument(self, symbol: str) -> InstrumentInfo | None: ...

    def quote(self, symbol: str, now: datetime) -> Quote | None: ...

    def daily_bars(self, symbol: str, now: datetime, n: int) -> list[Bar]:
        """只返回在 now 之前已经完整收盘的日 K。"""
        ...

    def corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]: ...
