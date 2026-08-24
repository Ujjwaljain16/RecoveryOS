"""
Recovery task — execute_recovery
===================================
The single Celery task that performs recovery actions.

Idempotency guarantee (TRD §4.3, gaps.md §B.2):
  1. Acquire Postgres advisory lock FIRST (lock-before-check, not check-then-lock).
  2. Check if idempotency_key already has a completed outcome.
  3. If yes: return cached result — no re-execution.
  4. If no: call ProviderAdapter → upsert result.

The UNIQUE constraint on recoveries.idempotency_key is the physical DB-level backstop.
Even if two workers race past the advisory lock, only one INSERT can succeed.

The lock-before-check pattern itself is implemented and proven correct under
genuine concurrency (threading.Barrier, real Postgres, a call-counting spy
standing in for the provider call) in
services/execution_engine/idempotency.py:execute_with_idempotency +
tests/integration/test_idempotent_execution.py — this task does NOT yet call
it, because that requires ProviderAdapter and the recovery-decision logic
(candidate generation, policy evaluation) that don't exist yet. Wiring it in
once those exist is:

    from services.execution_engine.idempotency import execute_with_idempotency
    result = execute_with_idempotency(
        conn, idempotency_key,
        action_fn=lambda: provider_adapter.retry(job["payment_id"]),
        get_existing=lambda k: db.get_recovery(k),
        save_result=lambda k, r: db.upsert_recovery(k, r),
    )
"""

from __future__ import annotations

import logging
from typing import Any

from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="workers.tasks.execute_recovery",
    bind=True,
    max_retries=3,
    default_retry_delay=30,  # seconds between worker-level retries
    acks_late=True,
)
def execute_recovery(self, job: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a scheduled recovery action.

    job schema:
      {
        "recovery_id": str,
        "payment_id": str,
        "idempotency_key": str,
        "action_type": str,
        "attempt_number": int,
        "adapter": "simulator" | "razorpay_test"
      }

    Phase 7 implementation: wire this to a real ProviderAdapter + recovery
    decision. The idempotency wrapper itself is not scaffolding — it's real,
    tested code — see services.execution_engine.idempotency.execute_with_idempotency
    and the module docstring above for the exact call shape this task will
    use once ProviderAdapter and the recovery-decision path exist.
    Scaffold: log and return pending.
    """
    idempotency_key = job.get("idempotency_key", "unknown")
    logger.info(
        "execute_recovery called",
        extra={"idempotency_key": idempotency_key, "attempt": job.get("attempt_number")},
    )

    return {"idempotency_key": idempotency_key, "outcome": "PENDING", "scaffold": True}
