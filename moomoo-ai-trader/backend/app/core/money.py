"""价格档位、交易单位与费用计算（全部使用 Decimal）。"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from ..db.types import D

CENT = Decimal("0.01")


def tick_size(market: str, price: Decimal) -> Decimal:
    """最小价格变动单位。

    马股（Bursa Malaysia）：<1.00 为 0.005；1.00–9.99 为 0.01；10.00–99.98 为 0.02；≥100 为 0.10。
    美股：≥1.00 为 0.01；<1.00 为 0.0001。
    数值需与券商/交易所当前规则核对，见 docs/capabilities.md。
    """
    p = D(price)
    if market == "MY":
        if p < 1:
            return Decimal("0.005")
        if p < 10:
            return Decimal("0.01")
        if p < 100:
            return Decimal("0.02")
        return Decimal("0.10")
    if market == "US":
        return Decimal("0.01") if p >= 1 else Decimal("0.0001")
    raise ValueError(f"unsupported market {market}")


def round_to_tick(market: str, price: Decimal, direction: str) -> Decimal:
    """direction='down' 用于买入限价（不超过上限）；'up' 用于卖出限价（不低于下限）。"""
    p = D(price)
    t = tick_size(market, p)
    n = (p / t).to_integral_value(rounding=ROUND_FLOOR if direction == "down" else ROUND_CEILING)
    out = n * t
    # 跨越档位边界时重新校验
    t2 = tick_size(market, out)
    if t2 != t:
        n = (out / t2).to_integral_value(rounding=ROUND_FLOOR if direction == "down" else ROUND_CEILING)
        out = n * t2
    return out.normalize() if out != 0 else Decimal(0)


def is_valid_tick(market: str, price: Decimal) -> bool:
    p = D(price)
    t = tick_size(market, p)
    return (p / t) == (p / t).to_integral_value()


def round_lot_down(qty: int, lot: int) -> int:
    if lot <= 0:
        raise ValueError("lot must be positive")
    return (int(qty) // lot) * lot


def calc_fees(schedule: dict[str, Any] | None, side: str, qty: int, price: Decimal) -> Decimal | None:
    """按费用表计算单笔费用。schedule 缺失时返回 None（调用方应拒绝生成买单）。"""
    if not schedule or not schedule.get("items"):
        return None
    notional = D(price) * qty
    total = Decimal(0)
    for it in schedule["items"]:
        if it.get("side", "BOTH") not in ("BOTH", side):
            continue
        if "rate" in it:
            amt = notional * D(it["rate"])
        elif "per_share" in it:
            amt = D(it["per_share"]) * qty
        elif "fixed" in it:
            amt = D(it["fixed"])
        else:
            continue
        if it.get("min") is not None:
            amt = max(amt, D(it["min"]))
        if it.get("max") is not None:
            amt = min(amt, D(it["max"]))
        if it.get("max_rate_of_value") is not None:
            amt = min(amt, notional * D(it["max_rate_of_value"]))
        total += amt.quantize(CENT, rounding=ROUND_HALF_UP)
    return total


def q2(x: Decimal) -> Decimal:
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_int(x: Decimal) -> int:
    return int(D(x).to_integral_value(rounding=ROUND_DOWN))


# 测试费用表（非官方费率！仅用于模拟；实盘前请按 moomoo 官网当前收费核对并修改）
TEST_FEE_SCHEDULES: dict[str, dict[str, Any]] = {
    "MY": {
        "label": "马股测试费用（非官方，需核对）",
        "items": [
            {"name": "佣金（测试值）", "rate": "0.0008", "min": "3.00", "side": "BOTH"},
            {"name": "清算费（测试值）", "rate": "0.0003", "max": "1000", "side": "BOTH"},
            {"name": "印花税（测试值）", "rate": "0.001", "max": "1000", "side": "BOTH"},
        ],
    },
    "US": {
        "label": "美股测试费用（非官方，需核对）",
        "items": [
            {"name": "佣金（测试值）", "per_share": "0.0049", "min": "0.99", "max_rate_of_value": "0.005", "side": "BOTH"},
            {"name": "平台费（测试值）", "per_share": "0.005", "min": "1.00", "max_rate_of_value": "0.005", "side": "BOTH"},
            {"name": "卖出监管费（测试值）", "rate": "0.0000278", "min": "0.01", "side": "SELL"},
        ],
    },
}
