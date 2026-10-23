"""命名空间（sim / live）内的运行、订单、决策、日志接口。"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from ..core.audit import audit
from ..core.calendar import MYT
from ..core.enums import OrderStatus
from ..db.models import (
    AICallLog,
    AuditEvent,
    Command,
    Decision,
    Fill,
    Notification,
    OrderEvent,
    OrderIntent,
    Portfolio,
    Position,
)
from ..db.session import read_session, write_session
from ..services.runs import current_portfolio, revoke
from . import security
from .deps import Mutator, Viewer, j, ns_of, row
from .views import decision_view, intent_view, overview

router = APIRouter(prefix="/api/{ns}")


def _portfolio(s, ns) -> Portfolio:
    p = current_portfolio(s, ns)
    if p is None:
        raise HTTPException(404, "尚未启动运行")
    return p


def _cmd(s, type_: str, payload: dict, actor: str = "user") -> Command:
    c = Command(type=type_, payload=payload, created_by=actor)
    s.add(c)
    s.flush()
    return c


@router.get("/overview")
def get_overview(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        return overview(s, ns)


# ------------------------------------------------------------------ 控制
class PauseBody(BaseModel):
    paused: bool


@router.post("/control/pause")
def pause(ns: str, body: PauseBody, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        p = _portfolio(s, ns)
        p.pause_new_entries = body.paused
        audit(s, "CONTROL", "PAUSE" if body.paused else "RESUME",
              "暂停新开仓（已批准的减仓与保护逻辑继续运行，不会清仓）" if body.paused else "恢复新开仓",
              actor="user", portfolio_id=p.id)
    return {"ok": True, "paused": body.paused}


@router.post("/control/emergency-stop")
def emergency_stop(ns: str, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        p = _portfolio(s, ns)
        p.emergency_stop = True
        p.pause_new_entries = True
        c = _cmd(s, "EMERGENCY_STOP", {"portfolio_id": p.id})
        audit(s, "CONTROL", "EMERGENCY_STOP",
              "紧急停止新交易：停止产生新开仓，撤销系统挂起的入场单；保护性订单与持仓默认保留，不会卖出您的资产",
              actor="user", portfolio_id=p.id)
    return {"ok": True, "command_id": c.id}


class ConfirmBody(BaseModel):
    confirm: bool = False
    note: str = ""


@router.post("/control/clear-emergency")
def clear_emergency(ns: str, body: ConfirmBody, sess=Mutator):
    ns_of(ns)
    if not body.confirm:
        raise HTTPException(400, "需要确认")
    with write_session(ns) as s:
        p = _portfolio(s, ns)
        p.emergency_stop = False
        audit(s, "CONTROL", "CLEAR_EMERGENCY", "用户解除紧急停止（新开仓仍保持暂停，需要单独恢复）", actor="user",
              portfolio_id=p.id)
    return {"ok": True}


class LiquidateBody(BaseModel):
    confirm: bool = False
    symbols: list[str] = []


@router.post("/control/liquidate")
def liquidate(ns: str, body: LiquidateBody, sess=Mutator):
    ns_of(ns)
    if not body.confirm:
        raise HTTPException(400, "手动清仓是高影响操作，需要明确确认")
    if ns == "live":
        security.require_recent_reauth(sess)
    with write_session(ns) as s:
        p = _portfolio(s, ns)
        c = _cmd(s, "LIQUIDATE", {"portfolio_id": p.id, "symbols": body.symbols})
        audit(s, "CONTROL", "LIQUIDATE_REQUEST",
              f"用户确认手动清仓：{'全部系统管理的持仓' if not body.symbols else ', '.join(body.symbols)}（不涉及账户中的其他持仓）",
              actor="user", portfolio_id=p.id)
    return {"ok": True, "command_id": c.id}


@router.get("/control/liquidate/preview")
def liquidate_preview(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        p = _portfolio(s, ns)
        ov = overview(s, ns)
        return {"positions": ov["positions"], "note": "只会卖出本系统管理的持仓；按买一价附近的限价卖出，可能部分成交或无法成交；"
                "估算金额 = 数量 × 最新价，未扣除费用。", "portfolio_id": p.id}


@router.post("/control/stop-run")
def stop_run(ns: str, sess=Mutator):
    ns_of(ns)
    with read_session(ns) as s:
        p = _portfolio(s, ns)
    n = revoke(ns, p.id, "user")
    return {"ok": True, "revoked": n, "note": "已停止新开仓；已有持仓继续按批准的保护规则管理"}


@router.post("/control/end-sim")
def end_sim(ns: str, sess=Mutator):
    if ns != "sim":
        raise HTTPException(400, "只适用于模拟")
    with write_session(ns) as s:
        p = _portfolio(s, ns)
        p.running = False
        audit(s, "CONTROL", "END_SIM", "用户结束本次模拟运行（模拟时钟停止）", actor="user", portfolio_id=p.id)
    return {"ok": True}


@router.post("/control/reconcile")
def reconcile_now(ns: str, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        p = _portfolio(s, ns)
        c = _cmd(s, "RECONCILE_NOW", {"portfolio_id": p.id})
    return {"ok": True, "command_id": c.id}


@router.get("/commands/{cid}")
def get_command(ns: str, cid: int, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        c = s.get(Command, cid)
        if c is None:
            raise HTTPException(404)
        return row(c)


# ------------------------------------------------------------------ 决策
@router.get("/decisions")
def list_decisions(ns: str, sess=Viewer, symbol: str | None = None, outcome: str | None = None,
                   source: str | None = None, day: str | None = None, include_routine: bool = False,
                   limit: int = Query(100, le=1000), offset: int = 0):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        if p is None:
            return {"items": [], "total": 0}
        q = select(Decision).where(Decision.portfolio_id == p.id)
        if symbol:
            q = q.where(Decision.symbol == symbol)
        if outcome:
            q = q.where(Decision.outcome == outcome)
        if source:
            q = q.where(Decision.source == source)
        if not include_routine and not outcome:
            q = q.where(or_(Decision.outcome != "NO_ACTION", Decision.action.in_(["BUY_PROPOSAL", "SELL_PROPOSAL"])))
        if day:
            d0 = datetime.combine(date.fromisoformat(day), datetime.min.time(), MYT)
            q = q.where(Decision.as_of >= d0, Decision.as_of < d0 + timedelta(days=1))
        total = s.execute(select(func.count()).select_from(q.subquery())).scalar_one()
        items = list(s.execute(q.order_by(Decision.as_of.desc(), Decision.created_at_utc.desc()).offset(offset).limit(limit)).scalars())
        return {"items": [decision_view(s, d) for d in items], "total": total, "offset": offset, "limit": limit,
                "sources": ["AI_MODEL", "AI_FIXTURE", "BASELINE", "PROTECTION"]}


@router.get("/decisions/{did}")
def get_decision(ns: str, did: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        d = s.get(Decision, did)
        if d is None:
            raise HTTPException(404)
        out = decision_view(s, d, full=True)
        if d.ai_call_id:
            c = s.get(AICallLog, d.ai_call_id)
            out["ai_call"] = row(c, ["id", "provider", "model", "prompt_version", "status", "error", "latency_ms",
                                     "input_tokens", "output_tokens", "input_hash", "created_at_utc"]) if c else None
        return out


@router.get("/ai-calls/{cid}")
def get_ai_call(ns: str, cid: int, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        c = s.get(AICallLog, cid)
        if c is None:
            raise HTTPException(404)
        return row(c)


# ------------------------------------------------------------------ 订单
@router.get("/orders")
def list_orders(ns: str, sess=Viewer, status: str | None = None, symbol: str | None = None,
                limit: int = Query(200, le=2000)):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        if p is None:
            return {"items": []}
        q = select(OrderIntent).where(OrderIntent.portfolio_id == p.id)
        if status:
            q = q.where(OrderIntent.status.in_(status.split(",")))
        if symbol:
            q = q.where(OrderIntent.symbol == symbol)
        return {"items": [intent_view(s, i) for i in s.execute(q.order_by(OrderIntent.created_at_utc.desc()).limit(limit)).scalars()]}


@router.get("/orders/{iid}")
def get_order(ns: str, iid: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        i = s.get(OrderIntent, iid)
        if i is None:
            raise HTTPException(404)
        return intent_view(s, i, full=True)


@router.post("/orders/{iid}/cancel")
def cancel_order(ns: str, iid: str, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        i = s.get(OrderIntent, iid)
        if i is None:
            raise HTTPException(404)
        c = _cmd(s, "CANCEL_ORDER", {"portfolio_id": i.portfolio_id, "intent_id": iid})
    return {"ok": True, "command_id": c.id, "note": "撤单请求已发出；撤单成功以券商确认为准"}


@router.post("/orders/{iid}/approve")
def approve_order(ns: str, iid: str, sess=Mutator):
    """仅 LIVE_REVIEW（可选模式）使用：用户逐笔确认后才提交。"""
    ns_of(ns)
    from ..core import authz
    from ..db.types import utcnow
    from ..risk.locks import blocking_locks
    from .views import active_auth

    with write_session(ns) as s:
        i = s.get(OrderIntent, iid)
        if i is None or i.status != OrderStatus.AWAITING_APPROVAL:
            raise HTTPException(400, "该订单不在待确认状态")
        a = active_auth(s, i.portfolio_id)
        ok, why = authz.check(a, utcnow(), i.mode)
        if not ok:
            raise HTTPException(400, f"运行授权无效：{why}")
        if i.side == "BUY" and blocking_locks(s, i.portfolio_id, i.symbol):
            raise HTTPException(400, "当前有风控锁定，不能确认买入")
        s.add(OrderEvent(intent_id=i.id, event="USER_APPROVED", from_status=i.status, to_status=OrderStatus.CREATED,
                         detail="用户确认提交（LIVE_REVIEW）"))
        i.status = OrderStatus.CREATED
        audit(s, "ORDER", "APPROVED", f"用户确认订单 {i.symbol} {i.side} {i.qty}", actor="user",
              portfolio_id=i.portfolio_id, intent_id=i.id)
    return {"ok": True}


@router.post("/orders/{iid}/reject")
def reject_order(ns: str, iid: str, sess=Mutator):
    ns_of(ns)
    from ..risk.engine import release_reservation

    with write_session(ns) as s:
        i = s.get(OrderIntent, iid)
        if i is None or i.status != OrderStatus.AWAITING_APPROVAL:
            raise HTTPException(400, "该订单不在待确认状态")
        s.add(OrderEvent(intent_id=i.id, event="USER_REJECTED", from_status=i.status, to_status=OrderStatus.CANCELLED,
                         detail="用户拒绝（LIVE_REVIEW）"))
        i.status = OrderStatus.CANCELLED
        release_reservation(s, i.id, "user rejected")
    return {"ok": True}


class ResolveBody(BaseModel):
    resolution: str  # NOT_SUBMITTED / ATTACH
    broker_order_id: str | None = None
    note: str = ""


@router.post("/orders/{iid}/resolve")
def resolve_order(ns: str, iid: str, body: ResolveBody, sess=Mutator):
    """人工核对未知订单。工作进程之外直接修改状态，但不会重发订单。"""
    ns_of(ns)
    from ..orders.manager import OrderManager

    class _A:  # 只用于记录券商标识，不发起任何券商调用
        broker = "MOOMOO" if ns == "live" else "LOCAL_SIM"
        trd_env = "REAL" if ns == "live" else "LOCAL"

    try:
        st = OrderManager(ns, _A(), {}, lambda: datetime.now().astimezone()).manual_resolve(
            iid, body.resolution, body.broker_order_id, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": st}


# ------------------------------------------------------------------ 持仓/成交
@router.get("/positions")
def list_positions(ns: str, sess=Viewer, status: str = "ALL"):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        if p is None:
            return {"items": []}
        q = select(Position).where(Position.portfolio_id == p.id)
        if status != "ALL":
            q = q.where(Position.status == status)
        return {"items": [row(x) for x in s.execute(q.order_by(Position.opened_at.desc())).scalars()]}


@router.get("/fills")
def list_fills(ns: str, sess=Viewer, limit: int = 500):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        if p is None:
            return {"items": []}
        return {"items": [row(f) for f in s.execute(select(Fill).where(Fill.portfolio_id == p.id)
                                                    .order_by(Fill.id.desc()).limit(limit)).scalars()]}


# ------------------------------------------------------------------ 日志 / 因果链
@router.get("/logs")
def logs(ns: str, sess=Viewer, category: str | None = None, symbol: str | None = None, decision_id: str | None = None,
         intent_id: str | None = None, day: str | None = None, q: str | None = None, limit: int = Query(300, le=3000)):
    ns_of(ns)
    with read_session(ns) as s:
        qq = select(AuditEvent)
        if category:
            qq = qq.where(AuditEvent.category == category)
        if symbol:
            qq = qq.where(AuditEvent.symbol == symbol)
        if decision_id:
            qq = qq.where(AuditEvent.decision_id == decision_id)
        if intent_id:
            qq = qq.where(AuditEvent.intent_id == intent_id)
        if q:
            qq = qq.where(AuditEvent.message.contains(q))
        if day:
            d0 = datetime.combine(date.fromisoformat(day), datetime.min.time(), MYT)
            qq = qq.where(or_(AuditEvent.as_of.between(d0, d0 + timedelta(days=1)),
                              AuditEvent.created_at_utc.between(d0, d0 + timedelta(days=1))))
        return {"items": [row(e) for e in s.execute(qq.order_by(AuditEvent.id.desc()).limit(limit)).scalars()]}


@router.get("/trace")
def trace(ns: str, sess=Viewer, decision_id: str | None = None, intent_id: str | None = None):
    """因果链：决策 → 风控 → 订单意图 → 券商事件 → 成交 → 持仓。"""
    ns_of(ns)
    with read_session(ns) as s:
        if intent_id and not decision_id:
            i = s.get(OrderIntent, intent_id)
            decision_id = i.decision_id if i else None
        out: dict[str, Any] = {}
        if decision_id:
            d = s.get(Decision, decision_id)
            out["decision"] = decision_view(s, d, full=True) if d else None
        intents = []
        if decision_id:
            intents = list(s.execute(select(OrderIntent).where(OrderIntent.decision_id == decision_id)).scalars())
        elif intent_id:
            i = s.get(OrderIntent, intent_id)
            intents = [i] if i else []
        out["intents"] = [intent_view(s, i, full=True) for i in intents]
        ids = [i.id for i in intents]
        out["audit"] = [row(e) for e in s.execute(select(AuditEvent).where(or_(
            AuditEvent.decision_id == (decision_id or "__"), AuditEvent.intent_id.in_(ids or ["__"]))).order_by(AuditEvent.id)).scalars()]
        pos_ids = {i.position_id for i in intents if i.position_id}
        out["positions"] = [row(s.get(Position, pid)) for pid in pos_ids]
        return out


# ------------------------------------------------------------------ 通知
@router.get("/notifications")
def notifications(ns: str, sess=Viewer, limit: int = 100):
    ns_of(ns)
    with read_session(ns) as s:
        return {"items": [row(n) for n in s.execute(select(Notification).order_by(Notification.id.desc()).limit(limit)).scalars()]}


@router.post("/notifications/read")
def notifications_read(ns: str, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        for n in s.execute(select(Notification).where(Notification.read.is_(False))).scalars():
            n.read = True
    return {"ok": True}


# ------------------------------------------------------------------ 导出
def _csv(rows: list[dict], name: str) -> StreamingResponse:
    buf = io.StringIO()
    buf.write("﻿")  # Excel 识别 UTF-8
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v) if not isinstance(v, (str, int)) and v is not None else v) for k, v in r.items()})
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/export/{kind}.csv")
def export(ns: str, kind: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        pid = p.id if p else -1
        tag = "模拟" if ns == "sim" else "实盘"
        if kind == "decisions":
            rows = [decision_view(s, d) for d in s.execute(select(Decision).where(Decision.portfolio_id == pid)
                                                           .order_by(Decision.as_of)).scalars()]
            for r in rows:
                r.pop("proposal", None)
        elif kind == "orders":
            rows = [intent_view(s, i) for i in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid)
                                                         .order_by(OrderIntent.created_at_utc)).scalars()]
        elif kind == "fills":
            rows = [row(f) for f in s.execute(select(Fill).where(Fill.portfolio_id == pid).order_by(Fill.id)).scalars()]
        elif kind == "audit":
            rows = [row(e) for e in s.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()]
            for r in rows:
                r["data"] = str(r.get("data"))
        else:
            raise HTTPException(404)
        for r in rows:
            r["数据性质"] = tag
        return _csv([j(r) for r in rows], f"{ns}_{kind}.csv")
