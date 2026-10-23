"""登录、系统状态、连接诊断、设置、备份、实时推送。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import get_settings
from ..core.audit import audit, mask_account
from ..core.calendar import MYT
from ..db.models import AccountBinding, AuditEvent, MarketCapability
from ..db.session import read_session, write_session
from ..db.types import utcnow
from . import security
from .deps import Mutator, Viewer, row
from .views import worker_info

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ 登录
class PwBody(BaseModel):
    password: str


class LoginBody(PwBody):
    remember: bool = False  # 记住这台 Mac 30 天（默认不勾选）


def _set_session_cookies(resp: Response, sess: security.Session) -> None:
    age = security.REMEMBER_S if sess.remember else None  # 不记住：浏览器会话 Cookie（关闭浏览器即失效）
    resp.set_cookie(security.SESSION_COOKIE, sess.token, httponly=True, samesite="strict", secure=False, path="/",
                    max_age=age)
    resp.set_cookie(security.CSRF_COOKIE, sess.csrf, httponly=False, samesite="strict", secure=False, path="/",
                    max_age=age)


@router.get("/auth/state")
def auth_state(request: Request):
    s = security.store.get(request.cookies.get(security.SESSION_COOKIE))
    return {"password_set": security.password_is_set(), "logged_in": s is not None}


@router.post("/auth/setup")
def auth_setup(body: PwBody, response: Response):
    if security.password_is_set():
        raise HTTPException(400, "密码已设置")
    security.set_password(body.password)
    sess = security.store.create()
    _set_session_cookies(response, sess)
    return {"ok": True}


@router.post("/auth/login")
def login(body: LoginBody, response: Response):
    if not security.store.login_allowed():
        raise HTTPException(429, "失败次数过多，请 1 分钟后再试")
    if not security.check_password(body.password):
        security.store.record_failure()
        raise HTTPException(401, "密码错误")
    sess = security.store.create(remember=body.remember)
    _set_session_cookies(response, sess)
    return {"ok": True, "remembered_days": security.REMEMBER_S // 86400 if body.remember else 0}


@router.post("/auth/reauth")
def reauth(body: PwBody, request: Request, sess=Mutator):
    if not security.check_password(body.password):
        security.store.record_failure()
        raise HTTPException(401, "密码错误")
    import time

    sess.reauth_at = time.time()
    return {"ok": True, "valid_seconds": security.REAUTH_WINDOW_S}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    security.store.drop(request.cookies.get(security.SESSION_COOKIE))
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    response.delete_cookie(security.CSRF_COOKIE, path="/")
    return {"ok": True, "note": "已退出登录。已获授权的后台运行不受网页会话影响，会继续按授权运行。"}


# ------------------------------------------------------------------ 系统状态
@router.get("/system/status")
def system_status(sess=Viewer):
    st = get_settings()
    from ..ai.providers import get_provider

    prov = get_provider()
    out = {"now_utc": utcnow().isoformat(), "now_myt": utcnow().astimezone(MYT).strftime("%Y-%m-%d %H:%M:%S"),
           "deployment": {"live_trading_enabled": st.live_trading_enabled,
                          "allow_client_side_stops_live": st.allow_client_side_stops_live},
           "ai": {"provider": prov.name, "label": prov.label, "model": prov.model, "is_fixture": prov.is_fixture,
                  "key_present": bool(st.anthropic_api_key)},
           "workers": {}}
    from ..services.runs import ensure_seed

    for ns in ("sim", "live"):
        ensure_seed(ns)
        with read_session(ns) as s:
            out["workers"][ns] = worker_info(s, ns)
    from ..broker.moomoo_adapter import tcp_probe

    out["opend"] = {"address": f"{st.moomoo_opend_host}:{st.moomoo_opend_port}",
                    "reachable": tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port, timeout=0.3)}
    return out


@router.get("/live/gate")
def live_gate_status(sess=Viewer):
    from ..broker.gate import gate_checks
    from ..services.runs import current_portfolio

    with read_session("live") as s:
        p = current_portfolio(s, "live")
        checks = gate_checks(s, p.id if p else None)
    return {"passed": all(c["ok"] for c in checks), "checks": checks,
            "note": "实盘交付时默认关闭。全部条件满足且您在向导中确认运行授权后，AI 才能在授权范围内自动下单。"}


# ------------------------------------------------------------------ 连接诊断
class DiagBody(BaseModel):
    acc_id: str | None = None
    trd_env: str | None = None
    symbols: list[str] = []


def _diag_path():
    return get_settings().data_dir / "diagnostics_last.json"


@router.get("/connection/status")
def connection_status(sess=Viewer):
    p = _diag_path()
    last = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    caps = {}
    for ns in ("sim", "live"):
        with read_session(ns) as s:
            caps[ns] = [row(c) for c in s.execute(select(MarketCapability)).scalars()]
            caps[ns + "_bindings"] = [row(b, ["id", "broker", "trd_env", "acc_id_masked", "markets", "confirmed_by_user",
                                              "verified_at", "active", "acc_type"])
                                      for b in s.execute(select(AccountBinding)).scalars()]
    from ..broker.diagnostics import LOGIN_STEPS

    return {"last": last, "capabilities": caps, "login_steps": LOGIN_STEPS}


@router.post("/connection/diagnose")
def diagnose(body: DiagBody, sess=Mutator):
    from ..broker.diagnostics import run_diagnostics

    res = run_diagnostics(body.acc_id, body.trd_env, body.symbols)
    res["checked_at"] = utcnow().isoformat()
    # 保存（账户 ID 脱敏后写盘）
    safe = json.loads(json.dumps(res, ensure_ascii=False))
    for a in safe.get("accounts", []):
        a["acc_id"] = mask_account(a["acc_id"])
    _diag_path().write_text(json.dumps(safe, ensure_ascii=False, indent=1), encoding="utf-8")
    ns = "live" if body.trd_env == "REAL" else "sim"
    with write_session(ns) as s:
        for it in res.get("items", []):
            cap = it.get("capability")
            mk = it.get("market", "ALL")
            if not cap:
                continue
            status = {"PASS": "LOCAL_PASSED", "NEEDS_PERMISSION": "NEEDS_PERMISSION", "UNSUPPORTED": "UNSUPPORTED"}.get(it["status"], "UNVERIFIED")
            c = s.execute(select(MarketCapability).where(MarketCapability.market == mk, MarketCapability.item == cap)).scalar_one_or_none()
            if c is None:
                c = MarketCapability(market=mk, item=cap, status=status)
                s.add(c)
            c.status, c.detail, c.source, c.checked_at = status, it["detail"][:1000], "本机只读诊断", utcnow()
        if body.acc_id and res.get("ok"):
            b = s.execute(select(AccountBinding).where(AccountBinding.acc_id == str(body.acc_id),
                                                       AccountBinding.trd_env == body.trd_env)).scalar_one_or_none()
            if b is not None:
                b.verified_at = utcnow()
                b.verification = {"items": [i["key"] + ":" + i["status"] for i in res["items"]]}
        audit(s, "CONNECTION", "DIAGNOSE", f"只读诊断完成：{'通过' if res.get('ok') else '未通过'}", actor="user")
    return res


class BindBody(BaseModel):
    acc_id: str
    trd_env: str  # SIMULATE / REAL
    markets: list[str]
    acc_type: str = ""
    confirm: bool = False


@router.post("/connection/bind")
def bind(body: BindBody, sess=Mutator):
    if not body.confirm:
        raise HTTPException(400, "请确认选择的账户")
    if body.trd_env not in ("SIMULATE", "REAL") or not body.acc_id or body.acc_id == "0":
        raise HTTPException(400, "必须明确选择账户 ID 与环境")
    last = json.loads(_diag_path().read_text(encoding="utf-8")) if _diag_path().exists() else {}
    ns = "live" if body.trd_env == "REAL" else "sim"
    if ns == "live":
        security.require_recent_reauth(sess)
    with write_session(ns) as s:
        for b in s.execute(select(AccountBinding).where(AccountBinding.trd_env == body.trd_env)).scalars():
            b.active = False
        b = AccountBinding(broker="MOOMOO", trd_env=body.trd_env, acc_id=str(body.acc_id),
                           acc_id_masked=mask_account(body.acc_id), security_firm=get_settings().moomoo_security_firm,
                           acc_type=body.acc_type, markets=body.markets, confirmed_by_user=True, confirmed_at=utcnow(),
                           verification={"from_last_diagnostics": bool(last)})
        s.add(b)
        audit(s, "CONNECTION", "BIND", f"用户选择账户 {mask_account(body.acc_id)}（{body.trd_env}，市场 {body.markets}）",
              actor="user")
    return {"ok": True, "note": "已绑定。请再运行一次只读诊断（带账户）以验证现金字段、交易权限与订单类型。"}


# ------------------------------------------------------------------ 设置与备份
@router.get("/settings")
def settings(sess=Viewer):
    st = get_settings()
    return {
        "ai": {"provider": st.ai_provider, "model": st.ai_model, "key_present": bool(st.anthropic_api_key),
               "key_hint": ("已配置（不显示内容）" if st.anthropic_api_key else "未配置：使用固定测试响应"),
               "timeout_s": st.ai_timeout_s, "max_retries": st.ai_max_retries, "monthly_call_limit": st.ai_monthly_call_limit,
               "monthly_token_limit": st.ai_monthly_token_limit, "monthly_budget_usd": st.ai_monthly_budget_usd,
               "price_input_per_mtok": st.ai_price_input_per_mtok, "price_output_per_mtok": st.ai_price_output_per_mtok,
               "price_source": st.ai_price_source, "min_interval_minutes": st.min_ai_interval_minutes},
        "deployment": {"live_trading_enabled": st.live_trading_enabled, "allow_client_side_stops_live": st.allow_client_side_stops_live,
                       "app": f"{st.app_host}:{st.app_port}", "opend": f"{st.moomoo_opend_host}:{st.moomoo_opend_port}",
                       "security_firm": st.moomoo_security_firm},
        "data_dir": str(st.data_dir),
        "how_to_change": "这些设置在项目根目录的 .env 文件中修改（参考 .env.example），修改后重新运行启动脚本。网页不能修改部署级开关与密钥。",
        "backup": "点击「立即备份」会把 sim/live 数据库用 SQLite 在线备份到 data/backups/。恢复：停止系统 → 用备份文件替换 data/ 下同名文件 → 启动。恢复后系统保持暂停，完成对账后再恢复。",
        "notifications": "首版提供站内通知与异常日志；邮件/推送未配置，不会发送。关闭页面后您可能收不到通知，无人值守实盘需另行验证告警渠道。",
    }


@router.post("/backup")
def backup(sess=Mutator):
    st = get_settings()
    out_dir = st.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    files = []
    for ns in ("sim", "live"):
        src = st.db_path(ns)
        if not src.exists():
            continue
        dst = out_dir / f"{ns}_{stamp}.db"
        with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
            a.backup(b)
        files.append(str(dst.name))
        # 恢复后保持暂停：在备份里把运行中的账本标记为暂停新开仓
        with sqlite3.connect(dst) as b:
            b.execute("UPDATE portfolio SET pause_new_entries = 1")
    return {"ok": True, "files": files, "dir": str(out_dir),
            "note": "备份中的账本已标记为「暂停新开仓」：恢复后需完成对账并手动恢复。"}


# ------------------------------------------------------------------ 实时推送（SSE）
@router.get("/{ns}/stream")
async def stream(ns: str, request: Request, sess=Viewer):
    if ns not in ("sim", "live"):
        raise HTTPException(404)

    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            with read_session(ns) as s:
                mx = s.execute(select(func.max(AuditEvent.id))).scalar() or 0
                w = worker_info(s, ns)
            sig = (mx, w.get("heartbeat_at"))
            if sig != last:
                last = sig
                yield f"data: {json.dumps({'audit_id': mx, 'heartbeat_at': w.get('heartbeat_at'), 'alive': w.get('alive')})}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
