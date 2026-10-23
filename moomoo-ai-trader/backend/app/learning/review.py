"""每日中文总结与每周改进分析。"""
from __future__ import annotations

import copy
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.audit import audit, notify
from ..core.calendar import MYT
from ..core.enums import CandidateStatus
from ..db.models import (
    DailyEquity,
    Decision,
    ExperienceRecord,
    ImprovementCandidate,
    OrderIntent,
    Portfolio,
    Position,
    PositionLedger,
    ReviewRun,
    RiskLock,
    StrategyVersion,
)
from ..db.types import D
from . import experience

SOURCE_LABEL = {
    "DEMO_FIXTURE": "模拟·演示数据（虚构证券）",
    "HISTORICAL_REPLAY": "模拟·历史回放",
    "MOOMOO_REALTIME": "实时行情",
}
OUTCOME_CN = {"AUTO_EXECUTED": "已自动执行", "WAITING_PRICE": "等待价格条件", "RISK_BLOCKED": "被风控拦截",
              "NO_ACTION": "无需交易", "INVALID": "无效输出", "EXPIRED": "过期", "AWAITING_APPROVAL": "待确认"}

# 预先设定的评价门槛（写入每个候选，先冻结再评估）
EVAL_PLAN = {
    "min_closed_trades": 8,
    "min_window_days": 20,
    "max_window_days": 90,
    "accept_if": {"net_return_improvement_pct_min": "0.50", "max_drawdown_worse_pct_max": "0.50",
                  "fee_share_worse_pct_max": "10"},
    "metrics": ["扣除费用后的净收益", "最大回撤", "交易数量", "费用占比", "盈利因子"],
    "note": "只使用候选生成时点之后的数据；样本不足或结果冲突时继续模拟，不自动上线。",
}
WEEKLY_MIN_TRADES = 6


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    a = datetime.combine(day, time(0, 0), MYT)
    return a, a + timedelta(days=1)


def daily_review(s: Session, pid: int, day: date, data_source: str, now: datetime) -> ReviewRun | None:
    key = f"DAILY:{pid}:{day.isoformat()}"
    if s.execute(select(ReviewRun.id).where(ReviewRun.dedupe_key == key)).first():
        return None
    a, b = _day_bounds(day)
    p = s.get(Portfolio, pid)
    rr = ReviewRun(kind="DAILY", dedupe_key=key, portfolio_id=pid, period_start=a, period_end=b, status="RUNNING",
                   data_source=data_source)
    s.add(rr)
    s.flush()
    decs = list(s.execute(select(Decision).where(Decision.portfolio_id == pid, Decision.as_of >= a,
                                                 Decision.as_of < b)).scalars())
    intents = list(s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid, OrderIntent.as_of >= a,
                                                       OrderIntent.as_of < b)).scalars())
    realized: dict[str, Decimal] = {}
    for pl in s.execute(select(PositionLedger).where(PositionLedger.portfolio_id == pid, PositionLedger.as_of >= a,
                                                     PositionLedger.as_of < b)).scalars():
        pos = s.get(Position, pl.position_id)
        realized[pos.currency] = realized.get(pos.currency, Decimal(0)) + pl.realized_pnl
    eq = {r.currency: r for r in s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == pid,
                                                                       DailyEquity.day_myt == day.isoformat())).scalars()}
    fees = sum((i.fees for i in intents), Decimal(0))
    by_outcome = Counter(d.outcome for d in decs)
    by_action = Counter(d.action for d in decs if d.source.startswith("AI"))
    by_status = Counter(i.status for i in intents)
    locks = list(s.execute(select(RiskLock).where(RiskLock.portfolio_id == pid, RiskLock.as_of >= a,
                                                  RiskLock.as_of < b)).scalars())
    open_pos = list(s.execute(select(Position).where(Position.portfolio_id == pid, Position.status == "OPEN")).scalars())
    no_trade_reasons = Counter()
    for d in decs:
        if d.outcome in ("NO_ACTION", "RISK_BLOCKED", "INVALID") and d.outcome_reason:
            no_trade_reasons[d.outcome_reason.split("：")[0][:40]] += 1
    label = SOURCE_LABEL.get(data_source, data_source)
    lines = [f"【{label}｜{p.mode}】{day.isoformat()} 日报（马来西亚时间日界）"]
    if not decs and not intents:
        lines.append("今天没有决策或订单（可能是休市日、授权未生效或没有候选）。")
    else:
        lines.append(f"决策：共 {len(decs)} 条；" + "、".join(f"{OUTCOME_CN.get(k, k)} {v}" for k, v in by_outcome.items())
                     + (f"。AI 动作：" + "、".join(f"{k} {v}" for k, v in by_action.items()) if by_action else ""))
        lines.append("订单：" + ("、".join(f"{k} {v}" for k, v in by_status.items()) if intents else "无") +
                     f"；费用合计 {fees:.2f}（按各自币种）")
    for c, v in sorted(realized.items()):
        lines.append(f"{c} 当日已实现盈亏 {v:+.2f}（已扣费用；未平仓浮动盈亏不计入已实现）")
    for c, r in sorted(eq.items()):
        base = r.start_equity + r.flows
        lines.append(f"{c} 策略净值：日初 {base:.2f} → 最新 {r.last_equity:.2f}（{(r.last_equity - base):+.2f}）")
    if open_pos:
        lines.append("持仓：" + "、".join(f"{x.symbol} {x.qty}" for x in open_pos) + "（未完成交易，盈亏未实现）")
    if locks:
        lines.append("风控：" + "；".join(lk.reason for lk in locks))
    if no_trade_reasons:
        lines.append("为什么没有（更多）交易：" + "；".join(f"{k}（{v} 次）" for k, v in no_trade_reasons.most_common(3)))
    lines.append("跨市场说明：美股同一交易日在马来西亚时间次日凌晨收市；仍持有的仓位视为未完成交易。")
    experience.record_no_trade_day(s, pid, data_source, day.isoformat(), dict(by_outcome), b)
    rr.summary = "\n".join(lines)
    rr.stats = {"decisions": dict(by_outcome), "orders": dict(by_status), "realized": {k: str(v) for k, v in realized.items()},
                "fees": str(fees), "open_positions": len(open_pos)}
    rr.status = "DONE"
    audit(s, "REVIEW", "DAILY", f"已生成 {day.isoformat()} 日报", actor="worker", portfolio_id=pid, as_of=now)
    return rr


def _get_path(d: dict, path: str):
    cur = d
    for k in path.split("."):
        cur = cur.get(k, {}) if isinstance(cur, dict) else {}
    return cur if cur != {} else None


def _set_path(d: dict, path: str, v) -> None:
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = v


def propose_candidates(s: Session, pid: int, rr: ReviewRun, exps: list[ExperienceRecord], sv: StrategyVersion,
                       week_key: str, now: datetime) -> list[ImprovementCandidate]:
    """确定性规则提出候选（每个只改一个参数，且只能是白名单内的决策参数）。"""
    from ..strategies.ai_autonomous import DEFAULT_AI_PARAMS, TUNABLE_PARAMS

    trades = [e for e in exps if e.kind == "CLOSED_TRADE"]
    cls = Counter(e.outcome_class for e in trades)
    exit_r = Counter()
    for e in trades:
        exit_r.update((e.details or {}).get("exit_reasons", {}))
    params = copy.deepcopy(DEFAULT_AI_PARAMS) | copy.deepcopy(sv.params or {})
    ideas = []
    n = len(trades)
    if cls["SIGNAL_ERROR"] / n >= 0.4:
        cur = float(_get_path(params, "fixture_params.entry_min_score") or 1.5)
        ideas.append(("fixture_params.entry_min_score", round(cur + 0.5, 2),
                      f"{cls['SIGNAL_ERROR']}/{n} 笔亏损交易以止损结束，假设：提高入场门槛可减少错误信号",
                      "减少被止损的交易", "可能错过部分趋势，交易次数下降"))
    if cls["COST_IMPACT"] / n >= 0.25:
        cur = float(_get_path(params, "fixture_params.entry_min_score") or 1.5)
        ideas.append(("fixture_params.entry_min_score", round(cur + 0.3, 2),
                      f"{cls['COST_IMPACT']}/{n} 笔交易扣费前盈利、扣费后亏损，假设：减少低质量交易可降低费用拖累",
                      "降低费用占比", "交易更少，样本积累更慢"))
    if exit_r.get("EXIT_TIME", 0) / max(1, n) >= 0.4:
        cur = int(_get_path(params, "fixture_params.max_holding_days") or 10)
        ideas.append(("fixture_params.max_holding_days", min(20, cur + 5),
                      f"{exit_r['EXIT_TIME']} 次因持有期限退出，假设：延长持有期可让趋势交易更完整",
                      "提高单笔盈利", "持仓风险暴露时间更长"))
    if cls["WIN"] / n < 0.35 and exit_r.get("EXIT_STOP", 0) / max(1, n) >= 0.5:
        cur = float(_get_path(params, "fixture_params.stop_atr_mult") or 2.0)
        ideas.append(("fixture_params.stop_atr_mult", round(min(4.0, cur + 0.5), 2),
                      "多数交易被止损且胜率偏低，假设：止损过紧，被正常波动触发",
                      "减少噪音止损", "单笔亏损可能变大（风控会相应减少数量）"))
    out = []
    for path, new, hyp, benefit, cost in ideas:
        lo, hi = TUNABLE_PARAMS[path]
        if not (lo <= float(new) <= hi):
            continue
        key = f"CAND:{pid}:{week_key}:{path}"
        if s.execute(select(ImprovementCandidate.id).where(ImprovementCandidate.dedupe_key == key)).first():
            continue
        # 同一基准版本上已经试过（或正在模拟）完全相同的改动：不重复提出，保留原记录
        tried = [c for c in s.execute(select(ImprovementCandidate).where(
            ImprovementCandidate.base_strategy_version_id == sv.id)).scalars()
            if c.changes.get("path") == path and str(c.changes.get("to")) == str(new)]
        if tried:
            continue
        new_params = copy.deepcopy(params)
        old = _get_path(new_params, path)
        _set_path(new_params, path, new)
        n_versions = s.execute(select(StrategyVersion).where(StrategyVersion.key == sv.key)).scalars().all()
        cand_sv = StrategyVersion(key=sv.key, version=f"v{len(n_versions) + 1}-cand", params=new_params,
                                  prompt_version=sv.prompt_version, status="CANDIDATE", parent_id=sv.id,
                                  note=f"候选：{path} {old} → {new}")
        s.add(cand_sv)
        s.flush()
        c = ImprovementCandidate(review_run_id=rr.id, dedupe_key=key, base_strategy_version_id=sv.id,
                                 candidate_strategy_version_id=cand_sv.id, hypothesis=hyp,
                                 changes={"path": path, "from": old, "to": new}, evidence={"trades": n, "classes": dict(cls),
                                                                                           "exit_reasons": dict(exit_r)},
                                 expected_benefit=benefit, possible_cost=cost, data_cutoff=now, eval_plan=EVAL_PLAN,
                                 status=CandidateStatus.SIMULATING, proposed_by="RULES",
                                 status_reason="已冻结评价指标与门槛，等待候选生成之后的新数据进行隔离模拟")
        s.add(c)
        out.append(c)
    return out


def weekly_review(s: Session, rt, pid: int, week_key: str, period: tuple[datetime, datetime], data_source: str,
                  now: datetime) -> ReviewRun | None:
    key = f"WEEKLY:{pid}:{week_key}"
    if s.execute(select(ReviewRun.id).where(ReviewRun.dedupe_key == key)).first():
        return None
    rr = ReviewRun(kind="WEEKLY", dedupe_key=key, portfolio_id=pid, period_start=period[0], period_end=period[1],
                   status="RUNNING", data_source=data_source)
    s.add(rr)
    s.flush()
    experience.collect(s, pid, data_source, now)
    s.flush()
    exps = list(s.execute(select(ExperienceRecord).where(ExperienceRecord.portfolio_id == pid,
                                                         ExperienceRecord.data_source == data_source)).scalars())
    trades = [e for e in exps if e.kind == "CLOSED_TRADE"]
    sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS",
                                                 StrategyVersion.status == "ACTIVE")).scalar_one_or_none()
    lines = [f"【{SOURCE_LABEL.get(data_source, data_source)}】{week_key} 周度改进分析",
             f"经验记录 {len(exps)} 条（已平仓交易 {len(trades)}，被拒绝/无效 "
             f"{len([e for e in exps if e.kind in ('RISK_REJECTED', 'INVALID_OUTPUT')])}，执行故障 "
             f"{len([e for e in exps if e.kind == 'EXECUTION_FAILURE'])}）。"]
    cands = []
    if len(trades) < WEEKLY_MIN_TRADES or sv is None:
        lines.append(f"证据不足，保持当前版本（已平仓交易 {len(trades)} 笔 < {WEEKLY_MIN_TRADES}）。不会为凑样本而交易。")
        rr.status = "INSUFFICIENT_EVIDENCE"
    else:
        cls = Counter(e.outcome_class for e in trades)
        lines.append("交易结果分类：" + "、".join(f"{k} {v}" for k, v in cls.items()))
        cands = propose_candidates(s, pid, rr, exps, sv, week_key, now)
        if cands:
            lines.append(f"提出 {len(cands)} 个候选改进，进入隔离模拟（不会自动上线）：")
            lines += [f"- {c.changes['path']}：{c.changes['from']} → {c.changes['to']}。{c.hypothesis}" for c in cands]
        else:
            lines.append("未发现满足提出条件的改进方向，保持当前版本。")
        rr.status = "DONE"
    # 评估进行中的候选
    from .evaluation import evaluate_due

    evals = evaluate_due(s, rt, pid, now)
    if evals:
        lines.append("候选评估：" + "；".join(evals))
    rr.summary = "\n".join(lines)
    rr.stats = {"experiences": len(exps), "closed_trades": len(trades), "candidates": len(cands)}
    audit(s, "REVIEW", "WEEKLY", f"周度改进分析完成：{rr.status}", actor="worker", portfolio_id=pid, as_of=now)
    if cands:
        notify(s, "info", f"有 {len(cands)} 个策略改进候选进入模拟", "候选不会自动上线，模拟结果满足门槛后需要您审核。",
               dedupe_key=f"cands:{key}")
    return rr
