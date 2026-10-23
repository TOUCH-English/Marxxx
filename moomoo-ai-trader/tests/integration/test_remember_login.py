"""「记住这台 Mac 30 天」：重启后仍登录、退出与过期、改密码失效、Cookie 属性、文件里不存令牌。"""
import json
import os
import stat
import time

from fastapi.testclient import TestClient


def _client():
    from app.api.app import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1:8710")


def _login(c, remember):
    r = c.post("/api/auth/login", json={"password": "password123", "remember": remember})
    assert r.status_code == 200, r.text
    return r


def _setup():
    c = _client()
    c.post("/api/auth/setup", json={"password": "password123"})
    c.cookies.clear()
    return c


def test_remembered_login_survives_restart_and_keeps_reauth_requirement(env):
    from app.api import security
    from app.config import get_settings

    c = _setup()
    r = _login(c, remember=True)
    cookies = r.headers.get_list("set-cookie")
    sess_cookie = next(x for x in cookies if x.startswith("mt_session="))
    csrf_cookie = next(x for x in cookies if x.startswith("mt_csrf="))
    for ck in (sess_cookie, csrf_cookie):
        assert "Max-Age=2592000" in ck and "SameSite=strict" in ck
    assert "HttpOnly" in sess_cookie and "HttpOnly" not in csrf_cookie
    token = c.cookies.get("mt_session")
    # 文件只存摘要，权限 0600
    p = get_settings().secrets_dir / "remembered_sessions.json"
    raw = p.read_text()
    assert token not in raw and "password123" not in raw
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # 模拟进程重启：内存中的会话全部丢失
    security.store._sessions.clear()
    assert c.get("/api/auth/state").json()["logged_in"] is True
    s = security.store.get(token)
    assert s.remember and s.reauth_at == 0.0  # 重启恢复的会话：敏感操作仍需再次输入密码
    h = {"x-csrf-token": c.cookies.get("mt_csrf")}
    assert c.get("/api/sim/overview").status_code == 200
    # 退出：记录删除，重启后也不能再用
    assert c.post("/api/auth/logout", headers=h).status_code == 200
    security.store._sessions.clear()
    assert security.store.get(token) is None
    assert token[:8] not in p.read_text() and json.loads(p.read_text()) == {}


def test_plain_login_is_not_remembered(env):
    from app.api import security

    c = _setup()
    r = _login(c, remember=False)
    assert not any("Max-Age" in x for x in r.headers.get_list("set-cookie"))
    token = c.cookies.get("mt_session")
    security.store._sessions.clear()
    assert security.store.get(token) is None


def test_remembered_login_expires_after_30_days_and_on_password_change(env, monkeypatch):
    from app.api import security

    c = _setup()
    _login(c, remember=True)
    token = c.cookies.get("mt_session")
    real = time.time
    monkeypatch.setattr(security.time, "time", lambda: real() + security.REMEMBER_S + 5)
    assert security.store.get(token) is None
    monkeypatch.setattr(security.time, "time", real)
    # 改密码（这里直接重设密码文件）：之前记住的登录全部失效，即使进程没有重启
    c2 = _setup()
    _login(c2, remember=True)
    t2 = c2.cookies.get("mt_session")
    assert security.store.get(t2) is not None
    security.set_password("another-pass-1")
    assert security.store.get(t2) is None
