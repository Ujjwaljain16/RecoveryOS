"""
Razorpay webhook reconciliation — Task WEBHOOK1. Resolves a PENDING
recovery (RazorpayTestAdapter created a real order, outcome was
provisionally PENDING) to its real terminal outcome once a real webhook
confirms it, and writes the terminal recovery_ledger/audit_log row that
ledger.py's own docstring says a PENDING outcome never got.

app_role only (same as every other writer in services/pipeline/) — this
runs from apps/api/routers/razorpay_webhooks.py's request handler.

Recovery Mission correctness fix: for the real (non-simulator) provider
path, a PENDING outcome's eventual SUCCESS/FAILED confirmation arrives ONLY through
this webhook reconciliation call — workers/execution_worker.py's own
action_fn already returned once it recorded PENDING, so its mission-
advancing logic (_advance_mission_after_outcome) never runs for these
payments at all. Without the fix below, a mission attached to such a
payment would sit in EXECUTING/OBSERVING_OUTCOME forever once the world
outside RecoveryOS (a real webhook) already resolved it -- "the world
changed" needs the mission to actually notice, not just the ledger.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos import clock
from recoveryos.database import advisory_lock_async
from services.pipeline.ledger import populate_ledger_and_audit_async
from services.recovery_engine.mission import check_budget, transition_mission_async
from services.recovery_engine.scheduling import schedule_reevaluation


async def _find_active_mission(app_session: AsyncSession, payment_id: str) -> dict | None:
    row = (
        (
            await app_session.execute(
                text(
                    "SELECT mission_id, state, current_attempt, max_attempts, "
                    "current_round, max_investigation_rounds, started_at, "
                    "max_mission_duration_seconds "
                    "FROM recovery_missions WHERE payment_id = :pid "
                    "AND state NOT IN ('RECOVERED','ESCALATED','TERMINATED') "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def _compute_stopping_rule_async(
    app_session: AsyncSession, decision_id: str, attempt_number: int, outcome: str
) -> str | None:
    """Async mirror of workers/execution_worker.py::_compute_stopping_rule --
    same merchant policy_configs join, same priority order (a genuine
    SUCCESS with stop_after_success ends the sequence outright, otherwise
    reaching max_retries this attempt does)."""
    row = (
        (
            await app_session.execute(
                text(
                    "SELECT pc.max_retries, pc.stop_after_success FROM policy_configs pc "
                    "JOIN policy_decisions pd ON pd.policy_config_id = pc.policy_config_id "
                    "WHERE pd.decision_id = :decision_id"
                ),
                {"decision_id": decision_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if outcome == "SUCCESS" and row["stop_after_success"]:
        return "STOP_AFTER_SUCCESS"
    if attempt_number >= row["max_retries"]:
        return "MAX_RETRIES"
    return None


async def _fetch_retry_cooldown_hours_async(app_session: AsyncSession, decision_id: str) -> int:
    """Async mirror of workers/execution_worker.py::_fetch_retry_cooldown_hours."""
    row = (
        await app_session.execute(
            text(
                "SELECT pc.retry_cooldown_hours FROM policy_configs pc "
                "JOIN policy_decisions pd ON pd.policy_config_id = pc.policy_config_id "
                "WHERE pd.decision_id = :decision_id"
            ),
            {"decision_id": decision_id},
        )
    ).first()
    return (
        row[0] if row is not None else 12
    )  # matches PolicyConfig.retry_cooldown_hours' own default


async def _advance_mission_on_external_resolution(
    app_session: AsyncSession,
    *,
    payment_id: str,
    decision_id: str,
    attempt_number: int,
    outcome: str,
) -> None:
    """
    Async mirror of workers/execution_worker.py::_advance_mission_after_outcome,
    for the one caller (this module) that can report a REAL terminal outcome
    the sync execution path never itself observed. Only SUCCESS/FAILED ever
    reach here (reconcile_pending_recovery's own guard above already
    filtered anything else) -- REMINDER/ESCALATE never have a provider_ref/
    order_id to reconcile against in the first place, so this never needs
    to branch on action_type the way the sync version does.

    Unlike the sync version, this function never has to transition INTO
    OBSERVING_OUTCOME -- by the time a PENDING/FAILED recoveries row exists
    for reconcile_pending_recovery to match against, execution_worker.py's
    own _advance_mission_after_outcome has already put the mission there
    (a PENDING outcome's own fallback branch, or a FAILED outcome's first
    transition). Logging EXTERNAL_RESOLUTION via log_mission_event_async
    (no state change) rather than re-transitioning to the state the mission
    is already in -- OBSERVING_OUTCOME -> OBSERVING_OUTCOME is a self-loop
    ALLOWED_TRANSITIONS deliberately rejects.
    """
    from services.recovery_engine.mission import log_mission_event_async

    mission = await _find_active_mission(app_session, payment_id)
    if mission is None:
        return
    now = clock.utcnow()

    if outcome == "SUCCESS":
        # No increment_attempt here -- the attempt itself was already
        # counted when execution_worker.py first recorded its PENDING
        # outcome (its own money-moving-PENDING fallback branch increments
        # current_attempt); this call is that SAME attempt's conclusion,
        # not a new one.
        await log_mission_event_async(
            app_session,
            mission_id=mission["mission_id"],
            event_type="EXTERNAL_RESOLUTION",
            actor="system",
            payload={"outcome": "SUCCESS", "source": "webhook_reconciliation"},
        )
        await transition_mission_async(
            app_session,
            mission_id=mission["mission_id"],
            to_state="RECOVERED",
            event_type="MISSION_RECOVERED",
            actor="system",
            payload={"source": "webhook_reconciliation"},
            now=now,
        )
        return

    # outcome == "FAILED" -- the mission is already in OBSERVING_OUTCOME
    # (see this function's docstring). No further attempt increment here
    # either, same reasoning as the SUCCESS branch above -- this attempt
    # was already counted when its PENDING outcome was first recorded.
    stopping_rule = await _compute_stopping_rule_async(
        app_session, decision_id, attempt_number, outcome
    )
    await log_mission_event_async(
        app_session,
        mission_id=mission["mission_id"],
        event_type="EXTERNAL_RESOLUTION",
        actor="system",
        payload={
            "outcome": "FAILED",
            "source": "webhook_reconciliation",
            "stopping_rule_triggered": stopping_rule,
        },
    )
    updated = mission

    if stopping_rule is not None:
        await transition_mission_async(
            app_session,
            mission_id=mission["mission_id"],
            to_state="TERMINATED",
            event_type="STOPPING_RULE_TRIGGERED",
            actor="policy_engine",
            payload={"stopping_rule": stopping_rule},
            now=now,
        )
        return

    budget = check_budget(
        current_round=0,  # round budget is enforced at investigation entry, not here -- same as the sync path
        max_investigation_rounds=updated["max_investigation_rounds"],
        current_attempt=updated["current_attempt"],
        max_attempts=updated["max_attempts"],
        started_at=updated["started_at"],
        max_mission_duration_seconds=updated["max_mission_duration_seconds"],
        now=now,
    )
    if budget.exhausted:
        await transition_mission_async(
            app_session,
            mission_id=mission["mission_id"],
            to_state="TERMINATED",
            event_type="MISSION_BUDGET_EXHAUSTED",
            actor="system",
            payload={"reason": budget.reason},
            now=now,
        )
        return

    cooldown_hours = await _fetch_retry_cooldown_hours_async(app_session, decision_id)
    await schedule_reevaluation(
        payment_id=payment_id,
        decision_id=decision_id,
        diagnosis_id=None,
        source_event_id=None,
        scheduled_for=now + timedelta(hours=cooldown_hours),
        mission_id=mission["mission_id"],
    )


async def reconcile_pending_recovery(
    app_session: AsyncSession,
    *,
    order_id: str,
    outcome: str,
    recovered_amount_paise: int,
) -> str | None:
    """
    Returns the matched recovery_id, or None if no PENDING/FAILED recovery
    has this order_id as its provider_ref (e.g. a webhook for an order
    RecoveryOS didn't create), or the match isn't one of the two real
    cases this function actually resolves (see below).

    Two real cases, found live-testing Task WEBHOOK1 -- Razorpay lets a
    customer retry a DIFFERENT payment method on the SAME order after a
    decline (e.g. an international card declined, then netbanking
    succeeds):
      - PENDING -> terminal (SUCCESS or FAILED): the original path.
      - FAILED -> SUCCESS: a later webhook for the SAME order_id reports
        the retried payment method actually succeeded. This UPGRADES the
        already-resolved recovery instead of being silently dropped.
        Deliberately one-directional: a FAILED recovery already matched
        SUCCESS -> FAILED (a later failed webhook after a real capture)
        is NOT matched here at all (excluded by the WHERE clause below,
        since only outcome='FAILED' rows are eligible for an upgrade, and
        only when the INCOMING event is SUCCESS) -- a real captured
        payment is unambiguous ground truth and must never be un-recovered.

    Both cases now call the SAME populate_ledger_and_audit_async
    (Domain Audit finding #2 generalized its own ON-CONFLICT branch to
    self-correct via _should_correct_ledger's invariant) -- this function
    no longer needs its own separate insert-vs-correct dispatch; it only
    decides WHICH recoveries row is eligible to be matched/updated at all.

    Domain Audit finding #5: the entire check-then-act-then-write sequence
    below is held inside a single Postgres advisory lock, keyed on
    order_id -- lock BEFORE the check, exactly like services/
    execution_engine/idempotency.py:execute_with_idempotency (the
    execution path's own reference pattern for this class of race).
    Without this, two genuinely concurrent webhook deliveries for the
    SAME order_id (plausible under Razorpay's documented at-least-once
    redelivery landing on two request handlers simultaneously) could both
    read the row as still PENDING/FAILED before either commits its
    UPDATE, and both proceed to write a ledger entry.
    """
    async with advisory_lock_async(app_session, key=f"razorpay-reconcile:{order_id}"):
        recovery_row = (
            (
                await app_session.execute(
                    text(
                        "SELECT recovery_id, payment_id, decision_id, action_type, attempt_number, outcome "
                        "FROM recoveries WHERE provider_ref = :order_id AND outcome IN ('PENDING', 'FAILED')"
                    ),
                    {"order_id": order_id},
                )
            )
            .mappings()
            .first()
        )
        if recovery_row is None:
            return None

        was_pending = recovery_row["outcome"] == "PENDING"
        if not was_pending and outcome != "SUCCESS":
            # A FAILED recovery can only ever be revised by a genuine SUCCESS
            # (the upgrade case) -- a second FAILED webhook for an
            # already-FAILED recovery (e.g. a redelivery, or the customer
            # failing a second payment method too) is correctly a no-op, not
            # a new correction.
            return None

        await app_session.execute(
            text(
                "UPDATE recoveries SET outcome = :outcome, recovered_amount_paise = :amount, "
                "executed_at = COALESCE(executed_at, now()) WHERE recovery_id = :rid"
            ),
            {
                "outcome": outcome,
                "amount": recovered_amount_paise,
                "rid": recovery_row["recovery_id"],
            },
        )

        decision_row = (
            (
                await app_session.execute(
                    text(
                        "SELECT pd.verdict, pd.candidate_id, ca.recovery_prob_bps, ca.cost_paise "
                        "FROM policy_decisions pd JOIN candidate_actions ca ON ca.candidate_id = pd.candidate_id "
                        "WHERE pd.decision_id = :did"
                    ),
                    {"did": recovery_row["decision_id"]},
                )
            )
            .mappings()
            .first()
        )

        diagnosis_row = (
            (
                await app_session.execute(
                    text(
                        "SELECT diagnosis_id FROM diagnoses WHERE payment_id = :pid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"pid": recovery_row["payment_id"]},
                )
            )
            .mappings()
            .first()
        )

        await app_session.commit()

        await populate_ledger_and_audit_async(
            app_session,
            payment_id=recovery_row["payment_id"],
            candidate_id=decision_row["candidate_id"] if decision_row else None,
            decision_id=recovery_row["decision_id"],
            verdict=decision_row["verdict"] if decision_row else "ALLOW",
            chosen_action=recovery_row["action_type"],
            recovery_prob_bps=decision_row["recovery_prob_bps"] if decision_row else 0,
            cost_paise=decision_row["cost_paise"] if decision_row else 0,
            actual_recovery_paise=recovered_amount_paise,
            recovery_id=recovery_row["recovery_id"],
            diagnosis_id=diagnosis_row["diagnosis_id"] if diagnosis_row else None,
            outcome=outcome,
        )

        # Recovery Mission correctness fix -- see this module's docstring and
        # _advance_mission_on_external_resolution's own docstring for why
        # this webhook path is the ONLY place a real (non-simulator)
        # provider's PENDING attempt ever reports its true terminal outcome.
        #
        # Re-Audit finding (MEDIUM, same root cause as the ledger HIGH
        # finding above): workers/retry_scheduler.py's _process_one reads a
        # mission's state ("still OBSERVING_OUTCOME?"), and only if that
        # passes does it later call process_payment_failure, which re-
        # investigates and burns a real LLM call. Nothing serialized that
        # read against THIS function closing the same mission out from
        # OBSERVING_OUTCOME between the scheduler's check and its own later
        # write -- the order_id lock above doesn't help here, since the
        # scheduler never touches an order_id at all. Locking on `mission:
        # {payment_id}` around the transition below, and requiring
        # retry_scheduler to acquire the SAME key before its own check,
        # closes the gap: whichever side gets there first, the other
        # observes its real, post-transition state, not a stale read.
        # (advisory_lock_async already imported at module level above.)
        async with advisory_lock_async(app_session, key=f"mission:{recovery_row['payment_id']}"):
            await _advance_mission_on_external_resolution(
                app_session,
                payment_id=recovery_row["payment_id"],
                decision_id=recovery_row["decision_id"],
                attempt_number=recovery_row["attempt_number"],
                outcome=outcome,
            )

        return recovery_row["recovery_id"]
