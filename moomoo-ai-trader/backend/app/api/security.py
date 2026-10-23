"""本地网页的访问控制。

- 首次使用设置本机密码（scrypt 哈希，保存在仅本机可读的 data/secrets/）。
- 登录后发放 HttpOnly + SameSite=Strict 会话 Cookie，空闲/绝对过期。
- 所有改变状态的请求：必须有有效会话 + CSRF 令牌（双重提交）+ 合法 Host/Origin。
- 不因为只监听 127.0.0.1 就放行：任意网页都无法跨站触发交易。
- 敏感操作（实盘授权、放宽风控、手动清仓、批准策略升级）需要最近 10 分钟内再次输入密码。
- 「记住这台 Mac 30 天」（用户可选，默认不勾选）：会话记录写入 data/secrets/remembered_sessions.json（0600），
  只存令牌的 SHA-256 摘要，不存令牌本身或密码；30 天绝对过期；改密码、退出登录后立即失效；
  系统重启后恢复的会话不算「最近确认过密码」，敏感操作仍要再输入密码。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from ..config import get_settings

SESSION_COOKIE = "mt_session"
CSRF_COOKIE = "mt_csrf"
CSRF_HEADER = "x-csrf-token"
REAUTH_WINDOW_S = 600
REMEMBER_S = 30 * 24 * 3600


@dataclass
class Session:
    token: str
    csrf: str
    created: float
    last_seen: float
    reauth_at: float = 0.0
    remember: bool = False
    data: dict = field(default_factory=dict)


class SessionStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._fail: list[float] = []

    def create(self, remember: bool = False) -> Session:
        now = time.time()
        s = Session(token=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(24), created=now, last_seen=now,
                    reauth_at=now, remember=remember)
        with self._lock:
            self._sessions[s.token] = s
            if remember:
                recs = _load_remembered()
                recs[_digest(s.token)] = {"csrf": s.csrf, "created": now, "pw": _pw_fingerprint()}
                _save_remembered(recs)
        return s

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        st = get_settings()
        with self._lock:
            now = time.time()
            s = self._sessions.get(token)
            if s is None or s.remember:
                # 记住的会话每次都核对持久记录：退出、过期、改密码后立即失效；进程重启后从记录恢复
                rec = self._remembered(token, now)
                if rec is None:
                    self._sessions.pop(token, None)
                    return None
                if s is None:
                    s = Session(token=token, csrf=rec["csrf"], created=rec["created"], last_seen=now, reauth_at=0.0,
                                remember=True)
                    self._sessions[token] = s
            elif now - s.created > st.session_ttl_hours * 3600 or now - s.last_seen > st.session_idle_minutes * 60:
                self._sessions.pop(token, None)
                return None
            s.last_seen = now
            return s

    @staticmethod
    def _remembered(token: str, now: float) -> dict | None:
        recs = _load_remembered()
        key = _digest(token)
        rec = recs.get(key)
        if rec is None:
            return None
        if now - float(rec.get("created", 0)) > REMEMBER_S or rec.get("pw") != _pw_fingerprint():
            recs.pop(key, None)
            _save_remembered(recs)
            return None
        return rec

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)
            recs = _load_remembered()
            if token and recs.pop(_digest(token), None) is not None:
                _save_remembered(recs)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            if _remembered_path().exists():
                _save_remembered({})

    def login_allowed(self) -> bool:
        now = time.time()
        with self._lock:
            self._fail = [t for t in self._fail if now - t < 60]
            return len(self._fail) < 5

    def record_failure(self) -> None:
        with self._lock:
            self._fail.append(time.time())


store = SessionStore()


# ------------------------------------------------------------------ 记住登录（持久记录）
def _remembered_path():
    return get_settings().secrets_dir / "remembered_sessions.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pw_fingerprint() -> str | None:
    """当前密码文件的摘要：改密码（或删除密码文件）后，所有记住的登录失效。"""
    p = _pw_path()
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _load_remembered() -> dict:
    p = _remembered_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_remembered(recs: dict) -> None:
    p = _remembered_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(recs, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------ 密码
def _pw_path():
    return get_settings().secrets_dir / "web_password.json"


def password_is_set() -> bool:
    return _pw_path().exists()


def set_password(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "密码至少 8 位")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    p = _pw_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"salt": salt.hex(), "hash": h.hex(), "alg": "scrypt-n16384-r8-p1"}, f)
    os.replace(tmp, p)


def check_password(pw: str) -> bool:
    if not password_is_set():
        return False
    d = json.loads(_pw_path().read_text())
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(d["salt"]), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(h.hex(), d["hash"])


# ------------------------------------------------------------------ 请求检查
def allowed_hosts() -> set[str]:
    st = get_settings()
    port = st.app_port
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}
    extra = os.environ.get("MT_EXTRA_HOSTS", "")
    hosts |= {h.strip() for h in extra.split(",") if h.strip()}
    return hosts


def allowed_origins() -> set[str]:
    out = {f"http://{h}" for h in allowed_hosts()}
    if os.environ.get("MT_DEV_ORIGIN"):
        out.add(os.environ["MT_DEV_ORIGIN"])
    return out


def check_host_origin(request: Request) -> None:
    host = request.headers.get("host", "")
    if host not in allowed_hosts():
        raise HTTPException(403, "Host 不被允许（只接受本机地址）")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins():
            raise HTTPException(403, "跨站请求被拒绝（Origin 不匹配）")
        ref = request.headers.get("referer")
        if origin is None and ref is not None and not any(ref.startswith(o + "/") or ref == o for o in allowed_origins()):
            raise HTTPException(403, "跨站请求被拒绝（Referer 不匹配）")


def current_session(request: Request) -> Session:
    s = store.get(request.cookies.get(SESSION_COOKIE))
    if s is None:
        raise HTTPException(401, "请先登录")
    return s


def require_csrf(request: Request, sess: Session) -> None:
    tok = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not tok or not cookie or not hmac.compare_digest(tok, cookie) or not hmac.compare_digest(tok, sess.csrf):
        raise HTTPException(403, "CSRF 校验失败，请刷新页面后重试")


def require_recent_reauth(sess: Session) -> None:
    if time.time() - sess.reauth_at > REAUTH_WINDOW_S:
        raise HTTPException(428, "此操作需要再次输入密码确认")
