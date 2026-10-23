"""验收 23–27：复盘任务去重、候选不读未来数据、失败候选保留、不自动上线、升级/回退审计与锁定保留。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.enums import CandidateStatus
from app.db.models import (
    AuditEvent,
    DeploymentHistory,
    ExperienceRecord,
    ImprovementCandidate,
    Position,
    ReviewRun,
    RiskLock,
    StrategyVersion,
)
from app.db.session import read_session, write_session
from app.db.types import utcnow


def _demo(ticks):
    from app.services.runs import start_sim_run
    from app.workers.engine import TradingEngine

    pid, _ = start_sim_run()
    eng = TradingEngine("sim")
    for _ in range(ticks):
        eng.tick()
    return pid, eng


def test_daily_weekly_jobs_run_once_and_survive_restart(env):
    pid, eng = _demo(220)
    with read_session("sim") as s:
        daily = list(s.execute(select(ReviewRun).where(ReviewRun.kind == "DAILY")).scalars())
        weekly = list(s.execute(select(ReviewRun).where(ReviewRun.kind == "WEEKLY")).scalars())
    assert len(daily) >= 5 and len({r.dedupe_key for r in daily}) == len(daily)
    assert weekly, "新的一周开始后应自动运行周度分析"
    w = weekly[0]
    assert w.status == "INSUFFICIENT_EVIDENCE" and "证据不足，保持当前版本" in w.summary
    # 重启：新引擎实例从同一时钟继续，不会重复生成
    from app.workers.engine import TradingEngine

    eng2 = TradingEngine("sim")
    for _ in range(3):
        eng2.tick()
    with read_session("sim") as s:
        keys = [r.dedupe_key for r in s.execute(select(ReviewRun)).scalars()]
    assert len(keys) == len(set(keys))
    # 日报里没有伪造结论：没有成交的日子写明「没有决策或订单」或给出原因统计
    assert all(("日报" in r.summary) for r in daily)


def _candidate(s, sv, cutoff, status=CandidateStatus.SIMULATING, to=2.0):
    import copy

    params = copy.deepcopy(sv.params)
    params["fixture_params"]["entry_min_score"] = to
    cs = StrategyVersion(key=sv.key, version=f"vtest-{to}-cand", params=params, status="CANDIDATE", parent_id=sv.id)
    s.add(cs)
    s.flush()
    from app.learning.review import EVAL_PLAN

    c = ImprovementCandidate(review_run_id=0, dedupe_key=f"test:{to}:{status}", base_strategy_version_id=sv.id,
                             candidate_strategy_version_id=cs.id, hypothesis="测试", changes={"path": "fixture_params.entry_min_score", "from": 1.5, "to": to},
                             data_cutoff=cutoff, eval_plan=EVAL_PLAN, status=status)
    s.add(c)
    s.flush()
    return c, cs


def test_candidate_uses_only_post_cutoff_past_data_and_never_auto_activates(env, monkeypatch):
    pid, eng = _demo(5)
    rt = eng.runtimes[pid]
    from app.learning import evaluation

    seen = {}
    real = evaluation.run_backtest

    def spy(*a, **kw):
        seen.setdefault("calls", []).append((kw["start"], kw["end"]))
        return real(*a, **kw)

    monkeypatch.setattr(evaluation, "run_backtest", spy)
    now = rt.now() + timedelta(days=40)
    with write_session("sim") as s:
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS", StrategyVersion.status == "ACTIVE")).scalar_one()
        c, cs = _candidate(s, sv, cutoff=rt.now())
        evaluation.evaluate_candidate(s, rt, pid, c, now)
        status = c.status
    for start, end in seen["calls"]:
        assert start > rt.now().date(), "只用候选生成之后的数据"
        assert end < now.date(), "不读取当前业务时间之后（未来）的数据"
    assert status in (CandidateStatus.SIMULATING, CandidateStatus.READY_FOR_REVIEW, CandidateStatus.REJECTED,
                      CandidateStatus.INSUFFICIENT_EVIDENCE)
    with read_session("sim") as s:
        active = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS",
                                                         StrategyVersion.status == "ACTIVE")).scalars().all()
        assert len(active) == 1 and active[0].id == sv.id, "候选评估不会修改正在运行的政策"
        assert s.get(StrategyVersion, cs.id).status == "CANDIDATE"


def test_candidate_changes_are_whitelisted():
    from app.strategies.ai_autonomous import TUNABLE_PARAMS

    for k in TUNABLE_PARAMS:
        assert not any(w in k for w in ("risk", "position_pct", "capital", "universe", "pool", "stop_price", "exposure"))


def test_failed_and_insufficient_candidates_are_kept_and_ready_needs_user(env):
    pid, eng = _demo(5)
    rt = eng.runtimes[pid]
    from app.learning.evaluation import approve_and_activate, evaluate_candidate

    with write_session("sim") as s:
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS", StrategyVersion.status == "ACTIVE")).scalar_one()
        c1, _ = _candidate(s, sv, cutoff=rt.now() - timedelta(days=200), to=3.9)
        c1.eval_plan = c1.eval_plan | {"max_window_days": 1}
        evaluate_candidate(s, rt, pid, c1, rt.now())
        c2, _ = _candidate(s, sv, cutoff=rt.now(), status=CandidateStatus.REJECTED, to=0.6)
    with read_session("sim") as s:
        statuses = {c.id: c.status for c in s.execute(select(ImprovementCandidate)).scalars()}
    assert len(statuses) == 2, "失败与证据不足的候选都被保留"
    assert CandidateStatus.ACTIVE not in statuses.values()
    with write_session("sim") as s:
        with pytest.raises(ValueError):
            approve_and_activate(s, c2.id, "user")  # 非 READY_FOR_REVIEW 不能上线


def test_approved_switch_keeps_protection_no_duplicate_orders_and_rollback_keeps_locks(env):
    pid, eng = _demo(60)
    from app.learning.evaluation import approve_and_activate, rollback
    from app.risk.locks import add_lock

    with read_session("sim") as s:
        before_pos = {p.id: (p.stop_price, p.take_profit_price, p.max_hold_until)
                      for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars()}
        n_int = s.execute(select(func.count()).select_from(__import__("app.db.models", fromlist=["x"]).OrderIntent)).scalar_one()
    with write_session("sim") as s:
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS", StrategyVersion.status == "ACTIVE")).scalar_one()
        c, cs = _candidate(s, sv, cutoff=utcnow(), status=CandidateStatus.READY_FOR_REVIEW, to=2.5)
        add_lock(s, pid, "DRAWDOWN", "测试回撤锁", utcnow(), manual=True)
        new = approve_and_activate(s, c.id, "user", "测试升级")
        assert new.status == "ACTIVE" and new.approved_by_user
    # 切换后立即运行一次：不产生重复订单，原持仓保护不变
    eng.tick()
    with read_session("sim") as s:
        after = {p.id: (p.stop_price, p.take_profit_price, p.max_hold_until)
                 for p in s.execute(select(Position)).scalars() if p.id in before_pos}
        assert after == before_pos, "已有持仓保留原有退出与保护规则"
        from app.db.models import OrderIntent

        keys = [i.intent_key for i in s.execute(select(OrderIntent)).scalars()]
        assert len(keys) == len(set(keys))
    with write_session("sim") as s:
        prev = rollback(s, "AI_AUTONOMOUS", "user", "测试回退")
        assert prev.id == sv.id
    with read_session("sim") as s:
        assert s.execute(select(RiskLock).where(RiskLock.kind == "DRAWDOWN", RiskLock.active.is_(True))).first(), "回退不能清除锁定"
        acts = [d.action for d in s.execute(select(DeploymentHistory)).scalars()]
        assert acts == ["ACTIVATE", "ROLLBACK"]
        aud = {e.action for e in s.execute(select(AuditEvent).where(AuditEvent.category == "STRATEGY")).scalars()}
        assert {"ACTIVATE", "ROLLBACK"} <= aud
        assert s.get(ImprovementCandidate, c.id).status == CandidateStatus.ROLLED_BACK


def test_unverified_experience_never_enters_prompt_and_sources_are_separated(env):
    pid, eng = _demo(120)
    rt = eng.runtimes[pid]
    from app.strategies.ai_autonomous import _params, build_payload

    ts = eng._tick_state(rt, utcnow())
    payload, meta = build_payload(rt, ts, "US", _params(ts))
    dumped = str(payload)
    assert "experience" not in dumped.lower() and "经验" not in dumped
    with read_session("sim") as s:
        srcs = {e.data_source for e in s.execute(select(ExperienceRecord)).scalars()}
        assert srcs <= {"DEMO_FIXTURE"}, "演示经验只标记为演示数据，不与实盘混合"
        assert all(not e.verified for e in s.execute(select(ExperienceRecord)).scalars())
