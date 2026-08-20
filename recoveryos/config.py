"""
RecoveryOS — Central Configuration
===================================
Single source of truth for all environment-sourced settings.
Pydantic v2 BaseSettings reads from environment variables, with .env file fallback.

Every service imports from here — never os.environ directly.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(str, Enum):
    """
    DEMO:    SimulatorAdapter is default; /v1/simulate/degrade endpoint is ENABLED.
    STAGING: RazorpayTestAdapter is default; /v1/simulate/degrade is DISABLED.
    TEST:    Used by pytest — points to testcontainer-managed Postgres.
    """
    DEMO = "demo"
    STAGING = "staging"
    TEST = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Environment
    # ─────────────────────────────────────────────────────────────────────────
    env: AppEnvironment = Field(
        default=AppEnvironment.DEMO,
        description="Runtime environment. Controls adapter selection and demo endpoints.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Database
    # ─────────────────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://recoveryos:recoveryos@localhost:5432/recoveryos",
        description="Async-compatible PostgreSQL DSN used by the application.",
    )
    database_url_sync: str = Field(
        default="postgresql://recoveryos:recoveryos@localhost:5432/recoveryos",
        description="Synchronous DSN used by Alembic migrations.",
    )

    # DB role DSNs for privilege-separated connections
    diagnoser_database_url: str = Field(
        default="postgresql+asyncpg://diagnoser_role:diagnoser_pass@localhost:5432/recoveryos",
        description="Restricted DSN for the AI Diagnoser — no access to ground_truth columns.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Redis / Queue
    # ─────────────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis DSN for job queue and stream buffering.",
    )
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0",
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/1",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # LLM / AI
    # ─────────────────────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="", description="API key for LLM provider.")
    ai_diagnoser_timeout_seconds: float = Field(
        default=2.5,
        description="Hard timeout for AI Diagnoser (TRD §8). Triggers deterministic fallback.",
    )
    ai_diagnoser_model: str = Field(default="gpt-4o-mini")

    # ─────────────────────────────────────────────────────────────────────────
    # Policy defaults (can be overridden per merchant in policy_configs table)
    # ─────────────────────────────────────────────────────────────────────────
    default_max_retries: int = Field(default=2)
    default_retry_cooldown_hours: int = Field(default=12)
    default_max_amount_paise: int = Field(default=2_500_000)  # ₹25,000
    default_min_expected_value_paise: int = Field(default=0)

    # ─────────────────────────────────────────────────────────────────────────
    # API
    # ─────────────────────────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    api_title: str = Field(default="RecoveryOS API")
    api_version: str = Field(default="v1")

    # ─────────────────────────────────────────────────────────────────────────
    # Observability
    # ─────────────────────────────────────────────────────────────────────────
    prometheus_port: int = Field(default=8001)

    # ─────────────────────────────────────────────────────────────────────────
    # Anomaly detection
    # ─────────────────────────────────────────────────────────────────────────
    anomaly_z_score_high_threshold: float = Field(default=3.0)
    anomaly_z_score_medium_threshold: float = Field(default=2.0)
    anomaly_min_sample_size: int = Field(default=30)
    anomaly_bucket_minutes: int = Field(default=15)

    @property
    def is_demo(self) -> bool:
        return self.env == AppEnvironment.DEMO

    @property
    def is_test(self) -> bool:
        return self.env == AppEnvironment.TEST


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Cached settings singleton.
    Use `get_settings.cache_clear()` in tests that need to override env.
    """
    return Settings()
