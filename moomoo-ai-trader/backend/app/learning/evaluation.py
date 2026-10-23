"""候选改进的隔离评估、审核上线与回退。

- 评估只使用「候选生成时点之后」且「当前业务时间之前已收盘」的数据，不读取未来数据。
- 当前运行版作为基准，候选版与基准在同一窗口、同一假设下回放比较；评价指标与门槛在候选创建时冻结。
- 结果只决定 READY_FOR_REVIEW / REJECTED / INSUFFICIENT_EVIDENCE；任何候选都不会自动成为 ACTIVE。
- 上线（用户确认一次）只影响新决策；已有持仓保留原有保护。回退不会清除任何风控锁定，也不会下单。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.audit import audit, notify
from ..core.enums import CandidateStatus
from ..db.models import (
    DeploymentHistory,
    EvaluationRun,
    ImprovementCandidate,
    StrategyVersion,
    UniverseSnapshot,
)
from ..db.types import D, utcnow
from ..portfolio import ledger
from ..risk.policy import SEED_RISK_PARAMS
from ..simulation.backtest import run_backtest


def _symbols(s: Session, rt) -> list[str]:
    snap = s.execute(select(UniverseSnapshot).order_by(UniverseSnapshot.id.desc()).limit(1)).scalar_one_or_none()
    if snap is not None:
        return [m["symbol"] for m in snap.members]
    return [i.symbol for i in rt.provider.instruments() if not i.is_leveraged]


def evaluate_due(s: Session, rt, pid: int, now: datetime) -> list[str]:
    out = []
    if rt is None or not getattr(rt.ai_provider, "is_fixture", False):
        for c in s.execute(select(ImprovementCandidate).where(ImprovementCandidate.status == CandidateStatus.SIMULATING)).scalars():
            c.status_reason = "当前使用真实模型：候选需以影子模式前向模拟评估（首版未自动实现，标示为未验证），保持模拟状态"
        return out
    for c in s.execute(select(ImprovementCandidate).where(ImprovementCandidate.status == CandidateStatus.SIMULATING)).scalars():
        out.append(evaluate_candidate(s, rt, pid, c, now))
    return out


def evaluate_candidate(s: Session, rt, pid: int, c: ImprovementCandidate, now: datetime) -> str:
    plan = c.eval_plan
    start = (c.data_cutoff + timedelta(days=1)).date()
    end = (now - timedelta(hours=23, minutes=59)).date()  # 只用当前业务时间之前已完整收盘的日 K
    days = (end - start).days
    if days < int(plan["min_window_days"]):
        c.status_reason = f"评估窗口 {max(days, 0)} 天 < {plan['min_window_days']} 天，继续模拟"
        return f"候选#{c.id} 数据不足，继续模拟"
    base = s.get(StrategyVersion, c.base_strategy_version_id)
    cand = s.get(StrategyVersion, c.candidate_strategy_version_id)
    capital = {ccy: ledger.total_flows(s, pid, ccy) for ccy in ("MYR", "USD")}
    capital = {k: v for k, v in capital.items() if v > 0} or {"MYR": Decimal(20000)}
    kw = dict(symbols=_symbols(s, rt), start=start, end=end, capital=capital, fee_schedules=rt.fee_schedules,
              strategy_key="AI_FIXTURE", risk_params=SEED_RISK_PARAMS)
    rb = run_backtest(rt.provider, rt.calendar, strategy_params=base.params, **kw)
    rc = run_backtest(rt.provider, rt.calendar, strategy_params=cand.params, **kw)
    trades = rc.metrics["round_trips"]
    acc = plan["accept_if"]
    better, worse_dd, conflict = 0, False, False
    detail = []
    for ccy in capital:
        b, k = rb.metrics["per_currency"][ccy], rc.metrics["per_currency"][ccy]
        diff = D(k["net_return_pct"]) - D(b["net_return_pct"])
        dd = D(k["max_drawdown_pct"]) - D(b["max_drawdown_pct"])
        detail.append(f"{ccy} 净收益差 {diff:+.2f}%，回撤差 {dd:+.2f}%")
        if diff >= D(acc["net_return_improvement_pct_min"]):
            better += 1
        elif diff < 0:
            conflict = True
        if dd > D(acc["max_drawdown_worse_pct_max"]):
            worse_dd = True
    if better and conflict:
        verdict = "CONFLICT"
    elif trades < int(plan["min_closed_trades"]):
        verdict = "INSUFFICIENT"
    elif better and not worse_dd:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    s.add(EvaluationRun(candidate_id=c.id, window_start=c.data_cutoff, window_end=now, baseline_result=rb.metrics,
                        candidate_result=rc.metrics, verdict=verdict, notes="；".join(detail)))
    window_over = days >= int(plan["max_window_days"])
    if verdict == "PASS":
        c.status = CandidateStatus.READY_FOR_REVIEW
        c.status_reason = "满足预设门槛，可供审核（不代表已获准控制实盘）：" + "；".join(detail)
        notify(s, "info", f"候选改进 #{c.id} 可供审核", c.hypothesis, dedupe_key=f"ready:{c.id}")
    elif verdict in ("INSUFFICIENT", "CONFLICT") and not window_over:
        c.status_reason = ("交易样本不足" if verdict == "INSUFFICIENT" else "不同市场结果冲突") + "，延长模拟：" + "；".join(detail)
    elif verdict in ("INSUFFICIENT", "CONFLICT"):
        c.status = CandidateStatus.INSUFFICIENT_EVIDENCE
        c.status_reason = "已达最长评估窗口仍证据不足，保留记录，不上线：" + "；".join(detail)
    else:
        c.status = CandidateStatus.REJECTED
        c.status_reason = "未达到预设门槛，保留失败记录：" + "；".join(detail)
    c.updated_at_utc = utcnow()
    return f"候选#{c.id} → {c.status}"


def approve_and_activate(s: Session, cid: int, actor: str, note: str = "") -> StrategyVersion:
    c = s.get(ImprovementCandidate, cid)
    if c is None or c.status != CandidateStatus.READY_FOR_REVIEW:
        raise ValueError("只有「可供审核」的候选可以批准上线")
    new = s.get(StrategyVersion, c.candidate_strategy_version_id)
    cur = s.execute(select(StrategyVersion).where(StrategyVersion.key == new.key,
                                                  StrategyVersion.status == "ACTIVE")).scalar_one_or_none()
    c.status = CandidateStatus.APPROVED
    if cur is not None:
        cur.status = "INACTIVE"
    new.status = "ACTIVE"
    new.approved_by_user = True
    new.approved_at = utcnow()
    new.version = new.version.replace("-cand", "")
    c.status = CandidateStatus.ACTIVE
    s.add(DeploymentHistory(strategy_key=new.key, from_version_id=cur.id if cur else None, to_version_id=new.id,
                            action="ACTIVATE", candidate_id=c.id, actor=actor,
                            note=note or "用户确认策略升级：只影响新决策，已有持仓保留原保护"))
    audit(s, "STRATEGY", "ACTIVATE", f"策略 {new.key} 升级到 {new.version}（候选 #{c.id}）", actor=actor)
    return new


def rollback(s: Session, key: str, actor: str, note: str = "") -> StrategyVersion:
    last = s.execute(select(DeploymentHistory).where(DeploymentHistory.strategy_key == key,
                                                     DeploymentHistory.action == "ACTIVATE")
                     .order_by(DeploymentHistory.id.desc()).limit(1)).scalar_one_or_none()
    if last is None or last.from_version_id is None:
        raise ValueError("没有可回退的上一版本")
    cur = s.get(StrategyVersion, last.to_version_id)
    prev = s.get(StrategyVersion, last.from_version_id)
    if cur.status != "ACTIVE":
        raise ValueError("当前生效版本不是最近一次上线的版本，无法回退")
    cur.status = "INACTIVE"
    prev.status = "ACTIVE"
    if last.candidate_id:
        cand = s.get(ImprovementCandidate, last.candidate_id)
        if cand is not None:
            cand.status = CandidateStatus.ROLLED_BACK
    s.add(DeploymentHistory(strategy_key=key, from_version_id=cur.id, to_version_id=prev.id, action="ROLLBACK",
                            actor=actor, note=note or "回退：不清除亏损锁定、不重新下单、不放宽已有保护"))
    audit(s, "STRATEGY", "ROLLBACK", f"策略 {key} 回退到 {prev.version}", actor=actor)
    return prev
