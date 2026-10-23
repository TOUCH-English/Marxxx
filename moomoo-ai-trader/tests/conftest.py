import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
