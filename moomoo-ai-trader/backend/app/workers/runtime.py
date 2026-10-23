"""每个运行中的策略账本（Portfolio）的运行时对象：行情、时钟、执行适配器、订单管理器。"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.enums import Mode
from ..market_data.base import Quote
from ..orders.manager import OrderManager


@dataclass
class PendingAIJob:
    future: Future
    market: str
    as_of: datetime
    payload: dict
    ctx_meta: dict


@dataclass
class Runtime:
    ns: str
    portfolio_id: int
    mode: Mode
    provider: Any
    clock: Any
    adapter: Any
    om: OrderManager
    calendar: Any
    fee_schedules: dict[str, dict]
    data_source: str
    account_id: str
    ai_provider: Any
    executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai"))
    last_reconcile_wall: datetime | None = None
    last_ai_batch: dict[str, datetime] = field(default_factory=dict)
    last_ai_prices: dict[str, dict] = field(default_factory=dict)
    ai_jobs: list[PendingAIJob] = field(default_factory=list)
    quotes: dict[str, Quote | None] = field(default_factory=dict)
    last_quote_store: datetime | None = None
    last_baseline_eval: dict[str, str] = field(default_factory=dict)
    adapter_instruments: set[str] = field(default_factory=set)  # 已登记到执行通道（本地模拟）的证券
    pool_info: dict[str, dict] = field(default_factory=dict)  # 当前股票池成员的筛选数据（市值、涨跌幅等）
    pool_retry: dict[int, datetime] = field(default_factory=dict)  # 规则 → 股票池更新失败后的下次重试时间
    started: bool = False

    def now(self) -> datetime:
        return self.clock.now()

    @property
    def is_replay(self) -> bool:
        return bool(getattr(self.clock, "is_replay", False))

    def quote(self, symbol: str) -> Quote | None:
        if symbol not in self.quotes:
            try:
                self.quotes[symbol] = self.provider.quote(symbol, self.now())
            except Exception:
                self.quotes[symbol] = None
        return self.quotes[symbol]

    def price_of(self, symbol: str) -> Decimal | None:
        q = self.quote(symbol)
        return q.last if q is not None and q.last is not None else None

    def reset_tick(self) -> None:
        self.quotes.clear()
