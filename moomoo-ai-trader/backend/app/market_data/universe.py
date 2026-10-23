"""授权股票池：由确定性规则 + 可信数据产生成员并留存快照。

AI 只能在快照成员内选择；规则本身由用户批准，AI 无法修改或扩大。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import MarketDataProvider

DEFAULT_POOL_RULES: dict[str, Any] = {
    "markets": ["MY", "US"],
    "sec_types": ["STOCK", "ETF"],
    "exclude_leveraged": True,
    "min_price": {"MY": "0.50", "US": "5"},
    "max_price": {"MY": "100", "US": "1000"},
    "min_avg_turnover_20d": {"MY": "1000000", "US": "20000000"},
    "exclude_symbols": [],
    "max_members": 20,
}

RULE_LABELS = {
    "markets": "允许市场",
    "sec_types": "证券类型",
    "exclude_leveraged": "排除杠杆/反向产品",
    "min_price": "最低价格",
    "max_price": "最高价格",
    "min_avg_turnover_20d": "20日平均成交额下限",
    "exclude_symbols": "手动排除",
    "max_members": "成员上限",
}


def screen(provider: MarketDataProvider, rules: dict[str, Any], now: datetime) -> tuple[list[dict], list[dict]]:
    from ..db.types import D

    members: list[dict] = []
    excluded: list[dict] = []
    for inst in provider.instruments():
        reasons = []
        if inst.market not in rules.get("markets", []):
            reasons.append("市场不在范围内")
        if inst.sec_type not in rules.get("sec_types", []):
            reasons.append("证券类型不允许")
        if rules.get("exclude_leveraged", True) and inst.is_leveraged:
            reasons.append("杠杆/反向产品已排除")
        if inst.symbol in rules.get("exclude_symbols", []):
            reasons.append("用户手动排除")
        bars = provider.daily_bars(inst.symbol, now, 20)
        turnover = None
        last = None
        if len(bars) < 20:
            reasons.append("历史数据不足 20 日")
        else:
            last = bars[-1].close
            turnover = sum(b.close * b.volume for b in bars) / 20
            mn = rules.get("min_price", {}).get(inst.market)
            mx = rules.get("max_price", {}).get(inst.market)
            if mn is not None and last < D(mn):
                reasons.append(f"价格低于 {mn}")
            if mx is not None and last > D(mx):
                reasons.append(f"价格高于 {mx}")
            mt = rules.get("min_avg_turnover_20d", {}).get(inst.market)
            if mt is not None and turnover < D(mt):
                reasons.append("流动性不足")
        row = {
            "symbol": inst.symbol, "name": inst.name, "market": inst.market, "sec_type": inst.sec_type,
            "last_close": str(last) if last is not None else None,
            "avg_turnover_20d": str(round(turnover, 0)) if turnover is not None else None,
        }
        if reasons:
            excluded.append(row | {"reasons": reasons})
        else:
            members.append(row)
    members.sort(key=lambda r: D(r["avg_turnover_20d"] or 0), reverse=True)
    cap = int(rules.get("max_members", 20))
    for extra in members[cap:]:
        excluded.append(extra | {"reasons": ["超过成员上限（按流动性排序）"]})
    return members[:cap], excluded
