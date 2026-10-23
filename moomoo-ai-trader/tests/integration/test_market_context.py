"""三方审核第 2 批：大盘弱势不开新仓、财报避让与财报前退出、同行业最多 1 只、持仓复核带入买入理由与 R 倍数。"""
import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.ai.providers import FixtureProvider
from app.db.models import Decision, OrderIntent
from app.db.session import read_session
from app.portfolio import ledger
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend
from integration.test_ai_safety import ScriptedAI, _buy


class CtxProvider(StaticProvider):
    def __init__(self, earnings=None, industry=None):
        super().__init__()
        self._earn = earnings or {}
        self._ind = industry or {}

    def earnings_calendar(self, now, days=21):
        return self._earn

    def industries(self, symbols):
        return {s: self._ind[s] for s in symbols if s in self._ind}


def _spy(prov, weak: bool):
    closes = trend(220, 500, -0.5 if weak else 0.5)
    prov.add("US.SPY", closes, str(closes[-1]), name="SPY", sec_type="ETF")


class Rec(FixtureProvider):
    def __init__(self):
        super().__init__()
        self.seen = []

    def complete(self, payload):
        self.seen.append(payload)
        return super().complete(payload)


def _run(prov, symbols, ai, n=3, clock=None):
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=symbols, capital={"USD": "10000"})
    clock = clock or ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock, ai=ai)
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(n):
        eng.tick()
        time.sleep(0.05)
    return pid, eng, fb, clock


def test_weak_market_means_no_new_entries_but_state_is_shown(env):
    prov = CtxProvider()
    _spy(prov, weak=True)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    ai = Rec()
    _run(prov, ["US.UPA"], ai)
    # 没有持仓也没有候选：不调用 AI
    assert ai.seen == []
    prov2 = CtxProvider()
    _spy(prov2, weak=False)
    prov2.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    ai2 = Rec()
    _run(prov2, ["US.UPA"], ai2)
    assert ai2.seen and ai2.seen[0]["market_state"]["risk_off"] is False
    assert "rel_strength_20d_vs_spy" in ai2.seen[0]["candidates"][0]


def test_waiting_buy_is_blocked_when_market_turns_weak(env):
    from app.strategies import market_context

    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    ai = ScriptedAI(lambda p: _buy(p, entry_type="LIMIT_AT_OR_BELOW", entry_price="63.00", max_buy_price="63.00",
                                   stop_loss_price="60.00", take_profit_price="70.00"))
    pid, eng, fb, clock = _run(prov, ["US.UPA"], ai)
    eng.runtimes[pid].regime = (market_context.us_day(clock.now()), {"risk_off": True, "vs_sma200": -0.05})
    prov.set_quote("US.UPA", "62.90")
    clock.advance(minutes=1)
    eng.tick()
    with read_session("sim") as s:
        d = s.execute(select(Decision).where(Decision.symbol == "US.UPA", Decision.source == "AI_MODEL")).scalars().first()
    assert d.outcome == "RISK_BLOCKED" and "大盘弱势" in d.outcome_reason


def test_earnings_soon_is_not_a_candidate_and_shown_as_skipped(env):
    soon = (US_OPEN + timedelta(days=3)).date()
    prov = CtxProvider(earnings={"US.ERN": {"date": soon, "pub_type": "AFTER"}})
    _spy(prov, weak=False)
    prov.add("US.ERN", trend(80, 60.0, 0.05), "64.00")
    prov.add("US.OK", trend(80, 40.0, 0.03), "42.40")
    ai = Rec()
    _run(prov, ["US.ERN", "US.OK"], ai)
    p = ai.seen[0]
    assert [c["symbol"] for c in p["candidates"]] == ["US.OK"]
    assert "财报" in p["skipped_candidates"]["US.ERN"]


def test_holding_exits_before_earnings_near_the_close(env):
    prov = CtxProvider()
    _spy(prov, weak=False)
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    pid, eng, fb, clock = _run(prov, ["US.UPA"], ScriptedAI(lambda p: _buy(p) if p["candidates"] else _buy(p)), n=4)
    with read_session("sim") as s:
        assert ledger.get_open_position(s, pid, "US.UPA") is not None
    prov._earn = {"US.UPA": {"date": US_OPEN.date(), "pub_type": "AFTER"}}  # 今天收市后公布
    eng.tick()  # 距收市还远：不退出
    with read_session("sim") as s:
        assert not s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).first()
    clock.t = US_OPEN.replace(hour=19, minute=40)  # 美东 15:40，距收市 20 分钟
    eng.tick()
    with read_session("sim") as s:
        it = s.execute(select(OrderIntent).where(OrderIntent.side == "SELL")).scalar_one()
        d = s.execute(select(Decision).where(Decision.source == "PROTECTION")).scalars().first()
    assert it.reason == "EXIT_TIME" and "财报" in d.outcome_reason


def test_one_position_per_industry(env):
    prov = CtxProvider(industry={"US.XOM": "石油天然气", "US.CVX": "石油天然气", "US.MSF": "软件"})
    _spy(prov, weak=False)
    for sym, base in (("US.XOM", 100.0), ("US.CVX", 120.0), ("US.MSF", 80.0)):
        prov.add(sym, trend(80, base, 0.05), str(base + 4))
    ai = ScriptedAI(lambda p: _buy(p, symbol="US.XOM", stop_loss_price="100.00", take_profit_price="112.00",
                                   evidence_ids=[e for c in p["candidates"] if c["symbol"] == "US.XOM" for e in c["evidence_ids"][:1]])
                    if any(c["symbol"] == "US.XOM" for c in p["candidates"]) else _buy(p))
    pid, eng, fb, clock = _run(prov, ["US.XOM", "US.CVX", "US.MSF"], ai, n=4)
    with read_session("sim") as s:
        assert ledger.get_open_position(s, pid, "US.XOM") is not None
    rec = Rec()
    eng.runtimes[pid].ai_provider = rec
    eng.runtimes[pid].last_ai_batch.clear()
    eng.tick()
    time.sleep(0.05)
    eng.tick()
    p = rec.seen[-1]
    assert "US.CVX" not in [c["symbol"] for c in p["candidates"]] and "同行业" in p["skipped_candidates"]["US.CVX"]
    assert "US.MSF" in [c["symbol"] for c in p["candidates"]]
    pos = p["positions"][0]
    assert pos["symbol"] == "US.XOM" and pos["entry_thesis"] and pos["initial_risk_per_share"]
    assert pos["r_multiple_now"] is not None


def test_entry_block_reason_checks_industry_directly(env):
    from app.strategies import market_context

    prov = CtxProvider(industry={"US.XOM": "石油天然气", "US.CVX": "石油天然气"})
    _spy(prov, weak=False)
    prov.add("US.XOM", trend(80, 100.0, 0.05), "104")
    prov.add("US.CVX", trend(80, 120.0, 0.05), "124")
    ai = ScriptedAI(lambda p: _buy(p, symbol="US.XOM", stop_loss_price="100.00", take_profit_price="112.00",
                                   evidence_ids=[e for c in p["candidates"] if c["symbol"] == "US.XOM" for e in c["evidence_ids"][:1]])
                    if any(c["symbol"] == "US.XOM" for c in p["candidates"]) else _buy(p))
    pid, eng, fb, clock = _run(prov, ["US.XOM", "US.CVX"], ai, n=4)
    with read_session("sim") as s:
        why = market_context.entry_block_reason(s, eng.runtimes[pid], pid, "US.CVX")
    assert why and "同行业" in why


def test_validator_rejects_holding_period_that_covers_earnings():
    from app.ai.validate import parse_and_validate
    from integration.test_ai_safety import _one, _vctx

    ctx = _vctx()
    ctx.earnings = {"US.Q": (US_OPEN + timedelta(days=8)).date()}
    r = parse_and_validate(_one(max_holding_days=10), ctx)
    assert "财报" in " ".join(e for d in r.decisions for e in d.errors)
    r = parse_and_validate(_one(max_holding_days=5), ctx)
    assert all(d.ok for d in r.decisions)
