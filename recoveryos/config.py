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
    DEMO:    /v1/simulate/degrade endpoint is ENABLED.
    STAGING: /v1/simulate/degrade is DISABLED.
    TEST:    Used by pytest — points to testcontainer-managed Postgres.

    Payment provider adapter selection is deliberately NOT tied to this enum
    (decided in the pre-Phase-8 audit) — see Settings.payment_provider_adapter
    below. Auto-coupling "staging" to "calls a real external payment API"
    would mean someone setting ENV=staging for an unrelated reason starts
    hitting Razorpay's real test servers unexpectedly; explicit-over-inferred
    matches this project's established config philosophy elsewhere
    (action_costs' merchant-scoped overrides, same reasoning).
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

    # ─────────────────────────────────────────────────────────────────────────
    # LLM / AI
    # ─────────────────────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="", description="API key for the OpenAI provider.")
    gemini_api_key: str = Field(default="", description="API key for the Gemini provider.")
    ai_diagnoser_provider: str = Field(
        default="openai",
        description=(
            "Which LLM provider diagnose_with_llm() calls: 'openai' or 'gemini'. Both share "
            "the exact same TRD §9 boundary (typed input, schema-constrained output, Pydantic "
            "re-validation, apply_adversarial_guards) -- this only selects which API is called."
        ),
    )
    ai_diagnoser_timeout_seconds: float = Field(
        default=2.5,
        description="Hard timeout for AI Diagnoser (TRD §8). Triggers deterministic fallback.",
    )
    ai_diagnoser_gemini_timeout_seconds: float = Field(
        default=4.0,
        description="Gemini-specific timeout -- free-tier flash-lite's observed cold-start "
        "latency runs higher than OpenAI's typical response time; tested live at 2.5s and saw "
        "3/6 calls time out even though every completed call was correct, so this is slightly "
        "more generous rather than treating slow-but-eventually-correct responses as failures.",
    )
    ai_diagnoser_model: str = Field(default="gpt-4o-mini")
    ai_diagnoser_gemini_model: str = Field(
        default="gemini-2.5-flash-lite",
        description="Gemini model id when ai_diagnoser_provider='gemini' -- flash-lite chosen "
        "deliberately for free-tier quota conservation over the heavier flash/pro variants.",
    )
    ai_recommendation_fusion_enabled: bool = Field(
        default=False,
        description=(
            "Phase 11 gate for the bounded AI tie-break/risk-escalation fusion step in "
            "services/recovery_engine/orchestrator.py. Off by default -- ships dark, same "
            "pattern as ai_diagnoser_provider gating the investigator itself. When off (or when "
            "no diagnosis_id is passed to build_decision), the decision pipeline is byte-"
            "identical to pre-Phase-11 behavior."
        ),
    )
    ai_tie_break_tolerance_bps: int = Field(
        default=100,
        description=(
            "Relative EVI tolerance, in basis points of the deterministic winner's EVI, within "
            "which an AI-recommended, individually policy-ALLOWED candidate can win a tie-break "
            "(100 = 1%). Must be fixed BEFORE running any ablation/measurement and never tuned "
            "post-hoc to match a desired result -- see docs/phase11_ai_ablation.md's 0/100/500 "
            "bps sensitivity sweep."
        ),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 12 -- Recovery Mission hard envelope. Code-set at mission
    # creation (services/recovery_engine/mission.py) and never modified by
    # anything downstream, including the AI recommendation -- see that
    # module's check_budget().
    # ─────────────────────────────────────────────────────────────────────────
    mission_max_investigation_rounds: int = Field(default=3)
    mission_max_attempts: int = Field(default=3)
    mission_max_duration_seconds: int = Field(default=604_800)  # 7 days

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
    # Comma-separated list of allowed CORS origins. Defaults to the Next.js
    # dev server so local development keeps working with zero config, but
    # is env-overridable — was hardcoded in apps/api/main.py until Task A4,
    # silently breaking CORS for any dashboard deployed somewhere that
    # isn't localhost with no code change required to notice.
    cors_allowed_origins: str = Field(default="http://localhost:3000")

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
    razorpay_timeout_seconds: float = Field(
        default=10.0,
        description="HTTP timeout for Razorpay API calls. Matches ai_diagnoser_timeout_seconds' pattern — was a bare hardcoded 10 in adapter.py.",
    )
    razorpay_webhook_secret: str = Field(
        default="",
        description="Webhook secret configured in the Razorpay dashboard for this endpoint's "
        "HMAC-SHA256 signature (Task WEBHOOK1). Distinct from razorpay_key_secret (the API key "
        "secret) -- Razorpay issues a separate secret specifically for webhook signing.",
    )

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
