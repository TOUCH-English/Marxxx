"""验收 16：无密钥的新环境能启动演示；页面上的按钮都产生真实的业务状态变化。"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Authorization, Command, Portfolio
from app.db.session import read_session


def _client():
    from app.api.app import create_app

    c = TestClient(create_app(), base_url="http://127.0.0.1:8710")
    assert c.get("/api/auth/state").json() == {"password_set": False, "logged_in": False}
    assert c.post("/api/auth/setup", json={"password": "short"}).status_code == 400
    assert c.post("/api/auth/setup", json={"password": "password123"}).status_code == 200
    return c


def test_fresh_env_demo_and_every_control(env):
    from app.workers.engine import TradingEngine

    c = _client()
    H = {"x-csrf-token": c.cookies.get("mt_csrf")}
    ov = c.get("/api/sim/overview").json()
    assert ov["portfolio"] is None and "尚未启动运行" in ov["allow_new_entries"]["reasons"][0]
    body = {"mode": "DEMO", "capital": {"MYR": "20000", "USD": "5000"}, "max_planned_loss": {"MYR": "1000", "USD": "250"},
            "scope_type": "POOL", "overnight": True, "days": 30, "strategy_key": "AI_AUTONOMOUS"}
    pv = c.post("/api/sim/wizard/preview", json=body).json()
    assert "不保证最大实际亏损" in pv["summary"] and pv["fee_impact"] and not pv["problems"]
    assert c.post("/api/sim/wizard/start", json=body, headers=H).status_code == 400  # 未确认
    r = c.post("/api/sim/wizard/start", json=body | {"confirm": True}, headers=H)
    assert r.status_code == 200, r.text
    eng = TradingEngine("sim")
    for _ in range(150):
        eng.tick()
    ov = c.get("/api/sim/overview").json()
    assert ov["portfolio"]["mode"] == "DEMO" and ov["auth"]["ok"]
    assert ov["currencies"] and ov["today"] is not None
    decs = c.get("/api/sim/decisions").json()["items"]
    assert decs, "应有 AI 决策记录"
    d = c.get(f"/api/sim/decisions/{decs[0]['id']}").json()
    assert {"facts", "rule_calc", "model_judgment", "evidence"} <= set(d)
    orders = c.get("/api/sim/orders").json()["items"]
    assert orders and c.get(f"/api/sim/orders/{orders[0]['id']}").json()["events"]
    assert c.get(f"/api/sim/trace?intent_id={orders[0]['id']}").json()["intents"]
    # 暂停 / 恢复
    assert c.post("/api/sim/control/pause", json={"paused": True}, headers=H).json()["paused"]
    with read_session("sim") as s:
        assert s.execute(select(Portfolio).where(Portfolio.running.is_(True))).scalar_one().pause_new_entries
    c.post("/api/sim/control/pause", json={"paused": False}, headers=H)
    # 紧急停止 → 指令被工作进程执行
    cid = c.post("/api/sim/control/emergency-stop", headers=H).json()["command_id"]
    eng.tick()
    cmd = c.get(f"/api/sim/commands/{cid}").json()
    assert cmd["status"] == "DONE" and "保护性订单与持仓保留" in cmd["result"]
    assert c.post("/api/sim/control/clear-emergency", json={"confirm": True}, headers=H).status_code == 200
    # 手动清仓：未确认被拒，确认后执行
    assert c.post("/api/sim/control/liquidate", json={}, headers=H).status_code == 400
    assert c.get("/api/sim/control/liquidate/preview").json()["note"]
    cid = c.post("/api/sim/control/liquidate", json={"confirm": True}, headers=H).json()["command_id"]
    eng.tick()
    assert c.get(f"/api/sim/commands/{cid}").json()["status"] == "DONE"
    # 对账
    cid = c.post("/api/sim/control/reconcile", headers=H).json()["command_id"]
    eng.tick()
    assert c.get(f"/api/sim/commands/{cid}").json()["status"] == "DONE"
    # 风控版本：放宽需要再次输入密码（428），确认后生效
    risk = c.get("/api/sim/risk").json()
    params = dict(risk["active"]["params"]) | {"max_positions": 4}
    v = c.post("/api/sim/risk/versions", json={"params": params, "note": "test"}, headers=H).json()
    assert v["loosens"]
    import time as _t

    from app.api import security

    for sess in security.store._sessions.values():
        sess.reauth_at = _t.time() - 10_000
    r = c.post(f"/api/sim/risk/versions/{v['version']['id']}/confirm", headers=H)
    assert r.status_code == 428
    assert c.post("/api/auth/reauth", json={"password": "password123"}, headers=H).status_code == 200
    assert c.post(f"/api/sim/risk/versions/{v['version']['id']}/confirm", headers=H).status_code == 200
    # 中文配置草稿 → 差异 → 确认
    dr = c.post("/api/sim/drafts", json={"text": "单笔风险改成 0.2%，最多持有 2 只，排除 US.DMOB"}, headers=H).json()
    assert {x["key"] for x in dr["diff"]} >= {"risk_per_trade_pct", "max_positions", "universe.exclude_symbols"}
    assert c.post(f"/api/sim/drafts/{dr['id']}/confirm", headers=H).status_code == 200
    # 股票池预览 / 自选
    pvw = c.get("/api/sim/universe/preview").json()
    assert pvw["members"] and any("杠杆" in " ".join(x["reasons"]) for x in pvw["excluded"])
    assert c.post("/api/sim/watchlist", json={"symbol": "US.DMOA"}, headers=H).json()["instrument"]["name"]
    assert c.delete("/api/sim/watchlist/US.DMOA", headers=H).status_code == 200
    # 回测 + 导出
    bt = c.post("/api/sim/backtests", json={"strategy_key": "BASELINE_MA", "start": "2025-06-02", "end": "2026-05-29"}, headers=H).json()
    assert "per_currency" in bt["metrics"]
    csv_text = c.get(f"/api/sim/backtests/{bt['id']}/trades.csv").text
    assert "模拟回测" in csv_text
    assert "模拟结果" in c.get(f"/api/sim/backtests/{bt['id']}/report.txt").text
    assert "模拟" in c.get("/api/sim/export/orders.csv").text
    # 复盘、日志、通知、设置、备份、AI 用量
    assert c.get("/api/sim/learning").json()["note"]
    assert c.get("/api/sim/reviews").status_code == 200
    assert c.get("/api/sim/logs?category=ORDER").json()["items"]
    assert c.post("/api/sim/notifications/read", headers=H).status_code == 200
    assert c.post("/api/backup", headers=H).json()["files"]
    assert c.get("/api/sim/ai/usage").json()["calls"] == 0  # 固定测试响应不计入模型费用
    # 停止本次运行（撤销授权）
    assert c.post("/api/sim/control/stop-run", headers=H).json()["revoked"] == 1
    with read_session("sim") as s:
        assert s.execute(select(Authorization).order_by(Authorization.id.desc())).scalars().first().status == "REVOKED"
    # 退出登录后，后台仍可按授权运行（这里已撤销）；网页需要重新登录
    c.post("/api/auth/logout", headers=H)
    assert c.get("/api/sim/overview").status_code == 401
