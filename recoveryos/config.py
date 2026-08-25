"""
RecoveryOS — Central Configuration
===================================
Single source of truth for all environment-sourced settings.
Pydantic v2 BaseSettings reads from environment variables, with .env file fallback.

Every service imports from here — never os.environ directly.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
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
    # Task 6: the password segments of these defaults used to be the same
    # literal values ('recoveryos', 'diagnoser_pass') that were also
    # hardcoded into migrations/versions/0002_db_roles.py and committed to
    # git — i.e. the "default" was a real, working, now-permanently-
    # compromised credential. CHANGE_ME deliberately does NOT authenticate
    # against anything: if these defaults are ever actually reached (no
    # .env, no env vars set), connecting fails loudly with an auth error
    # instead of silently succeeding with a known-leaked password.
    database_url: str = Field(
        default="postgresql+asyncpg://recoveryos:CHANGE_ME@localhost:5432/recoveryos",
        description="Async-compatible PostgreSQL DSN used by the application. Set via env/.env.",
    )
    database_url_sync: str = Field(
        default="postgresql://recoveryos:CHANGE_ME@localhost:5432/recoveryos",
        description="Synchronous DSN used by Alembic migrations. Set via env/.env.",
    )

    # DB role DSNs for privilege-separated connections.
    # NOTE: the login user is "diagnoser" (diagnoser_role is a NOLOGIN role
    # granted to it, not a connectable username — migrations/versions/0002_db_roles.py).
    diagnoser_database_url: str = Field(
        default="postgresql+asyncpg://diagnoser:CHANGE_ME@localhost:5432/recoveryos",
        description="Restricted DSN for the AI Diagnoser — no access to ground_truth columns. Set via env/.env.",
    )
    # NOTE: login user is "inference" (inference_role is NOLOGIN, granted to
    # it — migrations/versions/0008_inference_role.py). Same restrictions as
    # diagnoser_database_url: zero access to ground_truth_recoverable or
    # simulator_latent_state.
    inference_database_url: str = Field(
        default="postgresql+asyncpg://inference:CHANGE_ME@localhost:5432/recoveryos",
        description="Restricted DSN for the recovery propensity model's feature reads. Set via env/.env.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Auth (Task 4 — API key per merchant)
    # ─────────────────────────────────────────────────────────────────────────
    api_key_pepper: str = Field(
        default="dev-insecure-pepper-change-in-production",
        description=(
            "Server-side secret mixed into merchants.api_key_hash (HMAC-SHA256). "
            "MUST be overridden via env var in any non-dev deployment — the "
            "default here is intentionally labeled insecure so it can never be "
            "mistaken for a real secret if left unset."
        ),
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

    # ─────────────────────────────────────────────────────────────────────────
    # Payment provider (Phase 6 — integrations/razorpay/adapter.py)
    # ─────────────────────────────────────────────────────────────────────────
    # 'simulator' | 'razorpay_test' — the ONE line that swaps providers.
    # get_provider_adapter() reads only this field; no other code path
    # branches on env/is_demo for adapter selection (that would make the
    # swap NOT config-only — see test_provider_adapter_swap_is_config_only).
    payment_provider_adapter: str = Field(
        default="simulator",
        description="Which PaymentProvider implementation execution_worker uses. Set via env/.env.",
    )
    razorpay_key_id: str = Field(default="", description="Razorpay TEST-mode key id.")
    razorpay_key_secret: str = Field(default="", description="Razorpay TEST-mode key secret.")
    razorpay_base_url: str = Field(default="https://api.razorpay.com/v1")

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
