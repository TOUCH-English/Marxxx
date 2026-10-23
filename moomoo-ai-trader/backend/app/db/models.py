"""数据库实体。

每个命名空间（sim / live）各有一个独立的 SQLite 文件，表结构相同，
因此模拟与实盘数据在物理上分离，不会混合。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import Money, UTCDateTime, utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _now_col():
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)


MODES_SQL = "('DEMO','LOCAL_PAPER','BROKER_PAPER','LIVE_REVIEW','LIVE_AUTO')"


# ---------------------------------------------------------------- 元信息
class DbMeta(Base):
    """记录本数据库所属命名空间；打开时校验，防止 sim/live 文件被调换。"""

    __tablename__ = "db_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))


# ---------------------------------------------------------------- 账户与能力
class AccountBinding(Base):
    __tablename__ = "account_binding"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(16))  # LOCAL_SIM / MOOMOO
    trd_env: Mapped[str] = mapped_column(String(16))  # LOCAL / SIMULATE / REAL
    acc_id: Mapped[str] = mapped_column(String(64))
    acc_id_masked: Mapped[str] = mapped_column(String(64))
    security_firm: Mapped[str] = mapped_column(String(32), default="")
    acc_type: Mapped[str] = mapped_column(String(32), default="")
    markets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_utc: Mapped[datetime] = _now_col()


class MarketCapability(Base):
    __tablename__ = "market_capability"
    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(8))
    item: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(256), default="")
    checked_at: Mapped[datetime] = _now_col()
    __table_args__ = (UniqueConstraint("market", "item", name="uq_capability"),)


# ---------------------------------------------------------------- 证券与行情
class Instrument(Base):
    __tablename__ = "instrument"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)  # 如 US.AAPL / MY.1155
    market: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(128))
    sec_type: Mapped[str] = mapped_column(String(16))  # STOCK / ETF
    currency: Mapped[str] = mapped_column(String(8))
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_leveraged: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = _now_col()


class QuoteSnapshot(Base):
    __tablename__ = "quote_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    last: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32))
    is_realtime: Mapped[bool] = mapped_column(Boolean, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    source_ts: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    recv_ts: Mapped[datetime] = _now_col()


class Watchlist(Base):
    __tablename__ = "watchlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    in_whitelist: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(256), default="")
    added_at: Mapped[datetime] = _now_col()


class UniverseRule(Base):
    """授权股票池规则（版本化）。规则由用户批准，AI 无法修改。"""

    __tablename__ = "universe_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at_utc: Mapped[datetime] = _now_col()
    note: Mapped[str] = mapped_column(Text, default="")


class UniverseSnapshot(Base):
    __tablename__ = "universe_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("universe_rule.id"))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    members: Mapped[list[Any]] = mapped_column(JSON)
    excluded: Mapped[list[Any]] = mapped_column(JSON, default=list)
    data_source: Mapped[str] = mapped_column(String(64))
    created_at_utc: Mapped[datetime] = _now_col()


class FxRate(Base):
    __tablename__ = "fx_rate"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair: Mapped[str] = mapped_column(String(16))  # USDMYR
    rate: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(128))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at_utc: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------- 策略、风控、授权
class StrategyVersion(Base):
    __tablename__ = "strategy_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32))  # AI_AUTONOMOUS / BASELINE_MA
    version: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE / INACTIVE / CANDIDATE
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = _now_col()
    __table_args__ = (UniqueConstraint("key", "version", name="uq_strategy_version"),)


class RiskPolicyVersion(Base):
    __tablename__ = "risk_policy_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    is_test_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = _now_col()


class FeeSchedule(Base):
    __tablename__ = "fee_schedule"
    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(8))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(256))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    is_test: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_utc: Mapped[datetime] = _now_col()


class Portfolio(Base):
    """系统管理的策略子账本（与券商账户总资产区分）。"""

    __tablename__ = "portfolio"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    account_binding_id: Mapped[int] = mapped_column(ForeignKey("account_binding.id"))
    name: Mapped[str] = mapped_column(String(64))
    sim_clock: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    data_source: Mapped[str] = mapped_column(String(32), default="DEMO_FIXTURE")
    pause_new_entries: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at_utc: Mapped[datetime] = _now_col()
    __table_args__ = (CheckConstraint(f"mode IN {MODES_SQL}", name="ck_portfolio_mode"),)


class Authorization(Base):
    """运行授权：一次确认后，授权范围内的自动买卖不再逐笔确认。"""

    __tablename__ = "authorization"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"))
    mode: Mapped[str] = mapped_column(String(16))
    account_binding_id: Mapped[int] = mapped_column(Integer)
    capital: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"MYR": "20000"}
    max_planned_loss: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"MYR": "1000"}
    scope: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"type": "POOL", "universe_rule_id": 1} / WHITELIST
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"buy": true, "sell": true, "cancel": true, "reprice": true}
    strategy_version_ids: Mapped[list[Any]] = mapped_column(JSON)
    risk_policy_version_id: Mapped[int] = mapped_column(Integer)
    overnight: Mapped[bool] = mapped_column(Boolean, default=True)
    exit_rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE / REVOKED / EXPIRED / SUPERSEDED
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(String(128), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="user")
    created_at_utc: Mapped[datetime] = _now_col()
    __table_args__ = (CheckConstraint(f"mode IN {MODES_SQL}", name="ck_auth_mode"),)


# ---------------------------------------------------------------- 决策
class Decision(Base):
    __tablename__ = "decision"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), index=True)
    authorization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_version_id: Mapped[int] = mapped_column(Integer)
    strategy_version: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))  # AI_MODEL / AI_FIXTURE / BASELINE / PROTECTION
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(8), default="")
    currency: Mapped[str] = mapped_column(String(8), default="")
    action: Mapped[str] = mapped_column(String(16))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rule_calc: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_judgment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    ai_call_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(24))
    outcome_reason: Mapped[str] = mapped_column(Text, default="")
    data_source: Mapped[str] = mapped_column(String(32), default="")
    created_at_utc: Mapped[datetime] = _now_col()


class AICallLog(Base):
    __tablename__ = "ai_call_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32), default="DECISION")
    input_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24))  # OK / TIMEOUT / ERROR / INVALID / BUDGET_EXCEEDED / DISABLED
    error: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at_utc: Mapped[datetime] = _now_col()


class NewsItem(Base):
    __tablename__ = "news_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)


# ---------------------------------------------------------------- 订单
class OrderIntent(Base):
    __tablename__ = "order_intent"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_key: Mapped[str] = mapped_column(String(160), unique=True)  # 去重键
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), index=True)
    authorization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(64), default="")
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(64), default="")
    risk_policy_version: Mapped[int] = mapped_column(Integer, default=0)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(8))
    currency: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(24))
    qty: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(24), index=True)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    est_fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    ref_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_hold_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    remark: Mapped[str] = mapped_column(String(64), default="")
    risk_check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False)
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at_utc: Mapped[datetime] = _now_col()
    updated_at_utc: Mapped[datetime] = _now_col()
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_intent_qty"),
        CheckConstraint("filled_qty >= 0 AND filled_qty <= qty", name="ck_intent_filled"),
        CheckConstraint(f"mode IN {MODES_SQL}", name="ck_intent_mode"),
    )


class BrokerOrder(Base):
    __tablename__ = "broker_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intent.id"), unique=True)
    broker: Mapped[str] = mapped_column(String(16))
    trd_env: Mapped[str] = mapped_column(String(16))
    broker_order_id: Mapped[str] = mapped_column(String(64))
    raw_status: Mapped[str] = mapped_column(String(32), default="")
    dealt_qty: Mapped[int] = mapped_column(Integer, default=0)
    last_update: Mapped[datetime] = _now_col()
    __table_args__ = (UniqueConstraint("broker", "trd_env", "broker_order_id", name="uq_broker_order"),)


class Fill(Base):
    __tablename__ = "fill"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intent.id"), index=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    broker: Mapped[str] = mapped_column(String(16))
    broker_fill_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fee_estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    currency: Mapped[str] = mapped_column(String(8))
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at_utc: Mapped[datetime] = _now_col()
    __table_args__ = (
        UniqueConstraint("broker", "broker_fill_id", name="uq_fill_broker_id"),
        CheckConstraint("qty > 0", name="ck_fill_qty"),
    )


class OrderEvent(Base):
    __tablename__ = "order_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(36), index=True)
    event: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str] = mapped_column(String(24), default="")
    to_status: Mapped[str] = mapped_column(String(24), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_utc: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------- 账本
class CashLedger(Base):
    __tablename__ = "cash_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(24))  # ALLOCATION / BUY / SELL / FEE / FEE_ADJUST / DIVIDEND
    ref_type: Mapped[str] = mapped_column(String(16), default="")
    ref_id: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at_utc: Mapped[datetime] = _now_col()


class Position(Base):
    __tablename__ = "position"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(8))
    currency: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fees_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN / CLOSED
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    entry_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(64), default="")
    # 保护规则：随持仓保存，策略升级不会改变已有持仓的保护
    stop_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_hold_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    protection_version: Mapped[str] = mapped_column(String(64), default="")
    entry_ref_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_qty: Mapped[int] = mapped_column(Integer, default=0)
    created_at_utc: Mapped[datetime] = _now_col()
    __table_args__ = (
        CheckConstraint("qty >= 0", name="ck_position_qty_nonneg"),
        Index(
            "uq_open_position",
            "portfolio_id",
            "symbol",
            unique=True,
            sqlite_where=text("status = 'OPEN'"),
        ),
    )


class PositionLedger(Base):
    __tablename__ = "position_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id"))
    fill_id: Mapped[int] = mapped_column(ForeignKey("fill.id"), unique=True)
    qty_delta: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at_utc: Mapped[datetime] = _now_col()


class RiskReservation(Base):
    __tablename__ = "risk_reservation"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intent.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    cash_amount: Mapped[Decimal] = mapped_column(Money)  # 剩余预留（未成交部分 + 预估费用）
    risk_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    takes_slot: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE / RELEASED / CONSUMED
    trading_day: Mapped[str] = mapped_column(String(10))  # 计入当日新开仓名额（马来西亚日期）
    created_at_utc: Mapped[datetime] = _now_col()
    updated_at_utc: Mapped[datetime] = _now_col()


class DailyEquity(Base):
    __tablename__ = "daily_equity"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    day_myt: Mapped[str] = mapped_column(String(10))
    start_equity: Mapped[Decimal] = mapped_column(Money)
    last_equity: Mapped[Decimal] = mapped_column(Money)
    min_equity: Mapped[Decimal] = mapped_column(Money)
    flows: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    hwm_adjusted: Mapped[Decimal] = mapped_column(Money)  # 现金流调整后的净值高点
    fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("portfolio_id", "currency", "day_myt", name="uq_daily_equity"),)


class RiskLock(Base):
    __tablename__ = "risk_lock"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(24))
    scope: Mapped[str] = mapped_column(String(32), default="ALL")  # ALL 或 证券代码
    reason: Mapped[str] = mapped_column(Text)
    release_not_before: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    released_by: Mapped[str] = mapped_column(String(32), default="")
    release_note: Mapped[str] = mapped_column(Text, default="")
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at_utc: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------- 运维
class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(24), index=True)
    action: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(24))
    portfolio_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), default="", index=True)
    decision_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    intent_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at_utc: Mapped[datetime] = _now_col()


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))  # info / warning / error
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_at_utc: Mapped[datetime] = _now_col()


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    pid: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Command(Base):
    __tablename__ = "command"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    result: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="user")
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_at_utc: Mapped[datetime] = _now_col()
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ConfigDraft(Base):
    __tablename__ = "config_draft"
    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(32))  # risk / universe / strategy
    request_text: Mapped[str] = mapped_column(Text, default="")
    before: Mapped[dict[str, Any]] = mapped_column(JSON)
    after: Mapped[dict[str, Any]] = mapped_column(JSON)
    diff: Mapped[list[Any]] = mapped_column(JSON)
    loosens_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_by: Mapped[str] = mapped_column(String(32), default="user")
    created_at_utc: Mapped[datetime] = _now_col()
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class BacktestRun(Base):
    __tablename__ = "backtest_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))  # BACKTEST / OUT_OF_SAMPLE / FORWARD
    strategy_key: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    trades: Mapped[list[Any]] = mapped_column(JSON, default=list)
    equity_curve: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="DONE")
    created_at_utc: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------- 经验与改进
class ReviewRun(Base):
    __tablename__ = "review_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # DAILY / WEEKLY
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    portfolio_id: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(UTCDateTime)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    summary: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_source: Mapped[str] = mapped_column(String(32), default="")
    created_at_utc: Mapped[datetime] = _now_col()


class ExperienceRecord(Base):
    __tablename__ = "experience_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    data_source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(24))  # CLOSED_TRADE / RISK_REJECTED / NO_TRADE / EXECUTION_FAILURE
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    decision_id: Mapped[str] = mapped_column(String(36), default="")
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(64), default="")
    outcome_class: Mapped[str] = mapped_column(String(32), default="")  # WIN / LOSS / SIGNAL_ERROR / EXECUTION_FAULT / COST_IMPACT / ...
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    slippage: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    holding_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)  # 经验仅作假设参考
    created_at_utc: Mapped[datetime] = _now_col()


class ImprovementCandidate(Base):
    __tablename__ = "improvement_candidate"
    id: Mapped[int] = mapped_column(primary_key=True)
    review_run_id: Mapped[int] = mapped_column(Integer)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    base_strategy_version_id: Mapped[int] = mapped_column(Integer)
    candidate_strategy_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expected_benefit: Mapped[str] = mapped_column(Text, default="")
    possible_cost: Mapped[str] = mapped_column(Text, default="")
    data_cutoff: Mapped[datetime] = mapped_column(UTCDateTime)
    eval_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 预先冻结的评价指标与门槛
    status: Mapped[str] = mapped_column(String(24))
    status_reason: Mapped[str] = mapped_column(Text, default="")
    proposed_by: Mapped[str] = mapped_column(String(24), default="RULES")
    created_at_utc: Mapped[datetime] = _now_col()
    updated_at_utc: Mapped[datetime] = _now_col()


class EvaluationRun(Base):
    __tablename__ = "evaluation_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("improvement_candidate.id"))
    window_start: Mapped[datetime] = mapped_column(UTCDateTime)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime)
    baseline_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verdict: Mapped[str] = mapped_column(String(24), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = _now_col()


class DeploymentHistory(Base):
    __tablename__ = "deployment_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(32))
    from_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_version_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))  # ACTIVATE / ROLLBACK
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(24))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = _now_col()

