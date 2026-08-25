"""
Task A2 — X-Model-Version / X-Policy-Version must be traceable to real
values (services.recovery_engine.propensity.MODEL_VERSION and the platform
PolicyConfig's version column), not a hardcoded literal that happens to be
present. Proven by actually swapping each source and observing the header
change — "no longer the same literal string" alone would not distinguish
"reads a real source" from "reads a DIFFERENT hardcoded literal."
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from apps.api.dependencies.auth import generate_api_key
from apps.api.versioning import clear_policy_version_cache
from services.recovery_engine.orchestrator import PLATFORM_DEFAULT_POLICY_CONFIG_ID
from tests.integration.conftest import seed_merchant_with_api_key, to_async_url


async def _seeded_merchant(migrated_db: str) -> str:
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, "header-test-merchant", raw_key)
    return raw_key


@pytest.mark.asyncio
async def test_model_version_header_reflects_the_actual_loaded_model(
    async_client, migrated_db, monkeypatch
):
    """
    Swap the value predict_recovery_probability() actually uses (not a
    second hardcoded string in the test) and confirm the header follows it.
    """
    await _seeded_merchant(migrated_db)

    resp_before = await async_client.get("/health")
    original_version = resp_before.headers["X-Model-Version"]

    import services.recovery_engine.propensity as propensity_module

    monkeypatch.setattr(propensity_module, "MODEL_VERSION", "test-swapped-model-v999")

    resp_after = await async_client.get("/health")
    assert resp_after.headers["X-Model-Version"] == "test-swapped-model-v999"
    assert resp_after.headers["X-Model-Version"] != original_version


@pytest.mark.asyncio
async def test_policy_version_header_reflects_a_real_version_bump(async_client, migrated_db):
    """
    Bump policy_configs.version for the real platform-default row and
    confirm the header changes to match — not just "no longer v1".
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_configs (policy_config_id, version) VALUES (:id, 1) "
                "ON CONFLICT (policy_config_id) DO UPDATE SET version = 1"
            ),
            {"id": PLATFORM_DEFAULT_POLICY_CONFIG_ID},
        )
    clear_policy_version_cache()

    resp_v1 = await async_client.get("/health")
    assert resp_v1.headers["X-Policy-Version"] == "1"

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE policy_configs SET version = 42 WHERE policy_config_id = :id"),
            {"id": PLATFORM_DEFAULT_POLICY_CONFIG_ID},
        )
    clear_policy_version_cache()

    resp_v42 = await async_client.get("/health")
    assert resp_v42.headers["X-Policy-Version"] == "42"
    assert resp_v42.headers["X-Policy-Version"] != resp_v1.headers["X-Policy-Version"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_version_header_falls_back_to_unknown_not_a_fake_number_when_row_missing(
    async_client, migrated_db, monkeypatch
):
    """
    Point the lookup at a policy_config_id that genuinely doesn't exist --
    the header must say "unknown", never silently fabricate a plausible-
    looking version number for data that isn't there. Deliberately does
    NOT delete the real platform-default row: by the time this runs as
    part of the full suite, other tests (Phase 5-7's decide_and_persist)
    have already created policy_decisions rows that FK-reference it --
    deleting the shared row breaks unrelated tests with an IntegrityError
    instead of testing anything about THIS code path. Caught by running
    the full suite, not just this file in isolation.
    """
    import services.recovery_engine.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "PLATFORM_DEFAULT_POLICY_CONFIG_ID", str(uuid.uuid4()))
    clear_policy_version_cache()

    resp = await async_client.get("/health")
    assert resp.headers["X-Policy-Version"] == "unknown"


@pytest.mark.asyncio
async def test_health_reports_unhealthy_when_redis_down(async_client, migrated_db, monkeypatch):
    """
    Task A3: point the app's Redis client at an address nothing listens on
    (a real, genuine connection failure -- not a mock returning False) and
    confirm /health reports 503 with the redis check failed, while
    database stays "ok" (proving the two checks are independent, not one
    flag for "something's wrong").
    """
    import recoveryos.redis as redis_module

    # Force a fresh pool bound to an address with nothing listening.
    redis_module._redis_pool = None
    monkeypatch.setattr(
        redis_module,
        "get_redis_pool",
        lambda: __import__("redis.asyncio", fromlist=["Redis"]).from_url(
            "redis://localhost:1/0",
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=1,
        ),
    )
    # health.py imports get_redis_pool directly into its own namespace at
    # import time -- patch it there too, or the route keeps the old ref.
    import apps.api.routers.health as health_module

    monkeypatch.setattr(health_module, "get_redis_pool", redis_module.get_redis_pool)

    resp = await async_client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert "unreachable" in body["checks"]["redis"]
    assert (
        body["checks"]["database"] == "ok"
    ), "a Redis outage must not also report the DB as unhealthy"

    # Restore the real pool for any test running after this one.
    redis_module._redis_pool = None
