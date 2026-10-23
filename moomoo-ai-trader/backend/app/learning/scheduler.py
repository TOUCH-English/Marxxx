"""日/周任务调度（按马来西亚时间；演示/回放按回放时钟）。

去重键（DAILY:<账本>:<日期> / WEEKLY:<账本>:<ISO周>）有数据库唯一约束，重启不会重复执行。
每天第一次运行时补齐最近 7 天未生成的日报；新的 ISO 周开始后分析上一周。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from ..core.calendar import MYT
from ..db.models import DailyEquity
from ..db.session import write_session
from . import experience, review


def run_due(rt, ts) -> None:
    if ts.auth is None:
        return
    now = rt.now()
    today = now.astimezone(MYT).date()
    if getattr(rt, "_learning_checked", None) == today:
        return
    pid = rt.portfolio_id
    with write_session(rt.ns) as s:
        first = s.execute(select(func.min(DailyEquity.day_myt)).where(DailyEquity.portfolio_id == pid)).scalar()
        if first is None:
            return
        first_day = date.fromisoformat(first)
        for back in range(7, 0, -1):
            day = today - timedelta(days=back)
            if day < first_day or not any(rt.calendar.is_session_day(m, day) for m in ("MY", "US")):
                continue
            review.daily_review(s, pid, day, rt.data_source, now)
        experience.collect(s, pid, rt.data_source, now)
        monday = today - timedelta(days=today.weekday())
        if first_day < monday:
            prev_monday = monday - timedelta(days=7)
            y, w, _ = prev_monday.isocalendar()
            period = (datetime.combine(prev_monday, time(0), MYT), datetime.combine(monday, time(0), MYT))
            review.weekly_review(s, rt, pid, f"{y}-W{w:02d}", period, rt.data_source, now)
    rt._learning_checked = today
