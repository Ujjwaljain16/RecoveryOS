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

    Phase 7 implementation: full advisory lock + ProviderAdapter call.
    Scaffold: log and return pending.
    """
    idempotency_key = job.get("idempotency_key", "unknown")
    logger.info(
        "execute_recovery called",
        extra={"idempotency_key": idempotency_key, "attempt": job.get("attempt_number")},
    )

    # Phase 7 will implement:
    # with db.advisory_lock(idempotency_key):          # LOCK FIRST
    #     existing = db.get_recovery(idempotency_key)  # then check
    #     if existing and existing.outcome is not None:
    #         return existing.to_dict()
    #     result = provider_adapter.retry(job["payment_id"])
    #     db.upsert_recovery(idempotency_key, result)
    #     return result

    return {"idempotency_key": idempotency_key, "outcome": "PENDING", "scaffold": True}
