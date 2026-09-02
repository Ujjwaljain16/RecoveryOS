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
from datetime import UTC, datetime, timedelta
from typing import Any

import redis as sync_redis
from sqlalchemy import text
from sqlalchemy.engine import Connection

from integrations.razorpay.adapter import ProviderResult, get_provider_adapter
from recoveryos.database import get_sync_engine
from recoveryos.metrics import (
    recovery_attempts_total,
    recovery_success_total,
    stopping_rule_triggers_total,
    stream_backlog_depth,
)
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


STOPPING_RULE_MAX_RETRIES = "MAX_RETRIES"
STOPPING_RULE_STOP_AFTER_SUCCESS = "STOP_AFTER_SUCCESS"


def _compute_stopping_rule(
    conn: Connection, decision_id: str, attempt_number: int, outcome: str
) -> str | None:
    """
    Real stopping-rule evaluation against THIS merchant's actual
    policy_configs row (via policy_decisions.policy_config_id) -- not a
    placeholder. `recoveries.stopping_rule_triggered` (migrations/0001)
    existed as a column nothing ever populated before this; this is its
    first real writer. Checked in the same priority order
    services/policy_engine/rules.py's RetryLimitRule/stop_after_success
    intent implies: a genuine SUCCESS ends the retry sequence outright,
    otherwise having reached the configured max_retries this attempt does.
    """
    config_row = (
        conn.execute(
            text(
                "SELECT pc.max_retries, pc.stop_after_success FROM policy_configs pc "
                "JOIN policy_decisions pd ON pd.policy_config_id = pc.policy_config_id "
                "WHERE pd.decision_id = :decision_id"
            ),
            {"decision_id": decision_id},
        )
        .mappings()
        .first()
    )
    if config_row is None:
        return None
    if outcome == "SUCCESS" and config_row["stop_after_success"]:
        return STOPPING_RULE_STOP_AFTER_SUCCESS
    if attempt_number >= config_row["max_retries"]:
        return STOPPING_RULE_MAX_RETRIES
    return None


def _fetch_retry_cooldown_hours(conn: Connection, decision_id: str) -> int:
    """This merchant's actual retry_cooldown_hours, via the same
    policy_decisions -> policy_configs join _compute_stopping_rule uses.
    Phase 13's rescheduled re-evaluation waits this long before firing, so
    it finds a genuinely CooldownRule-eligible payment rather than
    immediately re-blocking on the same cooldown a fresh decision cycle
    would enforce anyway."""
    row = conn.execute(
        text(
            "SELECT pc.retry_cooldown_hours FROM policy_configs pc "
            "JOIN policy_decisions pd ON pd.policy_config_id = pc.policy_config_id "
            "WHERE pd.decision_id = :decision_id"
        ),
        {"decision_id": decision_id},
    ).first()
    return row[0] if row is not None else 12  # matches PolicyConfig.retry_cooldown_hours' own default


def _advance_mission_after_outcome(
    conn: Connection,
    *,
    mission_id: str,
    payment_id: str,
    decision_id: str,
    action_type: str,
    attempt_number: int,
    result: ProviderResult,
) -> None:
    """
    Phase 12/13 -- advances the mission from EXECUTING once a real outcome
    is known. RETRY_NOW/ALT_ROUTE are the only "retryable" (money-moving)
    actions in the Phase 13 closed-loop sense: a FAILED attempt with
    mission AND policy budget still remaining schedules a re-evaluation
    (services.recovery_engine.scheduling.schedule_reevaluation_sync) at
    now + the SAME retry_cooldown_hours CooldownRule would enforce anyway --
    this IS the gap this phase closes (see services/recovery_engine/
    mission.py's module docstring): before Phase 13, a FAILED immediate
    retry was a dead end unless some unrelated new event happened to arrive
    for this payment.

    REMINDER/ESCALATE always report outcome=SUCCESS (services/
    execution_engine/notification.py, human_handoff.py) but neither one
    recovered any money -- mapped to TERMINATED/ESCALATED respectively,
    never RECOVERED, which is reserved for a genuine RETRY_NOW/ALT_ROUTE
    SUCCESS.
    """
    from services.recovery_engine.mission import check_budget, transition_mission_sync
    from services.recovery_engine.scheduling import schedule_reevaluation_sync

    now = _now()
    is_money_moving = action_type in ("RETRY_NOW", "ALT_ROUTE")

    if is_money_moving and result.outcome == "SUCCESS":
        transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="OBSERVING_OUTCOME",
            event_type="RECOVERY_SUCCEEDED",
            actor="execution_worker",
            payload={"action_type": action_type, "recovered_amount_paise": result.recovered_amount_paise},
            increment_attempt=True,
            now=now,
        )
        transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="RECOVERED",
            event_type="MISSION_RECOVERED",
            actor="system",
            payload={"recovered_amount_paise": result.recovered_amount_paise},
            now=now,
        )
        return

    if is_money_moving and result.outcome == "FAILED":
        stopping_rule = _compute_stopping_rule(conn, decision_id, attempt_number, result.outcome)
        mission = transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="OBSERVING_OUTCOME",
            event_type="RECOVERY_FAILED",
            actor="execution_worker",
            payload={"action_type": action_type, "stopping_rule_triggered": stopping_rule},
            increment_attempt=True,
            now=now,
        )
        if stopping_rule is not None:
            # The deterministic policy's own retry limit already said stop --
            # the mission's own budget check below is moot; terminate now.
            transition_mission_sync(
                conn,
                mission_id=mission_id,
                to_state="TERMINATED",
                event_type="STOPPING_RULE_TRIGGERED",
                actor="policy_engine",
                payload={"stopping_rule": stopping_rule},
                now=now,
            )
            return

        budget = check_budget(
            current_round=0,  # round budget is enforced at investigation entry (services/pipeline/consumer.py), not here
            max_investigation_rounds=mission["max_investigation_rounds"],
            current_attempt=mission["current_attempt"],
            max_attempts=mission["max_attempts"],
            started_at=mission["started_at"],
            max_mission_duration_seconds=mission["max_mission_duration_seconds"],
            now=now,
        )
        if budget.exhausted:
            transition_mission_sync(
                conn,
                mission_id=mission_id,
                to_state="TERMINATED",
                event_type="MISSION_BUDGET_EXHAUSTED",
                actor="system",
                payload={"reason": budget.reason},
                now=now,
            )
            return

        cooldown_hours = _fetch_retry_cooldown_hours(conn, decision_id)
        schedule_reevaluation_sync(
            conn,
            payment_id=payment_id,
            decision_id=decision_id,
            diagnosis_id=None,
            source_event_id=None,
            scheduled_for=now + timedelta(hours=cooldown_hours),
            mission_id=mission_id,
        )
        # Mission stays in OBSERVING_OUTCOME -- workers/retry_scheduler.py
        # firing this re-evaluation later is what advances it to
        # INVESTIGATING again (Phase 13's shared closed-loop transition).
        return

    if action_type == "REMINDER":
        transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="OBSERVING_OUTCOME",
            event_type="REMINDER_SENT",
            actor="execution_worker",
            payload={},
            increment_attempt=True,
            now=now,
        )
        transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="TERMINATED",
            event_type="MISSION_TERMINATED",
            actor="system",
            payload={"reason": "reminder sent -- no further automated action scheduled"},
            now=now,
        )
        return

    if action_type == "ESCALATE":
        transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="OBSERVING_OUTCOME",
            event_type="HANDOFF_CREATED",
            actor="execution_worker",
            payload={},
            increment_attempt=True,
            now=now,
        )
        transition_mission_sync(
            conn,
            mission_id=mission_id,
            to_state="ESCALATED",
            event_type="MISSION_ESCALATED",
            actor="system",
            payload={"reason": "handoff created"},
            now=now,
        )
        return

    # A money-moving action's PENDING outcome (e.g. RazorpayTestAdapter's
    # "order created, not yet paid") -- not terminal yet; a webhook resolves
    # it later via services/pipeline/reconciliation.py, out of scope here.
    # Mission stays in OBSERVING_OUTCOME.
    transition_mission_sync(
        conn,
        mission_id=mission_id,
        to_state="OBSERVING_OUTCOME",
        event_type="OUTCOME_PENDING",
        actor="execution_worker",
        payload={"action_type": action_type, "outcome": result.outcome},
        increment_attempt=True,
        now=now,
    )


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
    stopping_rule = _compute_stopping_rule(conn, decision_id, attempt_number, result.outcome)
    conn.execute(
        text(
            """
            INSERT INTO recoveries
                (recovery_id, payment_id, decision_id, idempotency_key, attempt_number,
                 action_type, scheduled_for, executed_at, outcome, recovered_amount_paise,
                 provider_ref, stopping_rule_triggered)
            VALUES
                (:recovery_id, :payment_id, :decision_id, :idempotency_key, :attempt_number,
                 :action_type, :scheduled_for, :executed_at, :outcome, :recovered_amount_paise,
                 :provider_ref, :stopping_rule_triggered)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                executed_at = EXCLUDED.executed_at,
                outcome = EXCLUDED.outcome,
                recovered_amount_paise = EXCLUDED.recovered_amount_paise,
                provider_ref = EXCLUDED.provider_ref,
                stopping_rule_triggered = EXCLUDED.stopping_rule_triggered
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
            "stopping_rule_triggered": stopping_rule,
        },
    )
    if stopping_rule is not None:
        # TRD §10: stopping_rule_triggers_total{reason} -- action_fn (this
        # function's only caller) runs at most once per idempotency_key
        # (services/execution_engine/idempotency.py's check-then-act
        # guarantee), so no redelivery double-count guard is needed here.
        stopping_rule_triggers_total.labels(reason=stopping_rule).inc()
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


def process_job(
    conn: Connection,
    job: dict[str, Any],
    provider=None,
    notification_service=None,
    human_handoff_service=None,
) -> dict[str, Any]:
    """
    One recovery job, fully processed through the TRD §4.2 state machine,
    wrapped in the exact lock-before-check idempotency pattern (gaps.md
    §B.2, services/execution_engine/idempotency.py:execute_with_idempotency).

    Domain Audit finding #3: `action_type` now decides WHICH side-effecting
    call happens, not just which label gets logged. Before this fix, every
    action_type reached the exact same PaymentProvider.retry() (a real
    charge/order-creation call) -- including REMINDER, which services/
    policy_engine/rules.py's own QuietHoursComplianceRule docstring calls
    "the only customer-contact action in this system" (i.e. explicitly NOT
    a charge). The real execution boundary is now:
        RETRY_NOW / ALT_ROUTE -> PaymentProvider.retry() (real money)
        REMINDER              -> NotificationService (never money)
        ESCALATE              -> HumanHandoffService (never money)

    `provider`/`notification_service`/`human_handoff_service` are all
    injectable for tests (spies standing in for the real adapters);
    production callers omit them and get the real config-selected/demo
    implementations. `provider` is resolved LAZILY -- a REMINDER/ESCALATE
    job never needs a configured payment provider at all.

    Phase 12/13: mission_id is looked up (never created here under normal
    operation -- services/pipeline/consumer.py already created/transitioned
    the mission to EXECUTING before this job was ever enqueued) OUTSIDE the
    idempotency boundary, same as this function's own RECOVERY_SCHEDULED
    event below (a read-then-maybe-insert lookup is safe to repeat on
    redelivery). The actual mission STATE transition
    (EXECUTING -> OBSERVING_OUTCOME -> terminal/reschedule) happens INSIDE
    action_fn, after the same commit that backs _upsert_recovery's write --
    execute_with_idempotency's own get_existing() check means a redelivery
    that finds an already-committed recoveries row never re-enters action_fn
    at all, so the mission transition inherits the exact same
    at-most-once guarantee every other write in this function already has.
    """
    from services.execution_engine.human_handoff import get_human_handoff_service
    from services.execution_engine.notification import get_notification_service
    from services.recovery_engine.mission import find_mission_for_payment_sync

    payment_id = job["payment_id"]
    idempotency_key = job["idempotency_key"]
    action_type = job["action_type"]
    attempt_number = int(job["attempt_number"])
    decision_id = job["decision_id"]
    amount_paise = int(job["amount_paise"])

    notification_service = notification_service or get_notification_service()
    human_handoff_service = human_handoff_service or get_human_handoff_service()

    # Read-only, never creates (find_mission_for_payment_sync, not
    # get_or_create_mission_sync -- see that function's own docstring for
    # why: execution_worker never originates a mission, only reacts to a
    # job services/pipeline/consumer.py already decided to run). A job that
    # reaches here whose mission is missing entirely (a test/direct
    # process_job() call that never went through consumer.py) or in ANY
    # state other than EXECUTING (already terminal -- a genuinely
    # redelivered job for an already-completed mission, Redis's normal
    # at-least-once guarantee) is not a state transition_mission_sync's
    # ALLOWED_TRANSITIONS table can legally apply -- skip mission tracking
    # for this job entirely rather than raise InvalidMissionTransitionError
    # and strand the message in an infinite redelivery loop, or spuriously
    # create a second, orphaned mission. Same defensive discipline as
    # services/pipeline/consumer.py's own mission_trackable guard; the
    # actual execution/idempotency logic below is completely unaffected
    # either way.
    mission_row = find_mission_for_payment_sync(conn, payment_id)
    mission_id = mission_row["mission_id"] if mission_row is not None else None
    mission_trackable = mission_row is not None and mission_row["state"] == "EXECUTING"

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

        if action_type == "REMINDER":
            result = notification_service.send_reminder(
                conn, payment_id, amount_paise, attempt_number
            )
        elif action_type == "ESCALATE":
            result = human_handoff_service.create_escalation(
                conn, payment_id, amount_paise, attempt_number
            )
        else:
            # RETRY_NOW / ALT_ROUTE -- the only two action types that
            # genuinely mean "attempt to charge the customer."
            provider_adapter = provider or get_provider_adapter()
            result = provider_adapter.retry(conn, payment_id, amount_paise, attempt_number)

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
        # Domain Audit finding #2 (Production Architecture audit): these
        # used to increment BEFORE this commit -- if the process crashed
        # anywhere between the old increment point and this commit, the
        # `recoveries` row was never durably written, Redis would
        # redeliver the message, and action_fn would run again from
        # scratch (get_existing() finds nothing), permanently
        # over-counting these series even though `recoveries` itself
        # stayed correctly deduplicated via its UNIQUE(idempotency_key)
        # constraint. Moved to HERE, after the commit that actually backs
        # `_upsert_recovery`'s write (the immediately preceding
        # conn.commit() above): action_fn only ever reaches this point
        # once its own row is durably committed, and execute_with_
        # idempotency's get_existing() check (run BEFORE action_fn on
        # every call, including a redelivery) means a redelivery that
        # finds an already-committed row never re-enters action_fn at
        # all -- so this line now runs at most once per row that ever
        # gets a real commit, matching what these series claim to count.
        recovery_attempts_total.labels(action_type=action_type).inc()
        if result.outcome == "SUCCESS":
            recovery_success_total.labels(action_type=action_type).inc()

        if result.outcome in ("SUCCESS", "FAILED"):
            # A real terminal outcome -- this consumer (not
            # services/pipeline/consumer.py, which only handles the
            # no-execution BLOCK/DO_NOTHING case) is the one place that
            # writes recovery_ledger + audit_log for an executed job.
            _write_ledger_and_audit(conn, payment_id, decision_id, action_type, result)

        # Phase 12/13 -- advance the mission from EXECUTING now that a real
        # outcome is known. Inside action_fn (not process_job's top level):
        # execute_with_idempotency's own get_existing() check means a
        # redelivery that finds recoveries already committed never re-
        # enters action_fn at all, so this mutation inherits the exact same
        # at-most-once guarantee _upsert_recovery's own write already has.
        # Guarded on mission_trackable -- see its definition above.
        if mission_trackable:
            _advance_mission_after_outcome(
                conn,
                mission_id=mission_id,
                payment_id=payment_id,
                decision_id=decision_id,
                action_type=action_type,
                attempt_number=attempt_number,
                result=result,
            )

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


def _record_backlog(redis_client: sync_redis.Redis) -> None:
    """Domain Audit finding #4 -- sync mirror of the async consumers' own
    _record_backlog, for stream:recovery_jobs. Best-effort."""
    try:
        groups = redis_client.xinfo_groups(STREAM_NAME)
        for group in groups:
            if group.get("name") == GROUP_NAME:
                lag = group.get("lag")
                if lag is not None:
                    stream_backlog_depth.labels(stream=STREAM_NAME, group=GROUP_NAME).set(lag)
                break
    except Exception:
        logger.exception("[ExecutionWorker] failed to record stream backlog (non-fatal)")


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
        _record_backlog(redis_client)
        if not results:
            continue
        for _stream_name, messages in results:
            for stream_msg_id, raw_msg in messages:
                ok = _process_message(engine, stream_msg_id, raw_msg, provider=provider)
                if ok:
                    redis_client.xack(STREAM_NAME, GROUP_NAME, stream_msg_id)


def main() -> None:
    from prometheus_client import start_http_server

    from recoveryos.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    # TRD §10: recovery_attempts_total, recovery_success_total, and
    # stopping_rule_triggers_total are all recorded in THIS process (see
    # action_fn/_upsert_recovery above) -- needs its own scrape port.
    # start_http_server spawns a plain background thread, safe to call from
    # this sync (non-asyncio) process (see this module's own docstring for
    # why execution_worker stays sync).
    start_http_server(settings.prometheus_port)
    redis_client = sync_redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        run_worker(redis_client)
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
