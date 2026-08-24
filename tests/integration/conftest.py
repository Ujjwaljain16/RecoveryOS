"""
Shared fixtures for tests/integration/ — real Postgres (via the root
conftest.py) + real Redis + a live in-process ASGI app.

Extracted from test_ingest.py (Task 4) so test_auth.py doesn't have to
duplicate the same Redis-container/app/async_client plumbing — duplicating
fixture code across test files is how they silently drift out of sync with
each other.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient

try:
    from testcontainers.redis import RedisContainer
except ImportError:
    RedisContainer = None


STREAM_NAME = "stream:payment_failed"
STREAM_RISK = "stream:risk_engine"


def to_async_url(sync_url: str) -> str:
    """
    testcontainers' PostgresContainer defaults to driver="psycopg2"
    (tests/conftest.py), so migrated_db is scheme "postgresql+psycopg2://",
    not plain "postgresql://" — a bare .replace("postgresql://", "postgresql+asyncpg://")
    silently no-ops on that string (the substring never matches) and the
    resulting engine URL stays synchronous, which create_async_engine
    rejects. Strip whatever driver is present instead of assuming none is.
    """
    scheme, _, rest = sync_url.partition("://")
    driverless = scheme.split("+", 1)[0]
    return f"{driverless}+asyncpg://{rest}"


@pytest.fixture(scope="session")
def redis_container():
    if RedisContainer is None:
        pytest.skip("testcontainers[redis] not installed")
    with RedisContainer("redis:7-alpine") as container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container):
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture(autouse=True)
def patch_settings(redis_url, migrated_db, monkeypatch):
    """Redirect settings to use testcontainer URLs for both Postgres and Redis."""

    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("DATABASE_URL", to_async_url(migrated_db))
    monkeypatch.setenv("DATABASE_URL_SYNC", migrated_db)
    monkeypatch.setenv("ENV", "test")

    from recoveryos.config import get_settings

    get_settings.cache_clear()

    import recoveryos.redis as _redis_mod

    _redis_mod._redis_pool = None  # Reset pool so it picks up new URL

    # recoveryos.database.get_app_engine() is a process-global singleton
    # (recoveryos/database.py) — reasonable in production (one pool for the
    # life of the process) but each pytest-asyncio test function gets its
    # OWN event loop. An asyncpg engine/pool created under a previous test's
    # (now-closed) loop breaks the next test with opaque errors deep in
    # asyncpg/proactor internals ("NoneType has no attribute 'send'",
    # "Event loop is closed") — not a real DB problem, just a stale pool
    # bound to a dead loop. Force a fresh engine per test.
    import recoveryos.database as _db_mod

    _db_mod._app_engine = None
    _db_mod._app_session_factory = None
    _db_mod._diagnoser_engine = None
    _db_mod._diagnoser_session_factory = None


@pytest_asyncio.fixture()
async def redis_client(redis_url) -> AsyncGenerator[aioredis.Redis, None]:
    client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    try:
        await client.delete(STREAM_NAME, STREAM_RISK)
    except Exception:
        pass
    yield client
    await client.aclose()


@pytest.fixture()
def app():
    from apps.api.main import create_app

    return create_app()


@pytest_asyncio.fixture()
async def async_client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def seed_merchant_and_customer(migrated_db: str, merchant_id: str, customer_id: str) -> None:
    """
    payments.merchant_id and payments.customer_id are real FKs (models.py) to
    merchants/customers — onboarding those is a separate flow the TRD data
    model assumes already happened before a payment can exist. Tests that
    exercise the consumer's actual DB write must seed these prerequisite
    rows first, the same way a real deployment's onboarding step would have.
    """
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            _text(
                "INSERT INTO merchants (merchant_id, name) VALUES (:mid, :name) "
                "ON CONFLICT (merchant_id) DO NOTHING"
            ),
            {"mid": merchant_id, "name": f"test-merchant-{merchant_id[:8]}"},
        )
        await conn.execute(
            _text(
                "INSERT INTO customers (customer_id, merchant_id) VALUES (:cid, :mid) "
                "ON CONFLICT (customer_id) DO NOTHING"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
    await engine.dispose()


async def seed_merchant_with_api_key(
    migrated_db: str, merchant_id: str, name: str, raw_api_key: str
) -> None:
    """
    Seed a merchant with a real, verifiable API key — hashed exactly the way
    apps/api/dependencies/auth.py:verify_api_key looks it up (same
    hash_api_key function, same pepper from settings), so tests exercise the
    real verification path end to end rather than a parallel test-only one.
    """
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from apps.api.dependencies.auth import hash_api_key

    key_hash = hash_api_key(raw_api_key)
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            _text(
                "INSERT INTO merchants (merchant_id, name, api_key_hash) "
                "VALUES (:mid, :name, :key_hash) "
                "ON CONFLICT (merchant_id) DO UPDATE SET api_key_hash = :key_hash"
            ),
            {"mid": merchant_id, "name": name, "key_hash": key_hash},
        )
    await engine.dispose()
