"""验收 1、2、13、17、18：模式隔离、实盘不能被前端打开、账本平衡、未连接不冒充、密钥与跨站防护。"""
import json
import subprocess
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Authorization, CashLedger, Fill, OrderIntent, Portfolio, Position
from app.db.session import read_session, write_session
from app.portfolio import ledger
from helpers import US_OPEN, ManualClock, StaticProvider, make_engine, setup_run, trend


class FakeTradeCtx:
    """模拟 SDK 交易上下文，记录每次调用的参数。"""

    def __init__(self):
        self.calls = []

    def place_order(self, **kw):
        import pandas as pd

        self.calls.append(kw)
        return 0, pd.DataFrame([{"order_id": "123"}])


def test_sdk_calls_pass_explicit_env_and_account(env, monkeypatch):
    import moomoo as m

    from app.broker.moomoo_adapter import LiveTradingNotPermitted, MoomooExecutionAdapter

    ctx = FakeTradeCtx()
    a = MoomooExecutionAdapter(trd_env="SIMULATE", acc_id="998877", markets=["US"], ctx_factory=lambda mk: ctx)
    r = a.place_order(symbol="US.AAA", side="BUY", qty=3, limit_price=Decimal("10.5"), remark="intent-1", now=US_OPEN)
    assert r.status == "ACCEPTED"
    kw = ctx.calls[0]
    assert kw["trd_env"] == m.TrdEnv.SIMULATE, "必须显式传入环境（SDK 默认是 REAL）"
    assert kw["acc_id"] == 998877, "必须显式传入账户（不使用默认第一个账户）"
    with pytest.raises(ValueError):
        MoomooExecutionAdapter(trd_env="SIMULATE", acc_id="0", markets=["US"], ctx_factory=lambda mk: ctx)
    with pytest.raises(ValueError):
        MoomooExecutionAdapter(trd_env="", acc_id="1", markets=["US"])
    # 没有实盘许可令牌不能创建 REAL 通道
    with pytest.raises(LiveTradingNotPermitted):
        MoomooExecutionAdapter(trd_env="REAL", acc_id="998877", markets=["US"], ctx_factory=lambda mk: ctx)
    # 伪造令牌也不行
    with pytest.raises(LiveTradingNotPermitted):
        MoomooExecutionAdapter(trd_env="REAL", acc_id="998877", markets=["US"], gate_token="forged", ctx_factory=lambda mk: ctx)


def test_real_adapter_rechecks_deployment_switch_every_order(env, monkeypatch):
    from app.broker import gate
    from app.broker.moomoo_adapter import LiveTradingNotPermitted, MoomooExecutionAdapter

    gate._tokens["tok"] = "998877"
    ctx = FakeTradeCtx()
    a = MoomooExecutionAdapter(trd_env="REAL", acc_id="998877", markets=["US"], gate_token="tok", ctx_factory=lambda mk: ctx)
    with pytest.raises(LiveTradingNotPermitted):  # 部署开关关闭
        a.place_order(symbol="US.AAA", side="BUY", qty=1, limit_price=Decimal("1"), remark="x", now=US_OPEN)
    assert not ctx.calls


def test_demo_and_paper_never_touch_real_broker(env, monkeypatch):
    from app.broker import moomoo_adapter

    def boom(*a, **k):
        raise AssertionError("模拟路径不得构造 moomoo 执行适配器")

    monkeypatch.setattr(moomoo_adapter.MoomooExecutionAdapter, "__init__", boom)
    from app.services.runs import start_sim_run
    from app.workers.engine import TradingEngine

    pid, _ = start_sim_run()
    eng = TradingEngine("sim")
    for _ in range(30):
        st = eng.tick()
        assert "error" not in st["portfolios"][pid]
    rt = eng.runtimes[pid]
    assert rt.adapter.broker == "LOCAL_SIM" and rt.adapter.trd_env == "LOCAL"


def test_frontend_or_forged_request_cannot_enable_live(env):
    from app.api.app import create_app
    from app.services.runs import ensure_seed

    ensure_seed("sim"), ensure_seed("live")
    c = TestClient(create_app(), base_url="http://127.0.0.1:8710")
    c.post("/api/auth/setup", json={"password": "password123"})
    h = {"x-csrf-token": c.cookies.get("mt_csrf")}
    body = {"mode": "LIVE_AUTO", "capital": {"USD": "1000"}, "max_planned_loss": {"USD": "50"}, "confirm": True,
            "scope_type": "WHITELIST", "symbols": ["US.AAPL"]}
    r = c.post("/api/live/wizard/start", json=body, headers=h)
    assert r.status_code == 400 and "部署配置" in r.json()["detail"], r.json()
    # 伪造：把模式改成 LIVE_AUTO 发到 sim 命名空间
    r = c.post("/api/sim/wizard/start", json=body, headers=h)
    assert r.status_code == 400
    # 伪造：直接写数据库授权（无签名）→ 工作进程拒绝
    with write_session("live") as s:
        from app.db.models import AccountBinding

        b = AccountBinding(broker="MOOMOO", trd_env="REAL", acc_id="1", acc_id_masked="1", confirmed_by_user=True)
        s.add(b)
        s.flush()
        p = Portfolio(mode="LIVE_AUTO", account_binding_id=b.id, name="x", data_source="MOOMOO_REALTIME", running=True)
        s.add(p)
        s.flush()
        from app.db.types import utcnow
        from datetime import timedelta

        s.add(Authorization(portfolio_id=p.id, mode="LIVE_AUTO", account_binding_id=b.id, capital={"USD": "1"},
                            max_planned_loss={"USD": "1"}, scope={"type": "WHITELIST", "symbols": ["US.A"]},
                            permissions={"buy": True}, strategy_version_ids=[1], risk_policy_version_id=1,
                            exit_rules={}, valid_from=utcnow(), expires_at=utcnow() + timedelta(days=1), signature="x"))
        pid = p.id
    from app.core import authz

    with read_session("live") as s:
        a = s.execute(select(Authorization)).scalars().first()
        ok, why = authz.check(a, __import__("app.db.types", fromlist=["x"]).utcnow())
        assert not ok and "签名" in why
    from app.workers.engine import TradingEngine

    st = TradingEngine("live").tick()
    assert "实盘闸门未通过" in st["portfolios"][pid]["error"]
    # 实盘工作进程在部署开关关闭时不启动
    from app.workers.main import main

    assert main(["--ns", "live", "--max-ticks", "1"]) == 0


def test_ledger_balances_and_namespaces_never_mix(env):
    from app.services.runs import start_sim_run
    from app.workers.engine import TradingEngine

    pid, _ = start_sim_run()
    eng = TradingEngine("sim")
    for _ in range(400):
        eng.tick()
    rt = eng.runtimes[pid]
    with read_session("sim") as s:
        fills = list(s.execute(select(Fill)).scalars())
        assert fills, "演示运行应产生成交"
        for ccy in ("MYR", "USD"):
            cash = ledger.cash_balance(s, pid, ccy)
            broker_cash = Decimal(rt.adapter.state["cash"][ccy])
            assert cash == broker_cash, f"{ccy} 账本现金 {cash} 应等于模拟券商现金 {broker_cash}"
            exp = ledger.total_flows(s, pid, ccy)
            for f in fills:
                if f.currency != ccy:
                    continue
                exp += (-1 if f.side == "BUY" else 1) * f.price * f.qty - f.fee
            exp += sum((r.amount for r in s.execute(select(CashLedger).where(CashLedger.kind == "DIVIDEND",
                                                                             CashLedger.currency == ccy)).scalars()), Decimal(0))
            assert cash == exp, "现金 = 分配 ± 成交金额 − 费用 + 股息"
        bpos = {p.symbol: p.qty for p in rt.adapter.positions()}
        lpos = {p.symbol: p.qty for p in ledger.open_positions(s, pid)}
        assert bpos == lpos
        for it in s.execute(select(OrderIntent)).scalars():
            assert it.mode == "DEMO"
    # live 数据库里没有任何模拟记录
    from app.services.runs import ensure_seed

    ensure_seed("live")
    with read_session("live") as s:
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(OrderIntent)).scalar_one() == 0
    # 数据库文件命名空间标记防止调换
    from app.db.session import check_namespace

    with read_session("live") as s:
        with pytest.raises(RuntimeError):
            check_namespace(s, "sim")


def test_unconnected_api_never_reports_success_or_fake_realtime(env):
    from app.api.app import create_app

    c = TestClient(create_app(), base_url="http://127.0.0.1:8710")
    c.post("/api/auth/setup", json={"password": "password123"})
    h = {"x-csrf-token": c.cookies.get("mt_csrf")}
    st = c.get("/api/system/status").json()
    assert st["opend"]["reachable"] is False
    r = c.post("/api/connection/diagnose", json={}, headers=h).json()
    assert r["ok"] is False and r["items"][0]["status"] == "FAIL" and r["items"][0]["hint"]
    ov = c.get("/api/live/overview").json()
    assert ov["portfolio"] is None and ov["currencies"] == [] and ov["positions"] == []
    # 演示行情明确标记为非实时、虚构
    from app.services.runs import start_sim_run

    start_sim_run()
    d = c.get("/api/sim/instruments/US.DMOA").json()
    assert d["quote"] is None or d["quote"]["is_realtime"] is False
    assert d["data_source"] == "DEMO_FIXTURE" and "虚构" in d["disclaimer"]
    # 实盘证券详情在 OpenD 未连接时没有报价，不伪造
    d2 = c.get("/api/live/instruments/US.AAPL").json()
    assert d2["quote"] is None and d2["bars"] == []


def test_secrets_never_leak_and_cross_site_rejected(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TESTSECRET123")
    from app.config import reset_settings_cache

    reset_settings_cache()
    from app.api.app import create_app
    from app.core.audit import redact

    c = TestClient(create_app(), base_url="http://127.0.0.1:8710")
    # 未登录：所有接口 401
    assert c.get("/api/sim/overview").status_code == 401
    c.post("/api/auth/setup", json={"password": "password123"})
    body = json.dumps(c.get("/api/settings").json()) + json.dumps(c.get("/api/system/status").json())
    assert "TESTSECRET" not in body
    assert "sk-ant-" not in redact("key sk-ant-abc123 and api_key=xyz")
    # 跨站：缺 CSRF / 错误 Origin / 错误 Host
    assert c.post("/api/sim/control/pause", json={"paused": True}).status_code == 403
    h = {"x-csrf-token": c.cookies.get("mt_csrf")}
    assert c.post("/api/sim/control/pause", json={"paused": True}, headers=h | {"origin": "https://evil.example"}).status_code == 403
    evil = TestClient(create_app(), base_url="http://attacker.example")
    assert evil.get("/api/auth/state").status_code == 403
    # 会话 Cookie 是 HttpOnly + SameSite=Strict
    r = TestClient(create_app(), base_url="http://127.0.0.1:8710").post("/api/auth/login", json={"password": "password123"})
    sc = r.headers.get("set-cookie", "")
    assert "HttpOnly" in sc and "SameSite=strict" in sc.lower().replace("samesite=strict", "SameSite=strict")
    # Git 忽略密钥与数据库
    from pathlib import Path

    gi = (Path(__file__).resolve().parents[2] / ".gitignore").read_text()
    assert ".env" in gi and "data/*" in gi
    # 密码文件权限仅本人可读（POSIX）
    import os, stat

    from app.config import get_settings

    pw = get_settings().secrets_dir / "web_password.json"
    if os.name == "posix":
        assert stat.S_IMODE(pw.stat().st_mode) == 0o600
        assert "password123" not in pw.read_text()
