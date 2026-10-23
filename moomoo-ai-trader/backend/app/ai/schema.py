"""AI 结构化提案（Pydantic 校验）。模型只能输出这个结构，不能调用任何交易工具。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Action = Literal["BUY_PROPOSAL", "SELL_PROPOSAL", "HOLD", "NO_TRADE"]
EntryType = Literal["IMMEDIATE", "LIMIT_AT_OR_BELOW", "BREAKOUT_ABOVE", "NONE"]


def _dec(v):
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"不是有效数字：{v}") from e
    if not d.is_finite():
        raise ValueError("数字无效")
    return d


class RawDecision(BaseModel):
    """模型输出的单条决定（价格以字符串表示，避免浮点）。"""

    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(max_length=32)
    action: Action
    rationale: str = Field(max_length=1200)
    evidence_ids: list[str] = Field(max_length=30)
    risk_flags: list[str] = Field(max_length=20)
    entry_type: EntryType
    entry_price: Decimal | None
    max_buy_price: Decimal | None
    target_position_pct: Decimal | None
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    max_holding_days: int | None
    sell_fraction: Decimal | None
    min_sell_price: Decimal | None
    valid_minutes: int = Field(ge=1, le=24 * 60)

    _v = field_validator("entry_price", "max_buy_price", "target_position_pct", "stop_loss_price",
                         "take_profit_price", "sell_fraction", "min_sell_price", mode="before")(_dec)


class RawBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of_utc: str
    strategy_version: str
    market_view: str = Field(max_length=2000)
    decisions: list[RawDecision] = Field(max_length=40)


class AIProposal(BaseModel):
    """内部提案：在 RawDecision 基础上补齐编号、时间、版本（规格 §6 的结构）。"""

    decision_id: str
    symbol: str
    action: Action
    as_of_utc: datetime
    expires_at_utc: datetime
    evidence_ids: list[str]
    rationale: str
    risk_flags: list[str]
    entry_condition: dict | None
    exit_condition: dict | None
    target_position_pct: Decimal | None = None
    max_buy_price: Decimal | None = None
    sell_fraction: Decimal | None = None
    min_sell_price: Decimal | None = None
    strategy_version: str


_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}

OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "as_of_utc": {"type": "string"},
        "strategy_version": {"type": "string"},
        "market_view": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["BUY_PROPOSAL", "SELL_PROPOSAL", "HOLD", "NO_TRADE"]},
                    "rationale": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "risk_flags": {"type": "array", "items": {"type": "string"}},
                    "entry_type": {"type": "string", "enum": ["IMMEDIATE", "LIMIT_AT_OR_BELOW", "BREAKOUT_ABOVE", "NONE"]},
                    "entry_price": _NULLABLE_STR,
                    "max_buy_price": _NULLABLE_STR,
                    "target_position_pct": _NULLABLE_STR,
                    "stop_loss_price": _NULLABLE_STR,
                    "take_profit_price": _NULLABLE_STR,
                    "max_holding_days": _NULLABLE_INT,
                    "sell_fraction": _NULLABLE_STR,
                    "min_sell_price": _NULLABLE_STR,
                    "valid_minutes": {"type": "integer"},
                },
                "required": ["symbol", "action", "rationale", "evidence_ids", "risk_flags", "entry_type",
                             "entry_price", "max_buy_price", "target_position_pct", "stop_loss_price",
                             "take_profit_price", "max_holding_days", "sell_fraction", "min_sell_price",
                             "valid_minutes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["as_of_utc", "strategy_version", "market_view", "decisions"],
    "additionalProperties": False,
}
