"""Runtime configuration loaded from environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

S3Region = Literal["fsn1", "hel1", "nbg1"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str
    telegram_webhook_secret: str
    anthropic_api_key: str

    database_url: str
    public_base_url: str

    s3_endpoint_url: str = "https://fsn1.your-objectstorage.com"
    s3_region: S3Region = "fsn1"
    s3_access_key: str
    s3_secret_key: str
    s3_bucket_receipts: str = "receipts-raw"
    s3_bucket_reports: str = "reports"

    anthropic_model: str = "claude-sonnet-4-5"
    batch_poll_interval_seconds: int = 60
    presign_ttl_seconds: int = 60 * 60 * 24

    log_level: str = "INFO"
    bundle_size_limit_mb: int = 45


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
