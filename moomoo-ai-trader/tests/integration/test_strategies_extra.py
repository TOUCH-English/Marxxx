"""补充：重要价格变动触发的额外 AI 复核（去重、限频）；基准策略的「AI 可否决」实验只能否决不能批准。"""
import json
import time
from decimal import Decimal

from sqlalchemy import select

from app.ai.providers import AIResult
from app.db.models import AuditEvent, OrderIntent, StrategyVersion
from app.db.session import read_session, write_session
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend
from integration.test_ai_safety import ScriptedAI, _buy


def _no_trade(p):
    c = p["candidates"][0]
    return AIResult("OK", text=json.dumps({"as_of_utc": p["as_of_utc"], "strategy_version": p["strategy_version"],
                                           "market_view": "", "decisions": [{
        "symbol": c["symbol"], "action": "NO_TRADE", "rationale": "AI 认为信号不可靠", "evidence_ids": [], "risk_flags": [],
        "entry_type": "NONE", "entry_price": None, "max_buy_price": None, "target_position_pct": None,
        "stop_loss_price": None, "take_profit_price": None, "max_holding_days": None, "sell_fraction": None,
        "min_sell_price": None, "valid_minutes": 30}]}))


def test_event_triggered_extra_review_is_rate_limited(env):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"])
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock)
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(3):
        eng.tick()
        time.sleep(0.05)
    rt = eng.runtimes[pid]
    n0 = len([j for j in rt.last_ai_batch])
    first = rt.last_ai_batch["US"]
    clock.advance(minutes=1)
    eng.tick()  # 记录新持仓的基准价
    prov.set_quote("US.UPA", "67.50")  # +5%
    clock.advance(minutes=4)  # 不到 15 分钟：不触发
    eng.tick()
    assert rt.last_ai_batch["US"] == first
    clock.advance(minutes=11)  # 16 分钟后：触发额外复核（常规间隔是 60 分钟）
    eng.tick()
    assert rt.last_ai_batch["US"] > first
    with read_session("sim") as s:
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "AI_EVENT_REVIEW")).first()


def _cross_series():
    return [50.0] * 70 + [55.0]


def _baseline_with_overlay(env, ai, overlay):
    prov = StaticProvider()
    prov.add("US.X", _cross_series(), "55.10")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.X"], strategy_key="BASELINE_MA")
    with write_session("sim") as s:
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == "BASELINE_MA")).scalar_one()
        sv.params = dict(sv.params) | {"ai_overlay": overlay}
    eng, fb = make_engine("sim", prov, ManualClock(US_OPEN), ai=ai)
    for _ in range(2):
        eng.tick()
    with read_session("sim") as s:
        return list(s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid)).scalars())


def test_ai_veto_can_only_block(env):
    assert len(_baseline_with_overlay(env, ScriptedAI(_no_trade), "veto")) == 0, "AI 否决 → 不下单"


def test_ai_veto_agreeing_keeps_rule_trade(env):
    ints = _baseline_with_overlay(env, ScriptedAI(lambda p: _buy(p, stop_loss_price="52.00")), "veto")
    assert len(ints) == 1 and ints[0].reason == "ENTRY"


def test_ai_cannot_approve_trades_rule_does_not_allow(env):
    prov = StaticProvider()
    prov.add("US.Y", [50.0] * 71, "50.10")  # 没有交叉
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.Y"], strategy_key="BASELINE_MA")
    with write_session("sim") as s:
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == "BASELINE_MA")).scalar_one()
        sv.params = dict(sv.params) | {"ai_overlay": "veto"}
    ai = ScriptedAI(lambda p: _buy(p, stop_loss_price="48.00"))
    eng, fb = make_engine("sim", prov, ManualClock(US_OPEN), ai=ai)
    for _ in range(2):
        eng.tick()
    with read_session("sim") as s:
        assert not s.execute(select(OrderIntent)).first()
    assert not ai.payloads, "规则没有信号时根本不会询问 AI"
