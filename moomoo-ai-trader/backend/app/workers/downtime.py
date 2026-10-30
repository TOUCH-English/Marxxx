"""停机检测：工作进程不在运行的时段如果跨过了交易时段，那段时间止损、止盈都没有被执行。

两种情况都会记录：
- 重启时：上一个工作进程最后一次心跳 → 本次启动（电脑重启、程序崩溃、手动停止）；
- 运行中：两次循环之间隔了很久（电脑睡眠时进程被挂起）。

只在停机时段与交易时段有重叠时写审计（PROTECTION / WORKER_DOWNTIME）并发通知；成绩单把这些交易日算作「停机」，
不计入运行天数。没有重叠（例如周末、夜里）只写日志。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from ..core.audit import audit, notify
from ..core.calendar import CCY_MARKET, MYT, get_calendar
from ..db.models import Authorization
from ..db.session import write_session
from ..services.runs import current_portfolio

log = logging.getLogger(__name__)

MIN_GAP = timedelta(minutes=2)
WORKER_DOWNTIME = "WORKER_DOWNTIME"


def session_overlaps(start: datetime, end: datetime, markets) -> list[dict]:
    """停机时段与各市场常规交易时段的重叠：[{market, day, minutes, from, to}]。"""
    cal = get_calendar()
    out = []
    for m in markets:
        d = cal.local_date(m, start) - timedelta(days=1)
        last = cal.local_date(m, end)
        while d <= last:
            for a, b in cal.windows(m, d):
                lo, hi = max(a, start), min(b, end)
                if hi > lo:
                    out.append({"market": m, "day": d.isoformat(), "minutes": int((hi - lo).total_seconds() // 60),
                                "from": lo.isoformat(), "to": hi.isoformat()})
            d += timedelta(days=1)
    return out


def record_downtime(ns: str, start: datetime, end: datetime, reason: str) -> list[dict]:
    """记录一次停机；返回与交易时段的重叠（空表示没有影响交易时段）。"""
    if end - start < MIN_GAP:
        return []
    with write_session(ns) as s:
        p = current_portfolio(s, ns)
        if p is None or not p.running or p.data_source in ("DEMO_FIXTURE", "HISTORICAL_REPLAY"):
            return []  # 没有在运行的真实行情组合：没有需要保护的持仓
        a = s.execute(select(Authorization).where(Authorization.portfolio_id == p.id, Authorization.status == "ACTIVE")
                      .order_by(Authorization.id.desc()).limit(1)).scalar_one_or_none()
        markets = sorted({CCY_MARKET.get(c, "US") for c in a.capital}) if a is not None and a.capital else ["US"]
        overlaps = session_overlaps(start, end, markets)
        span = f"{start.astimezone(MYT):%m-%d %H:%M} → {end.astimezone(MYT):%m-%d %H:%M}（马来西亚时间）"
        if not overlaps:
            log.info("工作进程停机 %s，没有跨过交易时段（%s）", span, reason)
            return []
        mins = sum(o["minutes"] for o in overlaps)
        days = "、".join(sorted({f"{o['market']} {o['day']}" for o in overlaps}))
        msg = f"工作进程停机 {span}，其中 {mins} 分钟在交易时段（{days}）：这段时间止损、止盈没有执行。原因：{reason}"
        audit(s, "PROTECTION", WORKER_DOWNTIME, msg, actor="worker", portfolio_id=p.id,
              data={"start": start.isoformat(), "end": end.isoformat(), "reason": reason, "overlaps": overlaps},
              as_of=end)
        notify(s, "error", "系统曾停机，期间没有执行止损", msg, dedupe_key=f"downtime:{ns}:{start.isoformat()}")
        log.warning(msg)
        return overlaps
