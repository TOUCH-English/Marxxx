"""成绩单：和大盘比较，并按用户确认的及格标准（关卡 A：系统运作；关卡 B：有没有优势）自动判断。

标准来源：docs/review-2026-10-23.md「及格标准」与「决定」。所有金额为该币种（美股为 USD）。
- 收益扣除手续费（账本已含）和 AI 费用（按 token 与价目估算）。
- 基准：同期 SPY 买入持有；以及「资金占用匹配」的 SPY（× 总投入上限），因为策略最多只投入这么多资金。
- 样本不足时标为「样本不足」，不下结论。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.calendar import CCY_MARKET, MARKET_TZ, get_calendar
from ..db.models import (
    AICallLog,
    AuditEvent,
    Authorization,
    DailyEquity,
    OrderIntent,
    Portfolio,
    Position,
    RiskPolicyVersion,
)
from ..db.types import D, utcnow
from ..portfolio import ledger

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT"
STATUS_CN = {PASS: "通过", FAIL: "未通过", INSUFFICIENT: "样本不足"}

# 关卡标准（docs/review-2026-10-23.md）
GATE_A_MIN_SESSIONS = 10
GATE_B_MIN_TRADES = 30
MIN_AVG_R = Decimal("0.2")
MIN_PROFIT_FACTOR = Decimal("1.3")
MAX_DRAWDOWN = Decimal("0.05")
MAX_COST_SHARE = Decimal("0.30")
MAX_STOP_LOSS_R = Decimal("-1.3")
MAX_INVALID_AI_RATE = Decimal("0.05")


def _ai_cost(s: Session, since: datetime) -> tuple[Decimal, int, int]:
    st = get_settings()
    calls = list(s.execute(select(AICallLog).where(AICallLog.created_at_utc >= since,
                                                    AICallLog.provider == "anthropic")).scalars())
    tin = sum(c.input_tokens for c in calls)
    tout = sum(c.output_tokens for c in calls)
    cost = Decimal(0)
    if st.ai_price_input_per_mtok and st.ai_price_output_per_mtok:
        cost = (Decimal(tin) * D(st.ai_price_input_per_mtok) + Decimal(tout) * D(st.ai_price_output_per_mtok)) / Decimal(1_000_000)
    bad = sum(1 for c in calls if c.status in ("INVALID", "ERROR", "TIMEOUT"))
    return cost, len(calls), bad


def _benchmark(provider, start: datetime, now: datetime) -> Decimal | None:
    """同期 SPY 涨跌：开始前最后一个收盘价 → 最新价（没有报价时用最新收盘）。取不到返回 None。"""
    if provider is None:
        return None
    try:
        bars = provider.daily_bars("US.SPY", now, 400)
    except Exception:
        return None
    if not bars:
        return None
    start_day = start.astimezone(MARKET_TZ["US"]).date()
    before = [b for b in bars if b.day < start_day]
    if not before:
        return None
    base = before[-1].close
    last = None
    try:
        q = provider.quote("US.SPY", now)
        last = q.last if q is not None else None
    except Exception:
        last = None
    last = last or bars[-1].close
    return (D(last) / D(base) - 1) if base else None


def session_days(s: Session, portfolio_id: int, rows: list, start: datetime, now: datetime, market: str
                 ) -> tuple[list[date], list[date]]:
    """运行以来已经收市的交易日，分成（完整，停机）两类。

    完整 = 该日净值记录在收市之后仍有更新（工作进程一直跑到收市），且没有交易时段内的停机记录。
    其余（记录在收市前就停止更新、整天没有记录、或有停机记录）都算停机：那天至少有一段时间止损没有运行。
    """
    from ..workers.downtime import WORKER_DOWNTIME

    cal = get_calendar()
    by_day = {str(r.day_myt): r for r in rows}
    down = set()
    for ev in s.execute(select(AuditEvent).where(AuditEvent.portfolio_id == portfolio_id,
                                                 AuditEvent.action == WORKER_DOWNTIME)).scalars():
        down |= {o["day"] for o in (ev.data or {}).get("overlaps", []) if o.get("market") == market}
    complete, missed = [], []
    d, last = cal.local_date(market, start), cal.local_date(market, now)
    while d <= last:
        close = cal.session_close(market, d)
        if close is not None and start < close <= now:
            r = by_day.get(d.isoformat())
            ok = r is not None and r.updated_at is not None and r.updated_at >= close and d.isoformat() not in down
            (complete if ok else missed).append(d)
        d += timedelta(days=1)
    return complete, missed


def _item(name: str, value: str, target: str, status: str, note: str = "") -> dict:
    return {"name": name, "value": value, "target": target, "status": status, "status_cn": STATUS_CN[status], "note": note}


def _gate(items: list[dict]) -> dict:
    sts = {i["status"] for i in items}
    status = FAIL if FAIL in sts else (INSUFFICIENT if INSUFFICIENT in sts else PASS)
    return {"status": status, "status_cn": STATUS_CN[status], "items": items}


def build(s: Session, portfolio_id: int, *, price_of, provider=None, ccy: str = "USD", now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    p = s.get(Portfolio, portfolio_id)
    first_auth = s.execute(select(Authorization).where(Authorization.portfolio_id == portfolio_id)
                           .order_by(Authorization.id).limit(1)).scalar_one_or_none()
    start = first_auth.valid_from if first_auth else p.created_at_utc
    cur_auth = s.execute(select(Authorization).where(Authorization.portfolio_id == portfolio_id)
                         .order_by(Authorization.id.desc()).limit(1)).scalar_one_or_none()
    rp = s.get(RiskPolicyVersion, cur_auth.risk_policy_version_id) if cur_auth else None
    exposure_cap = D(rp.params["max_total_exposure_pct"]) if rp else Decimal(1)

    ev = ledger.equity_view(s, portfolio_id, ccy, price_of)
    capital = ledger.total_flows(s, portfolio_id, ccy)
    ai_cost, ai_calls, ai_bad = _ai_cost(s, start)
    pnl = ev.equity - capital
    ret = (pnl / capital) if capital else Decimal(0)
    net_ret = ((pnl - ai_cost) / capital) if capital else Decimal(0)
    spy = _benchmark(provider, start, now)
    spy_matched = spy * exposure_cap if spy is not None else None

    # 已平仓交易
    closed = list(s.execute(select(Position).where(Position.portfolio_id == portfolio_id, Position.currency == ccy,
                                                   Position.status == "CLOSED")).scalars())
    trades = []
    for pos in closed:
        exits = list(s.execute(select(OrderIntent).where(OrderIntent.position_id == pos.id, OrderIntent.side == "SELL",
                                                         OrderIntent.filled_qty > 0).order_by(OrderIntent.as_of)).scalars())
        last_reason = exits[-1].reason if exits else ""
        ref = pos.entry_ref_price or pos.avg_cost
        risk = (ref - pos.stop_price) * pos.max_qty if pos.stop_price is not None and ref > pos.stop_price else None
        r = (pos.realized_pnl / risk).quantize(Decimal("0.01")) if risk else None
        trades.append({"symbol": pos.symbol, "pnl": pos.realized_pnl, "fees": pos.fees_paid, "r": r, "exit_reason": last_reason,
                       "opened_at": pos.opened_at, "closed_at": pos.closed_at})
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum((t["pnl"] for t in wins), Decimal(0))
    gross_loss = -sum((t["pnl"] for t in trades if t["pnl"] < 0), Decimal(0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else None
    rs = [t["r"] for t in trades if t["r"] is not None]
    avg_r = (sum(rs, Decimal(0)) / len(rs)).quantize(Decimal("0.01")) if rs else None
    stop_rs = [t["r"] for t in trades if t["r"] is not None and t["exit_reason"] == "EXIT_STOP"]
    avg_stop_r = (sum(stop_rs, Decimal(0)) / len(stop_rs)).quantize(Decimal("0.01")) if stop_rs else None
    fees = sum((t["fees"] for t in trades), Decimal(0))
    gross_before_costs = sum(((t["pnl"] + t["fees"]) for t in trades if t["pnl"] + t["fees"] > 0), Decimal(0))
    cost_share = ((fees + ai_cost) / gross_before_costs) if gross_before_costs > 0 else None

    rows = list(s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == portfolio_id, DailyEquity.currency == ccy)
                          .order_by(DailyEquity.day_myt)).scalars())
    # 回撤：当日最低点只和「当天开始前的最高点」比（当天稍后才创新高时，不能拿后来的高点比早先的低点）；
    # 收市值和包含当天的最高点比。这样只会少估盘中回撤，不会多估。
    mdd = Decimal(0)
    prev_hwm: Decimal | None = None
    for r_ in rows:
        before = max(prev_hwm, r_.start_equity) if prev_hwm is not None else r_.start_equity
        if before > 0:
            mdd = max(mdd, (before - r_.min_equity) / before)
        if r_.hwm_adjusted > 0:
            mdd = max(mdd, (r_.hwm_adjusted - r_.last_equity) / r_.hwm_adjusted)
        prev_hwm = r_.hwm_adjusted
    complete, missed = session_days(s, portfolio_id, rows, start, now, CCY_MARKET.get(ccy, "US"))
    sessions = len(complete)

    prot_issues = s.execute(select(AuditEvent).where(AuditEvent.portfolio_id == portfolio_id, AuditEvent.created_at_utc >= start,
                                                     AuditEvent.action.in_(["NO_QUOTE", "PRICE_GAP_HOLD"]))).scalars().all()
    order_issues = s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == portfolio_id,
                                                       (OrderIntent.needs_manual_review.is_(True)) |
                                                       (OrderIntent.status == "UNKNOWN"))).scalars().all()

    pct = lambda x: f"{x * 100:.2f}%" if x is not None else "—"  # noqa: E731
    enough_days = sessions >= GATE_A_MIN_SESSIONS
    ai_rate = (Decimal(ai_bad) / Decimal(ai_calls)) if ai_calls else None
    month_budget = D(get_settings().ai_monthly_budget_usd or "0")
    gate_a = _gate([
        _item("运行天数", f"{sessions} 天", f"≥ {GATE_A_MIN_SESSIONS} 个交易日", PASS if enough_days else INSUFFICIENT,
              "只算整个交易时段系统都在运行的交易日"),
        _item("交易时段停机（期间止损不运行）", f"{len(missed)} 天" + (f"（{'、'.join(d.isoformat() for d in missed)}）" if missed else ""),
              "0 天", PASS if not missed else FAIL, "电脑重启、睡眠或程序停止都会造成；这些交易日不计入运行天数"),
        _item("保护异常（缺报价 / 价格断层）", f"{len(prot_issues)} 次", "0 次", PASS if not prot_issues else FAIL,
              "出现时请查看通知并人工确认"),
        _item("订单异常（状态未知 / 需人工复核）", f"{len(order_issues)} 笔", "0 笔", PASS if not order_issues else FAIL),
        _item("AI 无效输出比例", pct(ai_rate) + f"（{ai_bad}/{ai_calls}）", f"< {MAX_INVALID_AI_RATE * 100:.0f}%",
              INSUFFICIENT if ai_calls < 20 else (PASS if ai_rate < MAX_INVALID_AI_RATE else FAIL)),
        _item("AI 费用", f"{ai_cost:.2f} 美元", f"每月 ≤ {month_budget} 美元",
              PASS if ai_cost <= month_budget * ((now - start).days // 30 + 1) else FAIL),
        _item("止损交易的平均亏损", f"{avg_stop_r}R" if avg_stop_r is not None else "—", f"不差于 {MAX_STOP_LOSS_R}R",
              INSUFFICIENT if avg_stop_r is None else (PASS if avg_stop_r >= MAX_STOP_LOSS_R else FAIL),
              "比 -1R 差很多说明止损执行不到位（跳空或卖不出）"),
    ])
    gate_b = _gate([
        _item("已平仓交易数", f"{n} 笔", f"≥ {GATE_B_MIN_TRADES} 笔", PASS if n >= GATE_B_MIN_TRADES else INSUFFICIENT,
              "交易太少时任何结果都可能只是运气"),
        _item("扣除手续费和 AI 费用后的收益 vs 资金占用匹配的 SPY", f"{pct(net_ret)} vs {pct(spy_matched)}", "不低于基准",
              INSUFFICIENT if spy_matched is None or n < GATE_B_MIN_TRADES else (PASS if net_ret >= spy_matched else FAIL)),
        _item("平均每笔 R", f"{avg_r}R" if avg_r is not None else "—", f"≥ {MIN_AVG_R}R",
              INSUFFICIENT if avg_r is None or n < GATE_B_MIN_TRADES else (PASS if avg_r >= MIN_AVG_R else FAIL)),
        _item("盈亏比（总盈利 ÷ 总亏损）", f"{pf:.2f}" if pf is not None else "—", f"≥ {MIN_PROFIT_FACTOR}",
              INSUFFICIENT if pf is None or n < GATE_B_MIN_TRADES else (PASS if pf >= MIN_PROFIT_FACTOR else FAIL)),
        _item("最大回撤", pct(mdd), f"≤ {MAX_DRAWDOWN * 100:.0f}%", PASS if mdd <= MAX_DRAWDOWN else FAIL),
        _item("费用占毛利", pct(cost_share), f"≤ {MAX_COST_SHARE * 100:.0f}%",
              INSUFFICIENT if cost_share is None or n < GATE_B_MIN_TRADES else (PASS if cost_share <= MAX_COST_SHARE else FAIL)),
        _item("对照组（不用 AI 的规则）", "—", "AI 应跑赢规则对照组", INSUFFICIENT, "由历史回测（第 3 批）提供"),
    ])
    if gate_b["status"] == PASS:
        verdict = "关卡 B 通过：可以进入下一阶段的准备（仍需完成上真钱前的清单）。"
        if spy is not None and net_ret < spy:
            verdict += (f"注意：同期把全部资金买入 SPY 的收益（{pct(spy)}）更高。策略最多只投入一部分资金、风险较小，"
                        "所以按投入比例比较；但如果只看赚多少，指数基金更好。")
    elif gate_b["status"] == FAIL:
        verdict = "关卡 B 未通过：不建议用真钱。按约定，建议改为定期买入标普 500 指数基金，或先改进策略再测。"
    else:
        verdict = "样本还不够，暂不下结论。系统运作情况看关卡 A。"
    return {
        "currency": ccy, "period_start": start.isoformat(), "as_of": now.isoformat(), "sessions": sessions,
        "downtime_days": [d.isoformat() for d in missed],
        "numbers": {
            "capital": str(capital.quantize(Decimal("0.01"))), "equity": str(ev.equity.quantize(Decimal("0.01"))), "pnl": str(pnl.quantize(Decimal("0.01"))),
            "return_pct": pct(ret), "ai_cost_usd": str(ai_cost.quantize(Decimal("0.01"))), "net_return_pct": pct(net_ret),
            "spy_return_pct": pct(spy), "spy_matched_return_pct": pct(spy_matched),
            "exposure_cap": str(exposure_cap), "closed_trades": n, "win_rate_pct": pct(Decimal(len(wins)) / n if n else None),
            "avg_r": str(avg_r) if avg_r is not None else None, "profit_factor": f"{pf:.2f}" if pf is not None else None,
            "fees_usd": str(fees.quantize(Decimal("0.01"))), "max_drawdown_pct": pct(mdd),
        },
        "benchmark_note": ("SPY 数据不可用（需要实时行情）" if spy is None else
                           f"资金占用匹配基准 = SPY 涨跌 × 总投入上限 {exposure_cap * 100:.0f}%"),
        "gate_a": gate_a, "gate_b": gate_b, "verdict": verdict,
        "trades": [{k: (str(v) if isinstance(v, (Decimal, datetime)) else v) for k, v in t.items()} for t in trades[-50:]],
    }
