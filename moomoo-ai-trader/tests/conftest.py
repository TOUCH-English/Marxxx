import os
import shutil
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _closed_port() -> int:
    """一个当前没有程序监听的本机端口。测试不能依赖「本机没有运行 OpenD」（用户 Mac 上 OpenD 常驻在 11111）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每个测试一个全新的数据目录（无密钥、无账户）。"""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copytree(ROOT / "data" / "fixtures", data / "fixtures")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("AI_PROVIDER", "fixture")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("MOOMOO_OPEND_PORT", str(_closed_port()))
    from app.config import reset_settings_cache
    from app.db import session as dbs
    from app.core import calendar as cal
    from app.broker import gate
    from app.api import security
    from app.market_data.moomoo_quotes import MoomooQuoteProvider

    reset_settings_cache()
    dbs.dispose_all()
    cal._default = None
    gate.revoke_all_tokens()
    security.store.clear()
    MoomooQuoteProvider._shared = None
    yield data
    dbs.dispose_all()
    reset_settings_cache()
