"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Archangel."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Binance Futures
    binance_api_key: str = Field(default="", description="Binance Futures API key")
    binance_api_secret: str = Field(default="", description="Binance Futures API secret")
    binance_testnet: bool = Field(default=True, description="Use Binance Futures testnet")

    # Telegram (optional)
    telegram_bot_token: str = Field(default="", description="Telegram bot token from BotFather")
    telegram_chat_id: str = Field(default="", description="Authorized chat ID")

    # Risk management
    risk_per_trade_pct: float = Field(
        default=1.0, ge=0.01, le=100.0, description="Percent of equity to risk per trade"
    )
    max_open_positions: int = Field(default=5, ge=1, description="Max simultaneous positions")
    max_daily_loss_pct: float = Field(
        default=5.0, ge=0.0, le=100.0, description="Daily loss circuit breaker"
    )
    default_leverage: int = Field(default=5, ge=1, le=125, description="Default leverage")
    require_stop_loss: bool = Field(
        default=True, description="Reject orders without an explicit stop-loss"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset the cached settings (useful for tests)."""
    global _settings
    _settings = None
