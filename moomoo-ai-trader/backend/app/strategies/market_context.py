"""市场背景：大盘状态、财报日、行业。供 AI 输入和硬性规则使用（三方审核第 2 批）。

数据来自行情源（moomoo）；行情源不提供时一律视为「未知」，不会因此伪造结论。
- 大盘弱势（SPY 收盘低于 200 日均线）：不开新仓（已有持仓照常保护和复核）。
- 财报：财报日在 blackout 天数内的证券不开新仓；持仓在财报前的最后一个交易时段收市前 30 分钟内退出。
- 行业：同一行业最多持有 1 只（含待成交买单）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from ..config import get_settings
from ..core.calendar import MARKET_TZ
from ..db.models import RiskReservation
from ..portfolio import ledger

BENCHMARK = "US.SPY"


def us_day(now: datetime) -> date:
    return now.astimezone(MARKET_TZ["US"]).date()


def market_regime(rt) -> dict | None:
    """SPY 的 20/60 日涨跌、相对 200 日均线的位置。每个美东日期计算一次。"""
    now = rt.now()
    day = us_day(now)
    cached = getattr(rt, "regime", None)
    if cached is not None and cached[0] == day:
        return cached[1]
    try:
        bars = rt.provider.daily_bars(BENCHMARK, now, 220)
    except Exception:
        bars = []
    out = None
    if len(bars) >= 200:
        closes = [float(b.close * b.adj_factor) for b in bars]
        last = closes[-1]
        sma200 = sum(closes[-200:]) / 200
        out = {"symbol": BENCHMARK, "ret_20d": round(last / closes[-21] - 1, 4), "ret_60d": round(last / closes[-61] - 1, 4),
               "vs_sma200": round(last / sma200 - 1, 4), "risk_off": last < sma200,
               "as_of_close": bars[-1].day.isoformat()}
    rt.regime = (day, out)
    return out


def earnings(rt) -> dict[str, dict] | None:
    fn = getattr(rt.provider, "earnings_calendar", None)
    if fn is None:
        return None
    try:
        return fn(rt.now())
    except Exception:
        return None


def earnings_blocked(rt, symbol: str) -> date | None:
    """财报日在 blackout 天数内（含今天）则返回财报日。"""
    cal = earnings(rt)
    if not cal or symbol not in cal:
        return None
    d = cal[symbol]["date"]
    today = us_day(rt.now())
    days = int(get_settings().earnings_blackout_days)
    return d if today <= d <= today + timedelta(days=days) else None


def industries(rt, symbols) -> dict[str, str]:
    fn = getattr(rt.provider, "industries", None)
    if fn is None or not get_settings().one_position_per_industry:
        return {}
    try:
        return fn(list(symbols))
    except Exception:
        return {}


def held_industries(s, rt, portfolio_id: int) -> dict[str, str]:
    """已持有和待成交买单所属的行业：industry → symbol。"""
    syms = {p.symbol for p in ledger.open_positions(s, portfolio_id)}
    syms |= set(s.execute(select(RiskReservation.symbol).where(RiskReservation.portfolio_id == portfolio_id,
                                                               RiskReservation.status == "ACTIVE")).scalars())
    ind = industries(rt, syms)
    return {v: k for k, v in ind.items()}


def entry_block_reason(s, rt, portfolio_id: int, symbol: str) -> str | None:
    """开新仓前的市场背景硬性检查。返回拦截原因，或 None 表示通过。"""
    st = get_settings()
    if st.regime_filter:
        rg = market_regime(rt)
        if rg is not None and rg["risk_off"]:
            return f"大盘弱势（SPY 低于 200 日均线 {rg['vs_sma200'] * 100:.1f}%）：暂停新开仓"
    d = earnings_blocked(rt, symbol)
    if d is not None:
        return f"{d.isoformat()} 公布财报（{st.earnings_blackout_days} 天内）：财报可能跳空，不开新仓"
    ind = industries(rt, [symbol]).get(symbol)
    if ind:
        held = held_industries(s, rt, portfolio_id)
        if ind in held and held[ind] != symbol:
            return f"同行业（{ind}）已持有 {held[ind]}：同一行业最多 1 只"
    return None


def exit_before_earnings(rt, pos, now: datetime) -> str | None:
    """持仓在财报前最后一个交易时段的收市前 30 分钟内退出。返回原因或 None。"""
    if not get_settings().exit_before_earnings:
        return None
    cal = earnings(rt)
    if not cal or pos.symbol not in cal:
        return None
    info = cal[pos.symbol]
    today = us_day(now)
    nxt = today + timedelta(days=1)
    while not rt.calendar.is_session_day(pos.market, nxt) and nxt < today + timedelta(days=10):
        nxt += timedelta(days=1)
    before_open_today = info["date"] == today and info["pub_type"].upper() == "BEFORE"
    if before_open_today or info["date"] not in (today, nxt):
        return None
    mtc = rt.calendar.minutes_to_close(pos.market, now)
    if mtc is None or mtc > 30:
        return None
    return f"{info['date'].isoformat()} 公布财报：收市前退出，避免财报跳空"


def r_multiple(pos, price: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """（每股初始风险，当前 R 倍数）。初始风险 = 入场参考价（或成本）− 止损价。"""
    if pos.stop_price is None:
        return None, None
    ref = pos.entry_ref_price or pos.avg_cost
    risk = ref - pos.stop_price
    if risk <= 0 or price is None:
        return (risk if risk > 0 else None), None
    return risk, ((price - pos.avg_cost) / risk).quantize(Decimal("0.01"))
