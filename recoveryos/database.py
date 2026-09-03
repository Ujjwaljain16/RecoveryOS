"""
RecoveryOS — Database Session Factory
======================================
Provides async SQLAlchemy engine + session factory.
Also exposes a synchronous engine for Alembic migrations.

Connection roles:
  - get_app_session()       → uses app_role (full R/W, except audit_log/events no DELETE/UPDATE)
  - get_diagnoser_session() → uses diagnoser_role (SELECT only, no ground_truth columns)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recoveryos.config import get_settings


def _build_async_engine(url: str):
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=False,
    )


def _build_sync_engine(url: str):
    return create_engine(url, pool_pre_ping=True)


# ─── Application engine (app_role) ───────────────────────────────────────────
_app_engine = None
_app_session_factory = None

# ─── Diagnoser engine (diagnoser_role — read-only, no ground_truth) ──────────
_diagnoser_engine = None
_diagnoser_session_factory = None

# ─── Inference engine (inference_role — read-only, no ground_truth) ─────────
_inference_engine = None
_inference_session_factory = None


def get_app_engine():
    global _app_engine
    if _app_engine is None:
        _app_engine = _build_async_engine(get_settings().database_url)
    return _app_engine


def get_diagnoser_engine():
    global _diagnoser_engine
    if _diagnoser_engine is None:
        _diagnoser_engine = _build_async_engine(get_settings().diagnoser_database_url)
    return _diagnoser_engine


def get_inference_engine():
    global _inference_engine
    if _inference_engine is None:
        _inference_engine = _build_async_engine(get_settings().inference_database_url)
    return _inference_engine


def get_app_session_factory() -> async_sessionmaker[AsyncSession]:
    global _app_session_factory
    if _app_session_factory is None:
        _app_session_factory = async_sessionmaker(
            bind=get_app_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _app_session_factory


def get_diagnoser_session_factory() -> async_sessionmaker[AsyncSession]:
    global _diagnoser_session_factory
    if _diagnoser_session_factory is None:
        _diagnoser_session_factory = async_sessionmaker(
            bind=get_diagnoser_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _diagnoser_session_factory


def get_inference_session_factory() -> async_sessionmaker[AsyncSession]:
    global _inference_session_factory
    if _inference_session_factory is None:
        _inference_session_factory = async_sessionmaker(
            bind=get_inference_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _inference_session_factory


async def get_app_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an app_role session."""
    async with get_app_session_factory()() as session:
        yield session


async def get_diagnoser_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a diagnoser_role session (read-only, no ground_truth)."""
    async with get_diagnoser_session_factory()() as session:
        yield session


_sync_engine = None


def get_sync_engine():
    """Returns a synchronous engine for CLI utilities, data migrations, and batch loaders."""
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = _build_sync_engine(get_settings().database_url_sync)
    return _sync_engine


def _advisory_lock_key(key: str) -> int:
    """
    Deterministically map an arbitrary string idempotency key to the signed
    64-bit integer pg_advisory_lock(bigint) requires.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    value = int.from_bytes(digest, byteorder="big", signed=False)
    return value - (1 << 64) if value >= (1 << 63) else value


@contextlib.contextmanager
def advisory_lock(conn: Connection, key: str) -> Generator[None, None, None]:
    """
    Postgres session-level advisory lock, keyed by an arbitrary string.

    MUST be acquired BEFORE the existence check in any idempotent
    check-then-act flow (gaps.md §B.2) — locking AFTER the check is the
    textbook TOCTOU race this exists to close: two callers can both pass a
    "does this already exist?" check before either has written a result,
    then both perform the side effect. The caller is responsible for
    structuring their code so the check happens inside this context, not
    around it — see services/execution_engine/idempotency.py:execute_with_idempotency
    for the reference usage.

    `conn` must be a single checked-out connection/transaction held for the
    ENTIRE duration of the lock (through the existence check, the action,
    and the result write) — pg_advisory_lock is session-scoped, so pulling a
    fresh connection per statement (e.g. a new `engine.connect()` for the
    check and another for the write) would silently make each acquisition a
    no-op against a different session and defeat the whole guarantee.

    Blocks (does not fail) if another session already holds the same key —
    this is the intended behavior: a concurrent caller waits its turn rather
    than racing.

    Robustness note: if the wrapped code raises after leaving the
    connection's transaction in a FAILED state (e.g. an IntegrityError from
    a bad write), Postgres refuses to run ANY further command — including
    the unlock call itself — until the transaction is rolled back. The
    rollback below is scoped to exactly that path (Task R1, pre-Phase-8
    audit) — it used to run unconditionally, including on the clean-success
    path, which would silently discard any write made through this
    connection that wasn't followed by an explicit commit before the `with`
    block exited. Today's caller (workers/execution_worker.py) always
    commits before returning, so it was never actually hit, but nothing
    about this function guaranteed that — the next caller that didn't would
    have lost a write with no error raised anywhere. Scoping the rollback to
    the exception path preserves the FAILED-transaction recovery this exists
    for, without imposing it on callers who never hit that state.
    """
    lock_key = _advisory_lock_key(key)
    conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
    try:
        yield
    except BaseException:
        # BaseException, not Exception -- pg_advisory_lock is SESSION-scoped
        # (a COMMIT/ROLLBACK does NOT release it; only an explicit unlock,
        # or the connection closing, does), so ANY unhandled path out of
        # the wrapped block that leaves the transaction FAILED must still
        # be rolled back before the finally block's own unlock statement
        # below can run at all (Postgres refuses every command, the unlock
        # included, until a failed transaction is rolled back) -- narrowing
        # this to `Exception` would silently skip that rollback for
        # KeyboardInterrupt/SystemExit, leaking this lock on this pooled
        # connection for as long as the connection itself stays alive
        # (found live: a leaked lock this way permanently deadlocked every
        # later caller of the SAME key, with zero exception ever logged).
        conn.rollback()
        raise
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})


@contextlib.asynccontextmanager
async def advisory_lock_async(session: AsyncSession, key: str) -> AsyncGenerator[None, None]:
    """
    Async counterpart to `advisory_lock` above, for AsyncSession-based
    callers (services/pipeline/reconciliation.py — Domain Audit finding
    #5: its check-then-act on `outcome = 'PENDING'` had no lock at all,
    unlike execution_worker.py's execute_with_idempotency, which
    deliberately closes this exact TOCTOU race). Same lock-BEFORE-check
    ordering requirement, same session-scoped-lock caveat (the `session`
    passed in must be held for the entire check-then-act-then-write
    sequence, not a fresh session per statement) — see the sync version's
    docstring for the full reasoning, not duplicated here.

    Three failure modes the sync version doesn't have to worry about, all
    found live (a genuine, permanent deadlock: every later caller for the
    same key blocked forever on SELECT pg_advisory_lock, zero exception
    ever logged anywhere -- the connection that actually held the lock
    just sat in the pool "idle", never touched again):
      - The big one: `session` is an ORM AsyncSession, not a raw Core
        Connection -- and AsyncSession.commit() (unlike Connection.commit())
        releases its DBAPI connection back to the pool, checking out a
        (possibly DIFFERENT) one for the session's next statement. Callers
        that commit `session` WHILE still inside this lock -- deliberately,
        to make their whole check-then-act-then-write sequence atomic
        (services/pipeline/reconciliation.py's reconcile_pending_recovery,
        services/risk_engine/anomaly.py's persist_anomaly_window) -- would
        silently sever the SESSION-scoped lock from whatever connection
        later serves this function's own unlock call: confirmed live,
        pg_advisory_unlock running on a DIFFERENT backend pid than the one
        that actually acquired the lock, returning false ("not held by
        this session") with no error, while the true holder went back to
        the pool still holding it. Fixed by never touching `session`'s own
        connection for the lock/unlock calls at all -- a genuinely separate
        connection, held for exactly this function's lifetime, immune to
        whatever `session` does with its own transaction.

        That separate connection deliberately comes from get_sync_engine()
        (run off-loop via asyncio.to_thread), not from wrapping `session`'s
        own async engine in a second AsyncEngine(...) -- an earlier version
        of this fix did exactly that and it's individually correct, but
        broke this codebase's OWN documented test-isolation contract:
        tests/integration/conftest.py's patch_settings fixture rebuilds
        recoveryos.database's async engine singletons fresh every test
        specifically because "an asyncpg engine/pool created under a
        previous test's (now-closed) loop breaks the next test with opaque
        errors deep in asyncpg/proactor internals" (that comment predates
        this fix). A second AsyncEngine wrapper constructed per call, even
        around a same-test engine, tripped that exact failure mode two
        tests later. get_sync_engine()'s psycopg2 connections have no
        event-loop affinity at all, sidestepping the whole class of risk --
        and a session-scoped lock doesn't care which role/engine acquired
        it; Postgres advisory locks serialize across every connection to
        the cluster regardless of which one holds them.
      - `except Exception` doesn't catch `asyncio.CancelledError` (a
        BaseException since Python 3.8) -- a task cancelled while the
        wrapped block is mid-write (e.g. a server shutdown, or a caller
        this session's own demo endpoints spawn via BackgroundTasks that
        outlives the request that scheduled it) would skip the rollback
        entirely.
      - The finally block's own `await` is itself cancellable -- a
        cancellation landing there (not inside the wrapped block) would
        skip the unlock outright. asyncio.shield gives that specific
        statement immunity to the cancellation that's actively unwinding
        this frame; suppress keeps a genuinely broken connection (rather
        than a live one just needing the unlock) from masking whatever
        exception is already propagating.
    """
    lock_key = _advisory_lock_key(key)
    lock_conn = await asyncio.to_thread(get_sync_engine().connect)
    try:
        await asyncio.to_thread(
            lock_conn.execute, text("SELECT pg_advisory_lock(:key)"), {"key": lock_key}
        )
        try:
            yield
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.shield(session.rollback())
            raise
        finally:
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    asyncio.to_thread(
                        lock_conn.execute,
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": lock_key},
                    )
                )
    finally:
        await asyncio.to_thread(lock_conn.close)
