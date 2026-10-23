"""授权股票池：由确定性规则 + 可信数据产生成员并留存快照。

AI 只能在快照成员内选择；规则本身由用户批准，AI 无法修改或扩大。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import MarketDataProvider

DEFAULT_POOL_RULES: dict[str, Any] = {
    "source": "WATCHLIST",  # WATCHLIST：只筛本机证券表（自选）；MOOMOO_SCAN：每天用 moomoo 条件选股扫描全部美股
    "markets": ["MY", "US"],
    "sec_types": ["STOCK", "ETF"],
    "exclude_leveraged": True,
    "min_price": {"MY": "0.50", "US": "5"},
    "max_price": {"MY": "100", "US": "1000"},
    "min_avg_turnover_20d": {"MY": "1000000", "US": "20000000"},
    "exclude_symbols": [],
    "max_members": 20,
    "min_market_cap_usd": None,  # 仅 MOOMOO_SCAN 使用
    "scan_top_by_turnover": 100,  # 仅 MOOMOO_SCAN 使用：按 20 日成交额取前 N 只再排序
}

# 「每日自动股票池」预设：全部美股 → 大市值、成交活跃、价格买得起的普通股 → 按近期强弱取前 20 只
AUTO_US_POOL_RULES: dict[str, Any] = DEFAULT_POOL_RULES | {
    "source": "MOOMOO_SCAN",
    "markets": ["US"],
    "sec_types": ["STOCK"],
    "min_price": {"US": "10"},
    "max_price": {"US": "600"},
    "min_avg_turnover_20d": {"US": "50000000"},
    "min_market_cap_usd": "10000000000",
    "scan_top_by_turnover": 100,
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
    "source": "候选来源",
    "min_market_cap_usd": "最低市值（美元）",
    "scan_top_by_turnover": "按成交额先取前几只",
}


def _num(v):
    from ..db.types import D

    return D(str(v))


def screen_scan(provider, rules: dict[str, Any], now: datetime) -> tuple[list[dict], list[dict], dict]:
    """每日自动股票池：全部美股 →（服务器端条件选股）→ 券商核实类型 → 规则复核 → 按近期强弱排序取前 N。

    排序分 = 0.6 × 20 日涨跌幅 + 0.4 × 60 日涨跌幅（确定性规则，不是盈利预测，也不是 AI 判断）。
    """
    rows, stats = provider.scan_us(rules)
    provider.ensure_instruments([r["symbol"] for r in rows])
    members: list[dict] = []
    excluded: list[dict] = []
    mn, mx = rules.get("min_price", {}).get("US"), rules.get("max_price", {}).get("US")
    turn, cap = rules.get("min_avg_turnover_20d", {}).get("US"), rules.get("min_market_cap_usd")
    verified = 0
    for r in rows:
        reasons = []
        inst = provider.instrument(r["symbol"])
        if inst is None:
            reasons.append("券商资料未核实（类型/每手股数），暂不纳入")
        else:
            verified += 1
            if inst.sec_type not in rules.get("sec_types", ["STOCK"]):
                reasons.append(f"证券类型 {inst.sec_type} 不允许")
            if rules.get("exclude_leveraged", True) and inst.is_leveraged:
                reasons.append("杠杆/反向产品已排除")
        if r["symbol"] in rules.get("exclude_symbols", []):
            reasons.append("用户手动排除")
        price = r.get("price")
        if price is None:
            reasons.append("缺少价格")
        else:
            if mn is not None and _num(price) < _num(mn):
                reasons.append(f"价格低于 {mn}")
            if mx is not None and _num(price) > _num(mx):
                reasons.append(f"价格高于 {mx}")
        if turn is not None and (r.get("turnover_20d") is None or _num(r["turnover_20d"]) < _num(turn)):
            reasons.append("成交额不足")
        if cap is not None and (r.get("market_cap") is None or _num(r["market_cap"]) < _num(cap)):
            reasons.append("市值不足")
        c20, c60 = r.get("change_20d_pct"), r.get("change_60d_pct")
        score = round(0.6 * (c20 or 0) + 0.4 * (c60 or 0), 2) if c20 is not None or c60 is not None else None
        row = {"symbol": r["symbol"], "name": inst.name if inst else r["name"], "market": "US",
               "sec_type": inst.sec_type if inst else "UNVERIFIED",
               "last_close": str(price) if price is not None else None,
               "avg_turnover_20d": str(round(r["turnover_20d"])) if r.get("turnover_20d") is not None else None,
               "market_cap_usd": str(round(r["market_cap"])) if r.get("market_cap") is not None else None,
               "change_20d_pct": c20, "change_60d_pct": c60, "change_ytd_pct": r.get("change_ytd_pct"),
               "pe_ttm": r.get("pe_ttm"), "rank_score": score}
        (excluded.append(row | {"reasons": reasons}) if reasons else members.append(row))
    members.sort(key=lambda x: (x["rank_score"] is not None, x["rank_score"] or 0), reverse=True)
    n = int(rules.get("max_members", 20))
    for extra in members[n:]:
        excluded.append(extra | {"reasons": ["超过成员上限（按近期强弱排序）"]})
    members = members[:n]
    for i, mbr in enumerate(members, 1):
        mbr["pool_rank"] = i
    stats = stats | {"verified": verified, "members": len(members), "excluded": len(excluded),
                     "method": "moomoo 条件选股（服务器计算）→ 券商核实类型 → 规则复核 → 按 20/60 日涨跌幅排序"}
    return members, excluded, stats


def screen(provider: MarketDataProvider, rules: dict[str, Any], now: datetime) -> tuple[list[dict], list[dict], dict]:
    """返回（成员，被排除，统计）。候选来源由规则的 source 决定。"""
    if rules.get("source") == "MOOMOO_SCAN":
        if not hasattr(provider, "scan_us"):
            raise RuntimeError("「每日自动股票池」需要 moomoo 实时行情（OpenD 已登录）")
        return screen_scan(provider, rules, now)
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
    return members[:cap], excluded, {"source": "WATCHLIST", "members": len(members[:cap]), "excluded": len(excluded)}
