"""部署配置（Deployment config）。

这些值只能通过本机 `.env` 文件或环境变量设置，网页和 API 无法修改。
实盘相关开关（LIVE_TRADING_ENABLED 等）因此不可能被浏览器请求打开。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Web ---
    app_host: str = "127.0.0.1"
    app_port: int = 8710
    session_ttl_hours: int = 12
    session_idle_minutes: int = 120

    # --- Data ---
    data_dir: Path = PROJECT_ROOT / "data"

    # --- 实盘总开关（部署级，默认关闭） ---
    live_trading_enabled: bool = False
    # 只有在券商原生止损无法验证时，用户可在部署配置中明确接受「客户端监控止损」
    allow_client_side_stops_live: bool = False

    # --- moomoo OpenD ---
    moomoo_opend_host: str = "127.0.0.1"
    moomoo_opend_port: int = 11111
    moomoo_security_firm: str = "FUTUMY"
    moomoo_connect_timeout_s: float = 8.0
    moomoo_order_timeout_s: float = 10.0

    # --- AI ---
    ai_provider: str = "auto"  # auto | anthropic | fixture | disabled
    anthropic_api_key: str = Field(default="", repr=False)
    ai_model: str = "claude-opus-5-5"
    ai_timeout_s: float = 60.0
    ai_max_retries: int = 1
    ai_monthly_call_limit: int = 3000
    ai_monthly_token_limit: int = 20_000_000
    # 每百万 token 价格（美元）。默认值为 Anthropic 官方价目中 claude-opus-5-5 的标价
    # （资料缓存日期 2026-10-06），换模型或价目变化时请按官网核对修改；留空则只统计 token。
    ai_price_input_per_mtok: str = "4.00"
    ai_price_output_per_mtok: str = "20.00"
    ai_price_source: str = "Anthropic 官方价目 claude-opus-5-5（缓存于 2026-10-06，需核对）"
    ai_monthly_budget_usd: str = "30"
    # 演示/历史回放是加速运行的（每 2 秒前进 30 分钟）。若在其中调用真实模型，几分钟就会产生大量费用，
    # 且历史回放结果有信息泄漏问题。因此默认演示/回放一律使用固定测试响应；真实模型只用于真实行情。
    ai_in_replay: bool = False

    # --- Worker ---
    worker_tick_seconds: float = 2.0
    replay_step_minutes: int = 30
    min_ai_interval_minutes: int = 15
    # 常规 AI 分析间隔（分钟）。留空则用策略版本里的值（60）。建议 180：日线数据一天变化不大，过于频繁只会多花钱、反复改主意。
    # 持仓大幅波动时的额外复核不受影响（仍至少间隔 min_ai_interval_minutes）。
    ai_batch_interval_minutes: int | None = None

    @field_validator("ai_batch_interval_minutes", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    def db_path(self, namespace: str) -> Path:
        return self.data_dir / f"{namespace}.db"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.secrets_dir.mkdir(parents=True, exist_ok=True)
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()
