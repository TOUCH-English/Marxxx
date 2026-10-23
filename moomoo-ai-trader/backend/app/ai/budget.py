"""AI 调用预算：按自然月统计调用次数、token 与估算费用。预算耗尽即停止 AI 调用（规则保护不受影响）。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import AICallLog


def month_usage(s: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = select(func.count(), func.coalesce(func.sum(AICallLog.input_tokens), 0),
               func.coalesce(func.sum(AICallLog.output_tokens), 0)).where(
        AICallLog.created_at_utc >= start, AICallLog.provider == "anthropic")
    calls, tin, tout = s.execute(q).one()
    st = get_settings()
    cost = None
    if st.ai_price_input_per_mtok and st.ai_price_output_per_mtok:
        cost = (Decimal(tin) * Decimal(st.ai_price_input_per_mtok) + Decimal(tout) * Decimal(st.ai_price_output_per_mtok)) / Decimal(1_000_000)
    return {
        "calls": int(calls), "input_tokens": int(tin), "output_tokens": int(tout),
        "estimated_cost_usd": str(cost.quantize(Decimal("0.01"))) if cost is not None else None,
        "call_limit": st.ai_monthly_call_limit, "token_limit": st.ai_monthly_token_limit,
        "budget_usd": st.ai_monthly_budget_usd, "price_source": st.ai_price_source,
    }


def budget_ok(s: Session) -> tuple[bool, str]:
    u = month_usage(s)
    st = get_settings()
    if u["calls"] >= st.ai_monthly_call_limit:
        return False, f"本月 AI 调用次数已达上限 {st.ai_monthly_call_limit}"
    if u["input_tokens"] + u["output_tokens"] >= st.ai_monthly_token_limit:
        return False, "本月 AI token 已达上限"
    if u["estimated_cost_usd"] is not None and st.ai_monthly_budget_usd:
        if Decimal(u["estimated_cost_usd"]) >= Decimal(st.ai_monthly_budget_usd):
            return False, f"本月 AI 估算费用已达预算 ${st.ai_monthly_budget_usd}"
    return True, "预算充足"
