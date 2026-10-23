"""日内亏损线、最大回撤线、计划亏损上限与其他锁定。

- 锁定持久化在数据库中：服务重启或价格短暂回升都不会自动解锁。
- 日内亏损锁：延续到下一个马来西亚时间 00:00，且健康检查通过后才由工作进程解除。
- 回撤锁、计划亏损上限锁：必须人工复核后解除。
- 策略版本回退不会、也不能清除任何锁定。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.audit import audit, notify
from ..core.calendar import myt_day, next_myt_midnight
from ..core.enums import LockKind
from ..db.models import DailyEquity, RiskLock
from ..db.types import D


def active_locks(s: Session, pid: int) -> list[RiskLock]:
    return list(s.execute(select(RiskLock).where(RiskLock.portfolio_id == pid, RiskLock.active.is_(True))).scalars())


def blocking_locks(s: Session, pid: int, symbol: str | None = None) -> list[RiskLock]:
    return [lk for lk in active_locks(s, pid) if lk.scope == "ALL" or (symbol and lk.scope == symbol)]


def add_lock(s: Session, pid: int, kind: str, reason: str, now: datetime, *, scope: str = "ALL",
             release_not_before: datetime | None = None, manual: bool = False) -> RiskLock:
    for lk in active_locks(s, pid):
        if lk.kind == kind and lk.scope == scope:
            return lk
    lk = RiskLock(portfolio_id=pid, kind=kind, scope=scope, reason=reason, release_not_before=release_not_before,
                  requires_manual_review=manual, as_of=now)
    s.add(lk)
    audit(s, "RISK", f"LOCK_{kind}", reason, actor="worker", portfolio_id=pid, symbol="" if scope == "ALL" else scope,
          as_of=now)
    notify(s, "warning", f"风控锁定：{reason}", "已停止相关新开仓；保护性退出不受影响。")
    s.flush()
    return lk


def release_lock(s: Session, lk: RiskLock, by: str, note: str, now: datetime) -> None:
    lk.active = False
    lk.released_at = now
    lk.released_by = by
    lk.release_note = note
    audit(s, "RISK", f"UNLOCK_{lk.kind}", f"解除锁定：{lk.reason}（{note}）", actor=by, portfolio_id=lk.portfolio_id,
          as_of=now)


def _row(s: Session, pid: int, ccy: str, day: str) -> DailyEquity | None:
    return s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == pid, DailyEquity.currency == ccy,
                                               DailyEquity.day_myt == day)).scalar_one_or_none()


def _latest(s: Session, pid: int, ccy: str) -> DailyEquity | None:
    return s.execute(select(DailyEquity).where(DailyEquity.portfolio_id == pid, DailyEquity.currency == ccy)
                     .order_by(DailyEquity.day_myt.desc()).limit(1)).scalar_one_or_none()


def ensure_day(s: Session, pid: int, ccy: str, equity: Decimal, now: datetime) -> DailyEquity:
    day = myt_day(now)
    row = _row(s, pid, ccy, day)
    if row is None:
        prev = _latest(s, pid, ccy)
        start = prev.last_equity if prev else equity
        hwm = prev.hwm_adjusted if prev else equity
        row = DailyEquity(portfolio_id=pid, currency=ccy, day_myt=day, start_equity=start, last_equity=start,
                          min_equity=start, flows=Decimal(0), hwm_adjusted=max(hwm, start), updated_at=now)
        s.add(row)
        s.flush()
    return row


def record_flow(s: Session, pid: int, ccy: str, amount: Decimal, now: datetime) -> None:
    """入金/分配调整：从日内亏损和回撤计算中剔除。"""
    row = _row(s, pid, ccy, myt_day(now))
    if row is None:
        prev = _latest(s, pid, ccy)
        base = prev.last_equity if prev else Decimal(0)
        row = DailyEquity(portfolio_id=pid, currency=ccy, day_myt=myt_day(now), start_equity=base,
                          last_equity=base, min_equity=base, flows=Decimal(0),
                          hwm_adjusted=prev.hwm_adjusted if prev else base, updated_at=now)
        s.add(row)
    row.flows += D(amount)
    row.hwm_adjusted += D(amount)
    row.last_equity += D(amount)
    row.min_equity += D(amount)
    s.flush()


def update_and_check(s: Session, pid: int, ccy: str, equity: Decimal, params: dict, now: datetime,
                     *, allocation: Decimal | None = None, max_planned_loss: Decimal | None = None) -> list[str]:
    """更新净值并在超线时加锁。返回本次新触发的锁类型。"""
    row = ensure_day(s, pid, ccy, equity, now)
    row.last_equity = equity
    row.min_equity = min(row.min_equity, equity)
    row.hwm_adjusted = max(row.hwm_adjusted, equity)
    row.updated_at = now
    fired: list[str] = []
    base = row.start_equity + row.flows
    if base > 0:
        loss = base - equity
        if loss >= base * D(params["daily_loss_pct"]):
            add_lock(s, pid, LockKind.DAILY_LOSS,
                     f"{ccy} 当日亏损 {loss:.2f} 已达当日起始净值的 {D(params['daily_loss_pct']) * 100:.2f}%",
                     now, release_not_before=next_myt_midnight(now))
            fired.append(LockKind.DAILY_LOSS)
    if row.hwm_adjusted > 0:
        dd = (row.hwm_adjusted - equity) / row.hwm_adjusted
        if dd >= D(params["max_drawdown_pct"]):
            add_lock(s, pid, LockKind.DRAWDOWN,
                     f"{ccy} 回撤 {dd * 100:.2f}% 已达暂停线 {D(params['max_drawdown_pct']) * 100:.2f}%（需人工复核）",
                     now, manual=True)
            fired.append(LockKind.DRAWDOWN)
    if allocation is not None and max_planned_loss is not None and max_planned_loss > 0:
        if allocation - equity >= max_planned_loss:
            add_lock(s, pid, "MAX_LOSS",
                     f"{ccy} 累计亏损 {allocation - equity:.2f} 已达您设定的可承受亏损 {max_planned_loss:.2f}（需人工复核）",
                     now, manual=True)
            fired.append("MAX_LOSS")
    return fired


def try_release_daily(s: Session, pid: int, now: datetime, health_ok: bool) -> list[RiskLock]:
    released = []
    for lk in active_locks(s, pid):
        if lk.kind == LockKind.DAILY_LOSS and lk.release_not_before and now >= lk.release_not_before and health_ok:
            release_lock(s, lk, "worker", "已过马来西亚时间日界线且健康检查通过", now)
            released.append(lk)
    return released
