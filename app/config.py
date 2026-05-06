"""Runtime configuration loaded from environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = []
    anthropic_api_key: str

    database_url: str
    public_base_url: str

    s3_endpoint_url: str = "https://fsn1.your-objectstorage.com"
    s3_region: S3Region = "fsn1"
    s3_access_key: str
    s3_secret_key: str
    s3_bucket_receipts: str = "receipts-raw"
    s3_bucket_reports: str = "reports"
    s3_prefix_receipts: str = "receipts"
    s3_prefix_reports: str = "reports"

    anthropic_model: str = "claude-sonnet-4-5"
    batch_poll_interval_seconds: int = 60
    presign_ttl_seconds: int = 60 * 60 * 24

    log_level: str = "INFO"
    bundle_size_limit_mb: int = 45

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, v: object) -> object:
        if v is None or v == "":
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(s.strip()) for s in v.split(",") if s.strip()]
        return v

    @field_validator("s3_prefix_receipts", "s3_prefix_reports", mode="before")
    @classmethod
    def _strip_slashes(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().strip("/")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
