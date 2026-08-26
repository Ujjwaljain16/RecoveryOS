"""
RecoveryOS — Root conftest.py
================================
Sets up a real ephemeral PostgreSQL container for all tests.
NO SQLite. NO mocks. Financial logic runs against the real Postgres engine.

Test isolation strategy:
  - One container per test SESSION (fast; container startup ~3s).
  - One transaction per test, rolled back after each (clean state, no truncation needed).
  - DB roles (app_role, diagnoser_role) are created by the migration,
    so role-restriction tests are realistic.

Usage:
  pytest tests/                       # all suites
  pytest tests/unit/                  # unit only (fastest)
  pytest tests/integration/           # DB-dependent
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

@pytest.fixture(scope="session", autouse=True)
def _no_real_llm_calls_by_default():
    """
    .env defaults AI_DIAGNOSER_PROVIDER to 'gemini' with a real, working
    free-tier key (Task P11) -- correct for local/docker runs, but the test
    suite must stay hermetic: most integration tests don't mock the LLM
    path at all and rely on it failing instantly (a placeholder key ->
    immediate 401 -> deterministic fallback), not on a real network call
    that can rate-limit (429) under back-to-back test runs. Forces the
    pre-P11 safe default for the whole session; any test that specifically
    wants the real Gemini path already monkeypatches AI_DIAGNOSER_PROVIDER
    itself (see tests/unit/test_gemini_diagnoser.py), which correctly
    overrides this for the duration of that one test.
    """
    os.environ["AI_DIAGNOSER_PROVIDER"] = "openai"
    os.environ["OPENAI_API_KEY"] = "sk-placeholder"
    os.environ["GEMINI_API_KEY"] = ""

    from recoveryos.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ─── Session-scoped container ──────────────────────────────────────────────────

POSTGRES_IMAGE = "postgres:16-alpine"
POSTGRES_USER = "recoveryos_test"
POSTGRES_PASSWORD = "recoveryos_test"
POSTGRES_DB = "recoveryos_test"


@pytest.fixture(scope="session")
def postgres_container():
    """
    Start a real PostgreSQL 16 container for the entire test session.
    Yields the container object (with .get_connection_url() for the sync DSN).

    testcontainers is imported HERE, not at module level: this file is
    tests/conftest.py, a parent of both tests/unit/ and tests/integration/,
    so pytest loads it for every collected test regardless of suite. CI's
    unit-tests job deliberately doesn't install testcontainers (unit tests
    need no DB) — a module-level import here made that job fail before a
    single test even ran, for a dependency unit tests never needed.
    """
    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        raise RuntimeError("testcontainers not installed. Run: pip install testcontainers") from exc

    with PostgresContainer(
        image=POSTGRES_IMAGE,
        username=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DB,
        driver="psycopg2",
    ) as container:
        yield container


@pytest.fixture(scope="session")
def db_url_sync(postgres_container) -> str:
    """Synchronous DSN for Alembic migrations and direct psycopg2 connections."""
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session")
def migrated_db(db_url_sync: str):
    """
    Run all Alembic migrations against the test container.
    Session-scoped: migrations run once, all tests share the schema.
    """
    # Override DATABASE_URL_SYNC so alembic env.py picks up the testcontainer URL
    os.environ["DATABASE_URL_SYNC"] = db_url_sync
    os.environ["ENV"] = "test"

    # Task 6: migrations/versions/0002_db_roles.py reads DB role passwords
    # from these env vars (no hardcoded fallback, by design — see that
    # file). Set here to fixed test-only values: this is a throwaway
    # testcontainer destroyed at session end, so there's no real secret to
    # protect, only a requirement to provide *something* non-empty.
    os.environ.setdefault("RECOVERYOS_APP_ROLE_PASSWORD", "test-only-app-role-password")
    os.environ.setdefault("RECOVERYOS_DIAGNOSER_ROLE_PASSWORD", "test-only-diagnoser-role-password")
    os.environ.setdefault("RECOVERYOS_INFERENCE_ROLE_PASSWORD", "test-only-inference-role-password")

    # Clear the cached settings singleton so it re-reads the env
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_project_root()),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Alembic migration failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    yield db_url_sync

    # Teardown: container stops, all data gone — nothing to clean.


@pytest.fixture(scope="session")
def sync_engine(migrated_db: str):
    """Synchronous SQLAlchemy engine (for direct SQL in tests)."""

    engine = create_engine(migrated_db, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_conn(sync_engine):
    """
    Per-test transactional connection.
    Every test gets a clean slate via ROLLBACK — no truncation needed.
    """
    with sync_engine.connect() as conn:
        conn.begin()
        yield conn
        conn.rollback()


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _project_root():
    """Resolve the project root (the directory containing alembic.ini)."""
    import pathlib

    here = pathlib.Path(__file__).parent
    # Walk up until we find alembic.ini
    for candidate in [here, here.parent, here.parent.parent]:
        if (candidate / "alembic.ini").exists():
            return candidate
    return here.parent
