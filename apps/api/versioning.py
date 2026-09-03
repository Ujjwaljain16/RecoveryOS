"""
Real X-Model-Version / X-Policy-Version sources for the response-header
middleware (apps/api/main.py). TRD §5's stated purpose: "so any dashboard
screenshot is reproducible against the exact model/policy that produced
it." A hardcoded literal string defeats that regardless of how confident
it looks — these two functions are the ONE place either header's value
comes from, reading the actual sources of truth already built
(services/recovery_engine/propensity.MODEL_VERSION) and the actual
platform-default PolicyConfig row's version column, not a re-hardcoded
constant one layer down from main.py.

Policy version is DB-backed and therefore real I/O — cached with a short
TTL so it isn't queried on every single HTTP response (this middleware
runs for /health and /docs too, not just data endpoints), and wrapped so a
DB outage degrades the header to "unknown" rather than taking down every
response in the process (that failure mode is explicitly what Task A3's
health check exists to surface instead).
"""

from __future__ import annotations

import time

UNKNOWN_VERSION = "unknown"
POLICY_VERSION_CACHE_TTL_SECONDS = 30.0

_policy_version_cache: dict[str, object] = {"version": None, "cached_at": 0.0}


def get_current_model_version() -> str:
    """
    Real, no-I/O: services.recovery_engine.propensity.MODEL_VERSION is a
    plain module attribute reflecting exactly which certified artifact
    (model_lr.pkl, per the LR-vs-LightGBM correction) production
    inference actually loads — reading it directly means this header can
    never drift from what predict_recovery_probability() is really using.
    """
    from services.recovery_engine.propensity import MODEL_VERSION

    return MODEL_VERSION


def clear_policy_version_cache() -> None:
    """Test hook — force the next get_current_policy_version() call to
    re-query instead of serving a cached value, so a test that bumps
    policy_configs.version doesn't have to sleep out the TTL."""
    _policy_version_cache["version"] = None
    _policy_version_cache["cached_at"] = 0.0


async def get_current_policy_version() -> str:
    """
    Reads policy_configs.version for the platform-default policy config
    row (services.recovery_engine.orchestrator.PLATFORM_DEFAULT_POLICY_CONFIG_ID
    — the same row decide_and_persist() falls back to for any merchant
    without its own policy_config_id). Cached for
    POLICY_VERSION_CACHE_TTL_SECONDS to avoid a DB round trip on every
    response; falls back to UNKNOWN_VERSION (not a fabricated number) if
    the query fails, so a DB outage degrades this header instead of
    breaking every HTTP response in the process.
    """
    now = time.monotonic()
    cached_version = _policy_version_cache["version"]
    cached_at = _policy_version_cache["cached_at"]
    if cached_version is not None and (now - cached_at) < POLICY_VERSION_CACHE_TTL_SECONDS:
        return cached_version  # type: ignore[return-value]

    try:
        from sqlalchemy import select

        from recoveryos.database import get_app_session_factory
        from recoveryos.models import PolicyConfig
        from services.recovery_engine.orchestrator import PLATFORM_DEFAULT_POLICY_CONFIG_ID

        async with get_app_session_factory()() as session:
            row = (
                await session.execute(
                    select(PolicyConfig.version).where(
                        PolicyConfig.policy_config_id == PLATFORM_DEFAULT_POLICY_CONFIG_ID
                    )
                )
            ).scalar_one_or_none()
    except Exception:
        return UNKNOWN_VERSION

    version = str(row) if row is not None else UNKNOWN_VERSION
    _policy_version_cache["version"] = version
    _policy_version_cache["cached_at"] = now
    return version
