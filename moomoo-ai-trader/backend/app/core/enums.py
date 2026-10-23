from __future__ import annotations

from enum import StrEnum


class Namespace(StrEnum):
    SIM = "sim"    # DEMO / LOCAL_PAPER / BROKER_PAPER
    LIVE = "live"  # LIVE_REVIEW / LIVE_AUTO


class Mode(StrEnum):
    DEMO = "DEMO"
    LOCAL_PAPER = "LOCAL_PAPER"
    BROKER_PAPER = "BROKER_PAPER"
    LIVE_REVIEW = "LIVE_REVIEW"
    LIVE_AUTO = "LIVE_AUTO"

    @property
    def namespace(self) -> Namespace:
        return Namespace.LIVE if self in (Mode.LIVE_REVIEW, Mode.LIVE_AUTO) else Namespace.SIM

    @property
    def is_live(self) -> bool:
        return self.namespace == Namespace.LIVE

    @property
    def label(self) -> str:
        return MODE_LABELS[self]


MODE_LABELS = {
    Mode.DEMO: "演示（固定数据·本地模拟）",
    Mode.LOCAL_PAPER: "本地纸上交易（本地模拟成交）",
    Mode.BROKER_PAPER: "券商模拟账户",
    Mode.LIVE_REVIEW: "实盘·逐笔确认",
    Mode.LIVE_AUTO: "实盘·自动交易",
}


class Market(StrEnum):
    MY = "MY"
    US = "US"


MARKET_CURRENCY = {Market.MY: "MYR", Market.US: "USD"}


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATUSES = {
    OrderStatus.RISK_REJECTED,
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
}
OPEN_ORDER_STATUSES = {
    OrderStatus.CREATED,
    OrderStatus.AWAITING_APPROVAL,
    OrderStatus.SUBMITTING,
    OrderStatus.ACKNOWLEDGED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCEL_PENDING,
    OrderStatus.UNKNOWN,
}

ORDER_STATUS_LABELS = {
    OrderStatus.CREATED: "已建立",
    OrderStatus.RISK_REJECTED: "被风控拒绝",
    OrderStatus.AWAITING_APPROVAL: "待确认",
    OrderStatus.SUBMITTING: "提交中",
    OrderStatus.ACKNOWLEDGED: "已提交（券商已接收）",
    OrderStatus.PARTIALLY_FILLED: "部分成交",
    OrderStatus.FILLED: "全部成交",
    OrderStatus.CANCEL_PENDING: "撤单中",
    OrderStatus.CANCELLED: "已撤销",
    OrderStatus.REJECTED: "被拒绝",
    OrderStatus.UNKNOWN: "状态未知（需核对）",
}


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class IntentReason(StrEnum):
    ENTRY = "ENTRY"
    EXIT_STOP = "EXIT_STOP"
    EXIT_TAKE_PROFIT = "EXIT_TAKE_PROFIT"
    EXIT_TIME = "EXIT_TIME"
    EXIT_AI = "EXIT_AI"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    MANUAL_LIQUIDATE = "MANUAL_LIQUIDATE"


PROTECTIVE_REASONS = {
    IntentReason.EXIT_STOP,
    IntentReason.EXIT_TAKE_PROFIT,
    IntentReason.EXIT_TIME,
}


class DecisionAction(StrEnum):
    BUY_PROPOSAL = "BUY_PROPOSAL"
    SELL_PROPOSAL = "SELL_PROPOSAL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


class DecisionOutcome(StrEnum):
    AUTO_EXECUTED = "AUTO_EXECUTED"      # 已自动执行（已生成订单并提交）
    WAITING_PRICE = "WAITING_PRICE"      # 等待价格条件
    RISK_BLOCKED = "RISK_BLOCKED"        # 被风控拦截
    NO_ACTION = "NO_ACTION"              # 无需交易
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # LIVE_REVIEW 专用
    INVALID = "INVALID"                  # 输出无效/过期/超时，不产生订单
    EXPIRED = "EXPIRED"


DECISION_OUTCOME_LABELS = {
    DecisionOutcome.AUTO_EXECUTED: "已自动执行",
    DecisionOutcome.WAITING_PRICE: "等待价格条件",
    DecisionOutcome.RISK_BLOCKED: "被风控拦截",
    DecisionOutcome.NO_ACTION: "无需交易",
    DecisionOutcome.AWAITING_APPROVAL: "待确认",
    DecisionOutcome.INVALID: "无效（未产生订单）",
    DecisionOutcome.EXPIRED: "已过期（未产生订单）",
}


class LockKind(StrEnum):
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    RECONCILE = "RECONCILE"
    MANUAL = "MANUAL"
    HEALTH = "HEALTH"


class CandidateStatus(StrEnum):
    PROPOSED = "PROPOSED"
    SIMULATING = "SIMULATING"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECTED = "REJECTED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
