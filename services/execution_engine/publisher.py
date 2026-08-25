"""
Recovery job publisher — pushes an ALLOW'd policy decision onto
stream:recovery_jobs for workers/execution_worker.py to pick up.
Mirrors services/event_processor/publisher.py's XADD conventions.
"""

from __future__ import annotations

import redis as sync_redis

STREAM_NAME = "stream:recovery_jobs"
STREAM_MAXLEN = 50_000


def enqueue_recovery_job(
    redis_client: sync_redis.Redis,
    *,
    payment_id: str,
    decision_id: str,
    idempotency_key: str,
    action_type: str,
    attempt_number: int,
    amount_paise: int,
) -> str:
    """Publish one recovery job. Returns the Redis stream message ID."""
    msg = {
        "payment_id": payment_id,
        "decision_id": decision_id,
        "idempotency_key": idempotency_key,
        "action_type": action_type,
        "attempt_number": str(attempt_number),
        "amount_paise": str(amount_paise),
    }
    return redis_client.xadd(STREAM_NAME, msg, maxlen=STREAM_MAXLEN, approximate=True)
