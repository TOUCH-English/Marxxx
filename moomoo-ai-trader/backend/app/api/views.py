"""页面读取用的聚合视图。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import authz
from ..core.calendar import MYT
from ..core.enums import DECISION_OUTCOME_LABELS, MODE_LABELS, ORDER_STATUS_LABELS, Mode
from ..db.models import (
    AuditEvent,
    Authorization,
    DailyEquity,
    Decision,
    FxRate,
    Instrument,
    Notification,
    OrderIntent,
    Portfolio,
    Position,
    QuoteSnapshot,
    ReviewRun,
    RiskLock,
    StrategyVersion,
    WorkerHeartbeat,
)
from ..db.types import D, utcnow
from ..portfolio import ledger
from ..risk.locks import active_locks
from ..services.runs import current_portfolio
from .deps import j, row

DATA_SOURCE_LABELS = {
    "DEMO_FIXTURE": "演示固定数据（虚构证券，本地模拟成交）",
    "HISTORICAL_REPLAY": "历史回放（非实时，本地模拟成交）",
    "MOOMOO_REALTIME": "moomoo OpenD 实时行情",
}


def business_time(p: Portfolio | None) -> datetime:
    if p is not None and p.data_source in ("DEMO_FIXTURE", "HISTORICAL_REPLAY") and p.sim_clock:
        return p.sim_clock
    return utcnow()


def worker_info(s: Session, ns: str) -> dict:
    hb = s.get(WorkerHeartbeat, ns)
    if hb is None:
        return {"alive": False, "heartbeat_at": None, "age_s": None, "status": {}}
    age = (utcnow() - hb.heartbeat_at).total_seconds()
    return {"alive": age < 30, "heartbeat_at": hb.heartbeat_at.isoformat(), "age_s": int(age), "status": hb.status,
            "pid": hb.pid, "started_at": hb.started_at.isoformat()}


def latest_quote(s: Session, symbol: str) -> QuoteSnapshot | None:
    return s.execute(select(QuoteSnapshot).where(QuoteSnapshot.symbol == symbol)).scalar_one_or_none()


def _inst_name(s: Session, symbol: str) -> str:
    i = s.execute(select(Instrument.name).where(Instrument.symbol == symbol)).scalar()
    if i:
        return i
    from ..workers.engine import fixture_dir
    import json

    try:
        for x in json.loads((fixture_dir() / "demo_instruments.json").read_text(encoding="utf-8")):
            if x["symbol"] == symbol:
                return x["name"]
    except Exception:
        pass
    return symbol


def active_auth(s: Session, pid: int) -> Authorization | None:
    return s.execute(select(Authorization).where(Authorization.portfolio_id == pid)
                     .order_by(Authorization.id.desc()).limit(1)).scalar_one_or_none()


def overview(s: Session, ns: str) -> dict[str, Any]:
    st = get_settings()
    p = current_portfolio(s, ns)
    w = worker_info(s, ns)
    out: dict[str, Any] = {"ns": ns, "deployment": {"live_trading_enabled": st.live_trading_enabled},
                           "worker": w, "portfolio": None, "auth": None, "currencies": [], "positions": [],
                           "locks": [], "today": {}, "why_no_trade": [], "allow_new_entries": {"ok": False, "reasons": []}}
    out["notifications_unread"] = s.execute(select(func.count()).select_from(Notification)
                                            .where(Notification.read.is_(False))).scalar_one()
    if p is None:
        out["allow_new_entries"]["reasons"].append("尚未启动运行（请使用启动向导）")
        return out
    bt = business_time(p)
    pst = (w.get("status") or {}).get("portfolios", {}).get(str(p.id), {})
    out["portfolio"] = {
        "id": p.id, "mode": p.mode, "mode_label": MODE_LABELS[Mode(p.mode)], "name": p.name, "running": p.running,
        "data_source": p.data_source, "data_source_label": DATA_SOURCE_LABELS.get(p.data_source, p.data_source),
        "pause_new_entries": p.pause_new_entries, "emergency_stop": p.emergency_stop, "business_time": bt.isoformat(),
        "business_time_myt": bt.astimezone(MYT).strftime("%Y-%m-%d %H:%M"),
        "is_replay": p.data_source in ("DEMO_FIXTURE", "HISTORICAL_REPLAY"), "runtime": pst,
    }
    a = active_auth(s, p.id)
    reasons = []
    if a is not None:
        ok, why = authz.check(a, utcnow(), p.mode)
        sv = s.get(StrategyVersion, a.strategy_version_ids[0]) if a.strategy_version_ids else None
        out["auth"] = {"id": a.id, "status": a.status, "ok": ok, "reason": why, "expires_at": a.expires_at.isoformat(),
                       "summary_text": a.summary_text, "capital": a.capital, "max_planned_loss": a.max_planned_loss,
                       "scope": a.scope, "overnight": a.overnight,
                       "strategy": f"{sv.key} {sv.version}" if sv else None}
        if not ok:
            reasons.append(f"运行授权：{why}")
    else:
        reasons.append("没有运行授权")
    if p.pause_new_entries:
        reasons.append("已暂停新开仓")
    if p.emergency_stop:
        reasons.append("已紧急停止新交易")
    if not w["alive"]:
        reasons.append("后台工作进程未运行或心跳超时")
    for hb in pst.get("health_blocks", []) or []:
        reasons.append(hb)
    locks = active_locks(s, p.id)
    for lk in locks:
        if lk.scope == "ALL":
            reasons.append(f"风控锁定：{lk.reason}")
    out["locks"] = [row(lk) for lk in locks]
    out["allow_new_entries"] = {"ok": not reasons, "reasons": reasons}
    # 价格：最新快照
    def price_of(sym: str) -> Decimal | None:
        q = latest_quote(s, sym)
        return q.last if q is not None and q.last is not None else None

    ccys = sorted(set((a.capital or {}).keys()) if a else set()) or sorted({c for c, in s.execute(
        select(Position.currency).where(Position.portfolio_id == p.id)).all()})
    for ccy in ccys:
        ev = ledger.equity_view(s, p.id, ccy, price_of)
        alloc = ledger.total_flows(s, p.id, ccy)
        realized = sum((x.realized_pnl for x in s.execute(select(Position).where(Position.portfolio_id == p.id,
                                                                                  Position.currency == ccy)).scalars()), Decimal(0))
        fees = sum((x.fees for x in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == p.id,
                                                                           OrderIntent.currency == ccy)).scalars()), Decimal(0))
        rows = list(s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == p.id, DailyEquity.currency == ccy)
                              .order_by(DailyEquity.day_myt)).scalars())
        mdd = Decimal(0)
        for r in rows:
            if r.hwm_adjusted > 0:
                mdd = max(mdd, (r.hwm_adjusted - r.min_equity) / r.hwm_adjusted)
        today = rows[-1] if rows else None
        day_pnl = (ev.equity - (today.start_equity + today.flows)) if today else Decimal(0)
        out["currencies"].append(j({
            "currency": ccy, "allocation": alloc, "cash": ev.cash, "reserved": ev.reserved,
            "available": ev.cash - ev.reserved, "market_value": ev.market_value, "equity": ev.equity,
            "realized_pnl": realized, "unrealized_pnl": ev.unrealized, "fees": fees,
            "max_drawdown_pct": (mdd * 100).quantize(Decimal("0.01")), "day_pnl": day_pnl,
            "total_pnl": ev.equity - alloc, "missing_prices": ev.missing_prices,
        }))
    fx = s.execute(select(FxRate).where(FxRate.pair == "USDMYR").order_by(FxRate.as_of.desc()).limit(1)).scalar_one_or_none()
    out["fx"] = row(fx) if fx else None
    if fx and out["currencies"]:
        tot = Decimal(0)
        for c in out["currencies"]:
            tot += D(c["equity"]) * (D(fx.rate) if c["currency"] == "USD" else 1)
        out["combined_myr"] = {"equity": str(tot.quantize(Decimal("0.01"))),
                               "note": f"按 {fx.source}（{fx.as_of.date()}）汇率 {fx.rate} 折算，仅供参考"}
    else:
        out["combined_myr"] = None
    # 持仓
    rec_locked = {lk.scope for lk in locks if lk.kind == "RECONCILE"}
    for x in ledger.open_positions(s, p.id):
        q = latest_quote(s, x.symbol)
        last = q.last if q and q.last is not None else None
        out["positions"].append(j({
            "id": x.id, "symbol": x.symbol, "name": _inst_name(s, x.symbol), "market": x.market, "currency": x.currency,
            "qty": x.qty, "avg_cost": x.avg_cost.quantize(Decimal("0.0001")), "last": last,
            "market_value": (last * x.qty) if last is not None else None,
            "unrealized": ((last - x.avg_cost) * x.qty).quantize(Decimal("0.01")) if last is not None else None,
            "unrealized_pct": (((last / x.avg_cost) - 1) * 100).quantize(Decimal("0.01")) if last is not None and x.avg_cost else None,
            "stop_price": x.stop_price, "take_profit_price": x.take_profit_price, "max_hold_until": x.max_hold_until,
            "opened_at": x.opened_at, "strategy_version": x.strategy_version, "reconcile_locked": x.symbol in rec_locked,
            "quote_source": q.source if q else None, "quote_ts": q.source_ts if q else None,
        }))
    # 今天
    day0 = datetime.combine(bt.astimezone(MYT).date(), datetime.min.time(), MYT)
    decs = list(s.execute(select(Decision).where(Decision.portfolio_id == p.id, Decision.as_of >= day0)).scalars())
    ints = list(s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == p.id, OrderIntent.as_of >= day0)).scalars())
    out["today"] = {
        "day": day0.date().isoformat(),
        "decisions": {DECISION_OUTCOME_LABELS.get(k, k): v for k, v in Counter(d.outcome for d in decs).items()},
        "orders": {ORDER_STATUS_LABELS.get(k, k): v for k, v in Counter(i.status for i in ints).items()},
        "executed": [j({"id": d.id, "symbol": d.symbol, "action": d.action, "source": d.source,
                        "reason": d.outcome_reason, "as_of": d.as_of}) for d in decs if d.outcome == "AUTO_EXECUTED"][-10:],
    }
    why = Counter()
    for d in decs:
        if d.outcome in ("NO_ACTION", "RISK_BLOCKED", "INVALID", "EXPIRED") and d.outcome_reason:
            why[d.outcome_reason[:60]] += 1
    out["why_no_trade"] = [{"reason": k, "count": v} for k, v in why.most_common(5)]
    rr = s.execute(select(ReviewRun).where(ReviewRun.portfolio_id == p.id, ReviewRun.kind == "DAILY")
                   .order_by(ReviewRun.id.desc()).limit(1)).scalar_one_or_none()
    out["latest_daily_report"] = {"id": rr.id, "summary": rr.summary, "period_start": rr.period_start.isoformat()} if rr else None
    out["recent_events"] = [row(e, ["id", "category", "action", "actor", "symbol", "message", "as_of", "created_at_utc",
                                    "decision_id", "intent_id"])
                            for e in s.execute(select(AuditEvent).where(AuditEvent.portfolio_id == p.id)
                                               .order_by(AuditEvent.id.desc()).limit(12)).scalars()]
    out["account"] = {"broker_total_assets": (pst.get("broker_total_assets") if pst else None),
                      "note": "账户总资产来自券商只读查询；系统只管理自己的分配额度和持仓" if p.mode in ("LIVE_AUTO", "LIVE_REVIEW", "BROKER_PAPER")
                      else "模拟账户：总资产即模拟分配资金"}
    return out


def decision_view(s: Session, d: Decision, full: bool = False) -> dict:
    out = row(d, ["id", "symbol", "market", "currency", "action", "source", "as_of", "expires_at", "outcome",
                  "outcome_reason", "strategy_version", "mode", "data_source", "created_at_utc", "ai_call_id"])
    out["outcome_label"] = DECISION_OUTCOME_LABELS.get(d.outcome, d.outcome)
    out["rationale"] = (d.model_judgment or {}).get("rationale") or (d.proposal or {}).get("rule", "")
    out["proposal"] = d.proposal
    rc = d.rule_calc or {}
    first = s.execute(select(OrderIntent).where(OrderIntent.decision_id == d.id)
                      .order_by(OrderIntent.created_at_utc)).scalars().first()
    out["summary"] = {"qty": first.qty if first else rc.get("qty"),
                      "limit_price": j(first.limit_price) if first else rc.get("limit_price"),
                      "est_fee": j(first.est_fees) if first else rc.get("est_fee_buy"),
                      "order_id": first.id if first else None, "order_status": first.status if first else None,
                      "order_status_label": ORDER_STATUS_LABELS.get(first.status, first.status) if first else None,
                      "filled_qty": first.filled_qty if first else None}
    if full:
        out.update({"facts": d.facts, "rule_calc": d.rule_calc, "model_judgment": d.model_judgment,
                    "evidence": d.evidence})
        out["intents"] = [intent_view(s, i) for i in s.execute(select(OrderIntent).where(OrderIntent.decision_id == d.id)).scalars()]
    return out


def intent_view(s: Session, i: OrderIntent, full: bool = False) -> dict:
    out = row(i, ["id", "symbol", "market", "currency", "side", "reason", "qty", "limit_price", "status", "status_detail",
                  "filled_qty", "avg_fill_price", "fees", "est_fees", "broker_order_id", "decision_id", "as_of",
                  "created_at_utc", "updated_at_utc", "expires_at", "needs_manual_review", "mode", "strategy_version",
                  "stop_price", "take_profit_price", "ref_price"])
    out["status_label"] = ORDER_STATUS_LABELS.get(i.status, i.status)
    if full:
        from ..db.models import Fill, OrderEvent

        out["risk_check"] = i.risk_check
        out["events"] = [row(e) for e in s.execute(select(OrderEvent).where(OrderEvent.intent_id == i.id)
                                                   .order_by(OrderEvent.id)).scalars()]
        out["fills"] = [row(f) for f in s.execute(select(Fill).where(Fill.intent_id == i.id).order_by(Fill.id)).scalars()]
    return out
