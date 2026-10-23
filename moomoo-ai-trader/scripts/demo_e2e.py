"""可复现的端到端示例：一次启动 → AI 自动买入 → 持仓复核 → 自动减仓/卖出 → 中文日报。

  python scripts/demo_e2e.py [--ticks 900] [--out data/examples/demo_run] [--ai fixture|anthropic]

- 默认使用「固定测试响应」（确定性规则模拟的 AI 输出，非真实 AI），数据为虚构证券的演示数据，
  因此每次运行结果完全相同，可用于核对。
- --ai anthropic 需要在 .env 中配置 ANTHROPIC_API_KEY；那是真实模型测试，结果不可复现，
  也不代表历史收益（模型可能已知历史事件）。
- 在独立的数据目录中运行，不影响您正在使用的系统数据。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=900)
    ap.add_argument("--out", default=str(ROOT / "data" / "examples" / "demo_run"))
    ap.add_argument("--ai", default="fixture", choices=["fixture", "anthropic"])
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="mt-demo-"))
    shutil.copytree(ROOT / "data" / "fixtures", work / "fixtures")
    os.environ["DATA_DIR"] = str(work)
    os.environ["AI_PROVIDER"] = args.ai
    os.environ["LIVE_TRADING_ENABLED"] = "false"
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import select

    from app.api.deps import j, row
    from app.api.views import decision_view, intent_view, overview
    from app.db.models import Decision, Fill, OrderIntent, Position, ReviewRun
    from app.db.session import read_session
    from app.services.runs import start_sim_run
    from app.workers.engine import TradingEngine

    pid, aid = start_sim_run(mode="DEMO", capital={"MYR": "20000", "USD": "5000"},
                             max_planned_loss={"MYR": "1000", "USD": "250"})
    eng = TradingEngine("sim")
    milestones = {}
    for i in range(args.ticks):
        eng.tick()
        with read_session("sim") as s:
            ints = list(s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == pid,
                                                            OrderIntent.filled_qty > 0)).scalars())
        for it in ints:
            key = {"ENTRY": "首次自动买入", "EXIT_AI": "首次 AI 自动卖出/减仓", "EXIT_STOP": "首次自动止损",
                   "EXIT_TIME": "首次持有期限退出", "EXIT_TAKE_PROFIT": "首次止盈"}.get(it.reason)
            if key and key not in milestones:
                milestones[key] = {"tick": i, "business_time": it.as_of.isoformat(), "symbol": it.symbol,
                                   "side": it.side, "qty": it.qty, "status": it.status}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    def dump(name, rows):
        rows = [j(r) for r in rows]
        if not rows:
            return
        with (out / name).open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})

    with read_session("sim") as s:
        decs = [decision_view(s, d) for d in s.execute(select(Decision).where(
            Decision.portfolio_id == pid, Decision.action.in_(["BUY_PROPOSAL", "SELL_PROPOSAL"])).order_by(Decision.as_of)).scalars()]
        dump("decisions.csv", decs)
        dump("orders.csv", [intent_view(s, i) for i in s.execute(select(OrderIntent).order_by(OrderIntent.created_at_utc)).scalars()])
        dump("fills.csv", [row(f) for f in s.execute(select(Fill).order_by(Fill.id)).scalars()])
        dump("positions.csv", [row(p) for p in s.execute(select(Position).order_by(Position.id)).scalars()])
        reports = list(s.execute(select(ReviewRun).order_by(ReviewRun.period_start)).scalars())
        (out / "reports.txt").write_text("\n\n".join(r.summary for r in reports), encoding="utf-8")
        ov = overview(s, "sim")
        auto_confirmations = s.execute(select(OrderIntent).where(OrderIntent.status == "AWAITING_APPROVAL")).first()
    summary = {
        "label": "【模拟·演示数据（虚构证券）·" + ("固定测试响应（非真实 AI）】" if args.ai == "fixture" else "真实模型测试】"),
        "ticks": args.ticks, "business_time_end": ov["portfolio"]["business_time"] if ov["portfolio"] else None,
        "milestones": milestones, "currencies": ov["currencies"], "open_positions": ov["positions"],
        "orders_needing_confirmation": bool(auto_confirmations),
        "note": "固定测试响应只验证自动流程，不代表 AI 投资表现；演示证券为虚构，不构成投资推荐。",
    }
    # 同一份演示数据上的基准策略回测（模拟样例）
    from datetime import date
    from decimal import Decimal

    from app.core.calendar import get_calendar
    from app.core.money import TEST_FEE_SCHEDULES
    from app.market_data.replay import ReplayProvider
    from app.risk.policy import SEED_RISK_PARAMS
    from app.simulation.backtest import run_backtest
    from app.strategies.baseline_ma import DEFAULT_BASELINE_PARAMS

    prov = ReplayProvider(work / "fixtures", get_calendar())
    bt = run_backtest(prov, get_calendar(), symbols=[i.symbol for i in prov.instruments() if not i.is_leveraged],
                      start=date(2025, 6, 2), end=date(2026, 5, 29), capital={"MYR": Decimal(20000), "USD": Decimal(5000)},
                      fee_schedules=TEST_FEE_SCHEDULES, strategy_key="BASELINE_MA", strategy_params=DEFAULT_BASELINE_PARAMS,
                      risk_params=SEED_RISK_PARAMS)
    (out / "backtest_baseline_metrics.json").write_text(json.dumps({"label": "【模拟回测｜基准均线策略｜演示数据】", **bt.metrics},
                                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    dump("backtest_baseline_trades.csv", bt.trades)
    dump("backtest_baseline_equity.csv", bt.equity_curve)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
