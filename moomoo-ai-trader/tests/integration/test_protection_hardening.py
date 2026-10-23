"""风控审核第 1 批修复：报价缺失、价格断层、策略出错不影响保护、保护性卖单快速改价、真实 AI 不接收测试阈值、AI 间隔配置。"""
import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.enums import IntentReason
from app.db.models import AuditEvent, Notification, OrderIntent
from app.db.session import read_session
from app.db.types import utcnow
from app.portfolio import ledger
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend
from integration.test_ai_safety import ScriptedAI, _buy


def _holding(stop="61.00"):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"])
    clock = ManualClock(US_OPEN)
    ai = ScriptedAI(lambda p: _buy(p, stop_loss_price=stop) if p["candidates"] else _buy(p))
    eng, fb = make_engine("sim", prov, clock, ai=ai)
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(4):
        eng.tick()
        time.sleep(0.05)
    with read_session("sim") as s:
        pos = ledger.get_open_position(s, pid, "US.UPA")
    assert pos is not None
    return prov, pid, clock, eng, fb, ai, pos


def _audits(action):
    with read_session("sim") as s:
        return list(s.execute(select(AuditEvent).where(AuditEvent.action == action)).scalars())


def test_missing_quote_keeps_last_price_for_valuation_and_alerts(env):
    prov, pid, clock, eng, fb, ai, pos = _holding()
    rt = eng.runtimes[pid]
    prov.set_quote("US.UPA", "62.50")
    eng.tick()
    del prov.q["US.UPA"]  # 报价中断
    rt.no_quote_since["US.UPA"] = utcnow() - timedelta(seconds=40)
    eng.tick()
    assert rt.price_of("US.UPA") == Decimal("62.50")  # 不退回成本价
    assert _audits("NO_QUOTE")
    with read_session("sim") as s:
        assert s.execute(select(Notification).where(Notification.title.like("%没有报价%"))).first()


def test_price_gap_like_a_split_pauses_auto_stop_and_alerts(env):
    prov, pid, clock, eng, fb, ai, pos = _holding()
    prov.set_quote("US.UPA", "32.00")  # 价格腰斩（拆股或数据错误），远低于止损 61
    eng.tick()
    with read_session("sim") as s:
        sells = list(s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).scalars())
    assert sells == []
    assert _audits("PRICE_GAP_HOLD")


def test_normal_stop_still_fires_and_protective_exit_reprices_after_one_minute(env):
    prov, pid, clock, eng, fb, ai, pos = _holding()
    fb.fill_immediately = False  # 卖单挂着不成交
    prov.set_quote("US.UPA", "60.50")  # 跌破止损 61，但不是断层
    eng.tick()
    with read_session("sim") as s:
        it = s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).scalar_one()
    assert it.reason == IntentReason.EXIT_STOP
    assert (it.expires_at - it.as_of) == timedelta(minutes=1)


def test_strategy_error_does_not_block_protective_exit(env, monkeypatch):
    prov, pid, clock, eng, fb, ai, pos = _holding()
    from app.strategies import ai_autonomous

    def boom(*a, **k):
        raise RuntimeError("策略炸了")

    monkeypatch.setattr(ai_autonomous, "run", boom)
    prov.set_quote("US.UPA", "60.50")
    eng.tick()
    with read_session("sim") as s:
        it = s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).scalar_one()
    assert it.status not in ("CREATED",)  # 已提交
    assert _audits("STEP_ERROR")


def test_real_model_does_not_get_fixture_thresholds(env):
    prov, pid, clock, eng, fb, ai, pos = _holding()
    assert ai.payloads and all("fixture_params" not in p["policy"] for p in ai.payloads)


def test_ai_interval_can_be_set_in_deployment_config(env, monkeypatch):
    from app.config import reset_settings_cache
    from app.strategies.ai_autonomous import _params

    class TS:
        strategy = None
        params = {"max_position_pct": "0.25"}

    assert _params(TS())["interval_minutes"] == 60
    monkeypatch.setenv("AI_BATCH_INTERVAL_MINUTES", "180")
    reset_settings_cache()
    assert _params(TS())["interval_minutes"] == 180
    monkeypatch.setenv("AI_BATCH_INTERVAL_MINUTES", "5")
    reset_settings_cache()
    assert _params(TS())["interval_minutes"] == 15  # 不低于最小间隔
