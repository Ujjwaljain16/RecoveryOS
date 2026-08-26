"""
Execution Worker — Redis Streams consumer for recovery jobs, TRD §4.2/§4.3.

Mirrors services/event_processor/consumer.py's consumer-group pattern
deliberately, rather than inventing a second, subtly different resilience
mechanism: XREADGROUP + XAUTOCLAIM gives the same "a crashed consumer's
in-flight message gets reclaimed and reprocessed" guarantee this phase's
crash-recovery test proves.

Unlike event_processor, this worker is entirely SYNCHRONOUS (sync redis +
sync Postgres): services.execution_engine.idempotency.execute_with_idempotency
and recoveryos.database.advisory_lock both require holding ONE Connection
across the whole check-then-act-then-save sequence (see their docstrings)
— mixing that with an async DB session would mean threading an event loop
through a plain context manager for no real benefit. This worker's own
`while True: xreadgroup(...)` main loop already processes one job at a time
by construction (single-threaded, no concurrent task dispatch) — there is
no throughput reason for this path to be async.

Recovery workflow states (TRD §4.2), each a real `events` row:
    SCHEDULED -> EXECUTING -> VERIFYING -> (SUCCEEDED | FAILED)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import redis as sync_redis
from sqlalchemy import text
from sqlalchemy.engine import Connection

from integrations.razorpay.adapter import ProviderResult, get_provider_adapter
from recoveryos.database import get_sync_engine
from services.execution_engine.idempotency import execute_with_idempotency

logger = logging.getLogger(__name__)

STREAM_NAME = "stream:recovery_jobs"
GROUP_NAME = "cg_execution_worker"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"
BATCH_SIZE = 1  # one job at a time — matches this system's existing policy
BLOCK_MS = 1000
PENDING_RECLAIM_IDLE_MS = 5000

# Test-only fault-injection hooks — two distinct crash windows, each
# proving a different half of the idempotency guarantee:
#   INJECT_DELAY_MS_ENV: sleep AFTER the advisory lock but BEFORE calling
#     the provider. Killing here proves a crash before any money-moving
#     call leaves no dangling state that blocks a legitimate retry.
#   INJECT_DELAY_BEFORE_ACK_MS_ENV: sleep AFTER the job fully completes
#     (provider called, recovery row persisted) but BEFORE the stream
#     message is XACK'd. Killing here forces Redis to redeliver the SAME
#     message to the restarted worker — proving the harder, more important
#     case: the provider is NOT called a second time for an already-
#     completed job, even under genuine at-least-once redelivery.
INJECT_DELAY_MS_ENV = "RECOVERYOS_EXECUTION_WORKER_INJECT_DELAY_MS"
INJECT_DELAY_BEFORE_ACK_MS_ENV = "RECOVERYOS_EXECUTION_WORKER_INJECT_DELAY_BEFORE_ACK_MS"


def _now() -> datetime:
    return datetime.now(UTC)


def _emit_event(
    conn: Connection, payment_id: str, event_type: str, transition_key: str, payload: dict
) -> None:
    """
    Every state transition (TRD §4.2) is a logged events row. idempotency_key
    is deterministic (job's idempotency_key + event_type) — re-processing the
    same job after a crash re-emits the SAME key for a transition already
    logged, which the UNIQUE(payment_id, idempotency_key) constraint (used
    with ON CONFLICT DO NOTHING) silently dedupes rather than double-logging.
    """
    conn.execute(
        text(
            "INSERT INTO events (event_id, payment_id, idempotency_key, event_type, payload, occurred_at) "
            "VALUES (:event_id, :payment_id, :idempotency_key, :event_type, :payload, :occurred_at) "
            "ON CONFLICT (payment_id, idempotency_key) DO NOTHING"
        ),
        {
            "event_id": str(uuid.uuid4()),
            "payment_id": payment_id,
            "idempotency_key": transition_key,
            "event_type": event_type,
            "payload": json.dumps(payload),
            "occurred_at": _now(),
        },
    )


def _get_existing_recovery(conn: Connection, idempotency_key: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT outcome, recovered_amount_paise, provider_ref "
                "FROM recoveries WHERE idempotency_key = :key"
            ),
            {"key": idempotency_key},
        )
        .mappings()
        .first()
    )
    if row is None or row["outcome"] is None:
        return None
    # Same shape as _upsert_recovery's return value — a caller (or a test
    # asserting two racing callers got the SAME result) shouldn't have to
    # care whether it came from a fresh execution or a cached lookup.
    return {
        "outcome": row["outcome"],
        "recovered_amount_paise": row["recovered_amount_paise"],
        "provider_ref": row["provider_ref"],
    }


def _upsert_recovery(
    conn: Connection,
    *,
    payment_id: str,
    decision_id: str,
    idempotency_key: str,
    attempt_number: int,
    action_type: str,
    result: ProviderResult,
) -> dict[str, Any]:
    """
    Persist the outcome. ON CONFLICT (idempotency_key) DO UPDATE so a
    reclaimed/retried job that reaches here again (e.g. it crashed AFTER
    this upsert but BEFORE XACK) converges to the same final state rather
    than erroring — the UNIQUE constraint (gaps.md §B.2's physical
    backstop) still guarantees only ONE row, ever, per idempotency_key.
    """
    conn.execute(
        text(
            """
            INSERT INTO recoveries
                (recovery_id, payment_id, decision_id, idempotency_key, attempt_number,
                 action_type, scheduled_for, executed_at, outcome, recovered_amount_paise, provider_ref)
            VALUES
                (:recovery_id, :payment_id, :decision_id, :idempotency_key, :attempt_number,
                 :action_type, :scheduled_for, :executed_at, :outcome, :recovered_amount_paise, :provider_ref)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                executed_at = EXCLUDED.executed_at,
                outcome = EXCLUDED.outcome,
                recovered_amount_paise = EXCLUDED.recovered_amount_paise,
                provider_ref = EXCLUDED.provider_ref
            """
        ),
        {
            "recovery_id": str(uuid.uuid4()),
            "payment_id": payment_id,
            "decision_id": decision_id,
            "idempotency_key": idempotency_key,
            "attempt_number": attempt_number,
            "action_type": action_type,
            "scheduled_for": _now(),
            "executed_at": _now(),
            "outcome": result.outcome,
            "recovered_amount_paise": result.recovered_amount_paise,
            "provider_ref": result.provider_ref,
        },
    )
    return {
        "outcome": result.outcome,
        "recovered_amount_paise": result.recovered_amount_paise,
        "provider_ref": result.provider_ref,
    }


def _write_ledger_and_audit(
    conn: Connection, payment_id: str, decision_id: str, action_type: str, result: ProviderResult
) -> None:
    from services.pipeline.ledger import populate_ledger_and_audit_sync

    candidate_row = (
        conn.execute(
            text(
                "SELECT candidate_id, cost_paise, recovery_prob_bps FROM candidate_actions "
                "WHERE payment_id = :pid AND action_type = :action ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id, "action": action_type},
        )
        .mappings()
        .first()
    )

    recovery_row = conn.execute(
        text(
            "SELECT recovery_id FROM recoveries WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
        ),
        {"pid": payment_id},
    ).first()

    diagnosis_row = conn.execute(
        text(
            "SELECT diagnosis_id FROM diagnoses WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
        ),
        {"pid": payment_id},
    ).first()

    populate_ledger_and_audit_sync(
        conn,
        payment_id=payment_id,
        candidate_id=candidate_row["candidate_id"] if candidate_row else None,
        decision_id=decision_id,
        verdict="ALLOW",
        chosen_action=action_type,
        recovery_prob_bps=candidate_row["recovery_prob_bps"] if candidate_row else 0,
        cost_paise=candidate_row["cost_paise"] if candidate_row else 0,
        actual_recovery_paise=result.recovered_amount_paise,
        recovery_id=recovery_row[0] if recovery_row else None,
        diagnosis_id=diagnosis_row[0] if diagnosis_row else None,
        outcome=result.outcome,
    )


def process_job(conn: Connection, job: dict[str, Any], provider=None) -> dict[str, Any]:
    """
    One recovery job, fully processed through the TRD §4.2 state machine,
    wrapped in the exact lock-before-check idempotency pattern (gaps.md
    §B.2, services/execution_engine/idempotency.py:execute_with_idempotency).

    `provider` is injectable for tests (a call-counting spy standing in for
    a real PaymentProvider); production callers omit it and get
    get_provider_adapter()'s config-selected implementation.
    """
    payment_id = job["payment_id"]
    idempotency_key = job["idempotency_key"]
    action_type = job["action_type"]
    attempt_number = int(job["attempt_number"])
    decision_id = job["decision_id"]
    amount_paise = int(job["amount_paise"])

    provider = provider or get_provider_adapter()

    _emit_event(
        conn,
        payment_id,
        "RECOVERY_SCHEDULED",
        f"{idempotency_key}:SCHEDULED",
        {"action_type": action_type, "attempt_number": attempt_number},
    )
    conn.commit()

    def action_fn() -> dict[str, Any]:
        _emit_event(
            conn,
            payment_id,
            "RECOVERY_EXECUTING",
            f"{idempotency_key}:EXECUTING",
            {"action_type": action_type, "attempt_number": attempt_number},
        )
        conn.commit()

        inject_delay_ms = os.environ.get(INJECT_DELAY_MS_ENV)
        if inject_delay_ms:
            logger.info("[ExecutionWorker] test fault-injection sleep: %sms", inject_delay_ms)
            time.sleep(int(inject_delay_ms) / 1000.0)

        result = provider.retry(conn, payment_id, amount_paise, attempt_number)

        _emit_event(
            conn,
            payment_id,
            "RECOVERY_VERIFYING",
            f"{idempotency_key}:VERIFYING",
            {"outcome": result.outcome},
        )
        conn.commit()

        saved = _upsert_recovery(
            conn,
            payment_id=payment_id,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            attempt_number=attempt_number,
            action_type=action_type,
            result=result,
        )

        final_event_type = (
            "RECOVERY_SUCCEEDED"
            if result.outcome == "SUCCESS"
            else ("RECOVERY_FAILED" if result.outcome == "FAILED" else "RECOVERY_PENDING")
        )
        _emit_event(
            conn,
            payment_id,
            final_event_type,
            f"{idempotency_key}:{final_event_type}",
            {"outcome": result.outcome, "recovered_amount_paise": result.recovered_amount_paise},
        )
        conn.commit()

        if result.outcome in ("SUCCESS", "FAILED"):
            # A real terminal outcome -- this consumer (not
            # services/pipeline/consumer.py, which only handles the
            # no-execution BLOCK/DO_NOTHING case) is the one place that
            # writes recovery_ledger + audit_log for an executed job.
            _write_ledger_and_audit(conn, payment_id, decision_id, action_type, result)

        return saved

    return execute_with_idempotency(
        conn,
        idempotency_key,
        action_fn=action_fn,
        get_existing=lambda key: _get_existing_recovery(conn, key),
        save_result=lambda key, result: None,  # action_fn already persisted via _upsert_recovery
    )


def _process_message(engine, stream_msg_id: str, raw_msg: dict[str, str], provider=None) -> bool:
    job = {
        "payment_id": raw_msg["payment_id"],
        "idempotency_key": raw_msg["idempotency_key"],
        "action_type": raw_msg["action_type"],
        "attempt_number": raw_msg["attempt_number"],
        "decision_id": raw_msg["decision_id"],
        "amount_paise": raw_msg["amount_paise"],
    }
    try:
        with engine.connect() as conn:
            process_job(conn, job, provider=provider)

        inject_before_ack_ms = os.environ.get(INJECT_DELAY_BEFORE_ACK_MS_ENV)
        if inject_before_ack_ms:
            logger.info(
                "[ExecutionWorker] test fault-injection sleep before XACK: %sms",
                inject_before_ack_ms,
            )
            time.sleep(int(inject_before_ack_ms) / 1000.0)

        return True
    except Exception:
        logger.exception("[ExecutionWorker] job failed, leaving pending: %s", stream_msg_id)
        return False


def _ensure_consumer_group(redis_client: sync_redis.Redis) -> None:
    try:
        redis_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except sync_redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def _reclaim_pending(redis_client: sync_redis.Redis, engine, provider=None) -> None:
    """On startup: reclaim messages a previous (possibly crashed) consumer
    instance never XACK'd — same XAUTOCLAIM pattern as event_processor."""
    while True:
        next_id, messages, _ = redis_client.xautoclaim(
            STREAM_NAME,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=PENDING_RECLAIM_IDLE_MS,
            start_id="0-0",
            count=BATCH_SIZE,
        )
        if not messages:
            break
        logger.info(
            "[ExecutionWorker] reclaiming %d pending message(s) from a prior crashed consumer",
            len(messages),
        )
        for stream_msg_id, raw_msg in messages:
            ok = _process_message(engine, stream_msg_id, raw_msg, provider=provider)
            if ok:
                redis_client.xack(STREAM_NAME, GROUP_NAME, stream_msg_id)
        if next_id == "0-0":
            break


def run_worker(
    redis_client: sync_redis.Redis, *, max_iterations: int | None = None, provider=None
) -> None:
    """
    Main consumer loop. Runs indefinitely unless max_iterations is given
    (test-only — bounds how many poll cycles to run before returning).
    """
    engine = get_sync_engine()
    _ensure_consumer_group(redis_client)
    _reclaim_pending(redis_client, engine, provider=provider)

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        results = redis_client.xreadgroup(
            groupname=GROUP_NAME,
            consumername=CONSUMER_NAME,
            streams={STREAM_NAME: ">"},
            count=BATCH_SIZE,
            block=BLOCK_MS,
        )
        if not results:
            continue
        for _stream_name, messages in results:
            for stream_msg_id, raw_msg in messages:
                ok = _process_message(engine, stream_msg_id, raw_msg, provider=provider)
                if ok:
                    redis_client.xack(STREAM_NAME, GROUP_NAME, stream_msg_id)


def main() -> None:
    from recoveryos.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    redis_client = sync_redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        run_worker(redis_client)
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
