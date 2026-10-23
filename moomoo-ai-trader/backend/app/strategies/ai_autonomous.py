"""AI 自主决策策略（主功能）。

调度器按交易日历定时（同一市场至少间隔 15 分钟，回放时按回放时钟）把池内候选与现有持仓交给 AI；
AI 只输出结构化决定，校验通过后由风控程序计算数量并自动下单，无需逐笔确认。
AI 调用在后台线程进行：实时模式下等待模型时，保护性退出照常运行，不会被阻塞。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ..ai.budget import budget_ok
from ..ai.validate import ValidationContext, parse_and_validate
from ..config import get_settings
from ..core.enums import DecisionAction, DecisionOutcome
from ..db.models import AICallLog, Decision, NewsItem
from ..db.session import read_session, write_session
from ..db.types import D
from ..market_data.features import compute_features, momentum_score
from ..portfolio import ledger
from ..workers.runtime import PendingAIJob
from .common import TickState, execute_buy, execute_sell, note, record_decision

DEFAULT_AI_PARAMS: dict[str, Any] = {
    "interval_minutes": 60,
    "max_candidates": 6,
    "max_target_position_pct": "0.10",
    "max_holding_days": 20,
    "max_valid_minutes": 60,
    "bars_lookback": 260,
    "news_lookback_days": 3,
    "event_move_pct": "0.03",
    "fixture_params": {
        "entry_min_score": 1.5, "stop_atr_mult": "2.0", "take_profit_atr_mult": "4.0", "max_holding_days": 10,
        "target_position_pct": "0.08", "reduce_gain_atr": "1.5", "reduce_fraction": "0.5",
        "exit_ret_5d_below": -0.04, "max_new_per_call": 1,
    },
}

# 候选改进允许调整的参数（白名单与边界）。不包含资金、风险额度、股票池或保护规则。
TUNABLE_PARAMS: dict[str, tuple] = {
    "fixture_params.entry_min_score": (0.5, 4.0),
    "fixture_params.stop_atr_mult": (1.0, 4.0),
    "fixture_params.take_profit_atr_mult": (1.5, 8.0),
    "fixture_params.max_holding_days": (3, 20),
    "fixture_params.reduce_gain_atr": (0.8, 4.0),
    "fixture_params.exit_ret_5d_below": (-0.10, -0.01),
    "max_candidates": (3, 10),
}


def _params(ts: TickState) -> dict:
    p = dict(DEFAULT_AI_PARAMS)
    if ts.strategy:
        p.update(ts.strategy.params or {})
    p["interval_minutes"] = max(get_settings().min_ai_interval_minutes, int(p["interval_minutes"]))
    # AI 目标仓位上限以用户确认的风控参数「单标的仓位上限」为准（策略版本里的旧值不再单独限制）
    if ts.params and ts.params.get("max_position_pct") is not None:
        p["max_target_position_pct"] = str(D(ts.params["max_position_pct"]))
    return p


def _sv_label(ts: TickState) -> str:
    return f"{ts.strategy.key}:{ts.strategy.version}" if ts.strategy else ""


# ------------------------------------------------------------------ 调度
def run(rt, ts: TickState, wall_now: datetime) -> None:
    process_finished(rt, ts, wall_now)
    check_waiting(rt, ts, wall_now)
    if not ts.auth_ok or ts.portfolio.emergency_stop:
        return
    p = _params(ts)
    now = rt.now()
    markets = sorted({rt.provider.instrument(sym).market for sym in ts.scope if rt.provider.instrument(sym)}
                     | {pos.market for pos in _positions(rt, ts)})
    held = _positions(rt, ts)
    for m in markets:
        if not rt.calendar.is_open(m, now):
            continue
        if any(j.market == m for j in rt.ai_jobs):
            continue
        last = rt.last_ai_batch.get(m)
        due = last is None or now - last >= timedelta(minutes=int(p["interval_minutes"]))
        if not due and _important_event(rt, m, held, p, now, last):
            due = True
            with write_session(rt.ns) as s:
                note(s, rt, ts, f"{m} 持仓价格大幅变动，触发一次额外 AI 复核（距上次 ≥ {get_settings().min_ai_interval_minutes} 分钟）",
                     action="AI_EVENT_REVIEW")
        if not due:
            continue
        rt.last_ai_batch[m] = now
        rt.last_ai_prices[m] = {x.symbol: rt.price_of(x.symbol) for x in held if x.market == m}
        start_batch(rt, ts, wall_now, m, p)


def _important_event(rt, market: str, held, p: dict, now, last) -> bool:
    """重要事件：持仓价格相对上次复核变动超过阈值。限频：距上次复核至少 min_ai_interval_minutes。"""
    base = rt.last_ai_prices.setdefault(market, {})
    for x in held:  # 上次复核之后才建立的持仓：以首次看到的价格为基准
        if x.market == market and base.get(x.symbol) is None:
            base[x.symbol] = rt.price_of(x.symbol)
    if last is None or now - last < timedelta(minutes=get_settings().min_ai_interval_minutes):
        return False
    thr = Decimal(str(p.get("event_move_pct", "0.03")))
    for x in held:
        if x.market != market or base.get(x.symbol) is None:
            continue
        cur = rt.price_of(x.symbol)
        if cur is not None and abs(cur / base[x.symbol] - 1) >= thr:
            return True
    return False


def _positions(rt, ts: TickState):
    with read_session(rt.ns) as s:
        return ledger.open_positions(s, ts.portfolio.id)


def build_payload(rt, ts: TickState, market: str, p: dict) -> tuple[dict, dict]:
    now = rt.now()
    ns = rt.ns
    evidence: dict[str, set[str]] = {"*": set()}
    ev_desc: list[dict] = []
    with read_session(ns) as s:
        positions = [x for x in ledger.open_positions(s, ts.portfolio.id) if x.market == market]
        held = {x.symbol for x in ledger.open_positions(s, ts.portfolio.id)}
        cands = []
        for sym in sorted(ts.scope):
            inst = rt.provider.instrument(sym)
            if inst is None or inst.market != market or sym in held:
                continue
            bars = rt.provider.daily_bars(sym, now, int(p["bars_lookback"]))
            q = rt.quote(sym)
            f = compute_features(bars, q)
            if q is None or q.last is None or len(bars) < 30:
                continue
            cands.append((momentum_score(f), sym, inst, q, f, bars))
        cands.sort(key=lambda x: -x[0])
        cands = cands[: int(p["max_candidates"])]
        ccy = {"MY": "MYR", "US": "USD"}[market]
        ev = ledger.equity_view(s, ts.portfolio.id, ccy, rt.price_of)
        acct_id = f"account:{ccy}@{now.isoformat()}"
        evidence["*"].add(acct_id)
        ev_desc.append({"id": acct_id, "type": "account", "desc": f"{ccy} 策略净值 {ev.equity:.2f}，可用 {ev.cash - ev.reserved:.2f}"})
        cand_rows = []
        for score, sym, inst, q, f, bars in cands:
            ids = [f"quote:{sym}@{q.source_ts.isoformat() if q.source_ts else 'na'}",
                   f"features:{sym}@{bars[-1].day.isoformat()}", f"bars:{sym}:{len(bars)}d"]
            evidence[sym] = set(ids)
            ev_desc += [{"id": ids[0], "type": "quote", "symbol": sym, "desc": f"报价 last={q.last} bid={q.bid} ask={q.ask}，来源 {q.source}"},
                        {"id": ids[1], "type": "features", "symbol": sym, "desc": "技术特征（已收盘日K）"},
                        {"id": ids[2], "type": "bars", "symbol": sym, "desc": f"{len(bars)} 根已收盘日K，截至 {bars[-1].day}"}]
            cand_rows.append({
                "symbol": sym, "name": inst.name, "sec_type": inst.sec_type, "currency": inst.currency,
                "lot_size": inst.lot_size, "prescore": round(score, 3),
                "quote": {"last": str(q.last), "bid": str(q.bid) if q.bid else None, "ask": str(q.ask) if q.ask else None,
                          "source": q.source, "source_ts": q.source_ts.isoformat() if q.source_ts else None},
                "features": f, "recent_closes": [str(b.close) for b in bars[-10:]], "evidence_ids": ids,
            })
        pos_rows = []
        for pos in positions:
            q = rt.quote(pos.symbol)
            bars = rt.provider.daily_bars(pos.symbol, now, int(p["bars_lookback"]))
            f = compute_features(bars, q)
            pid = f"position:{pos.symbol}@{now.isoformat()}"
            ids = [pid, f"features:{pos.symbol}@{bars[-1].day.isoformat() if bars else 'na'}"]
            if q is not None and q.source_ts:
                ids.append(f"quote:{pos.symbol}@{q.source_ts.isoformat()}")
            evidence[pos.symbol] = set(ids)
            ev_desc.append({"id": pid, "type": "position", "symbol": pos.symbol, "desc": f"持仓 {pos.qty}，成本 {pos.avg_cost:.4f}"})
            pos_rows.append({
                "symbol": pos.symbol, "qty": pos.qty, "avg_cost": str(pos.avg_cost.quantize(Decimal('0.0001'))),
                "last": str(q.last) if q and q.last is not None else None,
                "held_days": round((now - pos.opened_at).total_seconds() / 86400, 1),
                "stop_price": str(pos.stop_price) if pos.stop_price else None,
                "take_profit_price": str(pos.take_profit_price) if pos.take_profit_price else None,
                "max_hold_until": pos.max_hold_until.isoformat() if pos.max_hold_until else None,
                "reduced_before": pos.qty < pos.max_qty, "features": f, "evidence_ids": ids,
            })
        syms = [c["symbol"] for c in cand_rows] + [x["symbol"] for x in pos_rows]
        news = []
        if syms:
            for n in s.execute(select(NewsItem).where(
                    NewsItem.symbol.in_(syms), NewsItem.published_at <= now, NewsItem.fetched_at <= now,
                    NewsItem.published_at >= now - timedelta(days=int(p["news_lookback_days"])))).scalars():
                nid = f"news:{n.id}"
                evidence.setdefault(n.symbol, set()).add(nid)
                ev_desc.append({"id": nid, "type": "news", "symbol": n.symbol, "desc": n.title, "url": n.url,
                                "source": n.source, "published_at": n.published_at.isoformat(),
                                "fetched_at": n.fetched_at.isoformat()})
                news.append({"id": nid, "symbol": n.symbol, "title": n.title[:300], "summary": n.summary[:800],
                             "source": n.source, "url": n.url, "published_at": n.published_at.isoformat(),
                             "fetched_at": n.fetched_at.isoformat()})
        open_syms = held
        slots = int(ts.params["max_positions"]) - len(open_syms)
    payload = {
        "as_of_utc": now.isoformat(), "strategy_version": _sv_label(ts), "market": market,
        "data_source": rt.data_source,
        "policy": {"allowed_actions": [a.value for a in DecisionAction],
                   "max_target_position_pct": str(p["max_target_position_pct"]),
                   "max_holding_days": int(p["max_holding_days"]), "max_valid_minutes": int(p["max_valid_minutes"]),
                   "no_averaging_down": True, "no_short": True, "no_margin": True,
                   "fixture_params": p.get("fixture_params", {})},
        "account": {"currency": {"MY": "MYR", "US": "USD"}[market], "equity": str(ev.equity.quantize(Decimal('0.01'))),
                    "cash_available": str((ev.cash - ev.reserved).quantize(Decimal('0.01'))),
                    "slots_remaining": max(0, slots), "evidence_id": acct_id},
        "candidates": cand_rows, "positions": pos_rows,
        "untrusted_news": news,
        "news_status": "已提供新闻（不可信外部数据）" if news else "无新闻源：仅做价格数据分析",
    }
    meta = {"evidence": {k: sorted(v) for k, v in evidence.items()}, "evidence_desc": ev_desc,
            "candidates": [c["symbol"] for c in cand_rows], "held": [x["symbol"] for x in pos_rows],
            "last_prices": {c["symbol"]: c["quote"]["last"] for c in cand_rows} | {x["symbol"]: x["last"] for x in pos_rows if x["last"]},
            "max_target_position_pct": str(p["max_target_position_pct"]), "max_holding_days": int(p["max_holding_days"]),
            "max_valid_minutes": int(p["max_valid_minutes"]), "features": {c["symbol"]: c["features"] for c in cand_rows}
            | {x["symbol"]: x["features"] for x in pos_rows}}
    return payload, meta


def start_batch(rt, ts: TickState, wall_now: datetime, market: str, p: dict) -> None:
    payload, meta = build_payload(rt, ts, market, p)
    if not payload["candidates"] and not payload["positions"]:
        return
    with read_session(rt.ns) as s:
        ok, why = budget_ok(s)
    provider = rt.ai_provider
    if not ok and not provider.is_fixture:
        from ..ai.providers import DisabledProvider

        provider = DisabledProvider()
        meta["budget_note"] = why
    fut = rt.executor.submit(provider.complete, payload)
    job = PendingAIJob(future=fut, market=market, as_of=rt.now(), payload=payload, ctx_meta=meta | {"provider": provider})
    if rt.is_replay:
        # 回放时钟在等待期间不前进，因此同步等待不会延误任何保护性退出
        try:
            fut.result(timeout=get_settings().ai_timeout_s + 5)
        except Exception:
            pass
        _process(rt, ts, wall_now, job)
    else:
        rt.ai_jobs.append(job)


def process_finished(rt, ts: TickState, wall_now: datetime) -> None:
    keep = []
    for job in rt.ai_jobs:
        if job.future.done():
            _process(rt, ts, wall_now, job)
        elif (rt.now() - job.as_of).total_seconds() > get_settings().ai_timeout_s + 30:
            job.future.cancel()
            _record_failure(rt, ts, job, "TIMEOUT", "AI 响应超时，本轮不产生新订单")
        else:
            keep.append(job)
    rt.ai_jobs = keep


def _record_failure(rt, ts, job, status: str, why: str, result=None) -> None:
    with write_session(rt.ns) as s:
        log = _log_call(s, job, status, why, result)
        record_decision(s, rt, ts, source="AI_FIXTURE" if job.ctx_meta["provider"].is_fixture else "AI_MODEL",
                        symbol="*", action=DecisionAction.NO_TRADE, outcome=DecisionOutcome.INVALID,
                        reason=why, as_of=job.as_of, facts={"market": job.market}, ai_call_id=log.id,
                        market=job.market)
        note(s, rt, ts, f"AI 决策（{job.market}）未产生订单：{why}", action="AI_FAILED")


def _log_call(s, job, status: str, error: str, result) -> AICallLog:
    prov = job.ctx_meta["provider"]
    text = json.dumps(job.payload, ensure_ascii=False, sort_keys=True, default=str)
    log = AICallLog(provider=prov.name, model=getattr(result, "model", "") or prov.model,
                    prompt_version=prov.prompt_version, input_hash=hashlib.sha256(text.encode()).hexdigest(),
                    input_snapshot=job.payload, output_text=getattr(result, "text", "") or "", status=status,
                    error=error, latency_ms=getattr(result, "latency_ms", 0) or 0,
                    input_tokens=getattr(result, "input_tokens", 0) or 0,
                    output_tokens=getattr(result, "output_tokens", 0) or 0, as_of=job.as_of)
    prev = s.execute(select(AICallLog.model).where(AICallLog.provider == prov.name, AICallLog.status == "OK")
                     .order_by(AICallLog.id.desc()).limit(1)).scalar()
    s.add(log)
    s.flush()
    if prev and log.model and status == "OK" and prev != log.model:
        from ..core.audit import audit, notify

        audit(s, "AI", "MODEL_CHANGED", f"模型供应商返回的模型版本变化：{prev} → {log.model}（已记录，需评估）", actor="worker")
        notify(s, "warning", "AI 模型版本发生变化", f"{prev} → {log.model}。请关注复盘结果，必要时暂停。",
               dedupe_key=f"model-change:{prev}->{log.model}")
    return log


def _process(rt, ts: TickState, wall_now: datetime, job) -> None:
    try:
        result = job.future.result(timeout=0)
    except Exception as e:  # 线程内异常
        _record_failure(rt, ts, job, "ERROR", f"AI 调用异常：{e}")
        return
    if result.status != "OK":
        why = {"DISABLED": "AI 未启用或预算耗尽（" + job.ctx_meta.get("budget_note", result.error) + "）",
               "TIMEOUT": "AI 响应超时", "REFUSAL": "模型拒绝回答"}.get(result.status, f"AI 调用失败：{result.error}")
        _record_failure(rt, ts, job, result.status, why + "；本轮不产生新的 AI 订单（保护规则不受影响）", result)
        return
    meta = job.ctx_meta
    vctx = ValidationContext(
        as_of=job.as_of, strategy_version=job.payload["strategy_version"],
        candidate_symbols=set(meta["candidates"]), held_symbols=set(meta["held"]),
        evidence_ids={k: set(v) for k, v in meta["evidence"].items()},
        last_prices={k: D(v) for k, v in meta["last_prices"].items() if v is not None},
        max_target_position_pct=D(meta["max_target_position_pct"]), max_holding_days=meta["max_holding_days"],
        max_valid_minutes=meta["max_valid_minutes"], now=rt.now(),
    )
    batch = parse_and_validate(result.text, vctx)
    source = "AI_FIXTURE" if meta["provider"].is_fixture else "AI_MODEL"
    with write_session(rt.ns) as s:
        log = _log_call(s, job, "OK" if batch.ok else "INVALID", "；".join(batch.errors), result)
        if not batch.ok:
            record_decision(s, rt, ts, source=source, symbol="*", action=DecisionAction.NO_TRADE,
                            outcome=DecisionOutcome.INVALID, reason="AI 输出无效：" + "；".join(batch.errors),
                            as_of=job.as_of, ai_call_id=log.id, market=job.market)
            return
        ev_desc = {e["id"]: e for e in meta["evidence_desc"]}
        for vd in batch.decisions:
            raw = vd.raw
            inst = rt.provider.instrument(raw.symbol)
            facts = {"features": meta["features"].get(raw.symbol, {}), "last_price": meta["last_prices"].get(raw.symbol),
                     "market_view": batch.market_view, "data_source": rt.data_source,
                     "news_status": job.payload["news_status"]}
            judgment = {"rationale": raw.rationale, "risk_flags": raw.risk_flags,
                        "provider_label": meta["provider"].label, "note": "模型判断，不代表获胜概率"}
            evidence = [ev_desc[i] for i in raw.evidence_ids if i in ev_desc]
            common = dict(source=source, symbol=raw.symbol, action=raw.action, as_of=job.as_of, facts=facts,
                          model_judgment=judgment, evidence=evidence, ai_call_id=log.id,
                          market=inst.market if inst else "", currency=inst.currency if inst else "")
            if not vd.ok:
                record_decision(s, rt, ts, outcome=DecisionOutcome.INVALID, reason="；".join(vd.errors),
                                proposal=raw.model_dump(mode="json"), **common)
                continue
            prop = vd.proposal
            d = record_decision(s, rt, ts, outcome=DecisionOutcome.NO_ACTION, reason="",
                                expires_at=prop.expires_at_utc, proposal=prop.model_dump(mode="json"),
                                decision_id=prop.decision_id, **common)
            _act(s, rt, ts, wall_now, d)


def _act(s, rt, ts: TickState, wall_now: datetime, d: Decision) -> None:
    prop = d.proposal
    if d.action in (DecisionAction.HOLD, DecisionAction.NO_TRADE):
        d.outcome, d.outcome_reason = DecisionOutcome.NO_ACTION, ("继续持有" if d.action == "HOLD" else "不交易") + "：" + prop.get("rationale", "")[:200]
        return
    if not ts.auth_ok:
        d.outcome, d.outcome_reason = DecisionOutcome.RISK_BLOCKED, f"运行授权无效：{ts.auth_reason}"
        return
    if d.action == DecisionAction.SELL_PROPOSAL:
        d.outcome, d.outcome_reason = execute_sell(s, rt, ts, d, fraction=D(prop["sell_fraction"]),
                                                   min_price=D(prop["min_sell_price"]) if prop.get("min_sell_price") else None)
        return
    ec = prop.get("entry_condition") or {}
    q = rt.quote(d.symbol)
    if ec.get("type") in ("LIMIT_AT_OR_BELOW", "BREAKOUT_ABOVE"):
        px = D(ec["price"])
        met = q is not None and q.last is not None and (
            (ec["type"] == "LIMIT_AT_OR_BELOW" and q.ask is not None and q.ask <= px) or
            (ec["type"] == "BREAKOUT_ABOVE" and q.last >= px))
        if not met:
            d.outcome = DecisionOutcome.WAITING_PRICE
            d.outcome_reason = f"等待价格条件：{'卖一价 ≤ ' if ec['type'] == 'LIMIT_AT_OR_BELOW' else '最新价 ≥ '}{px}（有效至 {d.expires_at.isoformat() if d.expires_at else ''}）"
            return
    _buy(s, rt, ts, wall_now, d)


def _buy(s, rt, ts, wall_now, d: Decision) -> None:
    prop = d.proposal
    ex = prop.get("exit_condition") or {}
    ec = prop.get("entry_condition") or {}
    max_price = D(prop["max_buy_price"]) if prop.get("max_buy_price") else None
    if ec.get("type") == "LIMIT_AT_OR_BELOW" and ec.get("price"):
        max_price = min(max_price, D(ec["price"])) if max_price else D(ec["price"])
    d.outcome, d.outcome_reason = execute_buy(
        s, rt, ts, wall_now, d, stop=D(ex["stop_loss_price"]) if ex.get("stop_loss_price") else None,
        take_profit=D(ex["take_profit_price"]) if ex.get("take_profit_price") else None,
        max_hold_days=ex.get("max_holding_days"), target_pct=D(prop["target_position_pct"]) if prop.get("target_position_pct") else None,
        max_price=max_price, intent_key=f"AI:{d.id}:BUY")


def check_waiting(rt, ts: TickState, wall_now: datetime) -> None:
    now = rt.now()
    with write_session(rt.ns) as s:
        waiting = list(s.execute(select(Decision).where(Decision.portfolio_id == ts.portfolio.id,
                                                        Decision.outcome == DecisionOutcome.WAITING_PRICE)).scalars())
        for d in waiting:
            if d.expires_at is not None and now >= d.expires_at:
                d.outcome = DecisionOutcome.EXPIRED
                d.outcome_reason = (d.outcome_reason or "") + "；价格条件在有效期内未满足，已过期，未下单"
                continue
            if not ts.auth_ok:
                continue
            if d.action == DecisionAction.SELL_PROPOSAL:
                prop = d.proposal
                d.outcome, d.outcome_reason = execute_sell(
                    s, rt, ts, d, fraction=D(prop["sell_fraction"]),
                    min_price=D(prop["min_sell_price"]) if prop.get("min_sell_price") else None)
            else:
                _act(s, rt, ts, wall_now, d)
