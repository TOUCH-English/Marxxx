from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException, Request

from ..core.enums import Namespace
from . import security


def ns_of(ns: str) -> str:
    if ns not in (Namespace.SIM.value, Namespace.LIVE.value):
        raise HTTPException(404, "未知命名空间")
    return ns


def session_dep(request: Request) -> security.Session:
    return security.current_session(request)


def mutate_dep(request: Request) -> security.Session:
    s = security.current_session(request)
    security.require_csrf(request, s)
    return s


Viewer = Depends(session_dep)
Mutator = Depends(mutate_dep)


def j(v: Any) -> Any:
    """JSON 友好化：Decimal→字符串（不经浮点），datetime→ISO。"""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [j(x) for x in v]
    return v


def row(obj, fields: list[str] | None = None) -> dict:
    cols = fields or [c.key for c in obj.__table__.columns]
    return {c: j(getattr(obj, c)) for c in cols}
