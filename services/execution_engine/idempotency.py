"""
Execution Engine — Idempotent Execution Wrapper
================================================
This is the ONE thing this task builds today: the lock-before-check pattern
that's supposed to guarantee a financial action (e.g. "retry this payment
via the provider") executes at most once per idempotency_key, even under
genuinely concurrent callers (two worker processes, a retried Redis message,
a duplicated Postgres advisory-lock consumer, etc).

It does NOT build execution logic (deciding what to retry, calling
Razorpay/the SimulatorAdapter, computing recovery outcomes) — that's a
later phase. `action_fn` here is deliberately a pluggable callable so this
can be proven correct against a stub today and wired to a real provider
call later without touching this file.

Historical context (gaps.md §B.2 + the pre-fix workers/tasks.py docstring):
this exact pattern was described in three separate docstrings/comments
across the repo and implemented in NONE of them — the described code was a
comment block, never executed. This module is that comment turned into
real, tested code.

Pattern (lock FIRST, not after the check — the ordering is the entire
point; see the TOCTOU explanation in recoveryos/database.py:advisory_lock):

    with advisory_lock(conn, idempotency_key):
        existing = get_existing(idempotency_key)
        if existing is not None:
            return existing
        result = action_fn()
        save_result(idempotency_key, result)
        return result
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.engine import Connection

from recoveryos.database import advisory_lock


def execute_with_idempotency(
    conn: Connection,
    idempotency_key: str,
    action_fn: Callable[[], Any],
    get_existing: Callable[[str], Any | None],
    save_result: Callable[[str, Any], None],
) -> Any:
    """
    Run `action_fn` at most once for a given `idempotency_key`, even under
    concurrent callers, by holding a Postgres advisory lock across the
    entire check-then-act-then-save sequence.

    Args:
        conn: a single checked-out Connection held for the whole call —
            see advisory_lock's docstring for why this can't be a fresh
            connection per statement.
        idempotency_key: the key identifying "this exact logical action" —
            e.g. f"recovery:{payment_id}:{action_type}:{attempt_number}"
            per the TRD's `recoveries.idempotency_key` format.
        action_fn: the side-effecting operation to run at most once
            (a provider call today; a stub/spy in tests).
        get_existing: looks up a prior result for this key, or None.
        save_result: persists the result of a fresh `action_fn()` call.

    Returns:
        The existing result if one was already recorded for this key,
        otherwise the freshly computed result of `action_fn()`.
    """
    with advisory_lock(conn, idempotency_key):
        existing = get_existing(idempotency_key)
        if existing is not None:
            return existing
        result = action_fn()
        save_result(idempotency_key, result)
        return result
