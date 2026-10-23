"""运行授权的签名与校验。

授权记录由后端在用户登录会话内确认后创建，并用仅本机可读的密钥做 HMAC 签名。
工作进程每次使用授权前都校验签名、状态和有效期：直接改数据库或伪造请求都无法得到有效授权。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from ..config import get_settings


def _key_path() -> Path:
    return get_settings().secrets_dir / "authorization_hmac.key"


def _secret() -> bytes:
    p = _key_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    return p.read_bytes()


def _canonical(a) -> bytes:
    payload = {
        "id": a.id,
        "portfolio_id": a.portfolio_id,
        "mode": a.mode,
        "account_binding_id": a.account_binding_id,
        "capital": a.capital,
        "max_planned_loss": a.max_planned_loss,
        "scope": a.scope,
        "permissions": a.permissions,
        "strategy_version_ids": a.strategy_version_ids,
        "risk_policy_version_id": a.risk_policy_version_id,
        "overnight": a.overnight,
        "exit_rules": a.exit_rules,
        "valid_from": a.valid_from.isoformat(),
        "expires_at": a.expires_at.isoformat(),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign(a) -> str:
    return hmac.new(_secret(), _canonical(a), hashlib.sha256).hexdigest()


def verify(a) -> bool:
    if not a.signature:
        return False
    return hmac.compare_digest(a.signature, sign(a))


def check(a, now: datetime, mode: str | None = None) -> tuple[bool, str]:
    if a is None:
        return False, "没有有效的运行授权"
    if not verify(a):
        return False, "运行授权签名无效（可能被篡改）"
    if a.status != "ACTIVE":
        return False, f"运行授权状态为 {a.status}"
    if now < a.valid_from:
        return False, "运行授权尚未生效"
    if now >= a.expires_at:
        return False, "运行授权已到期"
    if mode is not None and a.mode != mode:
        return False, "运行授权与当前模式不符"
    return True, "有效"
