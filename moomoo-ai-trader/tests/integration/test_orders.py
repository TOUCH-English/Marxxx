"""验收 4、5、6、11、12：重复、未知订单、回调乱序、部分成交撤单、撤单中成交、手动改仓、防超卖。"""
import time
from decimal import Decimal

from sqlalchemy import func, select

from app.broker.base import FillEvent
from app.core.enums import IntentReason, OrderStatus
from app.db.models import CashLedger, Fill, OrderIntent, Position, RiskLock, RiskReservation
from app.db.session import read_session, write_session
from app.orders import coordinator
from app.portfolio import ledger
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend


def _setup(ns="live", mode="LIVE_AUTO", fill=True, **kw):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, _ = setup_run(ns, mode, symbols=["US.UPA"], **kw)
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine(ns, prov, clock)
    return prov, pid, clock, eng, fb


def _intents(ns, pid, **flt):
    with read_session(ns) as s:
        q = select(OrderIntent).where(OrderIntent.portfolio_id == pid)
        for k, v in flt.items():
            q = q.where(getattr(OrderIntent, k) == v)
        return list(s.execute(q.order_by(OrderIntent.created_at_utc)).scalars())


def _run(eng, n=4):
    for _ in range(n):
        eng.tick()
        time.sleep(0.05)


def test_timeout_after_accept_goes_unknown_and_is_never_resent(env):
    prov, pid, clock, eng, fb = _setup()
    fb.timeout_after_accept = True
    _run(eng, 3)
    its = _intents("live", pid, side="BUY")
    assert len(its) == 1 and its[0].status == OrderStatus.UNKNOWN
    assert len(fb.place_calls) == 1, "不能重发 place_order"
    with read_session("live") as s:
        assert s.execute(select(RiskLock).where(RiskLock.kind == "UNKNOWN_ORDER", RiskLock.active.is_(True))).first()
        r = s.execute(select(RiskReservation).where(RiskReservation.intent_id == its[0].id)).scalar_one()
        assert r.status == "ACTIVE", "未知状态保留资金预留"
    # 更多轮次也不会重发
    clock.advance(minutes=61)
    _run(eng, 3)
    assert len(fb.place_calls) == 1
    # 重启（新引擎实例）→ 启动对账通过券商查询找到该订单（按 remark=intent_id 关联）
    fb.timeout_after_accept = False
    from helpers import make_engine as mk

    eng2, _ = mk("live", prov, clock, adapter=fb)
    _run(eng2, 2)
    it = _intents("live", pid, side="BUY")[0]
    # 对账确认券商已接收（随后因开仓委托有效期已过被正常撤销）
    assert it.status != OrderStatus.UNKNOWN and it.broker_order_id == "FK000001"
    with read_session("live") as s:
        assert not s.execute(select(RiskLock).where(RiskLock.kind == "UNKNOWN_ORDER", RiskLock.active.is_(True))).first()
    # 同一个订单意图从未被重发（之后若有新的 AI 决策，会是新的意图、新的 remark）
    assert len([c for c in fb.place_calls if c["remark"] == it.id]) == 1


def test_crash_after_submit_marks_unknown_on_restart(env):
    """进程在 SUBMITTING 后崩溃 → 重启对账标记 UNKNOWN，查询券商后归并，不重发。"""
    prov, pid, clock, eng, fb = _setup()
    # 模拟崩溃：直接把一个意图卡在 SUBMITTING，同时券商侧其实已经收到
    _run(eng, 1)
    with write_session("live") as s:
        it = s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid)).scalars().first()
    if it is None:
        _run(eng, 2)
    its = _intents("live", pid)
    assert its, "应已有买入意图"
    with write_session("live") as s:
        i = s.get(OrderIntent, its[0].id)
        i.status = OrderStatus.SUBMITTING
        i.broker_order_id = None
    eng2, _ = make_engine("live", prov, clock, adapter=fb)
    _run(eng2, 1)
    i = _intents("live", pid)[0]
    assert i.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.UNKNOWN)
    assert len(fb.place_calls) == 1


def test_callback_before_return_and_fills_dedup(env):
    prov, pid, clock, eng, fb = _setup()
    fb.callback_before_return = True
    fb.fill_immediately = True
    fb.duplicate_callbacks = True  # 每笔成交推送两次
    _run(eng, 4)
    its = _intents("live", pid, side="BUY")
    assert len(its) == 1 and its[0].status == OrderStatus.FILLED
    with read_session("live") as s:
        nf = s.execute(select(func.count()).select_from(Fill)).scalar_one()
        assert nf == 1, "重复回调只记账一次"
        p = ledger.get_open_position(s, pid, "US.UPA")
        assert p.qty == its[0].qty
        # 重放全部成交（模拟对账再次拉取）→ 仍然一次
    for f in fb.fills:
        fb.events.put(f)
    eng.runtimes[pid].om.process_events()
    with read_session("live") as s:
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 1


def test_partial_fill_then_cancel_and_fill_during_cancel(env):
    prov, pid, clock, eng, fb = _setup()
    _run(eng, 3)
    it = _intents("live", pid, side="BUY")[0]
    assert it.status == OrderStatus.ACKNOWLEDGED
    qty = it.qty
    assert qty >= 3
    fb.fill(it.broker_order_id, 1, it.limit_price)
    eng.runtimes[pid].om.process_events()
    assert _intents("live", pid, side="BUY")[0].status == OrderStatus.PARTIALLY_FILLED
    # 撤单期间又成交 1 股
    fb.fill_on_cancel = 1
    eng.runtimes[pid].om.cancel(it.id, "测试撤单")
    eng.runtimes[pid].om.process_events()
    it = _intents("live", pid, side="BUY")[0]
    assert it.status == OrderStatus.CANCELLED and it.filled_qty == 2
    with read_session("live") as s:
        r = s.execute(select(RiskReservation).where(RiskReservation.intent_id == it.id)).scalar_one()
        assert r.status in ("RELEASED", "CONSUMED") and r.cash_amount == 0, "只释放未成交部分"
        assert ledger.get_open_position(s, pid, "US.UPA").qty == 2
        cash = ledger.cash_balance(s, pid, "USD")
        spent = sum((f.price * f.qty + f.fee for f in s.execute(select(Fill)).scalars()), Decimal(0))
        assert cash == Decimal("10000") - spent


def test_cancel_rejected_keeps_broker_truth(env):
    prov, pid, clock, eng, fb = _setup()
    _run(eng, 3)
    it = _intents("live", pid, side="BUY")[0]
    fb.cancel_reject = "订单已全部成交"
    fb.fill(it.broker_order_id, it.qty, it.limit_price)
    st = eng.runtimes[pid].om.cancel(it.id, "测试")
    eng.runtimes[pid].om.process_events()
    it = _intents("live", pid, side="BUY")[0]
    assert it.status == OrderStatus.FILLED and it.filled_qty == it.qty


def test_manual_app_trade_triggers_reconcile_lock_without_auto_fix(env):
    prov, pid, clock, eng, fb = _setup(ns="sim", mode="LOCAL_PAPER")
    fb.callback_before_return = fb.fill_immediately = True
    _run(eng, 3)
    with read_session("sim") as s:
        p = ledger.get_open_position(s, pid, "US.UPA")
    assert p is not None
    fb.set_position("US.UPA", p.qty - 1)  # 用户在 App 里卖掉 1 股
    calls = len(fb.place_calls)
    for _ in range(2):
        eng.runtimes[pid].last_reconcile_wall = None
        _run(eng, 1)
    with read_session("sim") as s:
        lk = s.execute(select(RiskLock).where(RiskLock.kind == "RECONCILE", RiskLock.scope == "US.UPA",
                                              RiskLock.active.is_(True))).scalar_one()
        assert "少于系统账本" in lk.reason
        assert ledger.get_open_position(s, pid, "US.UPA").qty == p.qty, "不擅自修正账本"
    assert len(fb.place_calls) == calls, "不擅自下单修正账户"
    # 该证券的保护与 AI 管理都暂停：即使跌破止损也不自动卖
    prov.set_quote("US.UPA", str(p.stop_price - 1))
    clock.advance(minutes=61)
    _run(eng, 2)
    assert len(fb.place_calls) == calls


def test_stop_and_ai_exit_together_never_oversell(env):
    prov, pid, clock, eng, fb = _setup()
    fb.callback_before_return = fb.fill_immediately = True
    _run(eng, 3)
    fb.fill_immediately = False  # 之后的卖单不自动成交，制造在途卖单
    with write_session("live") as s:
        pos = ledger.get_open_position(s, pid, "US.UPA")
        qty = pos.qty
        # AI 先提交一个卖出全部的意图（未成交）
        it, _ = coordinator.request_exit(s, pos=pos, reason=IntentReason.EXIT_AI, bid=Decimal("64"), params={"exit_limit_discount_bps": 50, "exit_reprice_minutes": 10},
                                         mode="LIVE_AUTO", account_id="x", auth_id=None, now=clock.now(), fraction=Decimal(1))
        assert it.qty == qty
        # 同一时刻止损也想卖：可卖数量为 0 → 不产生新卖单
        it2, why = coordinator.request_exit(s, pos=pos, reason=IntentReason.EXIT_STOP, bid=Decimal("60"),
                                            params={"exit_limit_discount_bps": 50, "exit_reprice_minutes": 10},
                                            mode="LIVE_AUTO", account_id="x", auth_id=None, now=clock.now())
        assert it2 is None and "可卖数量为 0" in why
    # 引擎层：止损触发 → 撤销非保护卖单，之后补足；任何时候在途卖单总量 ≤ 持仓
    _run(eng, 1)  # 提交 AI 卖单
    prov.set_quote("US.UPA", "55")
    for _ in range(6):
        eng.tick()
        with read_session("live") as s:
            pos = ledger.get_open_position(s, pid, "US.UPA")
            if pos is None:
                break
            pend = sum(i.qty - i.filled_qty for i in coordinator.open_sells(s, pid, "US.UPA"))
            assert pend <= pos.qty
        # 让券商成交在途卖单
        for oid, o in list(fb.orders.items()):
            if o["side"] == "SELL" and o["status"] in ("SUBMITTED",):
                fb.fill(oid, o["qty"] - o["dealt"], o["price"])
        eng.runtimes[pid].om.process_events()
    with read_session("live") as s:
        sold = sum(f.qty for f in s.execute(select(Fill).where(Fill.side == "SELL")).scalars())
        assert sold == qty, "卖出总量恰好等于持仓，未超卖"
        assert s.execute(select(Position).where(Position.symbol == "US.UPA")).scalars().first().status == "CLOSED"
    assert any(i.reason == "EXIT_STOP" for i in _intents("live", pid))


def test_duplicate_signal_and_double_click_do_not_duplicate_orders(env):
    from app.risk.engine import EntryRequest, create_entry_intent

    prov, pid, clock, eng, fb = _setup(ns="sim", mode="LOCAL_PAPER")
    _run(eng, 3)
    n = len(_intents("sim", pid, side="BUY"))
    assert n == 1
    first = _intents("sim", pid, side="BUY")[0]
    # 同一 AI 决策再次处理（重复回调/重复点击）→ 去重
    rt = eng.runtimes[pid]
    ts = eng._tick_state(rt, __import__("app.db.types", fromlist=["x"]).utcnow())
    from app.strategies.common import risk_ctx

    with write_session("sim") as s:
        req = EntryRequest(symbol="US.UPA", instrument=prov.instrument("US.UPA"), quote=rt.quote("US.UPA"),
                           stop_price=Decimal("60"), intent_key=first.intent_key, strategy_version="x",
                           strategy_version_id=ts.strategy.id)
        it, res = create_entry_intent(s, risk_ctx(rt, ts, __import__("app.db.types", fromlist=["x"]).utcnow()), req)
        assert it is None
    assert len(_intents("sim", pid, side="BUY")) == 1


def test_second_worker_cannot_start(env):
    from filelock import FileLock

    from app.config import get_settings
    from app.workers.main import acquire_lease, main

    from app.services.runs import ensure_seed

    ensure_seed("sim")
    assert acquire_lease("sim", "worker-A")
    assert not acquire_lease("sim", "worker-B"), "数据库租约阻止第二个工作进程"
    lock = FileLock(str(get_settings().data_dir / "sim.worker.lock"))
    lock.acquire()
    try:
        assert main(["--ns", "sim", "--max-ticks", "1"]) == 2, "文件锁阻止第二个工作进程"
    finally:
        lock.release()
