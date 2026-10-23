"""配置类接口：风控、股票池、证券、策略、配置草稿、费用、汇率、新闻、启动向导、回测、复盘与改进。"""
from __future__ import annotations

import copy
import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from ..ai.budget import month_usage
from ..config import get_settings
from ..core.audit import audit
from ..core.calendar import get_calendar
from ..core.enums import Mode
from ..core.money import calc_fees
from ..db.models import (
    AccountBinding,
    BacktestRun,
    ConfigDraft,
    DeploymentHistory,
    EvaluationRun,
    ExperienceRecord,
    FeeSchedule,
    FxRate,
    ImprovementCandidate,
    Instrument,
    NewsItem,
    Portfolio,
    QuoteSnapshot,
    ReviewRun,
    RiskLock,
    RiskPolicyVersion,
    StrategyVersion,
    UniverseRule,
    UniverseSnapshot,
    Watchlist,
)
from ..db.session import read_session, write_session
from ..db.types import D, utcnow
from ..market_data.universe import DEFAULT_POOL_RULES, RULE_LABELS, screen
from ..risk.locks import release_lock
from ..risk.policy import HARD_LIMITS, PARAM_LABELS, SEED_RISK_PARAMS, diff_params, validate_params
from ..services.runs import authorization_summary, create_authorization, current_portfolio, ensure_seed, start_sim_run
from . import security
from .deps import Mutator, Viewer, j, ns_of, row

router = APIRouter(prefix="/api/{ns}")


def _provider(ns: str, source: str | None = None):
    from ..market_data.replay import ReplayProvider
    from ..workers.engine import fixture_dir

    cal = get_calendar()
    if source == "HISTORICAL_REPLAY":
        return ReplayProvider(get_settings().data_dir / "imports", cal, name="HISTORICAL_REPLAY", prefix="import")
    if ns == "live" or source == "MOOMOO_REALTIME":
        from ..market_data.moomoo_quotes import MoomooQuoteProvider

        return MoomooQuoteProvider.shared()
    return ReplayProvider(fixture_dir(), cal, name="DEMO_FIXTURE", prefix="demo")


def _biz_now(s, ns) -> datetime:
    from .views import business_time

    return business_time(current_portfolio(s, ns))


# ------------------------------------------------------------------ 风控
@router.get("/risk")
def get_risk(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        versions = list(s.execute(select(RiskPolicyVersion).order_by(RiskPolicyVersion.version.desc())).scalars())
        active = next((v for v in versions if v.confirmed_by_user), None)
        p = current_portfolio(s, ns)
        locks = list(s.execute(select(RiskLock).where(RiskLock.portfolio_id == (p.id if p else -1))
                               .order_by(RiskLock.id.desc()).limit(50)).scalars())
        return {"active": row(active) if active else None, "versions": [row(v) for v in versions],
                "labels": PARAM_LABELS, "hard_limits": HARD_LIMITS, "seed": SEED_RISK_PARAMS,
                "locks": [row(lk) for lk in locks],
                "note": "模拟参数仅为测试种子；实盘参数必须在实盘页面单独设置并确认，不会自动继承。"}


class RiskVersionBody(BaseModel):
    params: dict[str, Any]
    note: str = ""


def _create_risk_version(s, ns: str, params: dict, note: str) -> tuple[RiskPolicyVersion, list[dict]]:
    errs = validate_params(params)
    if errs:
        raise HTTPException(400, "；".join(errs))
    cur = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                    .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
    diff = diff_params(cur.params if cur else {}, params)
    nv = (s.execute(select(func.max(RiskPolicyVersion.version))).scalar() or 0) + 1
    v = RiskPolicyVersion(version=nv, params=params, confirmed_by_user=False, is_test_seed=False, note=note)
    s.add(v)
    s.flush()
    audit(s, "CONFIG", "RISK_DRAFT", f"新风控参数草稿 v{nv}：{len(diff)} 项变化", actor="user", data={"diff": diff})
    return v, diff


@router.post("/risk/versions")
def new_risk_version(ns: str, body: RiskVersionBody, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        v, diff = _create_risk_version(s, ns, body.params, body.note)
        return {"version": row(v), "diff": diff, "loosens": any(d["loosens"] for d in diff),
                "needs_reauth": ns == "live" or any(d["loosens"] for d in diff)}


@router.post("/risk/versions/{vid}/confirm")
def confirm_risk_version(ns: str, vid: int, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        v = s.get(RiskPolicyVersion, vid)
        if v is None or v.confirmed_by_user:
            raise HTTPException(400, "版本不存在或已确认")
        cur = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                        .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
        diff = diff_params(cur.params if cur else {}, v.params)
        if ns == "live" or any(d["loosens"] for d in diff):
            security.require_recent_reauth(sess)
        v.confirmed_by_user = True
        v.confirmed_at = utcnow()
        audit(s, "CONFIG", "RISK_CONFIRM", f"用户确认风控参数 v{v.version}（新授权起生效；当前授权仍使用其批准时的版本）",
              actor="user", data={"diff": diff})
        return {"ok": True, "version": row(v)}


class ReleaseBody(BaseModel):
    note: str


@router.post("/risk/locks/{lid}/release")
def release(ns: str, lid: int, body: ReleaseBody, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        lk = s.get(RiskLock, lid)
        if lk is None or not lk.active:
            raise HTTPException(400, "锁定不存在或已解除")
        if lk.kind == "DAILY_LOSS":
            raise HTTPException(400, "当日亏损锁定只能在马来西亚时间日界后、健康检查通过时由系统解除")
        if lk.kind == "UNKNOWN_ORDER":
            raise HTTPException(400, "请先在订单页面核对所有状态未知的订单，系统会自动解除")
        if len(body.note.strip()) < 4:
            raise HTTPException(400, "请填写复核说明")
        if ns == "live":
            security.require_recent_reauth(sess)
        release_lock(s, lk, "user", f"人工复核：{body.note}", utcnow())
    return {"ok": True}


# ------------------------------------------------------------------ 股票池
@router.get("/universe")
def get_universe(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        rules = list(s.execute(select(UniverseRule).order_by(UniverseRule.version.desc())).scalars())
        snap = s.execute(select(UniverseSnapshot).order_by(UniverseSnapshot.id.desc()).limit(1)).scalar_one_or_none()
        wl = [w.symbol for w in s.execute(select(Watchlist).where(Watchlist.in_whitelist.is_(True))).scalars()]
        return {"rules": [row(r) for r in rules], "labels": RULE_LABELS, "default_rules": DEFAULT_POOL_RULES,
                "latest_snapshot": row(snap) if snap else None, "whitelist": wl,
                "note": "成员由程序依据规则和可信数据确定并留存快照；AI 只能在快照成员中选择，无法扩大范围。"
                        + ("实盘默认范围为空，需要您批准。" if ns == "live" else "")}


class RuleBody(BaseModel):
    rules: dict[str, Any]
    note: str = ""


@router.post("/universe/rules")
def new_rule(ns: str, body: RuleBody, sess=Mutator):
    ns_of(ns)
    for k in body.rules:
        if k not in DEFAULT_POOL_RULES:
            raise HTTPException(400, f"未知规则项 {k}")
    with write_session(ns) as s:
        nv = (s.execute(select(func.max(UniverseRule.version))).scalar() or 0) + 1
        r = UniverseRule(version=nv, rules=DEFAULT_POOL_RULES | body.rules, confirmed_by_user=False, note=body.note)
        s.add(r)
        s.flush()
        audit(s, "CONFIG", "UNIVERSE_DRAFT", f"股票池规则草稿 v{nv}", actor="user")
        return row(r)


@router.post("/universe/rules/{rid}/confirm")
def confirm_rule(ns: str, rid: int, sess=Mutator):
    ns_of(ns)
    if ns == "live":
        security.require_recent_reauth(sess)
    with write_session(ns) as s:
        r = s.get(UniverseRule, rid)
        if r is None:
            raise HTTPException(404)
        r.confirmed_by_user = True
        r.confirmed_at = utcnow()
        audit(s, "CONFIG", "UNIVERSE_CONFIRM", f"用户确认股票池规则 v{r.version}（范围条件改变需要确认；范围内例行成员更新无需逐只确认）",
              actor="user")
        return row(r)


@router.get("/universe/preview")
def preview_rule(ns: str, sess=Viewer, rule_id: int | None = None, source: str | None = None):
    ns_of(ns)
    with read_session(ns) as s:
        r = s.get(UniverseRule, rule_id) if rule_id else \
            s.execute(select(UniverseRule).order_by(UniverseRule.version.desc()).limit(1)).scalar_one_or_none()
        if r is None and ns == "live":
            return {"as_of": utcnow().isoformat(), "members": [], "excluded": [], "data_source": None,
                    "rule": None, "disclaimer": "实盘默认交易范围为空：请先创建并确认股票池规则，或使用手动白名单。"}
        rules = r.rules if r else DEFAULT_POOL_RULES
        now = _biz_now(s, ns)
        p = current_portfolio(s, ns)
    prov = _provider(ns, source or (p.data_source if p else None))
    try:
        members, excluded = screen(prov, rules, now)
    except Exception as e:
        raise HTTPException(503, f"无法获取数据：{e}")
    return {"as_of": now.isoformat(), "members": members, "excluded": excluded, "data_source": prov.name,
            "rule": row(r) if r else None,
            "disclaimer": "演示证券为虚构，不构成投资推荐。" if prov.name == "DEMO_FIXTURE" else ""}


# ------------------------------------------------------------------ 证券 / 自选
@router.get("/instruments")
def search_instruments(ns: str, sess=Viewer, q: str = "", market: str | None = None):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
    prov = _provider(ns, p.data_source if p else None)
    out = []
    for i in prov.instruments():
        if market and i.market != market:
            continue
        if q and q.lower() not in i.symbol.lower() and q not in i.name:
            continue
        out.append({"symbol": i.symbol, "name": i.name, "market": i.market, "sec_type": i.sec_type, "currency": i.currency,
                    "lot_size": i.lot_size, "is_demo": i.is_demo, "is_leveraged": i.is_leveraged})
    note = ""
    if ns == "live" and not out:
        note = "实盘证券搜索需要 OpenD 连接；也可以直接输入代码（如 US.AAPL）并在添加时核对名称与类型。"
    return {"items": out, "data_source": prov.name, "note": note}


@router.get("/instruments/{symbol}")
def instrument_detail(ns: str, symbol: str, sess=Viewer, days: int = 250):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        now = _biz_now(s, ns)
        snap = s.execute(select(QuoteSnapshot).where(QuoteSnapshot.symbol == symbol)).scalar_one_or_none()
    prov = _provider(ns, p.data_source if p else None)
    inst = prov.instrument(symbol)
    try:
        bars = prov.daily_bars(symbol, now, days)
        q = prov.quote(symbol, now)
    except Exception as e:
        bars, q = [], None
        err = str(e)
    else:
        err = ""
    from ..market_data.features import compute_features

    quote = None
    if q is not None:
        quote = j({"last": q.last, "bid": q.bid, "ask": q.ask, "volume": q.volume, "prev_close": q.prev_close,
                   "source": q.source, "is_realtime": q.is_realtime, "source_ts": q.source_ts, "recv_ts": q.recv_ts,
                   "suspended": q.suspended, "note": q.note,
                   "delay_seconds": int((now - q.source_ts).total_seconds()) if q.source_ts else None,
                   "spread_bps": int((q.ask - q.bid) / ((q.ask + q.bid) / 2) * 10000) if q.bid and q.ask else None})
    return {"symbol": symbol, "instrument": inst.__dict__ if inst else None, "quote": quote,
            "bars": [j({"day": b.day.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                        "volume": b.volume, "adj_close": b.adj_close}) for b in bars],
            "features": compute_features(bars, q) if bars else {}, "data_source": prov.name, "error": err,
            "last_snapshot": row(snap) if snap else None, "as_of": now.isoformat(),
            "disclaimer": "演示证券为虚构，价格为确定性生成数据，不构成投资推荐。" if inst and inst.is_demo else ""}


class WatchBody(BaseModel):
    symbol: str
    in_whitelist: bool = False
    note: str = ""


@router.get("/watchlist")
def get_watchlist(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        items = [row(w) for w in s.execute(select(Watchlist).order_by(Watchlist.added_at)).scalars()]
        for it in items:
            q = s.execute(select(QuoteSnapshot).where(QuoteSnapshot.symbol == it["symbol"])).scalar_one_or_none()
            it["quote"] = row(q) if q else None
        return {"items": items}


@router.post("/watchlist")
def add_watch(ns: str, body: WatchBody, sess=Mutator):
    ns_of(ns)
    sym = body.symbol.strip().upper()
    if not re.fullmatch(r"(MY|US)\.[A-Z0-9.\-]{1,12}", sym):
        raise HTTPException(400, "代码格式应为 市场.代码，例如 US.AAPL 或 MY.1155")
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
    prov = _provider(ns, p.data_source if p else None)
    inst = prov.instrument(sym)
    if inst is None and prov.name == "MOOMOO_REALTIME":
        try:  # 用一次只读快照核对名称与交易单位
            prov.quote(sym, utcnow())
            inst = prov.instrument(sym)
        except Exception:
            inst = None
    if inst is None and ns == "sim" and prov.name != "MOOMOO_REALTIME":
        raise HTTPException(400, "当前数据源中没有这只证券")
    with write_session(ns) as s:
        w = s.execute(select(Watchlist).where(Watchlist.symbol == sym)).scalar_one_or_none()
        if w is None:
            w = Watchlist(symbol=sym)
            s.add(w)
        w.in_whitelist = body.in_whitelist
        w.note = body.note
        if inst is not None and not s.execute(select(Instrument.id).where(Instrument.symbol == sym)).first():
            s.add(Instrument(symbol=sym, market=inst.market, name=inst.name, sec_type=inst.sec_type, currency=inst.currency,
                             lot_size=inst.lot_size, is_demo=inst.is_demo, is_leveraged=inst.is_leveraged, source=prov.name))
        audit(s, "CONFIG", "WATCHLIST", f"自选 {sym}{'（加入白名单）' if body.in_whitelist else ''}", actor="user", symbol=sym)
    return {"ok": True, "instrument": inst.__dict__ if inst else None,
            "verify_note": "请核对名称与证券类型" if inst else "未能从数据源核对名称与类型，加入白名单前请先连接行情核对"}


@router.delete("/watchlist/{symbol}")
def del_watch(ns: str, symbol: str, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        w = s.execute(select(Watchlist).where(Watchlist.symbol == symbol)).scalar_one_or_none()
        if w:
            s.delete(w)
            audit(s, "CONFIG", "WATCHLIST_REMOVE", f"移除自选 {symbol}", actor="user", symbol=symbol)
    return {"ok": True}


# ------------------------------------------------------------------ 策略
@router.get("/strategies")
def strategies(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        return {"items": [row(v) for v in s.execute(select(StrategyVersion).order_by(StrategyVersion.id)).scalars()],
                "deployments": [row(d) for d in s.execute(select(DeploymentHistory).order_by(DeploymentHistory.id.desc())).scalars()]}


# ------------------------------------------------------------------ 中文配置草稿
_PCT = r"([0-9]+(?:\.[0-9]+)?)\s*%"
_PATTERNS = [
    (rf"(单笔|计划)风险.*?{_PCT}", "risk", "risk_per_trade_pct", "pct"),
    (rf"(单只|单标的|单个).*?{_PCT}", "risk", "max_position_pct", "pct"),
    (rf"(总投入|总仓位|总额度).*?{_PCT}", "risk", "max_total_exposure_pct", "pct"),
    (r"最多(持有)?\s*([0-9]+)\s*(只|个)", "risk", "max_positions", "int"),
    (r"(每天|每日|当日).*?最多\s*([0-9]+)\s*(笔|个|次)", "risk", "max_daily_new_orders", "int"),
    (rf"(日亏损|当日亏损|每日亏损).*?{_PCT}", "risk", "daily_loss_pct", "pct"),
    (rf"回撤.*?{_PCT}", "risk", "max_drawdown_pct", "pct"),
]


def parse_request(text: str, risk: dict, rules: dict) -> tuple[dict, dict, list[str]]:
    r2, u2 = copy.deepcopy(risk), copy.deepcopy(rules)
    hits = []
    for pat, target, key, kind in _PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        val = m.group(m.lastindex if kind == "pct" else 2)
        if kind == "pct":
            r2[key] = str((Decimal(val) / 100).normalize())
        else:
            r2[key] = int(val)
        hits.append(key)
    for sym in re.findall(r"排除\s*((?:MY|US)\.[A-Za-z0-9]+)", text):
        u2["exclude_symbols"] = sorted(set(u2.get("exclude_symbols", [])) | {sym.upper()})
        hits.append("exclude_symbols")
    if "只做美股" in text:
        u2["markets"] = ["US"]
        hits.append("markets")
    if "只做马股" in text:
        u2["markets"] = ["MY"]
        hits.append("markets")
    return r2, u2, hits


class DraftBody(BaseModel):
    text: str


@router.post("/drafts")
def create_draft(ns: str, body: DraftBody, sess=Mutator):
    """把中文设置要求转成草稿（只生成草稿，必须由用户确认后才保存）。"""
    ns_of(ns)
    with write_session(ns) as s:
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
        ur = s.execute(select(UniverseRule).order_by(UniverseRule.version.desc()).limit(1)).scalar_one_or_none()
        risk = rp.params if rp else dict(SEED_RISK_PARAMS)
        rules = ur.rules if ur else dict(DEFAULT_POOL_RULES)
        r2, u2, hits = parse_request(body.text, risk, rules)
        if not hits:
            raise HTTPException(400, "没有识别到可修改的设置。示例：「单笔风险改成 0.2%」「最多持有 2 只」「排除 US.DMOB」「只做马股」")
        diff = diff_params(risk, r2) + [{"key": f"universe.{k}", "label": RULE_LABELS.get(k, k), "before": rules.get(k),
                                         "after": u2.get(k), "loosens": False} for k in u2 if u2.get(k) != rules.get(k)]
        target = "risk" if r2 != risk else "universe"
        d = ConfigDraft(target=target, request_text=body.text, before={"risk": risk, "universe": rules},
                        after={"risk": r2, "universe": u2}, diff=diff, loosens_risk=any(x["loosens"] for x in diff))
        s.add(d)
        s.flush()
        audit(s, "CONFIG", "DRAFT", f"根据中文要求生成配置草稿 #{d.id}：{body.text[:80]}", actor="user")
        return row(d)


@router.get("/drafts")
def list_drafts(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        return {"items": [row(d) for d in s.execute(select(ConfigDraft).order_by(ConfigDraft.id.desc()).limit(50)).scalars()]}


@router.post("/drafts/{did}/{action}")
def decide_draft(ns: str, did: int, action: str, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        d = s.get(ConfigDraft, did)
        if d is None or d.status != "DRAFT":
            raise HTTPException(400, "草稿不存在或已处理")
        if action == "discard":
            d.status, d.decided_at = "DISCARDED", utcnow()
            return {"ok": True}
        if action != "confirm":
            raise HTTPException(404)
        if d.loosens_risk or ns == "live":
            security.require_recent_reauth(sess)
        out = {}
        if d.after["risk"] != d.before["risk"]:
            v, diff = _create_risk_version(s, ns, d.after["risk"], f"来自草稿 #{d.id}")
            v.confirmed_by_user, v.confirmed_at = True, utcnow()
            out["risk_version"] = v.version
        if d.after["universe"] != d.before["universe"]:
            nv = (s.execute(select(func.max(UniverseRule.version))).scalar() or 0) + 1
            r = UniverseRule(version=nv, rules=d.after["universe"], confirmed_by_user=True, confirmed_at=utcnow(),
                             note=f"来自草稿 #{d.id}")
            s.add(r)
            out["universe_version"] = nv
        d.status, d.decided_at = "CONFIRMED", utcnow()
        audit(s, "CONFIG", "DRAFT_CONFIRM", f"用户确认配置草稿 #{d.id}（新授权起生效）", actor="user", data=out)
        return {"ok": True, **out, "note": "新参数从下一次运行授权起生效；当前授权仍使用批准时的版本。"}


# ------------------------------------------------------------------ 费用 / 汇率 / 新闻
@router.get("/fees")
def get_fees(ns: str, sess=Viewer):
    ns_of(ns)
    st = get_settings()
    with read_session(ns) as s:
        return {"items": [row(f) for f in s.execute(select(FeeSchedule).order_by(FeeSchedule.id.desc())).scalars()],
                "ai_pricing": {"input_per_mtok_usd": st.ai_price_input_per_mtok, "output_per_mtok_usd": st.ai_price_output_per_mtok,
                               "source": st.ai_price_source, "model": st.ai_model},
                "cost_items": ["券商佣金、平台费、清算费、印花税/监管费（按 moomoo 官网当前收费核对）",
                               "行情订阅费（如需要更高级别行情）", "模型 API 费用（按 Anthropic 官方价目，按量计费；与 Claude 聊天订阅无关）",
                               "服务器费用（首版在本机运行，无此项）"]}


class FeeBody(BaseModel):
    market: str
    items: list[dict]
    source: str
    verified_against_official: bool = False


@router.post("/fees")
def set_fees(ns: str, body: FeeBody, sess=Mutator):
    ns_of(ns)
    if body.market not in ("MY", "US"):
        raise HTTPException(400, "市场无效")
    if ns == "live":
        security.require_recent_reauth(sess)
    with write_session(ns) as s:
        for f in s.execute(select(FeeSchedule).where(FeeSchedule.market == body.market, FeeSchedule.active.is_(True))).scalars():
            f.active = False
        label = f"{body.market} 费用表（{'用户已核对官方费率' if body.verified_against_official else '测试/未核对'}）"
        s.add(FeeSchedule(market=body.market, params={"label": label, "items": body.items}, source=body.source,
                          as_of=utcnow(), is_test=not body.verified_against_official))
        audit(s, "CONFIG", "FEES", f"更新 {body.market} 费用表：{body.source}", actor="user")
    return {"ok": True}


class FxBody(BaseModel):
    pair: str = "USDMYR"
    rate: str
    source: str


@router.post("/fx")
def set_fx(ns: str, body: FxBody, sess=Mutator):
    ns_of(ns)
    with write_session(ns) as s:
        s.add(FxRate(pair=body.pair, rate=D(body.rate), source=body.source, as_of=utcnow()))
    return {"ok": True}


class NewsBody(BaseModel):
    items: list[dict]


@router.post("/news/import")
def import_news(ns: str, body: NewsBody, sess=Mutator):
    """导入新闻（作为不可信外部数据进入 AI 输入）。必须有来源链接与发布时间。"""
    ns_of(ns)
    n = 0
    with write_session(ns) as s:
        for it in body.items:
            for k in ("symbol", "title", "url", "source", "published_at"):
                if not it.get(k):
                    raise HTTPException(400, f"新闻缺少 {k}")
            s.add(NewsItem(symbol=it["symbol"], title=it["title"], summary=it.get("summary", ""), url=it["url"],
                           source=it["source"], published_at=datetime.fromisoformat(it["published_at"]),
                           fetched_at=datetime.fromisoformat(it.get("fetched_at") or it["published_at"])))
            n += 1
    return {"ok": True, "imported": n}


# ------------------------------------------------------------------ 历史数据导入（LOCAL_PAPER 历史回放）
class ImportBody(BaseModel):
    instruments: list[dict]
    daily_csv: str
    note: str = ""


@router.post("/imports")
def import_history(ns: str, body: ImportBody, sess=Mutator):
    if ns != "sim":
        raise HTTPException(400, "历史数据只用于模拟")
    d = get_settings().data_dir / "imports"
    d.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(io.StringIO(body.daily_csv)))
    need = {"symbol", "date", "open", "high", "low", "close", "volume"}
    if not rows or not need <= set(rows[0].keys()):
        raise HTTPException(400, f"CSV 需要列：{sorted(need)}")
    for i in body.instruments:
        for k in ("symbol", "market", "name", "sec_type", "currency", "lot_size"):
            if k not in i:
                raise HTTPException(400, f"证券资料缺少 {k}")
    (d / "import_daily.csv").write_text(body.daily_csv, encoding="utf-8")
    (d / "import_instruments.json").write_text(json.dumps(body.instruments, ensure_ascii=False), encoding="utf-8")
    first = min(r["date"] for r in rows)
    (d / "import_meta.json").write_text(json.dumps({"disclaimer": "用户导入/获授权的历史数据回放（非实时）",
                                                    "replay_start": (datetime.fromisoformat(first) + timedelta(days=120)).isoformat() + "+00:00",
                                                    "note": body.note}, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "rows": len(rows)}


# ------------------------------------------------------------------ 启动向导
class WizardBody(BaseModel):
    mode: str = "DEMO"
    capital: dict[str, str]
    max_planned_loss: dict[str, str]
    scope_type: str = "POOL"
    symbols: list[str] = []
    universe_rule_id: int | None = None
    overnight: bool = True
    days: float = 30
    strategy_key: str = "AI_AUTONOMOUS"
    confirm: bool = False


def _fee_impact(s, capital: dict[str, str], params: dict) -> list[dict]:
    out = []
    fees = {f.market: f.params for f in s.execute(select(FeeSchedule).where(FeeSchedule.active.is_(True))).scalars()}
    for ccy, amt in capital.items():
        market = "MY" if ccy == "MYR" else "US"
        cap = D(amt) * D(params["max_position_pct"])
        sched = fees.get(market)
        if not sched or cap <= 0:
            continue
        price = Decimal("2.00") if market == "MY" else Decimal("50")
        lot = 100 if market == "MY" else 1
        qty = int(cap / price) // lot * lot
        if qty <= 0:
            out.append({"currency": ccy, "max_trade": str(cap.quantize(Decimal('0.01'))),
                        "note": f"单标的上限约 {cap:.2f} {ccy}，不足以买入一手（示例：价格 {price}、每手 {lot} 股）。资金过小将几乎无法交易。"})
            continue
        rt = (calc_fees(sched, "BUY", qty, price) or 0) + (calc_fees(sched, "SELL", qty, price) or 0)
        out.append({"currency": ccy, "max_trade": str((qty * price).quantize(Decimal('0.01'))),
                    "round_trip_fee": str(rt), "fee_pct": str((rt / (qty * price) * 100).quantize(Decimal('0.01'))),
                    "note": f"示例：一笔约 {qty * price:.2f} {ccy} 的交易，买卖来回费用约 {rt} {ccy}（{rt / (qty * price) * 100:.2f}%），"
                            f"价格需要先上涨这么多才能打平。{sched.get('label', '')}"})
    return out


@router.post("/wizard/preview")
def wizard_preview(ns: str, body: WizardBody, sess=Viewer):
    ns_of(ns)
    if Mode(body.mode).namespace.value != ns:
        raise HTTPException(400, "模式与页面不符")
    ensure_seed(ns)
    with read_session(ns) as s:
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == body.strategy_key,
                                                     StrategyVersion.status == "ACTIVE")).scalar_one_or_none()
        rule = s.get(UniverseRule, body.universe_rule_id) if body.universe_rule_id else \
            s.execute(select(UniverseRule).order_by(UniverseRule.version.desc()).limit(1)).scalar_one_or_none()
        problems = []
        if rp is None:
            problems.append("尚未确认风控参数（实盘必须单独设置，不会继承模拟参数）")
        if sv is None:
            problems.append("策略版本不可用")
        scope = {"type": "WHITELIST", "symbols": body.symbols} if body.scope_type == "WHITELIST" else \
            {"type": "POOL", "universe_rule_id": rule.id if rule else None}
        if body.scope_type == "POOL" and rule is None:
            problems.append("没有股票池规则（实盘默认范围为空，请先设置并确认）")
        summary = ""
        if rp and sv:
            from ..db.models import Authorization

            a = Authorization(capital=body.capital, max_planned_loss=body.max_planned_loss, overnight=body.overnight,
                              mode=body.mode, expires_at=utcnow() + timedelta(days=body.days))
            summary = authorization_summary(a, sv, rp, scope)
        gate = None
        if ns == "live":
            from ..broker.gate import gate_checks

            gate = gate_checks(s, None)
        return {"summary": summary, "problems": problems, "fee_impact": _fee_impact(s, body.capital, rp.params) if rp else [],
                "risk_params": rp.params if rp else None, "pool_rule": row(rule) if rule else None,
                "pool_rule_confirmed": bool(rule and rule.confirmed_by_user), "gate": gate,
                "warning": "停止线（可承受亏损）不保证最大实际亏损：跳空、停牌或无法成交时亏损可能更大。"
                           "所有数字只是您的设定，不代表收益预期。"}


@router.post("/wizard/start")
def wizard_start(ns: str, body: WizardBody, sess=Mutator):
    ns_of(ns)
    if not body.confirm:
        raise HTTPException(400, "请先阅读授权摘要并确认")
    mode = Mode(body.mode)
    if mode.namespace.value != ns:
        raise HTTPException(400, "模式与页面不符")
    scope = {"type": "WHITELIST", "symbols": body.symbols} if body.scope_type == "WHITELIST" else None
    if ns == "sim":
        if mode == Mode.BROKER_PAPER:
            return _start_broker_paper(body, scope)
        source = "DEMO_FIXTURE"
        if mode == Mode.LOCAL_PAPER:
            from ..broker.moomoo_adapter import tcp_probe

            st = get_settings()
            if (get_settings().data_dir / "imports" / "import_daily.csv").exists():
                source = "HISTORICAL_REPLAY"
            elif tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port):
                source = "MOOMOO_REALTIME"
            else:
                raise HTTPException(400, "本地纸上交易需要已连接的 moomoo 行情，或先导入获授权的历史数据（历史回放）")
        try:
            pid, aid = start_sim_run(mode=mode.value, data_source=source, capital=body.capital,
                                     max_planned_loss=body.max_planned_loss, scope=scope, strategy_key=body.strategy_key,
                                     overnight=body.overnight, valid_hours=body.days * 24, actor="user",
                                     confirm_pool_rule=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "portfolio_id": pid, "authorization_id": aid, "data_source": source}
    # ---- 实盘
    security.require_recent_reauth(sess)
    from ..broker.gate import gate_checks

    with write_session(ns) as s:
        fails = [c for c in gate_checks(s, None) if not c["ok"]]
        if fails:
            raise HTTPException(400, "实盘条件未满足：" + "；".join(f"{c['name']}：{c['detail']}" for c in fails))
        b = s.execute(select(AccountBinding).where(AccountBinding.trd_env == "REAL", AccountBinding.active.is_(True))
                      .order_by(AccountBinding.id.desc()).limit(1)).scalar_one()
        p = s.execute(select(Portfolio).where(Portfolio.account_binding_id == b.id, Portfolio.mode == mode.value)
                      .order_by(Portfolio.id.desc()).limit(1)).scalar_one_or_none()
        if p is None:
            p = Portfolio(mode=mode.value, account_binding_id=b.id, name=f"{mode.value} {b.acc_id_masked}",
                          data_source="MOOMOO_REALTIME", running=True)
            s.add(p)
            s.flush()
        p.running = True
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True),
                                                       RiskPolicyVersion.is_test_seed.is_(False))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one()
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == body.strategy_key,
                                                     StrategyVersion.status == "ACTIVE")).scalar_one()
        rule = s.get(UniverseRule, body.universe_rule_id) if body.universe_rule_id else None
        if scope is None and (rule is None or not rule.confirmed_by_user):
            raise HTTPException(400, "实盘交易范围必须是您已确认的股票池规则或手动白名单（默认范围为空）")
        sc = scope or {"type": "POOL", "universe_rule_id": rule.id}
        try:
            a = create_authorization(s, portfolio=p, capital=body.capital, max_planned_loss=body.max_planned_loss,
                                     scope=sc, strategy_version_id=sv.id, risk_policy_version_id=rp.id,
                                     overnight=body.overnight, valid_hours=body.days * 24, exit_rules=None,
                                     actor="user", as_of=utcnow())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "portfolio_id": p.id, "authorization_id": a.id}


def _start_broker_paper(body: WizardBody, scope):
    from ..broker.moomoo_adapter import tcp_probe

    st = get_settings()
    with write_session("sim") as s:
        b = s.execute(select(AccountBinding).where(AccountBinding.trd_env == "SIMULATE", AccountBinding.active.is_(True),
                                                   AccountBinding.confirmed_by_user.is_(True))
                      .order_by(AccountBinding.id.desc()).limit(1)).scalar_one_or_none()
        if b is None or not tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port):
            raise HTTPException(400, "券商模拟需要先在「连接与权限诊断」中检测通过并选择券商模拟账户")
        for old in s.execute(select(Portfolio).where(Portfolio.running.is_(True), Portfolio.mode == "BROKER_PAPER")).scalars():
            old.running = False
        p = Portfolio(mode="BROKER_PAPER", account_binding_id=b.id, name="券商模拟", data_source="MOOMOO_REALTIME", running=True)
        s.add(p)
        s.flush()
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one()
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == body.strategy_key,
                                                     StrategyVersion.status == "ACTIVE")).scalar_one()
        rule = s.execute(select(UniverseRule).where(UniverseRule.confirmed_by_user.is_(True))
                         .order_by(UniverseRule.version.desc()).limit(1)).scalar_one_or_none()
        sc = scope or {"type": "POOL", "universe_rule_id": rule.id if rule else None}
        try:
            a = create_authorization(s, portfolio=p, capital=body.capital, max_planned_loss=body.max_planned_loss, scope=sc,
                                     strategy_version_id=sv.id, risk_policy_version_id=rp.id, overnight=body.overnight,
                                     valid_hours=body.days * 24, exit_rules=None, actor="user", as_of=utcnow())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "portfolio_id": p.id, "authorization_id": a.id, "data_source": "MOOMOO_REALTIME"}


# ------------------------------------------------------------------ 回测
class BacktestBody(BaseModel):
    strategy_key: str = "BASELINE_MA"
    start: str = "2025-06-02"
    end: str = "2026-10-20"
    capital: dict[str, str] = {"MYR": "20000", "USD": "5000"}
    slippage_bps: int = 10
    entry_premium_bps: int = 30
    data_source: str = "DEMO_FIXTURE"
    symbols: list[str] = []
    kind: str = "BACKTEST"


@router.post("/backtests")
def run_bt(ns: str, body: BacktestBody, sess=Mutator):
    if ns != "sim":
        raise HTTPException(400, "回测属于模拟功能")
    from ..simulation.backtest import run_backtest

    prov = _provider(ns, body.data_source)
    with read_session(ns) as s:
        fees = {f.market: f.params for f in s.execute(select(FeeSchedule).where(FeeSchedule.active.is_(True))).scalars()}
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one()
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == ("BASELINE_MA" if body.strategy_key == "BASELINE_MA" else "AI_AUTONOMOUS"),
                                                     StrategyVersion.status == "ACTIVE")).scalar_one()
    syms = body.symbols or [i.symbol for i in prov.instruments() if not i.is_leveraged]
    res = run_backtest(prov, get_calendar(), symbols=syms, start=date.fromisoformat(body.start), end=date.fromisoformat(body.end),
                       capital={k: D(v) for k, v in body.capital.items()}, fee_schedules=fees,
                       strategy_key="BASELINE_MA" if body.strategy_key == "BASELINE_MA" else "AI_FIXTURE",
                       strategy_params=sv.params, risk_params=rp.params, slippage_bps=body.slippage_bps,
                       entry_premium_bps=body.entry_premium_bps)
    res.metrics["label"] = ("【模拟回测｜基准均线策略】" if body.strategy_key == "BASELINE_MA" else
                            "【模拟回测｜固定规则模拟的 AI 输出（非真实 AI 表现）】")
    res.metrics["data_source"] = prov.name
    res.metrics["leakage_note"] = ("真实模型可能已知历史事件，历史回放不能作为无泄漏的收益证明；AI 策略主要通过带真实时间戳的前向模拟评估。"
                                   if body.strategy_key != "BASELINE_MA" else "")
    with write_session(ns) as s:
        r = BacktestRun(kind=body.kind, strategy_key=body.strategy_key, params=body.model_dump(), result=res.metrics,
                        trades=res.trades, equity_curve=res.equity_curve)
        s.add(r)
        s.flush()
        return {"id": r.id, "metrics": res.metrics}


@router.get("/backtests")
def list_bt(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        return {"items": [row(r, ["id", "kind", "strategy_key", "params", "result", "created_at_utc"])
                          for r in s.execute(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(30)).scalars()]}


@router.get("/backtests/{bid}")
def get_bt(ns: str, bid: int, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        r = s.get(BacktestRun, bid)
        if r is None:
            raise HTTPException(404)
        return row(r)


@router.get("/backtests/{bid}/{kind}.csv")
def bt_csv(ns: str, bid: int, kind: str, sess=Viewer):
    ns_of(ns)
    from .routes_ns import _csv

    with read_session(ns) as s:
        r = s.get(BacktestRun, bid)
        if r is None:
            raise HTTPException(404)
        rows = r.trades if kind == "trades" else r.equity_curve
        rows = [x | {"标记": "模拟回测"} for x in rows]
        return _csv(rows, f"backtest_{bid}_{kind}.csv")


@router.get("/backtests/{bid}/report.txt", response_class=PlainTextResponse)
def bt_report(ns: str, bid: int, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        r = s.get(BacktestRun, bid)
        if r is None:
            raise HTTPException(404)
        m = r.result
        lines = [f"{m.get('label', '【模拟回测】')} 复盘报告 #{r.id}", f"样本：{r.params.get('start')} 至 {r.params.get('end')}，"
                 f"数据来源 {m.get('data_source')}，交易日 {m.get('sample_days')}", ""]
        for c, v in m.get("per_currency", {}).items():
            lines.append(f"{c}：初始 {v['initial']} → 期末 {v['final_equity']}，净收益 {v['net_return_pct']}%，最大回撤 "
                         f"{v['max_drawdown_pct']}%，等权买入持有基准 {v['benchmark_buy_hold_equal_weight_pct']}%，现金基准 0%；"
                         f"完成交易 {v['round_trips']} 笔，已实现盈亏 {v['net_pnl']}，盈亏比 {v['payoff_ratio']}，"
                         f"盈利因子 {v['profit_factor']}，费用 {v['fees']}（占毛盈亏 {m['fee_share_of_gross_pct'].get(c, '—')}%）")
        lines += [f"全部完成交易 {m['round_trips']} 笔，胜率 {m['win_rate_pct']}%，平均持有 {m['avg_holding_days']} 天（不同币种的金额不相加）",
                  f"样本提示：{m['sample_warning']}", m.get("leakage_note", ""), "", "撮合与数据假设："]
        lines += [f"- {a}" for a in m.get("assumptions", [])]
        lines.append("\n本报告为模拟结果，不代表真实交易收益，也不构成投资建议。")
        return "\n".join(lines)


# ------------------------------------------------------------------ 复盘与改进
@router.get("/learning")
def learning(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        pid = p.id if p else -1
        exps = dict(s.execute(select(ExperienceRecord.kind, func.count()).where(ExperienceRecord.portfolio_id == pid)
                              .group_by(ExperienceRecord.kind)).all())
        classes = dict(s.execute(select(ExperienceRecord.outcome_class, func.count()).where(
            ExperienceRecord.portfolio_id == pid, ExperienceRecord.kind == "CLOSED_TRADE").group_by(ExperienceRecord.outcome_class)).all())
        active = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS",
                                                         StrategyVersion.status == "ACTIVE")).scalar_one_or_none()
        cands = list(s.execute(select(ImprovementCandidate).order_by(ImprovementCandidate.id.desc()).limit(100)).scalars())
        evals = list(s.execute(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(50)).scalars())
        weekly = list(s.execute(select(ReviewRun).where(ReviewRun.portfolio_id == pid, ReviewRun.kind == "WEEKLY")
                                .order_by(ReviewRun.id.desc()).limit(10)).scalars())
        return {"data_source": p.data_source if p else None, "mode": p.mode if p else None,
                "experience_counts": exps, "trade_classes": classes, "active_version": row(active) if active else None,
                "candidates": [row(c) for c in cands],
                "evaluations": [row(e, ["id", "candidate_id", "window_start", "window_end", "verdict", "notes", "created_at_utc"]) for e in evals],
                "deployments": [row(d) for d in s.execute(select(DeploymentHistory).order_by(DeploymentHistory.id.desc())).scalars()],
                "weekly": [row(w) for w in weekly], "tried_total": len(cands),
                "failed_total": len([c for c in cands if c.status in ("REJECTED", "INSUFFICIENT_EVIDENCE")]),
                "note": "学习 = 积累可检索经验 + 提出并验证政策改进，不是训练模型权重；候选不会自动上线。模拟、回放与实盘经验分开统计。"}


class ApproveBody(BaseModel):
    note: str = ""


@router.post("/learning/candidates/{cid}/approve")
def approve_candidate(ns: str, cid: int, body: ApproveBody, sess=Mutator):
    ns_of(ns)
    security.require_recent_reauth(sess)
    from ..learning.evaluation import approve_and_activate

    with write_session(ns) as s:
        try:
            v = approve_and_activate(s, cid, "user", body.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "active_version": row(v)}


class RollbackBody(BaseModel):
    key: str = "AI_AUTONOMOUS"
    note: str = ""


@router.post("/learning/rollback")
def do_rollback(ns: str, body: RollbackBody, sess=Mutator):
    ns_of(ns)
    from ..learning.evaluation import rollback

    with write_session(ns) as s:
        try:
            v = rollback(s, body.key, "user", body.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "active_version": row(v)}


@router.get("/reviews")
def reviews(ns: str, sess=Viewer, kind: str | None = None, limit: int = 60):
    ns_of(ns)
    with read_session(ns) as s:
        p = current_portfolio(s, ns)
        q = select(ReviewRun).where(ReviewRun.portfolio_id == (p.id if p else -1))
        if kind:
            q = q.where(ReviewRun.kind == kind)
        return {"items": [row(r) for r in s.execute(q.order_by(ReviewRun.period_start.desc()).limit(limit)).scalars()]}


@router.get("/reviews/{rid}.txt", response_class=PlainTextResponse)
def review_txt(ns: str, rid: int, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        r = s.get(ReviewRun, rid)
        if r is None:
            raise HTTPException(404)
        return r.summary


@router.get("/ai/usage")
def ai_usage(ns: str, sess=Viewer):
    ns_of(ns)
    with read_session(ns) as s:
        return month_usage(s)
