"""每日自动股票池：全美股条件选股 → 券商核实（类型、交易所）→ 规则复核 → 排序；
更换交易范围保留持仓；历史 K 线额度保护。全部用假 moomoo 上下文，不连真实 OpenD。"""
import time
from datetime import date, timedelta
from decimal import Decimal

import moomoo as m
import pytest
from sqlalchemy import select

from app.db.models import Authorization, AuditEvent, Portfolio, UniverseRule, UniverseSnapshot
from app.db.session import read_session, write_session
from app.market_data import moomoo_quotes as mq
from app.market_data.universe import AUTO_US_POOL_RULES, screen
from app.services.runs import change_scope
from helpers import US_OPEN, ManualClock, make_engine, setup_run

# 代码 → (价格, 市值, 20日成交额, 20日涨跌%, 60日涨跌%, 类型, 交易所)
UNIVERSE = {
    "US.AAA": (100, 5e11, 9e9, 8, 20, "STOCK", "US_NASDAQ"),
    "US.BBB": (50, 2e11, 8e9, 2, 5, "STOCK", "US_NYSE"),
    "US.CCC": (300, 9e10, 7e9, 12, 1, "STOCK", "US_NYSE"),
    "US.DDDY": (40, 8e10, 6e9, 30, 40, "STOCK", "US_PINK"),  # 场外 ADR：排除
    "US.EEE": (80, 7e10, 5e9, 15, 30, "ETF", "US_NYSE"),  # ETF：排除
    "US.FFF": (20, 6e10, 4e9, -5, -10, "STOCK", "US_AMEX"),
    "US.GGG": (900, 5e11, 3e9, 1, 1, "STOCK", "US_NASDAQ"),  # 价格高于 600：排除
    "US.HHH": (60, 3e10, 2e9, 3, 3, "STOCK", None),  # 券商资料查不到：排除
}


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, orient):
        return self.rows


class _Item:
    def __init__(self, code, v):
        self.stock_code, self.stock_name = code, code + " Inc"
        price, cap, turn, c20, c60 = v[:5]
        self.__dict__.update({"cur_price": price, "market_val": cap, ("turnover", 20): turn,
                              ("change_rate", 20): c20, ("change_rate", 60): c60, "pe_ttm": 25.0,
                              "change_rate_begin_year": 10.0})


class FakeCtx:
    def __init__(self, remain=280):
        self.filters, self.klines, self.basic = [], [], []
        self.remain, self.seen = remain, set()
        self.fail_filter = False
        self.fail_basic = False
        self.on_filter = None

    def get_stock_filter(self, market, filter_list, begin=0, num=200):
        self.filters.append((market, len(filter_list), begin, num))
        if self.on_filter:
            self.on_filter()
        if self.fail_filter:
            return m.RET_ERROR, "busy"
        items = [_Item(c, v) for c, v in sorted(UNIVERSE.items(), key=lambda kv: -kv[1][2])]
        return m.RET_OK, (True, len(items), items[begin:begin + num])

    def get_stock_basicinfo(self, market, stock_type, code_list):
        self.basic.append(list(code_list))
        if self.fail_basic:
            return m.RET_ERROR, "timeout"
        return m.RET_OK, _Rows([{"code": c, "name": c + " Inc", "lot_size": 1, "stock_type": UNIVERSE[c][5],
                                  "exchange_type": UNIVERSE[c][6], "delisting": False}
                                 for c in code_list if c in UNIVERSE and UNIVERSE[c][6]])

    def get_market_snapshot(self, codes):
        return m.RET_OK, _Rows([{"code": c, "last_price": UNIVERSE.get(c, (100,))[0], "bid_price": UNIVERSE.get(c, (100,))[0] - 0.01,
                                  "ask_price": UNIVERSE.get(c, (100,))[0] + 0.01, "volume": 1, "suspension": False,
                                  "update_time": "2026-10-21 11:00:00", "lot_size": 1} for c in codes])

    def get_history_kl_quota(self, get_detail=False):
        return m.RET_OK, (300 - self.remain, self.remain, [{"code": c} for c in self.seen])

    def request_history_kline(self, code, start, end, ktype, autype, max_count):
        self.klines.append(code)
        px = UNIVERSE.get(code, (100,))[0]
        rows = [{"time_key": f"{date(2026, 10, 20) - timedelta(days=i)} 00:00:00", "open": px, "high": px * 1.01,
                 "low": px * 0.99, "close": px * (1 - i * 0.001), "volume": 1_000_000} for i in range(80)][::-1]
        return m.RET_OK, _Rows(rows), None

    def get_global_state(self):
        return m.RET_OK, {"qot_logined": True}


def _prov(ctx):
    return mq.MoomooQuoteProvider(ctx_factory=lambda: ctx)


def test_scan_keeps_only_listed_verified_stocks_and_ranks_them(env):
    ctx = FakeCtx()
    members, excluded, stats = screen(_prov(ctx), AUTO_US_POOL_RULES | {"max_members": 3}, US_OPEN)
    syms = [x["symbol"] for x in members]
    assert syms == ["US.AAA", "US.CCC", "US.BBB"]  # 分数 0.6×20日 + 0.4×60日：12.8 > 7.6 > 3.2（FFF 为负，被上限挤出）
    why = {x["symbol"]: " ".join(x["reasons"]) for x in excluded}
    assert "OTC" in why["US.DDDY"] and "ETF" in why["US.EEE"]
    assert "价格高于 600" in why["US.GGG"] and "未核实" in why["US.HHH"]
    assert "超过成员上限" in why["US.FFF"]
    assert stats["matched_total"] == 8 and stats["members"] == 3 and "条件选股" in stats["method"]
    assert members[0]["pool_rank"] == 1 and members[0]["market_cap_usd"] == "500000000000"
    assert ctx.klines == []  # 扫描全市场不下载日 K，不占历史 K 线额度
    assert len(ctx.basic) == 1  # 一次性核实全部候选


def test_kline_quota_guard_blocks_new_symbols_only(env):
    ctx = FakeCtx(remain=mq.KL_QUOTA_RESERVE)
    ctx.seen = {"US.AAA"}
    prov = _prov(ctx)
    assert prov.daily_bars("US.AAA", US_OPEN, 30)  # 30 天内已计过额度：可以再取
    assert prov.daily_bars("US.BBB", US_OPEN, 30) == []  # 新证券且余量不足：不取
    assert ctx.klines.count("US.BBB") == 0 and "额度" in prov._err


def _auto_rule(confirmed=True) -> int:
    with write_session("sim") as s:
        r = UniverseRule(version=99, rules=dict(AUTO_US_POOL_RULES), confirmed_by_user=confirmed)
        s.add(r)
        s.flush()
        return r.id


def test_change_scope_to_daily_pool_keeps_run_positions_and_limits(env):
    from integration.test_ai_safety import ScriptedAI, _buy

    ctx = FakeCtx()
    prov = _prov(ctx)
    pid, aid = setup_run("sim", "LOCAL_PAPER", symbols=["US.BBB"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock, ai=ScriptedAI(lambda p: _buy(p, stop_loss_price="47.00")))
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(4):
        eng.tick()
        time.sleep(0.05)
    from app.portfolio import ledger

    with read_session("sim") as s:
        held = [p.symbol for p in ledger.open_positions(s, pid)]
        old = s.get(Authorization, aid)
        flows_before = ledger.total_flows(s, pid, "USD")
    assert held == ["US.BBB"]
    # 规则未确认 → 拒绝
    rid_draft = _auto_rule(confirmed=False)
    with pytest.raises(ValueError):
        change_scope("sim", pid, {"type": "POOL", "universe_rule_id": rid_draft}, "user")
    rid = _auto_rule()
    a = change_scope("sim", pid, {"type": "POOL", "universe_rule_id": rid}, "user")
    with read_session("sim") as s:
        assert s.get(Authorization, aid).status == "SUPERSEDED"
        new = s.get(Authorization, a.id)
        assert new.status == "ACTIVE" and new.portfolio_id == pid and new.scope["type"] == "POOL"
        assert new.capital == old.capital and new.max_planned_loss == old.max_planned_loss
        assert new.risk_policy_version_id == old.risk_policy_version_id and new.overnight == old.overnight
        assert new.expires_at == old.expires_at
        assert [p.symbol for p in ledger.open_positions(s, pid)] == ["US.BBB"]  # 持仓保留
        assert ledger.total_flows(s, pid, "USD") == flows_before  # 没有新增资金流
        assert s.get(Portfolio, pid).running
        assert len(list(s.execute(select(Portfolio).where(Portfolio.running.is_(True))).scalars())) == 1
    # 新范围生效：当天扫描一次，候选来自股票池，并带上入池数据
    seen = []
    eng.runtimes[pid].ai_provider = _Recorder(seen)
    eng.runtimes[pid].last_ai_batch.clear()
    for _ in range(3):
        eng.tick()
        time.sleep(0.05)
    assert len(ctx.filters) == 1  # 同一美股交易日只扫描一次
    with read_session("sim") as s:
        snap = s.execute(select(UniverseSnapshot).where(UniverseSnapshot.rule_id == rid)).scalar_one()
        assert snap.stats["members"] == 4 and {x["symbol"] for x in snap.members} == {"US.AAA", "US.BBB", "US.CCC", "US.FFF"}
    assert seen, "AI 应收到新股票池的候选"
    cands = {c["symbol"]: c for c in seen[-1]["candidates"]}
    assert set(cands) <= {"US.AAA", "US.CCC", "US.FFF"}  # 已持有的 BBB 作为持仓出现，不作为新候选
    assert "US.DDDY" not in cands and "US.EEE" not in cands
    assert all("pool_data" in c and c["pool_data"]["pool_rank"] >= 1 for c in cands.values())
    assert [p["symbol"] for p in seen[-1]["positions"]] == ["US.BBB"]


class _Recorder:
    name, is_fixture, is_realtime, label, model = "rec", True, False, "记录", "rec"
    prompt_version = "test"

    def __init__(self, seen):
        self.seen = seen

    def complete(self, payload):
        from app.ai.providers import FixtureProvider

        self.seen.append(payload)
        return FixtureProvider().complete(payload)


def test_failed_scan_keeps_previous_pool_and_retries_after_30_minutes(env):
    ctx = FakeCtx()
    prov = _prov(ctx)
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.AAA"])
    rid = _auto_rule()
    change_scope("sim", pid, {"type": "POOL", "universe_rule_id": rid}, "user")
    clock = ManualClock(US_OPEN)
    eng, _ = make_engine("sim", prov, clock)
    eng.tick()
    assert len(ctx.filters) == 1
    ctx.fail_filter = True
    clock.advance(minutes=24 * 60)  # 下一个美股交易日
    for _ in range(5):
        eng.tick()
    assert len(ctx.filters) == 2  # 失败后不会每轮重试
    with read_session("sim") as s:
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "SNAPSHOT_FAILED")).first()
        assert len(list(s.execute(select(UniverseSnapshot)).scalars())) == 1  # 沿用上一份
    assert eng.runtimes[pid].pool_info  # 仍按上一份股票池运行
    clock.advance(minutes=31)
    ctx.fail_filter = False
    eng.tick()
    assert len(ctx.filters) == 3
    with read_session("sim") as s:
        assert len(list(s.execute(select(UniverseSnapshot)).scalars())) == 2


def test_scope_api_requires_confirmation_and_validates_rules(env):
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    c = TestClient(create_app(), base_url="http://127.0.0.1:8710")
    c.post("/api/auth/setup", json={"password": "password123"})
    h = {"x-csrf-token": c.cookies.get("mt_csrf")}
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.AAA"])
    r = c.post("/api/sim/universe/rules/auto-us", headers=h)
    assert r.status_code == 200 and r.json()["rules"]["source"] == "MOOMOO_SCAN" and not r.json()["confirmed_by_user"]
    rid = r.json()["id"]
    assert c.post("/api/sim/control/scope", json={"scope_type": "POOL", "universe_rule_id": rid, "confirm": True},
                  headers=h).status_code == 422  # 必须指明运行
    body = {"portfolio_id": pid, "scope_type": "POOL", "universe_rule_id": rid}
    assert c.post("/api/sim/control/scope", json=body, headers=h).status_code == 400  # 未勾选确认
    r = c.post("/api/sim/control/scope", json=body | {"confirm": True}, headers=h)
    assert r.status_code == 400 and "未确认" in r.json()["detail"]
    assert c.post(f"/api/sim/universe/rules/{rid}/confirm", headers=h).status_code == 200
    r = c.post("/api/sim/control/scope", json=body | {"confirm": True}, headers=h)
    assert r.status_code == 200 and "保留" in r.json()["note"] and "股票池" in r.json()["summary"]
    bad = c.post("/api/sim/universe/rules", json={"rules": {"source": "MOOMOO_SCAN", "markets": ["MY", "US"]}}, headers=h)
    assert bad.status_code == 400 and "美股" in bad.json()["detail"]


def test_broker_verification_failure_does_not_save_empty_pool_and_retries(env):
    ctx = FakeCtx()
    ctx.fail_basic = True
    prov = _prov(ctx)
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.AAA"])
    change_scope("sim", pid, {"type": "POOL", "universe_rule_id": _auto_rule()}, "user")
    clock = ManualClock(US_OPEN)
    eng, _ = make_engine("sim", prov, clock)
    eng.tick()
    with read_session("sim") as s:
        assert list(s.execute(select(UniverseSnapshot)).scalars()) == []  # 不保存空股票池
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "SNAPSHOT_FAILED")).first()
    ctx.fail_basic = False
    clock.advance(minutes=31)
    prov._type_try.clear()  # 证券资料的重试节流按真实时间计；测试里只推进了业务时钟
    eng.tick()
    with read_session("sim") as s:
        snap = s.execute(select(UniverseSnapshot)).scalar_one()
        assert len(snap.members) == 4


def test_scan_runs_outside_the_database_write_lock(env):
    """扫描（网络请求）期间，其他写操作（例如网页上的紧急停止）不能被卡住。"""
    from sqlalchemy import text

    ctx = FakeCtx()
    waited = []

    def concurrent_write():
        t0 = time.monotonic()
        with write_session("sim") as s:
            s.execute(text("select 1"))
        waited.append(time.monotonic() - t0)

    ctx.on_filter = concurrent_write
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.AAA"])
    change_scope("sim", pid, {"type": "POOL", "universe_rule_id": _auto_rule()}, "user")
    eng, _ = make_engine("sim", _prov(ctx), ManualClock(US_OPEN))
    eng.tick()
    assert waited and waited[0] < 1.0


def test_change_scope_follows_upgraded_strategy_and_clears_pool_info(env):
    from app.db.models import StrategyVersion

    ctx = FakeCtx()
    pid, aid = setup_run("sim", "LOCAL_PAPER", symbols=["US.AAA"])
    with write_session("sim") as s:
        old = s.execute(select(StrategyVersion).where(StrategyVersion.key == "AI_AUTONOMOUS",
                                                      StrategyVersion.status == "ACTIVE")).scalar_one()
        old.status = "INACTIVE"
        new = StrategyVersion(key="AI_AUTONOMOUS", version="v2", params=old.params, prompt_version=old.prompt_version,
                              status="ACTIVE", approved_by_user=True)
        s.add(new)
        s.flush()
        new_id = new.id
    a = change_scope("sim", pid, {"type": "POOL", "universe_rule_id": _auto_rule()}, "user")
    assert a.strategy_version_ids == [new_id]
    eng, _ = make_engine("sim", _prov(ctx), ManualClock(US_OPEN))
    eng.tick()
    assert eng.runtimes[pid].pool_info
    change_scope("sim", pid, {"type": "WHITELIST", "symbols": ["US.AAA"]}, "user")
    eng.tick()
    assert eng.runtimes[pid].pool_info == {}  # 回到白名单后不再附带旧股票池数据


def test_change_scope_can_adopt_latest_confirmed_risk_version_keeping_positions(env):
    from app.db.models import RiskPolicyVersion
    from app.risk.policy import SEED_RISK_PARAMS

    pid, aid = setup_run("sim", "LOCAL_PAPER", symbols=["US.AAA"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    with read_session("sim") as s:
        old_rp = s.get(Authorization, aid).risk_policy_version_id
    with write_session("sim") as s:
        draft = RiskPolicyVersion(version=500, params=dict(SEED_RISK_PARAMS) | {"max_position_pct": "0.15"},
                                  confirmed_by_user=False, is_test_seed=False)
        s.add(draft)
        s.flush()
        draft_id = draft.id
    a = change_scope("sim", pid, {"type": "WHITELIST", "symbols": ["US.AAA"]}, "user", adopt_latest_risk=True)
    assert a.risk_policy_version_id == old_rp  # 未确认的草稿不会被采用
    with write_session("sim") as s:
        s.get(RiskPolicyVersion, draft_id).confirmed_by_user = True
    b = change_scope("sim", pid, {"type": "WHITELIST", "symbols": ["US.AAA"]}, "user", adopt_latest_risk=True)
    assert b.risk_policy_version_id == draft_id
    c = change_scope("sim", pid, {"type": "WHITELIST", "symbols": ["US.AAA"]}, "user")
    assert c.risk_policy_version_id == draft_id  # 不勾选时沿用当前授权的版本


def test_unaffordable_stocks_do_not_take_ai_candidate_slots(env):
    """单只上限 15% × 2448 ≈ 367 美元：一股 900 美元的股票不会作为 AI 候选。"""
    from app.ai.providers import FixtureProvider
    from helpers import StaticProvider, trend

    seen = []

    class Rec(FixtureProvider):
        def complete(self, payload):
            seen.append([c["symbol"] for c in payload["candidates"]])
            return super().complete(payload)

    prov = StaticProvider()
    prov.add("US.CHEAP", trend(80, 100, 0.1), "108")
    prov.add("US.PRICY", trend(80, 880, 1.0), "960")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.CHEAP", "US.PRICY"], capital={"USD": "2448"},
                       max_loss={"USD": "122"}, risk={"max_position_pct": "0.15"})
    eng, _ = make_engine("sim", prov, ManualClock(US_OPEN), ai=Rec())
    for _ in range(3):
        eng.tick()
        time.sleep(0.05)
    assert seen and all("US.PRICY" not in x for x in seen) and any("US.CHEAP" in x for x in seen)


def test_turnover_divisor_converts_server_value_and_filter_threshold(env, monkeypatch):
    """服务器返回的 20 日成交额单位未定：除数可配置，显示值与服务器端筛选门槛按同一除数换算。"""
    from app.config import reset_settings_cache

    seen = {}

    class Ctx(FakeCtx):
        def get_stock_filter(self, market, filter_list, begin=0, num=200):
            seen["turnover_min"] = next(f.filter_min for f in filter_list if getattr(f, "days", None) == 20
                                        and f.stock_field == m.StockField.TURNOVER)
            return super().get_stock_filter(market, filter_list, begin, num)

    monkeypatch.setenv("SCAN_TURNOVER_DIVISOR", "20")
    reset_settings_cache()
    members, excluded, stats = screen(_prov(Ctx()), AUTO_US_POOL_RULES, US_OPEN)
    assert seen["turnover_min"] == 50_000_000 * 20
    aaa = next(x for x in members if x["symbol"] == "US.AAA")
    assert aaa["avg_turnover_20d"] == str(round(9e9 / 20))
    assert stats["turnover_divisor"] == 20


def _waiting_buy(sym, target="0.25"):
    """AI 提案：价格回落到 entry_price 以下才买（条件单，先进入 WAITING_PRICE）。"""
    from integration.test_ai_safety import ScriptedAI, _buy

    return ScriptedAI(lambda p: _buy(p, entry_type="LIMIT_AT_OR_BELOW", entry_price="100.00", max_buy_price="100.00",
                                     stop_loss_price="94.00", target_position_pct=target)
                      if p["candidates"] else _buy(p))


def _waiting_setup(symbols, risk):
    from app.db.models import Decision
    from helpers import StaticProvider, trend

    prov = StaticProvider()
    for sym in symbols:
        prov.add(sym, trend(80, 100, 0.05), "104")
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=symbols, capital={"USD": "2448"}, max_loss={"USD": "122"}, risk=risk)
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock, ai=_waiting_buy(symbols[0]))
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(3):
        eng.tick()
        time.sleep(0.05)
    with read_session("sim") as s:
        d = s.execute(select(Decision).where(Decision.outcome == "WAITING_PRICE")).scalars().first()
    assert d is not None, "应先产生一个等待价格的买入提案"
    return prov, pid, clock, eng, d.id, d.symbol


def test_waiting_proposal_is_rechecked_against_new_scope_after_change(env):
    """旧白名单下等待中的买入提案：换成不含该股的新范围后，价格条件满足也不能买。"""
    from app.db.models import Decision, OrderIntent

    prov, pid, clock, eng, did, sym = _waiting_setup(["US.W1", "US.W2"], {"max_position_pct": "0.25"})
    other = "US.W2" if sym == "US.W1" else "US.W1"
    change_scope("sim", pid, {"type": "WHITELIST", "symbols": [other]}, "user")
    prov.set_quote(sym, "99.50")  # 价格条件满足
    clock.advance(minutes=1)
    eng.tick()
    with read_session("sim") as s:
        d = s.get(Decision, did)
        assert d.outcome == "RISK_BLOCKED" and "授权范围" in d.outcome_reason
        assert not s.execute(select(OrderIntent).where(OrderIntent.symbol == sym)).first()


def test_waiting_proposal_uses_new_risk_version_after_change(env):
    """同一只股票仍在范围内时，按新授权的风控 v3（单只 15%）重新计算数量，不沿用旧的 25%。"""
    from app.db.models import Decision, OrderIntent, RiskPolicyVersion
    from app.risk.policy import SEED_RISK_PARAMS

    risk = {"max_position_pct": "0.25", "max_total_exposure_pct": "0.60", "risk_per_trade_pct": "0.02",
            "daily_loss_pct": "0.10", "max_stop_distance_pct": "0.20"}
    prov, pid, clock, eng, did, sym = _waiting_setup(["US.W1"], risk)
    with write_session("sim") as s:
        s.add(RiskPolicyVersion(version=600, params=dict(SEED_RISK_PARAMS) | risk | {"max_position_pct": "0.15",
                                "max_total_exposure_pct": "0.45"}, confirmed_by_user=True, is_test_seed=False))
    change_scope("sim", pid, {"type": "WHITELIST", "symbols": ["US.W1"]}, "user", adopt_latest_risk=True)
    prov.set_quote(sym, "99.50")
    clock.advance(minutes=1)
    eng.tick()
    with read_session("sim") as s:
        d = s.get(Decision, did)
        it = s.execute(select(OrderIntent).where(OrderIntent.symbol == sym)).scalar_one()
    assert d.outcome == "AUTO_EXECUTED"
    assert it.qty * it.limit_price <= Decimal("2448") * Decimal("0.15")  # 不超过 v3 的 15%
    assert it.risk_policy_version != 0 and it.authorization_id != None
