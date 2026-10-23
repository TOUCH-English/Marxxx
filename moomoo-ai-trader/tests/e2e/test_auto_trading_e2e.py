"""验收 19、20、21：一次启动后 AI 自动买入与后续卖出（部分减仓 + 全部退出），无逐笔确认；
AI 策略与均线基准策略未绑定；关闭浏览器/会话过期不影响后台授权运行。"""
import time
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.enums import OrderStatus
from app.db.models import Decision, OrderIntent, Position
from app.db.session import read_session
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend


def tick_until(eng, cond, n=20):
    for _ in range(n):
        eng.tick()
        if cond():
            return True
        time.sleep(0.05)
    return False


def _intents(ns, pid):
    with read_session(ns) as s:
        return list(s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid)
                              .order_by(OrderIntent.created_at_utc)).scalars())


def _pos(ns, pid, sym):
    with read_session(ns) as s:
        return s.execute(select(Position).where(Position.portfolio_id == pid, Position.symbol == sym)
                         .order_by(Position.id.desc())).scalars().first()


@pytest.mark.parametrize("ns,mode", [("sim", "LOCAL_PAPER"), ("live", "LIVE_AUTO")])
def test_ai_buys_then_reduces_then_exits_without_confirmation(env, ns, mode):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, _ = setup_run(ns, mode, symbols=["US.UPA"], capital={"USD": "10000"})
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine(ns, prov, clock)
    fb.callback_before_return = True
    fb.fill_immediately = True
    # 1) 自动买入
    assert tick_until(eng, lambda: _pos(ns, pid, "US.UPA") is not None), "AI 应自动买入"
    p = _pos(ns, pid, "US.UPA")
    assert p.qty > 0 and p.stop_price is not None
    first_qty = p.qty
    # 2) 价格上涨 → AI 部分减仓（同一标的 AI 间隔 ≥ 15 分钟，这里按 60 分钟）
    prov.set_quote("US.UPA", str(p.avg_cost + Decimal("2.5")))
    clock.advance(minutes=61)
    assert tick_until(eng, lambda: _pos(ns, pid, "US.UPA").qty < first_qty), "应部分减仓"
    p = _pos(ns, pid, "US.UPA")
    assert 0 < p.qty < first_qty
    # 3) 跌破 20 日均线（仍高于止损）→ AI 全部退出
    sma20 = sum(trend(80, 60.0, 0.05)[-20:]) / 20
    px = Decimal(str(round(sma20 * 0.98, 2)))
    assert px > p.stop_price, "测试价格应高于止损价，确保是 AI 卖出而不是止损"
    prov.set_quote("US.UPA", str(px))
    clock.advance(minutes=61)
    assert tick_until(eng, lambda: _pos(ns, pid, "US.UPA").status == "CLOSED"), "应全部退出"
    its = _intents(ns, pid)
    reasons = [i.reason for i in its]
    assert reasons.count("ENTRY") == 1 and reasons.count("EXIT_AI") == 2
    assert all(i.status != OrderStatus.AWAITING_APPROVAL for i in its), "自动模式不应出现逐笔确认"
    assert all(i.status == OrderStatus.FILLED for i in its)
    # 执行适配器收到的每笔委托都带显式环境与账户
    assert all(c["trd_env"] and c["acc_id"] for c in fb.place_calls)


def test_ai_and_baseline_are_independent(env):
    """同样没有均线交叉的平稳上涨：AI 策略可以在政策和风控允许时买入，基准策略不会交易。"""
    closes = trend(90, 50.0, 0.1)  # MA20 一直高于 MA60，最近没有交叉
    from app.strategies.baseline_ma import signal
    from app.market_data.base import Bar
    from datetime import date, timedelta

    bars = [Bar("X", date(2026, 1, 1) + timedelta(days=i), Decimal(str(c)), Decimal(str(c)), Decimal(str(c)),
                Decimal(str(c)), 1) for i, c in enumerate(closes)]
    sig = signal(bars, 20, 60)
    assert sig["ready"] and not sig["cross_up"]
    results = {}
    for key in ("AI_AUTONOMOUS", "BASELINE_MA"):
        prov = StaticProvider()
        prov.add("US.NOX", closes, str(closes[-1] + 0.1))
        pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.NOX"], strategy_key=key)
        eng, fb = make_engine("sim", prov, ManualClock(US_OPEN))
        fb.callback_before_return = fb.fill_immediately = True
        for _ in range(6):
            eng.tick()
            time.sleep(0.05)
        results[key] = len([i for i in _intents("sim", pid) if i.side == "BUY"])
        with read_session("sim") as s:
            if key == "BASELINE_MA":
                d = s.execute(select(Decision).where(Decision.portfolio_id == pid)).scalars().all()
                assert any("无上穿信号" in x.outcome_reason for x in d)
    assert results["AI_AUTONOMOUS"] == 1
    assert results["BASELINE_MA"] == 0


def test_background_continues_without_browser_and_revocation_blocks_new_risk(env):
    """21：网页会话不影响后台；撤销授权立即阻止新风险，保护性退出保留。"""
    from app.api import security
    from app.services.runs import revoke

    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    prov.add("US.UPB", trend(80, 40.0, 0.04), "43.20")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA", "US.UPB"])
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock)
    fb.callback_before_return = fb.fill_immediately = True
    security.store.clear()  # 没有任何网页会话（浏览器已关闭/会话过期）
    assert tick_until(eng, lambda: _pos("sim", pid, "US.UPA") is not None or _pos("sim", pid, "US.UPB") is not None)
    revoke("sim", pid, "test")
    held = [s for s in ("US.UPA", "US.UPB") if _pos("sim", pid, s) is not None]
    n_buys = len([i for i in _intents("sim", pid) if i.side == "BUY"])
    clock.advance(minutes=61)
    for _ in range(5):
        eng.tick()
        time.sleep(0.05)
    assert len([i for i in _intents("sim", pid) if i.side == "BUY"]) == n_buys, "撤销后不得产生新买单"
    # 保护性退出仍然运行：价格跌破止损
    sym = held[0]
    p = _pos("sim", pid, sym)
    prov.set_quote(sym, str(p.stop_price - Decimal("0.5")))
    clock.advance(minutes=5)
    assert tick_until(eng, lambda: _pos("sim", pid, sym).status == "CLOSED")
    assert any(i.reason == "EXIT_STOP" for i in _intents("sim", pid))
