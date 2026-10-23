"""AI 输出校验。任何一项不满足 → 该决定无效，不产生订单。

覆盖：无效 JSON、结构不符、伪造/范围外证券、缺少证据、引用不存在的证据、过期结果、
版本不符、超出政策上限的目标仓位/持有期、缺少止损、对未持有证券卖出、对已持有证券加仓。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError

from .schema import AIProposal, RawBatch, RawDecision


@dataclass
class ValidationContext:
    as_of: datetime
    strategy_version: str
    candidate_symbols: set[str]
    held_symbols: set[str]
    evidence_ids: dict[str, set[str]]  # symbol -> ids
    last_prices: dict[str, Decimal]
    max_target_position_pct: Decimal
    max_holding_days: int
    max_valid_minutes: int
    now: datetime
    # 入场质量硬性规则（只对真实模型启用；固定测试响应不受影响）
    strict_entry: bool = False
    atr: dict[str, float] = field(default_factory=dict)  # symbol -> atr_14（价格单位）
    min_reward_risk: Decimal = Decimal("2")
    min_stop_pct: Decimal = Decimal("0.015")


@dataclass
class ValidatedDecision:
    raw: RawDecision
    proposal: AIProposal | None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.proposal is not None and not self.errors


@dataclass
class BatchResult:
    ok: bool
    errors: list[str]
    decisions: list[ValidatedDecision]
    market_view: str = ""


def parse_and_validate(text: str, ctx: ValidationContext) -> BatchResult:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        return BatchResult(False, [f"无效 JSON：{e}"], [])
    try:
        batch = RawBatch.model_validate(data)
    except ValidationError as e:
        return BatchResult(False, [f"结构校验失败：{e.errors()[:3]}"], [])
    errors: list[str] = []
    if batch.as_of_utc != ctx.as_of.isoformat():
        errors.append(f"数据时点不符（输出 {batch.as_of_utc}，请求 {ctx.as_of.isoformat()}）：可能是过期或缓存的结果")
    if batch.strategy_version != ctx.strategy_version:
        errors.append(f"策略版本不符（输出 {batch.strategy_version}）")
    if errors:
        return BatchResult(False, errors, [], batch.market_view)
    seen: set[str] = set()
    out: list[ValidatedDecision] = []
    for d in batch.decisions:
        errs = _check(d, ctx)
        if d.symbol in seen:
            errs.append("同一证券重复决定，只采用第一条")
        seen.add(d.symbol)
        prop = None
        if not errs:
            expires = ctx.as_of + timedelta(minutes=d.valid_minutes)
            if ctx.now >= expires:
                errs.append("决定已过期")
            else:
                prop = AIProposal(
                    decision_id=str(uuid.uuid4()), symbol=d.symbol, action=d.action, as_of_utc=ctx.as_of,
                    expires_at_utc=expires, evidence_ids=d.evidence_ids, rationale=d.rationale,
                    risk_flags=d.risk_flags,
                    entry_condition=({"type": d.entry_type, "price": str(d.entry_price) if d.entry_price else None}
                                     if d.action == "BUY_PROPOSAL" else None),
                    exit_condition=({"stop_loss_price": str(d.stop_loss_price),
                                     "take_profit_price": str(d.take_profit_price) if d.take_profit_price else None,
                                     "max_holding_days": d.max_holding_days} if d.action == "BUY_PROPOSAL" else None),
                    target_position_pct=d.target_position_pct, max_buy_price=d.max_buy_price,
                    sell_fraction=d.sell_fraction, min_sell_price=d.min_sell_price,
                    strategy_version=ctx.strategy_version,
                )
        out.append(ValidatedDecision(d, prop if not errs else None, errs))
    return BatchResult(True, [], out, batch.market_view)


def _check(d: RawDecision, ctx: ValidationContext) -> list[str]:
    e: list[str] = []
    allowed = ctx.candidate_symbols | ctx.held_symbols
    if d.symbol not in allowed:
        e.append(f"证券 {d.symbol} 不在本次输入的候选/持仓中（可能是伪造或范围外证券）")
        return e
    if d.valid_minutes > ctx.max_valid_minutes:
        e.append(f"有效期 {d.valid_minutes} 分钟超过政策上限 {ctx.max_valid_minutes}")
    known = ctx.evidence_ids.get(d.symbol, set()) | ctx.evidence_ids.get("*", set())
    if d.action in ("BUY_PROPOSAL", "SELL_PROPOSAL"):
        if not d.evidence_ids:
            e.append("缺少证据编号")
        bad = [x for x in d.evidence_ids if x not in known]
        if bad:
            e.append(f"引用了不存在的证据：{bad[:3]}")
    last = ctx.last_prices.get(d.symbol)
    if d.action == "BUY_PROPOSAL":
        if d.symbol in ctx.held_symbols:
            e.append("已持有该证券：政策不允许加仓或摊平")
        if d.stop_loss_price is None:
            e.append("买入必须给出止损价")
        elif last is not None and d.stop_loss_price >= last:
            e.append(f"止损价 {d.stop_loss_price} 不低于当前价 {last}")
        if d.take_profit_price is not None and last is not None and d.take_profit_price <= last:
            e.append("止盈价必须高于当前价")
        if d.max_holding_days is None or not (1 <= d.max_holding_days <= ctx.max_holding_days):
            e.append(f"持有期必须在 1–{ctx.max_holding_days} 天之间")
        if d.target_position_pct is None or not (Decimal(0) < d.target_position_pct <= ctx.max_target_position_pct):
            e.append(f"目标仓位必须在 0–{ctx.max_target_position_pct} 之间")
        if d.entry_type in ("LIMIT_AT_OR_BELOW", "BREAKOUT_ABOVE") and d.entry_price is None:
            e.append("条件入场必须给出 entry_price")
        if d.entry_type == "NONE":
            e.append("买入必须给出入场方式")
        if ctx.strict_entry:
            e += _entry_quality(d, ctx, last)
        for name, v in (("entry_price", d.entry_price), ("max_buy_price", d.max_buy_price)):
            if v is not None and last is not None and not (last * Decimal("0.8") <= v <= last * Decimal("1.2")):
                e.append(f"{name} {v} 偏离当前价超过 20%")
    elif d.action == "SELL_PROPOSAL":
        if d.symbol not in ctx.held_symbols:
            e.append("未持有该证券，不能卖出（首版不做空）")
        if d.sell_fraction is None or not (Decimal(0) < d.sell_fraction <= 1):
            e.append("卖出比例必须在 0–1 之间")
    return e


def _entry_quality(d, ctx: ValidationContext, last: Decimal | None) -> list[str]:
    """盈亏比、止损距离（含 ATR 下限）、必须有止盈。审核建议：小账户固定费用高，差的交易结构直接拒绝。"""
    e: list[str] = []
    if d.take_profit_price is None:
        return ["买入必须给出止盈价（用于计算盈亏比）"]
    ref = d.entry_price if d.entry_price is not None else last
    if ref is None or d.stop_loss_price is None or d.stop_loss_price >= ref:
        return e
    risk = ref - d.stop_loss_price
    reward = d.take_profit_price - ref
    if reward <= 0 or reward / risk < ctx.min_reward_risk:
        e.append(f"盈亏比 {(reward / risk):.2f} 低于要求 {ctx.min_reward_risk}（止盈 {d.take_profit_price}、入场 {ref}、止损 {d.stop_loss_price}）")
    if risk / ref < ctx.min_stop_pct:
        e.append(f"止损距离 {(risk / ref * 100):.2f}% 小于 {ctx.min_stop_pct * 100}%：容易被日常波动触发")
    atr = ctx.atr.get(d.symbol)
    if atr and risk < Decimal(str(atr)):
        e.append(f"止损距离 {risk} 小于 1 倍 ATR（{atr}）：容易被日常波动触发")
    return e
