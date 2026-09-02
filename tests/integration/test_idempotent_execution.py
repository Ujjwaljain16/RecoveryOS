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


# ─── Task R1: advisory_lock's rollback scope ───────────────────────────────
#
# The pre-fix advisory_lock() called conn.rollback() unconditionally in its
# finally block -- on the clean-success path too, not just after an
# exception. Today's one real caller (workers/execution_worker.py) is safe
# only because it always commits before returning from inside the locked
# block; nothing about advisory_lock() itself guaranteed that. These two
# tests are the negative control the audit identified as missing:
# test_idempotent_execution.py's own concurrency proof above uses a
# SEPARATE connection for save_result (see _save_result's own
# engine.begin()), which structurally never exercises "a write through the
# SAME locked connection, with no explicit commit before the block exits."


def test_successful_write_through_locked_connection_survives_without_explicit_commit(
    scratch_table,
):
    """
    The exact scenario the pre-fix design failed silently on: a caller that
    writes INSIDE advisory_lock()'s block but only commits AFTER releasing
    the lock (a legitimate pattern -- e.g. holding the lock only around the
    check-and-write, then committing once outside it). Against the old
    unconditional-rollback code, advisory_lock()'s own finally block would
    roll back the pending write the instant the `with` block exits --
    before the caller's own conn.commit() below ever runs -- so that commit
    silently commits nothing. Against the fix (rollback scoped to the
    exception path only), the write is still pending when control reaches
    the caller's commit, and it lands for real.

    Note: this deliberately does NOT commit inside the block -- a variant
    that commits before the block exits would pass under both the old and
    new code, since there'd be nothing left for advisory_lock's rollback to
    discard either way. The commit has to happen AFTER the block for this
    test to actually distinguish the two behaviors.
    """
    engine = scratch_table
    key = f"no-explicit-commit-{uuid.uuid4()}"

    conn = engine.connect()
    with advisory_lock(conn, key):
        conn.execute(
            text(
                "INSERT INTO idempotent_action_results (idempotency_key, result_value) "
                "VALUES (:k, :v)"
            ),
            {"k": key, "v": "written-without-explicit-commit"},
        )
        # Deliberately NOT calling conn.commit() inside the block -- the
        # caller here commits only after the lock is released, below.
    conn.commit()
    conn.close()

    with engine.connect() as check_conn:
        row = check_conn.execute(
            text("SELECT result_value FROM idempotent_action_results WHERE idempotency_key = :k"),
            {"k": key},
        ).first()

    assert row is not None, (
        "a write made through the locked connection, with no explicit commit before the "
        "`with advisory_lock(...)` block exited, was silently discarded -- advisory_lock()'s "
        "finally block must not unconditionally roll back the connection on the success path"
    )
    assert row[0] == "written-without-explicit-commit"


def test_exception_path_still_clears_failed_transaction_before_unlock(scratch_table):
    """
    Re-verify the guarantee the rollback was actually FOR, after narrowing
    its scope to the exception path: a real Postgres-level error (not just
    an application-level raise) leaves the transaction in a FAILED state
    that refuses any further command -- including the unlock call itself --
    until rolled back. Confirm the narrowed advisory_lock() still recovers
    from this and the unlock genuinely runs (a second session can still
    acquire the same key afterward).
    """
    engine = scratch_table
    key = f"failed-txn-recovery-{uuid.uuid4()}"

    conn = engine.connect()
    # Genuinely any DB error is fine here (noqa: B017 -- broad Exception is intentional)
    with pytest.raises(Exception), advisory_lock(conn, key):  # noqa: B017
        # A real SQL-level error (violates the scratch table's PRIMARY KEY
        # by inserting NULL into it) -- this is what actually left Postgres
        # in a FAILED transaction state pre-fix, not a bare
        # `raise ValueError(...)` which never touches the DB at all.
        conn.execute(
            text(
                "INSERT INTO idempotent_action_results (idempotency_key, result_value) VALUES (NULL, 'x')"
            )
        )
    conn.close()

    # If the unlock call never actually ran (masked by a FAILED transaction
    # blocking it), the lock would still be held and this second acquisition
    # would hang/timeout. Use a bounded wait so a real regression fails the
    # test instead of hanging the suite.
    acquired = threading.Event()

    def second_holder():
        with engine.connect() as conn2, advisory_lock(conn2, key):
            acquired.set()

    t = threading.Thread(target=second_holder, daemon=True)
    t.start()
    t.join(timeout=5)

    assert acquired.is_set(), (
        "a second session could not acquire the same advisory lock key after the first "
        "session's block raised -- the unlock call was likely masked by a FAILED transaction "
        "that the exception-path rollback should have cleared"
    )
    engine.dispose()


# ─── gaps.md §B.2's own named backstop test ─────────────────────────────────
#
# The advisory lock is the primary idempotency mechanism (proved above), but
# gaps.md §B.2 explicitly calls for a SECOND, independent proof: even if the
# lock logic itself has a bug and two callers both reach the INSERT, the real
# recoveries.idempotency_key UNIQUE constraint (migrations/0001) must reject
# the second one outright. test_schema_and_roles.py's own
# test_recoveries_idempotency_key_is_unique only confirms the constraint
# EXISTS (an information_schema lookup) -- it never actually attempts a
# duplicate INSERT, so it can't prove the constraint does what it's for. This
# is the test gaps.md named and neither of those covers: a real duplicate
# INSERT, lock deliberately bypassed, asserted to raise IntegrityError.


def test_db_unique_constraint_backstop_rejects_duplicate_insert_even_if_lock_logic_is_bypassed(
    db_url_sync: str,
):
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(db_url_sync, pool_pre_ping=True)
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    idempotency_key = f"recovery:{payment_id}:RETRY_NOW:1"

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, :name)"),
            {"mid": merchant_id, "name": f"test-merchant-{merchant_id[:8]}"},
        )
        conn.execute(
            text("INSERT INTO customers (customer_id, merchant_id) VALUES (:cid, :mid)"),
            {"cid": customer_id, "mid": merchant_id},
        )
        conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 100000, 'upi', 'HDFC', 'failed', 'TIMEOUT', true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id},
        )
        conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:pcid)"),
            {"pcid": policy_config_id},
        )
        conn.execute(
            text(
                "INSERT INTO candidate_actions (candidate_id, payment_id, action_type, "
                "recovery_prob_bps, expected_value_paise, cost_paise, friction_penalty_paise, "
                "risk_penalty_paise, model_version, created_at) "
                "VALUES (:cid, :pid, 'RETRY_NOW', 8000, 80000, 0, 0, 0, 'test-v1', now())"
            ),
            {"cid": candidate_id, "pid": payment_id},
        )
        conn.execute(
            text(
                "INSERT INTO policy_decisions (decision_id, payment_id, candidate_id, "
                "policy_config_id, verdict, rule_trace, created_at) "
                "VALUES (:did, :pid, :cid, :pcid, 'ALLOW', '[]'::jsonb, now())"
            ),
            {"did": decision_id, "pid": payment_id, "cid": candidate_id, "pcid": policy_config_id},
        )
        # The FIRST insert -- succeeds, exactly like a real execute_with_idempotency
        # call's action_fn would produce.
        conn.execute(
            text(
                "INSERT INTO recoveries (recovery_id, payment_id, decision_id, idempotency_key, "
                "attempt_number, action_type, scheduled_for, executed_at, outcome, "
                "recovered_amount_paise, created_at) "
                "VALUES (:rid, :pid, :did, :ik, 1, 'RETRY_NOW', now(), now(), 'FAILED', 0, now())"
            ),
            {"rid": str(uuid.uuid4()), "pid": payment_id, "did": decision_id, "ik": idempotency_key},
        )

    # The SECOND insert -- deliberately bypasses execute_with_idempotency's
    # own lock-then-check entirely (raw INSERT, no advisory_lock, no
    # get_existing check) to prove the schema-level backstop holds even when
    # application-level idempotency logic is skipped outright, not just
    # buggy. A different recovery_id (a real duplicate INSERT has its own
    # PK), same idempotency_key.
    with pytest.raises(IntegrityError, match="idempotency_key"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO recoveries (recovery_id, payment_id, decision_id, idempotency_key, "
                    "attempt_number, action_type, scheduled_for, executed_at, outcome, "
                    "recovered_amount_paise, created_at) "
                    "VALUES (:rid, :pid, :did, :ik, 1, 'RETRY_NOW', now(), now(), 'FAILED', 0, now())"
                ),
                {
                    "rid": str(uuid.uuid4()),
                    "pid": payment_id,
                    "did": decision_id,
                    "ik": idempotency_key,
                },
            )

    with engine.connect() as conn:
        row_count = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE idempotency_key = :ik"),
            {"ik": idempotency_key},
        ).scalar_one()
    assert row_count == 1, (
        "the rejected duplicate INSERT must not have left a second row behind -- "
        f"found {row_count} rows for the same idempotency_key"
    )
    engine.dispose()
