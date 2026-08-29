from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Family Budget Bot"
    app_timezone: str = "Asia/Bangkok"
    base_currency: str = "KZT"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://family_bot:family_bot@localhost/family_bot"

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_webhook_url: str = ""
    telegram_allowed_chat_id: int | None = None
    telegram_owner_user_id: int | None = None
    telegram_member_user_id: int | None = None
    setup_mode: bool = False

    openai_api_key: SecretStr = SecretStr("")
    openai_receipt_model: str = "gpt-5.6-terra"
    openai_fallback_model: str = "gpt-5.6-sol"
    openai_timeout_seconds: float = 90.0

    receipt_storage_path: Path = Path("receipts")
    receipt_retry_limit: int = 3
    receipt_poll_seconds: float = 1.0
    receipt_review_confidence: float = Field(default=0.75, ge=0, le=1)

    daily_report_hour: int = Field(default=23, ge=0, le=23)
    daily_report_minute: int = Field(default=0, ge=0, le=59)
    financial_cycle_start_day: int = Field(default=5, ge=1, le=28)
    auto_seed: bool = True

    @field_validator("base_currency")
    @classmethod
    def normalize_base_currency(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("database_url")
    @classmethod
    def use_async_postgres_driver(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def bot_token(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def webhook_secret(self) -> str:
        return self.telegram_webhook_secret.get_secret_value()

    @property
    def openai_key(self) -> str:
        return self.openai_api_key.get_secret_value()

    @property
    def allowed_user_ids(self) -> set[int]:
        return {
            value
            for value in (self.telegram_owner_user_id, self.telegram_member_user_id)
            if value is not None
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
