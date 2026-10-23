"""下单前风控校验与仓位计算（确定性程序，AI 无法绕过）。

所有校验与资金/名额预留在同一个 BEGIN IMMEDIATE 写事务中完成，
并发的两个建议不可能读到同一笔可用资金后一起超额下单。

仓位 = min(计划风险允许数量, 单标的上限数量, 总额度剩余数量, 可用现金数量, [AI 目标仓位数量])，
再向下取整到合法交易单位；不足一手则不下单。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import authz
from ..core.audit import audit
from ..core.calendar import risk_day
from ..core.enums import MARKET_CURRENCY, IntentReason, Mode, OrderStatus, Side
from ..core.money import calc_fees, is_valid_tick, round_lot_down, round_to_tick
from ..db.models import Authorization, DailyEquity, OrderIntent, Portfolio, Position, RiskReservation
from ..db.types import D
from ..market_data.base import InstrumentInfo, Quote
from ..portfolio import ledger
from .locks import blocking_locks


@dataclass
class RiskContext:
    mode: Mode
    portfolio: Portfolio
    auth: Authorization | None
    params: dict[str, Any]
    risk_policy_version: int
    now: datetime          # 业务时间（回放时为回放时钟）
    wall_now: datetime     # 真实时间（授权有效期按真实时间判断）
    calendar: Any
    is_replay: bool
    fee_schedules: dict[str, dict]
    scope_symbols: set[str]
    account_id: str = ""
    broker_cash: dict[str, Any] = field(default_factory=dict)  # 实盘：券商现金信息
    health_blocks: list[str] = field(default_factory=list)
    price_of: Any = None  # symbol -> Decimal | None
    risk_policy_version_id: int = 0


@dataclass
class EntryRequest:
    symbol: str
    instrument: InstrumentInfo | None
    quote: Quote | None
    stop_price: Decimal | None
    intent_key: str
    strategy_version: str
    strategy_version_id: int
    decision_id: str | None = None
    take_profit_price: Decimal | None = None
    max_hold_days: int | None = None
    target_position_pct: Decimal | None = None
    max_price: Decimal | None = None
    min_reward_risk: Decimal | None = None  # 真实模型的买入：按实际限价复核盈亏比
    signal_time: datetime | None = None
    signal_expires_at: datetime | None = None
    reason: str = IntentReason.ENTRY


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class EntryResult:
    ok: bool
    checks: list[Check]
    qty: int = 0
    limit_price: Decimal | None = None
    est_fee_buy: Decimal = Decimal(0)
    est_fee_sell: Decimal = Decimal(0)
    sizing: dict[str, Any] = field(default_factory=dict)
    duplicate: bool = False

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}：{c.detail}" for c in self.checks if not c.ok]

    def as_json(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "sizing": {k: str(v) for k, v in self.sizing.items()},
            "qty": self.qty,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "est_fee_buy": str(self.est_fee_buy),
            "est_fee_sell": str(self.est_fee_sell),
        }


def quote_fresh(q: Quote | None, params: dict, now: datetime, is_replay: bool) -> tuple[bool, str]:
    if q is None:
        return False, "没有报价"
    if q.suspended:
        return False, "证券停牌"
    if q.bid is None or q.ask is None or q.last is None:
        return False, "缺少买卖价"
    if q.source_ts is None:
        return False, "报价缺少源时间戳"
    max_age = int(params["max_quote_age_seconds_replay" if is_replay else "max_quote_age_seconds_realtime"])
    age = (now - q.source_ts).total_seconds()
    if age > max_age:
        return False, f"报价已过期（{int(age)} 秒前，上限 {max_age} 秒）"
    if age < -5:
        return False, "报价时间晚于当前时间，数据异常"
    if not is_replay and not q.is_realtime:
        return False, "当前行情非实时（延迟行情不得用于开仓）"
    return True, f"报价时间 {q.source_ts.isoformat()}（{int(age)} 秒前）"


def _exposure(s: Session, ctx: RiskContext, ccy: str) -> dict[str, Decimal]:
    pid = ctx.portfolio.id
    ev = ledger.equity_view(s, pid, ccy, ctx.price_of or (lambda _s: None))
    pending_buy = Decimal(0)
    by_symbol: dict[str, Decimal] = {}
    for r in s.execute(select(RiskReservation).where(RiskReservation.portfolio_id == pid,
                                                     RiskReservation.currency == ccy,
                                                     RiskReservation.status == "ACTIVE")).scalars():
        pending_buy += r.cash_amount
        by_symbol[r.symbol] = by_symbol.get(r.symbol, Decimal(0)) + r.cash_amount
    return {"equity": ev.equity, "cash": ev.cash, "mv": ev.market_value, "pending_buy": pending_buy,
            "reserved": ev.reserved, "by_symbol": by_symbol}  # type: ignore[dict-item]


def _open_risk(s: Session, ctx: RiskContext, ccy: str) -> Decimal:
    """所有持仓若同时打到止损还会亏的金额（按现价到止损价），加上待成交买单已预留的计划风险。"""
    total = Decimal(0)
    for p in ledger.open_positions(s, ctx.portfolio.id):
        if p.currency != ccy or p.qty <= 0:
            continue
        px = (ctx.price_of(p.symbol) if ctx.price_of else None) or p.avg_cost
        if p.stop_price is None:
            total += px * p.qty  # 没有止损：整笔都算风险
        elif px > p.stop_price:
            total += (px - p.stop_price) * p.qty
    for r in s.execute(select(RiskReservation).where(RiskReservation.portfolio_id == ctx.portfolio.id,
                                                     RiskReservation.currency == ccy,
                                                     RiskReservation.status == "ACTIVE")).scalars():
        total += r.risk_amount or Decimal(0)
    return total


def _symbol_mv(s: Session, ctx: RiskContext, symbol: str) -> Decimal:
    p = ledger.get_open_position(s, ctx.portfolio.id, symbol)
    if p is None:
        return Decimal(0)
    px = (ctx.price_of(symbol) if ctx.price_of else None) or p.avg_cost
    return px * p.qty


def evaluate_entry(s: Session, ctx: RiskContext, req: EntryRequest) -> EntryResult:
    P = ctx.params
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> bool:
        checks.append(Check(name, ok, detail))
        return ok

    pid = ctx.portfolio.id
    # 1. 模式与命名空间
    add("模式", ctx.mode == Mode(ctx.portfolio.mode), f"{ctx.mode.label}")
    # 2. 运行授权
    ok, why = authz.check(ctx.auth, ctx.wall_now, ctx.mode.value)
    add("运行授权", ok, why)
    if ctx.auth is not None:
        add("买入权限", bool(ctx.auth.permissions.get("buy")), "授权允许买入" if ctx.auth.permissions.get("buy") else "授权未允许买入")
        add("策略版本", req.strategy_version_id in (ctx.auth.strategy_version_ids or []),
            f"策略版本 {req.strategy_version}")
        add("风控版本", ctx.auth.risk_policy_version_id == ctx.risk_policy_version_id,
            f"风控版本 v{ctx.risk_policy_version}（运行授权批准的版本）")
    # 3. 暂停/紧急停止/锁定/健康
    add("暂停新开仓", not ctx.portfolio.pause_new_entries, "已暂停新开仓" if ctx.portfolio.pause_new_entries else "未暂停")
    add("紧急停止", not ctx.portfolio.emergency_stop, "已紧急停止新交易" if ctx.portfolio.emergency_stop else "正常")
    locks = blocking_locks(s, pid, req.symbol)
    add("风控锁定", not locks, "；".join(lk.reason for lk in locks) if locks else "无")
    add("健康检查", not ctx.health_blocks, "；".join(ctx.health_blocks) if ctx.health_blocks else "正常")
    # 4. 授权范围
    add("授权范围", req.symbol in ctx.scope_symbols, "在授权范围内" if req.symbol in ctx.scope_symbols else "不在白名单/股票池快照内")
    # 5. 证券
    inst = req.instrument
    if not add("证券资料", inst is not None, "已核对" if inst else "未知证券"):
        return EntryResult(False, checks)
    market = inst.market
    ccy = inst.currency
    add("证券类型", inst.sec_type in ("STOCK", "ETF") and not inst.is_leveraged,
        "杠杆/反向产品不允许" if inst.is_leveraged else
        ("股票/普通ETF" if inst.sec_type in ("STOCK", "ETF") else f"证券类型 {inst.sec_type} 不允许"))
    add("币种", MARKET_CURRENCY.get(market) == ccy, f"{market} 市场以 {ccy} 交易（不自动换汇）")
    add("交易单位", inst.lot_size > 0, f"每手 {inst.lot_size}")
    # 6. 时段
    is_open = ctx.calendar.is_open(market, ctx.now)
    add("交易时段", is_open, ctx.calendar.market_status(market, ctx.now))
    mtc = ctx.calendar.minutes_to_close(market, ctx.now)
    cutoff = int(P["no_new_entries_minutes_before_close"])
    add("收市前禁开新仓", mtc is None or mtc > cutoff, f"距收市 {mtc:.0f} 分钟" if mtc is not None else "")
    if ctx.auth is not None and not ctx.auth.overnight and mtc is not None:
        add("不隔夜", mtc > 60, "授权不允许隔夜，收市前 60 分钟内不开新仓")
    # 7. 信号有效期
    if req.signal_expires_at is not None:
        add("信号有效期", ctx.now < req.signal_expires_at, f"有效至 {req.signal_expires_at.isoformat()}")
    # 8. 报价
    q = req.quote
    fresh, why = quote_fresh(q, P, ctx.now, ctx.is_replay)
    if not add("报价有效", fresh, why):
        return EntryResult(False, checks)
    mid = (q.bid + q.ask) / 2
    spread_bps = (q.ask - q.bid) / mid * 10000
    add("买卖价差", spread_bps <= int(P["max_spread_bps"]), f"{spread_bps:.0f} 基点（上限 {P['max_spread_bps']}）")
    # 9. 限价
    limit = round_to_tick(market, q.ask * (1 + D(P["max_entry_premium_bps"]) / 10000), "down")
    if limit < q.ask:
        limit = q.ask
    if req.max_price is not None:
        limit = min(limit, round_to_tick(market, D(req.max_price), "down"))
    add("限价", limit >= q.ask and is_valid_tick(market, limit),
        f"限价 {limit}（卖一 {q.ask}）" if limit >= q.ask else f"允许最高价 {req.max_price} 低于卖一 {q.ask}，等待价格条件")
    # 10. 止损
    stop = req.stop_price
    if not add("止损价", stop is not None and stop > 0 and stop < limit, f"止损 {stop}" if stop else "缺少止损价，不生成买单"):
        return EntryResult(False, checks)
    dist = (limit - stop) / limit
    if req.min_reward_risk is not None:
        tp = req.take_profit_price
        rr = (tp - limit) / (limit - stop) if tp is not None else None
        add("盈亏比（按实际限价）", rr is not None and rr >= req.min_reward_risk,
            f"{rr:.2f}（止盈 {tp}、限价 {limit}、止损 {stop}；要求 ≥ {req.min_reward_risk}）" if rr is not None else "缺少止盈价")
    add("止损距离", D(P["min_stop_distance_pct"]) <= dist <= D(P["max_stop_distance_pct"]),
        f"{dist * 100:.2f}%（允许 {D(P['min_stop_distance_pct']) * 100:.1f}%–{D(P['max_stop_distance_pct']) * 100:.0f}%）")
    # 11. 费用资料
    sched = ctx.fee_schedules.get(market)
    if not add("费用资料", sched is not None and bool(sched.get("items")), sched.get("label", "") if sched else "缺少费用表"):
        return EntryResult(False, checks)
    # 12. 名额
    open_syms = {p.symbol for p in ledger.open_positions(s, pid)}
    pending_new = set(s.execute(select(RiskReservation.symbol).where(
        RiskReservation.portfolio_id == pid, RiskReservation.status == "ACTIVE", RiskReservation.takes_slot.is_(True))).scalars())
    slots = len(open_syms | pending_new)
    is_new_symbol = req.symbol not in open_syms and req.symbol not in pending_new
    add("持有标的名额", (not is_new_symbol) or slots < int(P["max_positions"]),
        f"已占 {slots}/{P['max_positions']}（含待成交新标的）")
    day = risk_day(inst.currency, ctx.now)  # 按该市场的交易日计数（美股按美东日期）
    today_entries = s.execute(select(func.count()).select_from(RiskReservation).where(
        RiskReservation.portfolio_id == pid, RiskReservation.trading_day == day)).scalar_one()
    add("当日新开仓名额", today_entries < int(P["max_daily_new_orders"]),
        f"今日已用 {today_entries}/{P['max_daily_new_orders']}")
    # 13. 重复
    dup = s.execute(select(OrderIntent.id).where(OrderIntent.intent_key == req.intent_key)).first()
    if dup:
        add("重复意图", False, "相同信号已生成过订单意图")
        return EntryResult(False, checks, duplicate=True)
    if any(not c.ok for c in checks):
        return EntryResult(False, checks, limit_price=limit)

    # ---------------------------------------------------- 仓位计算
    ex = _exposure(s, ctx, ccy)
    equity = ex["equity"]
    cash_avail = ex["cash"] - ex["reserved"]
    lot = inst.lot_size
    slip = D(P["assumed_slippage_bps"]) / 10000

    def fees(qty: int) -> tuple[Decimal, Decimal]:
        fb = calc_fees(sched, "BUY", qty, limit) or Decimal(0)
        fs = calc_fees(sched, "SELL", qty, stop) or Decimal(0)
        return fb, fs

    risk_budget = equity * D(P["risk_per_trade_pct"])
    # 已开仓的风险：若所有持仓同时止损还会再亏多少，加上待成交买单已预留的风险
    open_risk = _open_risk(s, ctx, ccy)
    budget_notes = {"每笔上限": risk_budget.quantize(Decimal("0.01")), "已开仓风险": open_risk.quantize(Decimal("0.01"))}
    if ctx.auth is not None:
        mpl = ctx.auth.max_planned_loss.get(ccy)
        alloc = ctx.auth.capital.get(ccy)
        if mpl is not None and alloc is not None:
            remaining = D(mpl) - max(Decimal(0), D(alloc) - equity) - open_risk
            budget_notes["亏损线剩余"] = remaining.quantize(Decimal("0.01"))
            risk_budget = min(risk_budget, max(Decimal(0), remaining))
    day_row = s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == pid, DailyEquity.currency == ccy,
                                                  DailyEquity.day_myt == risk_day(ccy, ctx.now))).scalar_one_or_none()
    if day_row is not None:
        base = day_row.start_equity + day_row.flows
        day_left = base * D(P["daily_loss_pct"]) - max(Decimal(0), base - equity) - open_risk
        budget_notes["当日亏损额度剩余"] = day_left.quantize(Decimal("0.01"))
        risk_budget = min(risk_budget, max(Decimal(0), day_left))

    def risk_of(qty: int) -> Decimal:
        fb, fs = fees(qty)
        return (limit - stop + limit * slip) * qty + fb + fs

    per_share = limit - stop + limit * slip
    q_risk = round_lot_down(int(risk_budget / per_share), lot) if per_share > 0 else 0
    while q_risk > 0 and risk_of(q_risk) > risk_budget:
        q_risk -= lot
    sym_cap = equity * D(P["max_position_pct"]) - _symbol_mv(s, ctx, req.symbol) - ex["by_symbol"].get(req.symbol, Decimal(0))
    q_symbol = round_lot_down(max(0, int(sym_cap / limit)), lot)
    tot_cap = equity * D(P["max_total_exposure_pct"]) - ex["mv"] - ex["pending_buy"]
    q_total = round_lot_down(max(0, int(tot_cap / limit)), lot)
    q_cash = round_lot_down(max(0, int(cash_avail / limit)), lot)
    while q_cash > 0 and limit * q_cash + fees(q_cash)[0] > cash_avail:
        q_cash -= lot
    limits = {"计划风险": q_risk, "单标的上限": q_symbol, "总额度": q_total, "可用现金": q_cash}
    if req.target_position_pct is not None:
        tp = min(D(req.target_position_pct), D(P["max_position_pct"]))
        tgt = equity * tp - _symbol_mv(s, ctx, req.symbol)
        limits["AI目标仓位"] = round_lot_down(max(0, int(tgt / limit)), lot)
    # 实盘：券商可结算现金
    if ctx.mode.is_live:
        ci = ctx.broker_cash.get(ccy)
        if ci is None or not getattr(ci, "reliable", False) or ci.cash is None:
            add("券商现金", False, "无法可靠区分现金与融资额度，禁用自动开仓")
            return EntryResult(False, checks)
        broker_avail = D(ci.cash) - ex["reserved"]
        q_b = round_lot_down(max(0, int(broker_avail / limit)), lot)
        while q_b > 0 and limit * q_b + fees(q_b)[0] > broker_avail:
            q_b -= lot
        limits["券商可结算现金"] = q_b
    qty = min(limits.values())
    sizing: dict[str, Any] = {"策略净值": equity.quantize(Decimal("0.01")), "风险预算": risk_budget.quantize(Decimal("0.01")),
                              "每股风险": per_share.quantize(Decimal("0.0001")), "入场限价": limit, "止损": stop}
    sizing.update({f"风险预算·{k}": v for k, v in budget_notes.items()})
    sizing.update({f"{k}允许数量": v for k, v in limits.items()})
    binding = min(limits, key=lambda k: limits[k])
    if not add("仓位计算", qty >= lot, f"数量 {qty}（受「{binding}」限制，每手 {lot}）" if qty >= lot
               else f"不足一手：受「{binding}」限制（{limits[binding]}），不下单"):
        return EntryResult(False, checks, sizing=sizing, limit_price=limit)
    fb, fs = fees(qty)
    sizing["预计买入费用"] = fb
    sizing["预计卖出费用"] = fs
    sizing["计划风险金额"] = risk_of(qty).quantize(Decimal("0.01"))
    return EntryResult(True, checks, qty=qty, limit_price=limit, est_fee_buy=fb, est_fee_sell=fs, sizing=sizing)


def create_entry_intent(s: Session, ctx: RiskContext, req: EntryRequest) -> tuple[OrderIntent | None, EntryResult]:
    """在调用方的写事务中：校验 → 预留资金/名额 → 写入订单意图（CREATED）。"""
    res = evaluate_entry(s, ctx, req)
    if not res.ok:
        return None, res
    inst = req.instrument
    assert inst is not None and res.limit_price is not None
    iid = str(uuid.uuid4())
    ttl = int(ctx.params["entry_order_ttl_minutes"])
    intent = OrderIntent(
        id=iid, intent_key=req.intent_key, portfolio_id=ctx.portfolio.id,
        authorization_id=ctx.auth.id if ctx.auth else None, mode=ctx.mode.value, account_id=ctx.account_id,
        decision_id=req.decision_id, strategy_version=req.strategy_version, risk_policy_version=ctx.risk_policy_version,
        symbol=req.symbol, market=inst.market, currency=inst.currency, side=Side.BUY, reason=req.reason,
        qty=res.qty, limit_price=res.limit_price, status=OrderStatus.CREATED, est_fees=res.est_fee_buy,
        ref_price=req.quote.ask if req.quote else None, stop_price=req.stop_price,
        take_profit_price=req.take_profit_price,
        max_hold_days=req.max_hold_days or int(ctx.params["default_max_hold_days"]),
        signal_time=req.signal_time, expires_at=ctx.now + timedelta(minutes=ttl), remark=iid,
        risk_check=res.as_json(), as_of=ctx.now,
    )
    s.add(intent)
    s.flush()
    open_syms = {p.symbol for p in ledger.open_positions(s, ctx.portfolio.id)}
    s.add(RiskReservation(
        portfolio_id=ctx.portfolio.id, intent_id=iid, symbol=req.symbol, currency=inst.currency,
        cash_amount=res.limit_price * res.qty + res.est_fee_buy,
        risk_amount=D(res.sizing.get("计划风险金额", 0)), takes_slot=req.symbol not in open_syms,
        trading_day=risk_day(inst.currency, ctx.now),
    ))
    audit(s, "RISK", "ENTRY_APPROVED", f"{req.symbol} 买入 {res.qty} @≤{res.limit_price} 通过风控并预留资金",
          actor="worker", portfolio_id=ctx.portfolio.id, symbol=req.symbol, decision_id=req.decision_id or "",
          intent_id=iid, data=res.as_json(), as_of=ctx.now)
    return intent, res


def release_reservation(s: Session, intent_id: str, why: str) -> None:
    r = s.execute(select(RiskReservation).where(RiskReservation.intent_id == intent_id)).scalar_one_or_none()
    if r is not None and r.status == "ACTIVE":
        r.status = "RELEASED" if r.cash_amount > 0 else "CONSUMED"
        r.cash_amount = Decimal(0)
        r.updated_at_utc = datetime.now(tz=r.created_at_utc.tzinfo)


def position_for_update(s: Session, pid: int, symbol: str) -> Position | None:
    return ledger.get_open_position(s, pid, symbol)
