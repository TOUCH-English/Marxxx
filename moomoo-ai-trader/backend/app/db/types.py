"""精确数值与时间类型。

- Money: Decimal <-> 定点整数（×10^6）。SQLite 的 NUMERIC 会退化为浮点，
  所以金额、价格一律以整数存储，避免浮点累计误差。
- UTCDateTime: 统一以 UTC 存储，读出时附带 tzinfo。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.types import TypeDecorator

SCALE = 10**6
_Q = Decimal(1) / SCALE


def D(value) -> Decimal:
    """把任意输入转换成 Decimal（不经过浮点）。"""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # 浮点只可能来自外部 SDK；用 repr 字符串转换避免二进制误差扩散
        return Decimal(repr(value))
    return Decimal(str(value))


class Money(TypeDecorator):
    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        d = D(value).quantize(_Q, rounding=ROUND_HALF_EVEN)
        return int(d * SCALE)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return (Decimal(int(value)) / SCALE).quantize(_Q)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
