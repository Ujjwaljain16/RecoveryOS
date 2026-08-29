"""
Razorpay webhook reconciliation — Task WEBHOOK1. Resolves a PENDING
recovery (RazorpayTestAdapter created a real order, outcome was
provisionally PENDING) to its real terminal outcome once a real webhook
confirms it, and writes the terminal recovery_ledger/audit_log row that
ledger.py's own docstring says a PENDING outcome never got.

app_role only (same as every other writer in services/pipeline/) — this
runs from apps/api/routers/razorpay_webhooks.py's request handler.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.database import advisory_lock_async
from services.pipeline.ledger import populate_ledger_and_audit_async


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

        return recovery_row["recovery_id"]
