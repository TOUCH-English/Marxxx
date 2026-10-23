"""成绩单：与 SPY 比较、扣除 AI 费用、R 倍数、关卡 A/B 判断。"""
import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db.models import AICallLog, Position
from app.db.session import read_session, write_session
from app.learning import scorecard
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend
from integration.test_ai_safety import ScriptedAI, _buy


def _prices(prov):
    return lambda sym: prov.q[sym].last if sym in prov.q else None


def test_new_run_is_insufficient_not_pass_or_fail(env):
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.A"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    with read_session("sim") as s:
        sc = scorecard.build(s, pid, price_of=lambda _s: None, provider=None, now=US_OPEN + timedelta(days=1))
    assert sc["gate_b"]["status"] == "INSUFFICIENT" and "样本" in sc["verdict"]
    assert sc["numbers"]["capital"] == "2448.00" and sc["numbers"]["spy_return_pct"] == "—"
    assert "不可用" in sc["benchmark_note"]


def test_real_trade_flow_counts_r_ai_cost_and_spy(env):
    prov = StaticProvider()
    prov.add("US.UPA", trend(80, 60.0, 0.05), "64.00")
    closes = trend(300, 400, 0.5)
    prov.add("US.SPY", closes, str(closes[-1] * 1.05), sec_type="ETF")  # 开始后 SPY 涨了约 5%
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.UPA"], capital={"USD": "2448"}, max_loss={"USD": "122"},
                       risk={"max_total_exposure_pct": "0.45"})
    clock = ManualClock(US_OPEN)
    eng, fb = make_engine("sim", prov, clock, ai=ScriptedAI(lambda p: _buy(p)))
    fb.callback_before_return = fb.fill_immediately = True
    for _ in range(4):
        eng.tick()
        time.sleep(0.05)
    prov.set_quote("US.UPA", "60.80")  # 跌破止损 61
    eng.tick()
    eng.tick()
    with write_session("sim") as s:
        s.add(AICallLog(provider="anthropic", model="m", prompt_version="v", input_hash="x", input_snapshot={},
                        status="OK", input_tokens=1_000_000, output_tokens=100_000))  # 4 + 2 = 6 美元
    with read_session("sim") as s:
        sc = scorecard.build(s, pid, price_of=_prices(prov), provider=prov, now=US_OPEN + timedelta(hours=1))
    n = sc["numbers"]
    assert n["closed_trades"] == 1 and Decimal(n["avg_r"]) < 0
    assert sc["trades"][0]["exit_reason"] == "EXIT_STOP"
    assert n["ai_cost_usd"] == "6.00"
    assert Decimal(n["net_return_pct"].rstrip("%")) < Decimal(n["return_pct"].rstrip("%"))  # 扣了 AI 费用
    assert n["spy_return_pct"] != "—" and "0.45" in n["exposure_cap"] or "45" in sc["benchmark_note"]
    stop_item = next(i for i in sc["gate_a"]["items"] if i["name"].startswith("止损"))
    assert stop_item["status"] in ("PASS", "FAIL") and stop_item["value"].endswith("R")


def _closed(s, pid, n, pnl, risk_ps="2", qty=10, fees="1"):
    from app.db.types import utcnow

    for i in range(n):
        s.add(Position(portfolio_id=pid, mode="LOCAL_PAPER", symbol=f"US.T{i}", market="US", currency="USD", qty=0,
                       avg_cost=Decimal("100"), realized_pnl=Decimal(pnl), fees_paid=Decimal(fees), status="CLOSED",
                       opened_at=utcnow(), closed_at=utcnow(), stop_price=Decimal(100) - Decimal(risk_ps),
                       entry_ref_price=Decimal("100"), max_qty=qty))


def test_gate_b_fails_on_losing_sample_and_passes_on_good_sample(env):
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.A"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    with write_session("sim") as s:
        _closed(s, pid, 30, "-5")
    with read_session("sim") as s:
        sc = scorecard.build(s, pid, price_of=lambda _s: None, provider=None)
    assert sc["gate_b"]["status"] == "FAIL" and "指数基金" in sc["verdict"]
    avg = next(i for i in sc["gate_b"]["items"] if i["name"] == "平均每笔 R")
    assert avg["status"] == "FAIL" and avg["value"] == "-0.25R"

    pid2, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.B"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    with write_session("sim") as s:
        _closed(s, pid2, 20, "12")
        _closed(s, pid2, 12, "-6")
    pid3, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.C"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    with write_session("sim") as s:
        _closed(s, pid3, 20, "12", fees="3.2")  # 手续费占毛利超过 30%
        _closed(s, pid3, 12, "-6", fees="3.2")
    with read_session("sim") as s:
        sc3 = scorecard.build(s, pid3, price_of=lambda _s: None, provider=None)
    share = next(i for i in sc3["gate_b"]["items"] if i["name"] == "费用占毛利")
    assert share["status"] == "FAIL" and sc3["gate_b"]["status"] == "FAIL"
    with read_session("sim") as s:
        sc = scorecard.build(s, pid2, price_of=lambda _s: None, provider=None)
    items = {i["name"]: i for i in sc["gate_b"]["items"]}
    assert items["平均每笔 R"]["status"] == "PASS" and items["盈亏比（总盈利 ÷ 总亏损）"]["status"] == "PASS"
    # 没有 SPY 数据、没有规则对照组：整体仍是「样本不足」，不会误判为通过
    assert sc["gate_b"]["status"] == "INSUFFICIENT"


def test_scorecard_api(env):
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    c = TestClient(create_app(), base_url="http://127.0.0.1:8710")
    c.post("/api/auth/setup", json={"password": "password123"})
    setup_run("sim", "LOCAL_PAPER", symbols=["US.A"], capital={"USD": "2448"}, max_loss={"USD": "122"})
    r = c.get("/api/sim/scorecard")
    assert r.status_code == 200 and r.json()["gate_a"]["items"] and r.json()["currency"] == "USD"
