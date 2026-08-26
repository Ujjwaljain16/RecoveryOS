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

from services.pipeline.ledger import populate_ledger_and_audit_async


async def reconcile_pending_recovery(
    app_session: AsyncSession,
    *,
    order_id: str,
    outcome: str,
    recovered_amount_paise: int,
) -> str | None:
    """
    Returns the matched recovery_id, or None if no PENDING recovery has
    this order_id as its provider_ref (e.g. a webhook for an order
    RecoveryOS didn't create, or one already reconciled — recovery_ledger's
    own ON CONFLICT (payment_id) DO NOTHING makes a duplicate reconciliation
    attempt a safe no-op regardless).
    """
    recovery_row = (
        await app_session.execute(
            text(
                "SELECT recovery_id, payment_id, decision_id, action_type, attempt_number "
                "FROM recoveries WHERE provider_ref = :order_id AND outcome = 'PENDING'"
            ),
            {"order_id": order_id},
        )
    ).mappings().first()
    if recovery_row is None:
        return None

    await app_session.execute(
        text(
            "UPDATE recoveries SET outcome = :outcome, recovered_amount_paise = :amount, "
            "executed_at = COALESCE(executed_at, now()) WHERE recovery_id = :rid"
        ),
        {"outcome": outcome, "amount": recovered_amount_paise, "rid": recovery_row["recovery_id"]},
    )

    decision_row = (
        await app_session.execute(
            text(
                "SELECT pd.verdict, pd.candidate_id, ca.recovery_prob_bps, ca.cost_paise "
                "FROM policy_decisions pd JOIN candidate_actions ca ON ca.candidate_id = pd.candidate_id "
                "WHERE pd.decision_id = :did"
            ),
            {"did": recovery_row["decision_id"]},
        )
    ).mappings().first()

    diagnosis_row = (
        await app_session.execute(
            text(
                "SELECT diagnosis_id FROM diagnoses WHERE payment_id = :pid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": recovery_row["payment_id"]},
        )
    ).mappings().first()

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
