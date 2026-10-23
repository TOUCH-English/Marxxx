"""审计事件与站内通知。日志内容会经过脱敏。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import AuditEvent, Notification

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(api[_-]?key|password|passwd|pwd|secret|token)\s*[:=]\s*\S+"),
]


def redact(text: str) -> str:
    out = text
    for p in _SECRET_PATTERNS:
        out = p.sub("[已脱敏]", out)
    return out


def mask_account(acc_id: str | int | None) -> str:
    s = str(acc_id or "")
    if len(s) <= 4:
        return "****"
    return "*" * (len(s) - 4) + s[-4:]


def audit(
    s: Session,
    category: str,
    action: str,
    message: str,
    *,
    actor: str = "system",
    portfolio_id: int | None = None,
    symbol: str = "",
    decision_id: str = "",
    intent_id: str = "",
    data: dict[str, Any] | None = None,
    as_of: datetime | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        category=category,
        action=action,
        actor=actor,
        portfolio_id=portfolio_id,
        symbol=symbol or "",
        decision_id=decision_id or "",
        intent_id=intent_id or "",
        message=redact(message),
        data=data or {},
        as_of=as_of,
    )
    s.add(ev)
    return ev


def notify(s: Session, level: str, title: str, body: str = "", dedupe_key: str | None = None) -> None:
    if dedupe_key:
        exists = s.query(Notification.id).filter(Notification.dedupe_key == dedupe_key).first()
        if exists:
            return
    s.add(Notification(level=level, title=redact(title), body=redact(body), dedupe_key=dedupe_key))
    s.flush()
