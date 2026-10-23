"""风控参数（版本化）。

以下数值仅作 LOCAL_PAPER/DEMO 的测试种子。实盘必须由用户在 live 命名空间单独设置并确认，
不会从模拟参数自动继承为已批准。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

SEED_RISK_PARAMS: dict[str, Any] = {
    "max_position_pct": "0.10",
    "max_total_exposure_pct": "0.30",
    "risk_per_trade_pct": "0.0025",
    "max_positions": 3,
    "max_daily_new_orders": 3,
    "daily_loss_pct": "0.01",
    "max_drawdown_pct": "0.05",
    "max_spread_bps": 150,
    "max_quote_age_seconds_realtime": 15,
    "max_quote_age_seconds_replay": 1800,
    "entry_order_ttl_minutes": 30,
    "exit_reprice_minutes": 10,
    "no_new_entries_minutes_before_close": 15,
    "min_stop_distance_pct": "0.005",
    "max_stop_distance_pct": "0.20",
    "assumed_slippage_bps": 10,
    "max_entry_premium_bps": 30,
    "exit_limit_discount_bps": 50,
    "default_max_hold_days": 20,
    "allow_margin": False,
    "allow_short": False,
    "allow_fx": False,
    "reconcile_interval_minutes": 5,
}

PARAM_LABELS: dict[str, str] = {
    "max_position_pct": "单标的仓位上限（占策略净值）",
    "max_total_exposure_pct": "总投入上限（占策略净值）",
    "risk_per_trade_pct": "单笔计划风险（占策略净值）",
    "max_positions": "同时持有标的上限",
    "max_daily_new_orders": "当日新开仓订单上限",
    "daily_loss_pct": "当日亏损暂停线（占当日起始净值）",
    "max_drawdown_pct": "最大回撤暂停线（现金流调整后净值高点）",
    "max_spread_bps": "最大买卖价差（基点）",
    "max_quote_age_seconds_realtime": "实时报价最长允许延迟（秒）",
    "max_quote_age_seconds_replay": "回放报价最长允许延迟（秒）",
    "entry_order_ttl_minutes": "开仓委托有效期（分钟）",
    "exit_reprice_minutes": "退出委托未成交改价间隔（分钟）",
    "no_new_entries_minutes_before_close": "收市前多少分钟不再开新仓",
    "min_stop_distance_pct": "止损距离下限",
    "max_stop_distance_pct": "止损距离上限",
    "assumed_slippage_bps": "计算风险时假设的滑点（基点）",
    "max_entry_premium_bps": "买入限价最多高于卖一价（基点）",
    "exit_limit_discount_bps": "保护性卖出限价低于买一价（基点）",
    "default_max_hold_days": "默认最长持有天数",
    "allow_margin": "允许融资",
    "allow_short": "允许做空",
    "allow_fx": "允许自动换汇",
    "reconcile_interval_minutes": "对账间隔（分钟）",
}

# 数值越大越宽松的参数（用于识别「放宽风控」需要再次确认）
LOOSER_IF_HIGHER = {
    "max_position_pct", "max_total_exposure_pct", "risk_per_trade_pct", "max_positions", "max_daily_new_orders",
    "daily_loss_pct", "max_drawdown_pct", "max_spread_bps", "max_quote_age_seconds_realtime",
    "max_quote_age_seconds_replay", "entry_order_ttl_minutes", "max_stop_distance_pct", "max_entry_premium_bps",
    "default_max_hold_days",
}
LOOSER_IF_LOWER = {"no_new_entries_minutes_before_close", "min_stop_distance_pct"}
FORBIDDEN_TRUE = {"allow_margin", "allow_short", "allow_fx"}

# 硬上限：任何版本都不能超过（首版产品边界）
HARD_LIMITS: dict[str, Any] = {
    "max_position_pct": "0.50",
    "max_total_exposure_pct": "1.00",
    "risk_per_trade_pct": "0.02",
    "max_positions": 10,
    "max_daily_new_orders": 20,
    "daily_loss_pct": "0.10",
    "max_drawdown_pct": "0.30",
}


def _num(v):
    return Decimal(str(v))


def diff_params(before: dict, after: dict) -> list[dict]:
    out = []
    for k in sorted(set(before) | set(after)):
        a, b = before.get(k), after.get(k)
        if str(a) == str(b):
            continue
        looser = False
        if k in FORBIDDEN_TRUE and b is True:
            looser = True
        elif k in LOOSER_IF_HIGHER and a is not None and b is not None and _num(b) > _num(a):
            looser = True
        elif k in LOOSER_IF_LOWER and a is not None and b is not None and _num(b) < _num(a):
            looser = True
        out.append({"key": k, "label": PARAM_LABELS.get(k, k), "before": a, "after": b, "loosens": looser})
    return out


def validate_params(p: dict) -> list[str]:
    errors = []
    for k in SEED_RISK_PARAMS:
        if k not in p:
            errors.append(f"缺少参数 {k}")
    for k in FORBIDDEN_TRUE:
        if p.get(k):
            errors.append(f"{PARAM_LABELS[k]}：首版禁止")
    for k, lim in HARD_LIMITS.items():
        if k in p and _num(p[k]) > _num(lim):
            errors.append(f"{PARAM_LABELS[k]} 超过产品硬上限 {lim}")
    for k in ("max_position_pct", "max_total_exposure_pct", "risk_per_trade_pct", "daily_loss_pct", "max_drawdown_pct"):
        if k in p and _num(p[k]) <= 0:
            errors.append(f"{PARAM_LABELS[k]} 必须大于 0")
    return errors
