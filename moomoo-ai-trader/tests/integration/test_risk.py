"""验收 3、7、9、10：并发不超额、各类风控拒绝、锁定持续、暂停与紧急停止的边界。"""
import threading
import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.enums import LockKind, Mode, OrderStatus
from app.db.models import Authorization, OrderIntent, Portfolio, RiskLock
from app.db.session import read_session, write_session
from app.db.types import D, utcnow
from app.portfolio import ledger
from app.risk import locks
from app.risk.engine import EntryRequest, RiskContext, create_entry_intent, evaluate_entry
from app.risk.policy import SEED_RISK_PARAMS
from app.core.calendar import get_calendar
from app.core.money import TEST_FEE_SCHEDULES
from helpers import MY_OPEN, US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend


def _ctx(ns, pid, prov, now, scope, mode="LIVE_AUTO", params=None, broker_cash=None):
    with read_session(ns) as s:
        p = s.get(Portfolio, pid)
        a = s.execute(select(Authorization).where(Authorization.portfolio_id == pid)).scalars().first()
        from app.db.models import RiskPolicyVersion

        rpv = s.get(RiskPolicyVersion, a.risk_policy_version_id)
    from app.broker.base import CashInfo

    bc = broker_cash if broker_cash is not None else {"USD": CashInfo("USD", Decimal("1000000"), Decimal("1000000"), reliable=True),
                                                      "MYR": CashInfo("MYR", Decimal("1000000"), Decimal("1000000"), reliable=True)}
    return RiskContext(mode=Mode(mode), portfolio=p, auth=a, params=params or rpv.params, risk_policy_version=rpv.version,
                       risk_policy_version_id=rpv.id, now=now, wall_now=utcnow(), calendar=get_calendar(), is_replay=False,
                       fee_schedules=TEST_FEE_SCHEDULES, scope_symbols=set(scope), account_id="x", broker_cash=bc,
                       price_of=lambda sym: prov.q[sym].last if sym in prov.q else None)


def _req(prov, sym, key, stop, now, **kw):
    return EntryRequest(symbol=sym, instrument=prov.instrument(sym), quote=prov.quote(sym, now), stop_price=Decimal(stop),
                        intent_key=key, strategy_version="AI_AUTONOMOUS:v1", strategy_version_id=kw.pop("svid", 1), **kw)


def test_concurrent_entries_cannot_exceed_cash_or_slots(env):
    prov = StaticProvider()
    for i in range(6):
        prov.add(f"US.C{i}", trend(80, 50, 0.05), "54")
    syms = [f"US.C{i}" for i in range(6)]
    # 小资金：每笔都被单标的上限限制；名额 2；两两并发
    pid, _ = setup_run("live", "LIVE_AUTO", symbols=syms, capital={"USD": "3000"},
                       risk={"max_positions": 2, "max_position_pct": "0.5", "max_total_exposure_pct": "1.0",
                             "risk_per_trade_pct": "0.02", "daily_loss_pct": "0.10"})
    ctx = _ctx("live", pid, prov, US_OPEN, syms)
    results = []
    barrier = threading.Barrier(6)

    def worker(sym):
        barrier.wait()
        with write_session("live") as s:
            it, res = create_entry_intent(s, ctx, _req(prov, sym, f"k-{sym}", "50", US_OPEN))
            results.append((sym, it is not None, res.reasons))

    ts = [threading.Thread(target=worker, args=(s,)) for s in syms]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok = [r for r in results if r[1]]
    assert len(ok) == 2, f"名额 2，实际通过 {len(ok)}：{results}"
    with read_session("live") as s:
        reserved = ledger.reserved_cash(s, pid, "USD")
        assert reserved <= Decimal("3000")


def test_concurrent_entries_cannot_double_spend_cash(env):
    prov = StaticProvider()
    prov.add("US.A", trend(80, 50, 0.05), "54")
    prov.add("US.B", trend(80, 50, 0.05), "54")
    pid, _ = setup_run("live", "LIVE_AUTO", symbols=["US.A", "US.B"], capital={"USD": "1000"},
                       risk={"max_position_pct": "0.5", "max_total_exposure_pct": "1.0", "risk_per_trade_pct": "0.02"})
    ctx = _ctx("live", pid, prov, US_OPEN, ["US.A", "US.B"])
    # 先占用大部分现金
    with write_session("live") as s:
        it, res = create_entry_intent(s, ctx, _req(prov, "US.A", "a", "50", US_OPEN))
        assert it is not None
    out = []
    bar = threading.Barrier(2)

    def w(k):
        bar.wait()
        with write_session("live") as s:
            it, res = create_entry_intent(s, ctx, _req(prov, "US.B", k, "50", US_OPEN))
            out.append(it)

    ts = [threading.Thread(target=w, args=(k,)) for k in ("b1", "b2")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with read_session("live") as s:
        assert ledger.reserved_cash(s, pid, "USD") <= Decimal("1000"), "并发预留不能超过可用现金"


def test_rejections(env):
    prov = StaticProvider()
    prov.add("US.OK", trend(80, 50, 0.05), "54")
    prov.add("US.OUT", trend(80, 50, 0.05), "54")
    prov.add("US.STALE", trend(80, 50, 0.05), "54")
    prov.add("US.HALT", trend(80, 50, 0.05), "54")
    prov.add("US.WIDE", trend(80, 50, 0.05), "54")
    prov.add("US.LEV", trend(80, 50, 0.05), "54", leveraged=True)
    prov.add("US.ETFU", trend(80, 50, 0.05), "54", sec_type="ETF_UNVERIFIED")  # 实时行情里未确认非杠杆的 ETF
    prov.add("US.CCY", trend(80, 50, 0.05), "54", currency="MYR")
    prov.add("MY.LOT", trend(80, 2, 0.001), "2.10", market="MY", lot=100)
    prov.set_quote("US.STALE", "54", age_s=120)
    prov.set_quote("US.HALT", "54", suspended=True)
    prov.set_quote("US.WIDE", "54", bid="50", ask="58")
    scope = ["US.OK", "US.STALE", "US.HALT", "US.WIDE", "US.LEV", "US.ETFU", "US.CCY", "MY.LOT"]
    pid, _ = setup_run("live", "LIVE_AUTO", symbols=scope, capital={"USD": "10000", "MYR": "300"})
    ctx = _ctx("live", pid, prov, US_OPEN, scope)

    def reasons(sym, now=US_OPEN, stop="50", **kw):
        with read_session("live") as s:
            r = evaluate_entry(s, ctx if now == US_OPEN else _ctx("live", pid, prov, now, scope), _req(prov, sym, f"k{sym}{now}", stop, now, **kw))
            return r.ok, " | ".join(r.reasons)

    assert reasons("US.OK")[0]
    assert "授权范围" in reasons("US.OUT")[1]
    assert "报价已过期" in reasons("US.STALE")[1]
    assert "停牌" in reasons("US.HALT")[1]
    assert "买卖价差" in reasons("US.WIDE")[1]
    assert "杠杆" in reasons("US.LEV")[1]
    assert "证券类型 ETF_UNVERIFIED 不允许" in reasons("US.ETFU")[1]
    assert "币种" in reasons("US.CCY")[1]
    # 休市（美股收盘后）
    assert "交易时段" in reasons("US.OK", now=US_OPEN + timedelta(hours=8))[1]
    # 马股：资金 300 MYR × 10% 上限 = 30 MYR，不足一手（100 股 × 2.10）
    ok, why = reasons("MY.LOT", now=MY_OPEN, stop="2.00")
    assert not ok and "不足一手" in why
    # 缺止损 / 止损在价格之上
    assert "止损" in reasons("US.OK", stop="0")[1] or "止损" in reasons("US.OK", stop="60")[1]
    # 融资：券商现金字段不可靠 → 禁用开仓；现金不足 → 数量受限
    from app.broker.base import CashInfo

    c2 = _ctx("live", pid, prov, US_OPEN, scope, broker_cash={"USD": CashInfo("USD", None, None, reliable=False)})
    with read_session("live") as s:
        r = evaluate_entry(s, c2, _req(prov, "US.OK", "m1", "50", US_OPEN))
        assert not r.ok and "融资" in " ".join(r.reasons)
    c3 = _ctx("live", pid, prov, US_OPEN, scope, broker_cash={"USD": CashInfo("USD", Decimal("10"), Decimal("10"), reliable=True)})
    with read_session("live") as s:
        r = evaluate_entry(s, c3, _req(prov, "US.OK", "m2", "50", US_OPEN))
        assert not r.ok and "券商可结算现金" in " ".join(r.reasons), "不能用购买力/融资替代现金"


def test_illegal_lot_rejected_by_simulator(env):
    from app.simulation.local_sim import LocalSimBroker

    b = LocalSimBroker(env / "sim.json", acc_id="L", fee_schedules=TEST_FEE_SCHEDULES, calendar=get_calendar())
    b.register_instrument("MY.LOT", "MY", "MYR", 100)
    b.fund("MYR", Decimal(10000))
    r = b.place_order(symbol="MY.LOT", side="BUY", qty=150, limit_price=Decimal("2.10"), remark="x", now=MY_OPEN)
    assert r.status == "REJECTED" and "整数倍" in r.message
    r = b.place_order(symbol="MY.LOT", side="BUY", qty=100, limit_price=Decimal("2.105"), remark="x", now=MY_OPEN)
    assert r.status == "REJECTED" and "最小变动单位" in r.message


def test_daily_loss_and_drawdown_locks_persist(env):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "10000"},
                       risk={"daily_loss_pct": "0.001", "max_drawdown_pct": "0.0011"})
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock)
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(4):
        eng.tick()
        time.sleep(0.05)
    with read_session("sim") as s:
        p = ledger.get_open_position(s, pid, "US.UPA")
    assert p is not None
    # 新仓风险不超过当日亏损额度，正常止损不会超线；只有跳空到止损价以下才会触发（这里模拟跳空）
    prov.set_quote("US.UPA", str(p.stop_price - Decimal("10")))
    eng.tick()
    with read_session("sim") as s:
        kinds = {lk.kind for lk in locks.active_locks(s, pid)}
    assert LockKind.DAILY_LOSS in kinds and LockKind.DRAWDOWN in kinds
    # 价格反弹 + 重启（新引擎）→ 仍锁定
    prov.set_quote("US.UPA", str(p.avg_cost + Decimal("5")))
    eng2, _ = make_engine("sim", prov, clock, adapter=fb)
    eng2.tick()
    with read_session("sim") as s:
        kinds = {lk.kind for lk in locks.active_locks(s, pid)}
    assert LockKind.DAILY_LOSS in kinds and LockKind.DRAWDOWN in kinds
    # 过了马来西亚日界 + 健康 → 日亏损锁解除；回撤锁仍需人工复核
    clock.advance(days=1)
    eng2.tick()
    with read_session("sim") as s:
        kinds = {lk.kind for lk in locks.active_locks(s, pid)}
    assert LockKind.DAILY_LOSS not in kinds and LockKind.DRAWDOWN in kinds
    # 新开仓被锁定拦截
    with read_session("sim") as s:
        assert locks.blocking_locks(s, pid, "US.UPA")


def test_pause_keeps_protection_and_emergency_stop_does_not_sell(env):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    prov.add("US.UPB", trend(80, 30.0, 0.03), "32.40")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA", "US.UPB"])
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock)
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(4):
        eng.tick()
        time.sleep(0.05)
    with read_session("sim") as s:
        held = {p.symbol: p for p in ledger.open_positions(s, pid)}
    assert held
    # 暂停新开仓
    with write_session("sim") as s:
        s.get(Portfolio, pid).pause_new_entries = True
    sym, pos = next(iter(held.items()))
    prov.set_quote(sym, str(pos.stop_price - Decimal("0.2")))
    clock.advance(minutes=5)
    for _ in range(3):
        eng.tick()
    with read_session("sim") as s:
        assert ledger.get_open_position(s, pid, sym) is None, "暂停新开仓不影响止损"
    # 紧急停止：挂一个未成交的入场单，然后紧急停止 → 撤销入场单，不卖其他持仓
    fb.fill_immediately = False
    with write_session("sim") as s:
        p = s.get(Portfolio, pid)
        p.pause_new_entries = False
    prov.set_quote(sym, "64.00")
    clock.advance(minutes=61)
    for _ in range(4):
        eng.tick()
        time.sleep(0.05)
    with write_session("sim") as s:
        p = s.get(Portfolio, pid)
        p.emergency_stop = True
        p.pause_new_entries = True
        from app.db.models import Command

        s.add(Command(type="EMERGENCY_STOP", payload={"portfolio_id": pid}))
    before_sells = len([c for c in fb.place_calls if c["side"] == "SELL"])
    with read_session("sim") as s:
        remaining = {p.symbol for p in ledger.open_positions(s, pid)}
    eng.tick()
    with read_session("sim") as s:
        buys_open = s.execute(select(OrderIntent).where(OrderIntent.side == "BUY", OrderIntent.status.in_(
            [OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, OrderStatus.CREATED]))).scalars().all()
        assert not buys_open, "紧急停止撤销入场单"
        assert {p.symbol for p in ledger.open_positions(s, pid)} == remaining, "紧急停止不卖出持仓"
    assert len([c for c in fb.place_calls if c["side"] == "SELL"]) == before_sells


def test_us_daily_limits_follow_the_us_trading_day_not_malaysian_midnight(env):
    """美股盘中跨过马来西亚午夜，日亏损锁和每日开仓次数不能在盘中重置。"""
    from datetime import datetime, timezone

    from app.core.calendar import next_risk_day_start, risk_day

    before = datetime(2026, 10, 22, 15, 50, tzinfo=timezone.utc)  # 马来西亚 23:50，美东 11:50
    after = datetime(2026, 10, 22, 16, 30, tzinfo=timezone.utc)  # 马来西亚 00:30（次日），美东 12:30
    assert risk_day("USD", before) == risk_day("USD", after) == "2026-10-22"
    assert risk_day("MYR", before) != risk_day("MYR", after)
    assert next_risk_day_start("USD", after) == datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)  # 美东午夜
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.X"], capital={"USD": "10000"})
    with write_session("sim") as s:
        locks.update_and_check(s, pid, "USD", Decimal("10000"), {"daily_loss_pct": "0.01", "max_drawdown_pct": "0.5"}, before)
        locks.update_and_check(s, pid, "USD", Decimal("9800"), {"daily_loss_pct": "0.01", "max_drawdown_pct": "0.5"}, before)
    with write_session("sim") as s:
        lk = next(x for x in locks.active_locks(s, pid) if x.kind == LockKind.DAILY_LOSS)
        assert lk.release_not_before == datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
        assert not locks.try_release_daily(s, pid, after, True)  # 马来西亚午夜后仍在同一个美股交易日：不解除


def test_new_trade_risk_counts_risk_already_open(env):
    """已有持仓的止损风险要算进去：新仓风险不超过亏损线剩余额度和当日亏损额度剩余。"""
    prov = StaticProvider()
    prov.add("US.A1", trend(80, 50, 0.05), "54")
    prov.add("US.A2", trend(80, 50, 0.05), "54")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.A1", "US.A2"], capital={"USD": "2448"}, max_loss={"USD": "122"},
                       risk={"max_position_pct": "0.25", "max_total_exposure_pct": "0.60", "risk_per_trade_pct": "0.01",
                             "daily_loss_pct": "0.02", "max_positions": 3})
    ctx = _ctx("sim", pid, prov, US_OPEN, ["US.A1", "US.A2"], mode="LOCAL_PAPER")
    with write_session("sim") as s:
        it1, r1 = create_entry_intent(s, ctx, _req(prov, "US.A1", "k1", "51", US_OPEN))
    assert it1 is not None, r1.reasons
    first_risk = D(r1.sizing["风险预算"])
    assert first_risk <= Decimal("24.48")
    with write_session("sim") as s:
        it2, r2 = create_entry_intent(s, ctx, _req(prov, "US.A2", "k2", "51", US_OPEN))
    notes = r2.sizing
    assert D(notes["风险预算·已开仓风险"]) > 0
    # 当日额度 48.96 − 已开仓风险（第一笔预留）= 第二笔最多可用
    assert D(notes["风险预算"]) <= Decimal("48.96") - D(notes["风险预算·已开仓风险"]) + Decimal("0.01")
