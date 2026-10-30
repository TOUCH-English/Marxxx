"""独立审核（2026-10-23 夜，第 2 批复审）发现的问题的回归测试。"""
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.calendar import get_calendar
from app.db.models import DailyEquity, Decision, OrderIntent
from app.db.session import read_session, write_session
from app.portfolio import ledger
from app.strategies import market_context as mctx
from helpers import US_OPEN, trend
from integration.test_ai_safety import ScriptedAI, _buy, _one, _vctx
from integration.test_market_context import CtxProvider, _run, _spy


def test_reward_risk_uses_worst_entry_price_max_buy():
    from app.ai.validate import parse_and_validate

    # 最新价 100、止损 95、止盈 112：按 100 计 2.4；但最高买入价 104 时只有 (112-104)/(104-95)=0.89
    r = parse_and_validate(_one(max_buy_price="104.00"), _vctx())
    assert "盈亏比" in " ".join(e for d in r.decisions for e in d.errors)
    r = parse_and_validate(_one(stop_loss_price="100.00"), _vctx())
    assert "不低于入场价" in " ".join(e for d in r.decisions for e in d.errors)


def test_reward_risk_rechecked_at_actual_order_limit(env):
    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    # 按最新价 64：(68.2-64)/(64-61.9)=2.0 刚好合格；实际限价约 64.2 时低于 2
    ai = ScriptedAI(lambda p: _buy(p, stop_loss_price="61.90", take_profit_price="68.20") if p["candidates"] else _buy(p))
    pid, *_ = _run(prov, ["US.UPA"], ai)
    with read_session("sim") as s:
        d = s.execute(select(Decision).where(Decision.source == "AI_MODEL")).scalars().first()
        assert ledger.get_open_position(s, pid, "US.UPA") is None
    assert d.outcome == "RISK_BLOCKED" and "盈亏比（按实际限价）" in str(d.rule_calc)


def _rt(earn, now):
    return SimpleNamespace(provider=SimpleNamespace(earnings_calendar=lambda t, days=21: earn),
                           calendar=get_calendar(), now=lambda: now)


@pytest.mark.parametrize("earn_day,pub,expect", [
    (date(2026, 10, 24), "BEFORE", True),    # 周六公布（如伯克希尔）：周五收市前退出
    (date(2026, 10, 25), "N/A", True),       # 周日
    (date(2026, 10, 26), "BEFORE", True),    # 下一个交易日盘前
    (date(2026, 10, 27), "BEFORE", False),   # 还早
    (date(2026, 10, 23), "BEFORE", False),   # 今天盘前已公布
    (date(2026, 10, 23), "AFTER", True),     # 今天收市后
])
def test_exit_before_earnings_covers_weekends(earn_day, pub, expect):
    now = datetime(2026, 10, 23, 19, 45, tzinfo=timezone.utc)  # 周五 美东 15:45
    pos = SimpleNamespace(symbol="US.BRK", market="US")
    rt = _rt({"US.BRK": {"date": earn_day, "pub_type": pub}}, now)
    assert (mctx.exit_before_earnings(rt, pos, now) is not None) == expect


def test_regime_unknown_is_retried_not_cached_all_day(monkeypatch):
    calls = {"n": 0}
    bars = [SimpleNamespace(close=Decimal(500 + i), adj_factor=Decimal(1), day=date(2026, 1, 1)) for i in range(220)]

    def daily_bars(sym, now, n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("OpenD 暂时断开")
        return bars

    rt = SimpleNamespace(provider=SimpleNamespace(daily_bars=daily_bars), now=lambda: US_OPEN)
    assert mctx.market_regime(rt) is None
    assert mctx.market_regime(rt) is None and calls["n"] == 1  # 10 分钟内不重复请求
    rt.regime_try = time.monotonic() - mctx.UNKNOWN_RETRY_S - 1
    assert mctx.market_regime(rt)["risk_off"] is False and calls["n"] == 2


def test_error_in_earnings_rule_does_not_block_stop_loss(env, monkeypatch):
    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, eng, fb, clock = _run(prov, ["US.UPA"], ScriptedAI(lambda p: _buy(p)), n=4)
    with read_session("sim") as s:
        assert ledger.get_open_position(s, pid, "US.UPA") is not None

    def boom(*a, **k):
        raise KeyError("pub_type")

    monkeypatch.setattr(mctx, "exit_before_earnings", boom)
    prov.set_quote("US.UPA", "60.50")  # 跌破止损 61
    clock.advance(minutes=1)
    eng.tick()
    with read_session("sim") as s:
        sells = list(s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).scalars())
    assert sells and sells[0].reason == "EXIT_STOP"


def test_scorecard_counts_trading_days_and_does_not_overstate_drawdown(env):
    from app.core.audit import audit
    from app.learning import scorecard
    from helpers import setup_run

    from app.db.models import Authorization

    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "1000"})
    with write_session("sim") as s:
        for a in s.execute(select(Authorization).where(Authorization.portfolio_id == pid)).scalars():
            a.valid_from = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)  # 10-21（周三）开盘前开始运行
        for r in s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == pid)).scalars():
            s.delete(r)
        s.flush()
        for i in range(12):  # 10-16 到 10-27，含两个周末
            d = date(2026, 10, 16) + timedelta(days=i)
            eq = Decimal(1000) if d.isoformat() <= "2026-10-20" else Decimal(1100)
            s.add(DailyEquity(portfolio_id=pid, currency="USD", day_myt=d.isoformat(), start_equity=eq,
                              last_equity=eq, min_equity=eq, flows=Decimal(0), hwm_adjusted=eq,
                              updated_at=datetime(d.year, d.month, d.day, 23, tzinfo=timezone.utc)))  # 收市后仍在更新
        s.flush()
        # 某天先跌到 950 再涨到 1100：回撤是 5%，不是 (1100-950)/1100 = 13.6%
        row = s.execute(select(DailyEquity).where(DailyEquity.day_myt == "2026-10-20")).scalar_one()
        row.min_equity, row.last_equity, row.hwm_adjusted = Decimal(950), Decimal(1100), Decimal(1100)
        # 10-23：工作进程在开盘前就停了（记录停在 13:00 UTC）→ 停机日
        row = s.execute(select(DailyEquity).where(DailyEquity.day_myt == "2026-10-23")).scalar_one()
        row.updated_at = datetime(2026, 10, 23, 13, tzinfo=timezone.utc)
        # 10-26：盘中睡眠了一段时间，之后恢复（记录在收市后仍更新，但有停机记录）→ 停机日
        audit(s, "PROTECTION", "WORKER_DOWNTIME", "test", portfolio_id=pid,
              data={"overlaps": [{"market": "US", "day": "2026-10-26", "minutes": 40}]})
    with read_session("sim") as s:
        out = scorecard.build(s, pid, price_of=lambda sym: None, now=US_OPEN + timedelta(days=7))
    assert out["sessions"] == 3  # 10-21、10-22、10-27；周末不算，10-28 还没收市
    assert out["downtime_days"] == ["2026-10-23", "2026-10-26"]
    item = next(i for i in out["gate_a"]["items"] if i["name"].startswith("交易时段停机"))
    assert item["status"] == "FAIL" and "2026-10-23" in item["value"]
    assert out["numbers"]["max_drawdown_pct"] == "5.00%"
    # 运行在开盘后才开始：第一天只跑了半天，两类都不算
    with write_session("sim") as s:
        for a in s.execute(select(Authorization).where(Authorization.portfolio_id == pid)).scalars():
            a.valid_from = datetime(2026, 10, 21, 15, tzinfo=timezone.utc)
    with read_session("sim") as s:
        out = scorecard.build(s, pid, price_of=lambda sym: None, now=US_OPEN + timedelta(days=7))
    assert out["sessions"] == 2 and "2026-10-21" not in out["downtime_days"]


def test_downtime_overlap_with_trading_session_is_recorded(env):
    from app.core.audit import audit as _a  # noqa: F401
    from app.db.models import AuditEvent, Notification
    from app.workers import downtime
    from helpers import setup_run

    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "2448"})
    with write_session("sim") as s:
        from app.db.models import Portfolio

        s.get(Portfolio, pid).data_source = "MOOMOO_REALTIME"
    # 周三 04:18 UTC → 周四 06:36 UTC：覆盖了周三整个美股常规时段（13:30–20:00 UTC）
    start = datetime(2026, 10, 28, 4, 18, tzinfo=timezone.utc)
    end = datetime(2026, 10, 29, 6, 36, tzinfo=timezone.utc)
    ov = downtime.record_downtime("sim", start, end, "测试")
    assert [(o["market"], o["day"], o["minutes"]) for o in ov] == [("US", "2026-10-28", 390)]
    with read_session("sim") as s:
        ev = s.execute(select(AuditEvent).where(AuditEvent.action == "WORKER_DOWNTIME")).scalar_one()
        assert ev.portfolio_id == pid and "止损" in ev.message
        assert s.execute(select(Notification).where(Notification.level == "error")).first()
    # 周末或夜里停机：不影响交易时段，不记录
    assert downtime.record_downtime("sim", datetime(2026, 10, 31, 1, tzinfo=timezone.utc),
                                    datetime(2026, 11, 1, 23, tzinfo=timezone.utc), "周末") == []
    # 短于 2 分钟：不记录
    assert downtime.record_downtime("sim", end, end + timedelta(seconds=90), "短") == []


def test_worker_restart_records_gap_since_last_heartbeat(env, monkeypatch):
    from app.db.models import Portfolio, WorkerHeartbeat
    from app.workers import main as wmain
    from helpers import setup_run

    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "2448"})
    with write_session("sim") as s:
        s.get(Portfolio, pid).data_source = "MOOMOO_REALTIME"
        s.add(WorkerHeartbeat(name="sim", owner="mac:123", pid=123, started_at=US_OPEN, heartbeat_at=US_OPEN))
    assert wmain.last_seen("sim") == (US_OPEN, False)
    seen = []
    monkeypatch.setattr(wmain.downtime, "record_downtime", lambda ns, a, b, why: seen.append((ns, a, why)) or [])
    monkeypatch.setattr(wmain, "TradingEngine", lambda ns, worker_id: SimpleNamespace(tick=lambda: None,
                                                                                       heartbeat=lambda o, p: None))
    assert wmain.main(["--ns", "sim", "--max-ticks", "1"]) == 0
    assert seen and seen[0][1] == US_OPEN and "异常中断" in seen[0][2]
    # 正常停止后：记录为正常停止，时间按释放前的心跳
    assert wmain.last_seen("sim")[1] is True


def test_tick_rounding_never_shows_scientific_notation():
    from app.core.money import round_to_tick

    assert str(round_to_tick("US", Decimal("250.00"), "down")) == "250"
    assert f"@≤{round_to_tick('US', Decimal('2500.004'), 'down')}" == "@≤2500"
    assert str(round_to_tick("US", Decimal("182.385"), "up")) == "182.39"


def test_overview_marks_position_below_stop_while_market_closed(env, monkeypatch):
    from app.api import views
    from app.db.models import QuoteSnapshot, WorkerHeartbeat

    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, eng, fb, clock = _run(prov, ["US.UPA"], ScriptedAI(lambda p: _buy(p)), n=4)
    with write_session("sim") as s:
        assert ledger.get_open_position(s, pid, "US.UPA").stop_price == Decimal("61.00")
        s.execute(select(QuoteSnapshot).where(QuoteSnapshot.symbol == "US.UPA")).scalar_one().last = Decimal("62.00")
        hb = WorkerHeartbeat(name="sim", owner="t", pid=1, started_at=US_OPEN, heartbeat_at=US_OPEN)
        s.add(hb)
        hb.status = {"portfolios": {str(pid): {"quote_sessions": {"US.UPA": {"pre_price": "63.00", "after_price": "60.50"}}}}}
    saturday = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(views, "business_time", lambda p: saturday)
    with read_session("sim") as s:
        pos = views.overview(s, "sim")["positions"][0]
    # 周末：用周五的盘后价 60.50（不拿周五早上的盘前价 63），上一收盘价 62 仍高于止损 61
    assert pos["market_open"] is False and pos["ext_price"] == "60.50" and pos["ext_label"] == "盘后价"
    assert pos["below_stop"] is True and "收盘价" in pos["last_label"]
    # 交易日开盘前（美东 08:00）：用盘前价 63，高于止损
    monkeypatch.setattr(views, "business_time", lambda p: datetime(2026, 11, 2, 13, tzinfo=timezone.utc))
    with read_session("sim") as s:
        pos = views.overview(s, "sim")["positions"][0]
    assert pos["ext_label"] == "盘前价" and pos["ext_price"] == "63.00" and pos["below_stop"] is False
    monkeypatch.setattr(views, "business_time", lambda p: US_OPEN)  # 交易时段：只看最新成交价
    with read_session("sim") as s:
        pos = views.overview(s, "sim")["positions"][0]
    assert pos["market_open"] is True and pos["below_stop"] is False


def test_worker_loop_records_long_pause_such_as_sleep(env, monkeypatch):
    from app.db.models import Portfolio
    from app.workers import main as wmain
    from helpers import setup_run

    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "2448"})
    with write_session("sim") as s:
        s.get(Portfolio, pid).data_source = "MOOMOO_REALTIME"
    clock = {"t": US_OPEN}
    monkeypatch.setattr(wmain, "utcnow", lambda: clock["t"])

    def tick():  # 第一轮 tick 中途电脑睡了 40 分钟
        clock["t"] = clock["t"] + timedelta(minutes=40)

    seen = []
    monkeypatch.setattr(wmain.downtime, "record_downtime", lambda ns, a, b, why: seen.append((a, b, why)) or [])
    monkeypatch.setattr(wmain, "TradingEngine", lambda ns, worker_id: SimpleNamespace(tick=tick, heartbeat=lambda o, p: None))
    assert wmain.main(["--ns", "sim", "--max-ticks", "2"]) == 0
    assert seen and seen[0][0] == US_OPEN and seen[0][1] - seen[0][0] == timedelta(minutes=40) and "睡眠" in seen[0][2]
