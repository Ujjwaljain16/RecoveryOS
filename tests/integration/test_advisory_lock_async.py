"""
recoveryos.database.advisory_lock_async -- regression test for a real,
live-found deadlock: a session-scoped pg_advisory_lock leaking forever when
the wrapped block is interrupted by something `except Exception` doesn't
catch (asyncio.CancelledError is a BaseException, not an Exception, since
Python 3.8) while the session's transaction is left ABORTED -- Postgres then
refuses every further command, including the unlock call itself, until a
rollback runs first; the old code only rolled back inside `except
Exception`, so a BaseException skipped straight to the finally block's
unlock attempt against an aborted transaction, which failed silently and
left the lock held for as long as the connection stayed alive.

Found live: triggering RecoveryOS's POST /v1/simulate/scenario demo
endpoint left a session-scoped lock held with the connection sitting
"idle" and zero exception ever logged anywhere -- every later caller for
the same key (services/pipeline/reconciliation.py's own
reconcile_pending_recovery, in production) then blocked on
`SELECT pg_advisory_lock(...)` forever. See recoveryos/database.py's own
updated docstring on advisory_lock_async for the full story and the fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from sqlalchemy import text

from recoveryos.database import advisory_lock_async, get_app_session_factory


@pytest.mark.asyncio
async def test_lock_releases_after_a_cancellederror_with_an_aborted_transaction(
    migrated_db,
):
    key = f"test-advisory-lock-{uuid.uuid4()}"
    session_factory = get_app_session_factory()

    async with session_factory() as session:
        with pytest.raises(asyncio.CancelledError):
            async with advisory_lock_async(session, key):
                # Break this session's transaction (an invalid statement
                # leaves it ABORTED -- Postgres then refuses every further
                # command, including an unlock, until a rollback runs)
                # exactly like a real failed write would, then simulate a
                # BaseException arriving before any rollback ever got a
                # chance to run -- the precise condition the old code
                # mishandled (`except Exception` never even sees a
                # CancelledError).
                with contextlib.suppress(Exception):
                    await session.execute(text("SELECT 1/0"))
                raise asyncio.CancelledError()

    # The lock must be released -- a fresh acquisition of the SAME key,
    # from a DIFFERENT session, must succeed promptly, not hang forever
    # the way the live bug did (nothing ever logged, connection just sat
    # there "idle" holding the lock).
    async with session_factory() as reacquire_session:

        async def _reacquire() -> None:
            async with advisory_lock_async(reacquire_session, key):
                pass

        await asyncio.wait_for(_reacquire(), timeout=5.0)


@pytest.mark.asyncio
async def test_lock_releases_after_the_wrapped_block_commits_the_session(migrated_db):
    """
    The OTHER real, live-found bug, more fundamental than the
    BaseException-handling one above: `session` is an ORM AsyncSession, not
    a raw Core Connection -- AsyncSession.commit() releases its DBAPI
    connection back to the pool and checks out a (possibly DIFFERENT) one
    for the session's next statement, unlike Connection.commit(). Two real
    callers commit `session` WHILE still holding this lock, deliberately,
    to make their whole check-then-act-then-write sequence atomic
    (services/pipeline/reconciliation.py's reconcile_pending_recovery,
    services/risk_engine/anomaly.py's persist_anomaly_window) -- reproduced
    live via RecoveryOS's own POST /v1/simulate/scenario demo endpoint
    hitting persist_anomaly_window twice in a row for the same bank/bucket:
    the first call's own unlock ran on a DIFFERENT backend pid than the one
    that actually acquired the lock (confirmed via pg_backend_pid() either
    side of the commit), returned false ("not held by this session") with
    no error, while the true holder went back into the pool still holding
    it -- the second call then blocked on that leaked lock forever.
    """
    key = f"test-advisory-lock-commit-{uuid.uuid4()}"
    session_factory = get_app_session_factory()

    async with session_factory() as session:
        async with advisory_lock_async(session, key):
            # The exact shape both real callers use: a real write, then a
            # commit, still INSIDE the lock.
            await session.execute(text("SELECT 1"))
            await session.commit()

    # A fresh acquisition of the SAME key, from a DIFFERENT session, must
    # succeed promptly -- not hang forever the way the live bug did.
    async with session_factory() as reacquire_session:

        async def _reacquire() -> None:
            async with advisory_lock_async(reacquire_session, key):
                pass

        await asyncio.wait_for(_reacquire(), timeout=5.0)


@pytest.mark.asyncio
async def test_lock_and_unlock_never_go_through_the_callers_session(migrated_db, monkeypatch):
    """
    White-box guard for the actual fix mechanism (a dedicated connection,
    never `session` itself) -- test_lock_releases_after_the_wrapped_block_
    commits_the_session above exercises the real, live-found symptom, but
    real connection-pool churn is inherently timing-dependent: in a quiet,
    low-concurrency test process the pool can just hand the SAME physical
    connection back after `session`'s commit() releases it, masking the
    bug the exact way it stayed hidden until this session's own live,
    concurrently-loaded demo run finally triggered genuine churn. This
    test instead asserts the mechanism directly and deterministically:
    pg_advisory_lock/pg_advisory_unlock must NEVER be issued through
    `session.execute` at all -- if they were, whatever `session` does with
    its own commits (both real callers commit mid-lock, deliberately)
    could sever the lock from whatever connection later serves the unlock,
    regardless of whether THIS run's pool happened to reproduce it.
    """
    key = f"test-advisory-lock-mechanism-{uuid.uuid4()}"
    session_factory = get_app_session_factory()

    async with session_factory() as session:
        original_execute = session.execute

        async def _spy_execute(clause, *args, **kwargs):
            sql = str(clause)
            assert "pg_advisory_lock" not in sql, "lock must not go through the caller's session"
            assert "pg_advisory_unlock" not in sql, "unlock must not go through the caller's session"
            return await original_execute(clause, *args, **kwargs)

        monkeypatch.setattr(session, "execute", _spy_execute)

        async with advisory_lock_async(session, key):
            await session.execute(text("SELECT 1"))
            await session.commit()


@pytest.mark.asyncio
async def test_lock_still_releases_on_a_plain_exception(migrated_db):
    """Same shape, but the ordinary (already-covered) path: a plain
    Exception with an aborted transaction still must not leak the lock --
    guards against a regression in the opposite direction while widening
    `except Exception` to `except BaseException`."""
    key = f"test-advisory-lock-{uuid.uuid4()}"
    session_factory = get_app_session_factory()

    async with session_factory() as session:
        with pytest.raises(ValueError):
            async with advisory_lock_async(session, key):
                with contextlib.suppress(Exception):
                    await session.execute(text("SELECT 1/0"))
                raise ValueError("simulated failure mid-transaction")

    async with session_factory() as reacquire_session:

        async def _reacquire() -> None:
            async with advisory_lock_async(reacquire_session, key):
                pass

        await asyncio.wait_for(_reacquire(), timeout=5.0)
