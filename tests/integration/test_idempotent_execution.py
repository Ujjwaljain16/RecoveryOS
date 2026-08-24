"""
Concurrency proof for the lock-before-check idempotent-execution pattern
(services/execution_engine/idempotency.py:execute_with_idempotency).

This is the test named explicitly in gaps.md §B.2 and promised in three
docstrings across the repo (events.py, consumer.py, the old workers/tasks.py
comment block) and never delivered anywhere: a GENUINELY concurrent proof
that the same idempotency_key, hit by two real racing callers at the same
instant, results in the underlying side-effecting action running EXACTLY
ONCE — not a sequential "pretend concurrency" test (two awaits in a loop),
which cannot fail even if idempotency is completely broken.

Repo-wide grep for `threading.Barrier` before this file existed returned
exactly one hit — a load-pacing detail in tests/performance/test_ingest_throughput.py,
not a correctness assertion. This is the real one.

Requires real Postgres (testcontainers via tests/conftest.py). Uses plain
threading + a SYNCHRONOUS SQLAlchemy engine deliberately, not asyncio: the
whole point is to force two OS threads to hit `pg_advisory_lock` at the same
physical instant via threading.Barrier, which asyncio's single-threaded
event loop can't genuinely do (a single event loop can only *interleave*
coroutines cooperatively, never actually execute two at once — that's a
different, weaker claim than what real concurrent workers need to survive).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from recoveryos.database import advisory_lock
from services.execution_engine.idempotency import execute_with_idempotency


@pytest.fixture()
def scratch_table(db_url_sync: str):
    """
    A minimal, dedicated table for this test — NOT part of the app schema.
    execute_with_idempotency() is deliberately schema-agnostic (pluggable
    get_existing/save_result), so proving it doesn't require the full
    recoveries table and its policy_decisions/candidate_actions FK chain;
    it only requires SOME real, persistent, concurrency-safe backing store,
    which this is.
    """
    engine = create_engine(db_url_sync, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS idempotent_action_results"))
        conn.execute(
            text(
                "CREATE TABLE idempotent_action_results ("
                "  idempotency_key TEXT PRIMARY KEY,"
                "  result_value TEXT NOT NULL"
                ")"
            )
        )
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS idempotent_action_results"))
    engine.dispose()


def _get_existing(engine, key: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT result_value FROM idempotent_action_results WHERE idempotency_key = :k"),
            {"k": key},
        ).first()
    return row[0] if row else None


def _save_result(engine, key: str, value: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO idempotent_action_results (idempotency_key, result_value) "
                "VALUES (:k, :v) ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {"k": key, "v": value},
        )


def test_two_real_threads_racing_same_idempotency_key_execute_action_exactly_once(
    scratch_table,
):
    """
    Two OS threads, each with its OWN Postgres connection (advisory locks
    are session-scoped — sharing one connection would silently make this
    test meaningless, see advisory_lock's docstring), forced via
    threading.Barrier to call execute_with_idempotency with the SAME
    idempotency_key at the same instant. The action (a call-counting spy
    standing in for "call the payment provider") must fire exactly once;
    the loser of the race must receive the winner's persisted result, not
    run its own independent action.
    """
    engine = scratch_table
    idempotency_key = f"recovery:{uuid.uuid4()}:RETRY_NOW:1"

    call_count_lock = threading.Lock()
    call_log: list[float] = []

    def provider_call_spy() -> str:
        # A real call-counting spy, not a mock with a canned return — records
        # wall-clock time of each *actual* invocation so we can also show
        # (not just assert) that only one invocation ever happened.
        with call_count_lock:
            call_log.append(time.monotonic())
        time.sleep(0.3)  # simulate a slow provider call — widens the window
        # during which the second thread, if the lock is NOT actually
        # serializing them, would race in and also call this.
        return f"outcome-for-{idempotency_key}"

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def worker(thread_name: str) -> None:
        try:
            # Each thread opens its OWN connection — this is load-bearing,
            # not incidental: pg_advisory_lock only blocks OTHER sessions,
            # so if both threads shared one connection this test would pass
            # even with a completely broken lock.
            conn = engine.connect()
            try:
                barrier.wait()  # force both threads to hit the lock at the same instant
                result = execute_with_idempotency(
                    conn,
                    idempotency_key,
                    action_fn=provider_call_spy,
                    get_existing=lambda k: _get_existing(engine, k),
                    save_result=lambda k, v: _save_result(engine, k, v),
                )
                results[thread_name] = result
            finally:
                conn.close()
        except (
            BaseException
        ) as exc:  # noqa: BLE001 — surface any thread exception to the main thread
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("thread_a",))
    t2 = threading.Thread(target=worker, args=("thread_b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert not t1.is_alive() and not t2.is_alive(), "a worker thread did not complete in time"

    # ── The actual proof ─────────────────────────────────────────────────
    assert len(call_log) == 1, (
        f"provider_call_spy() must fire EXACTLY ONCE under real concurrent "
        f"racing callers sharing an idempotency_key — it fired {len(call_log)} times. "
        f"call timestamps: {call_log}"
    )

    # Both threads must have received the SAME result — the loser gets the
    # winner's persisted outcome, not an independently-computed one.
    assert results["thread_a"] == results["thread_b"] == f"outcome-for-{idempotency_key}"

    # And exactly one row landed in the backing store — no duplicate writes.
    with engine.connect() as conn:
        row_count = conn.execute(
            text("SELECT count(*) FROM idempotent_action_results WHERE idempotency_key = :k"),
            {"k": idempotency_key},
        ).scalar_one()
    assert row_count == 1


def test_advisory_lock_actually_blocks_a_second_session(db_url_sync: str):
    """
    Narrower, more mechanical proof than the test above: directly show that
    a SECOND connection attempting the SAME advisory lock key genuinely
    blocks until the first releases it — i.e. advisory_lock() isn't a no-op
    that happens to let both callers through fast enough to look correct.
    """
    engine = create_engine(db_url_sync, pool_pre_ping=True)
    key = f"lock-test-{uuid.uuid4()}"

    holder_acquired = threading.Event()
    release_holder = threading.Event()
    waiter_acquired_at: list[float] = []

    def holder():
        conn = engine.connect()
        with advisory_lock(conn, key):
            holder_acquired.set()
            release_holder.wait(timeout=10)
        conn.close()

    def waiter():
        holder_acquired.wait(timeout=10)
        start = time.monotonic()
        conn = engine.connect()
        with advisory_lock(conn, key):
            waiter_acquired_at.append(time.monotonic() - start)
        conn.close()

    t_holder = threading.Thread(target=holder)
    t_waiter = threading.Thread(target=waiter)
    t_holder.start()
    holder_acquired.wait(timeout=10)
    t_waiter.start()

    time.sleep(0.5)  # the waiter should still be blocked right now
    assert waiter_acquired_at == [], "waiter acquired the lock before the holder released it"

    release_holder.set()
    t_holder.join(timeout=10)
    t_waiter.join(timeout=10)

    assert waiter_acquired_at, "waiter never acquired the lock"
    assert waiter_acquired_at[0] >= 0.4, (
        f"waiter acquired the lock too quickly ({waiter_acquired_at[0]:.3f}s) — "
        f"expected it to block for ~0.5s until the holder released"
    )
    engine.dispose()
