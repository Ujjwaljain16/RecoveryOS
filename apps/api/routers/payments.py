"""Payment detail router — GET /v1/payments/{payment_id}/detail (PRD §45)"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.database import get_app_session
from recoveryos.models import Event, Merchant, Payment

router = APIRouter()


@router.get("/{payment_id}/detail", summary="Full decision chain for a payment")
async def payment_detail(
    payment_id: str,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Real implementation (Phase 9) — diagnosis/candidate_actions/
    policy_decision/recovery_history are all live queries now; the tables
    have been real since Phase 5-7, this endpoint just wasn't wired to
    read them yet.

    Scoped to the authenticated merchant — a payment_id belonging to a
    DIFFERENT merchant (or not existing at all) both 404 identically, so a
    caller can't enumerate other merchants' payment_ids by probing IDs.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.merchant_id != merchant.merchant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")

    event_rows = (
        (
            await session.execute(
                select(Event).where(Event.payment_id == payment_id).order_by(Event.occurred_at)
            )
        )
        .scalars()
        .all()
    )

    diagnosis_row = (
        (
            await session.execute(
                text(
                    "SELECT diagnosis_id, root_cause, confidence, confidence_band, "
                    "is_fallback, model_version, evidence, created_at "
                    "FROM diagnoses WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )

    # The most recent decision cycle's policy_decision + its candidate set.
    # candidate_actions are inserted 6-at-a-time per decision cycle
    # (services/recovery_engine/orchestrator.py:persist_decision); the
    # latest policy_decision's source_event_id pins down exactly which
    # batch belongs to THIS payment's most recent decision, rather than
    # mixing candidates across multiple retry attempts.
    policy_decision_row = (
        (
            await session.execute(
                text(
                    "SELECT decision_id, source_event_id, candidate_id, policy_config_id, "
                    "verdict, rule_trace, created_at "
                    "FROM policy_decisions WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )

    candidate_rows: list = []
    policy_config_row = None
    if policy_decision_row is not None:
        source_event_id = policy_decision_row["source_event_id"]
        if source_event_id is not None:
            candidate_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT candidate_id, action_type, recovery_prob_bps, "
                            "expected_value_paise, cost_paise, friction_penalty_paise, "
                            "risk_penalty_paise, action_confidence "
                            "FROM candidate_actions "
                            "WHERE payment_id = :pid AND source_event_id = :sid "
                            "ORDER BY recovery_prob_bps DESC"
                        ),
                        {"pid": payment_id, "sid": source_event_id},
                    )
                )
                .mappings()
                .all()
            )
        else:
            # Direct/test invocations with no source_event_id — fall back to
            # every candidate ever generated for this payment, still real
            # rows, just not scoped to one triggering event.
            candidate_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT candidate_id, action_type, recovery_prob_bps, "
                            "expected_value_paise, cost_paise, friction_penalty_paise, "
                            "risk_penalty_paise, action_confidence "
                            "FROM candidate_actions WHERE payment_id = :pid AND source_event_id IS NULL "
                            "ORDER BY recovery_prob_bps DESC"
                        ),
                        {"pid": payment_id},
                    )
                )
                .mappings()
                .all()
            )

        policy_config_row = (
            (
                await session.execute(
                    text(
                        "SELECT max_retries, retry_cooldown_hours, max_amount_paise, "
                        "escalate_after_failures FROM policy_configs WHERE policy_config_id = :pcid"
                    ),
                    {"pcid": policy_decision_row["policy_config_id"]},
                )
            )
            .mappings()
            .first()
        )

    recovery_history_rows = (
        (
            await session.execute(
                text(
                    "SELECT recovery_id, attempt_number, action_type, scheduled_for, executed_at, "
                    "outcome, recovered_amount_paise, provider_ref, stopping_rule_triggered "
                    "FROM recoveries WHERE payment_id = :pid ORDER BY attempt_number ASC"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .all()
    )

    return {
        "payment_id": payment.payment_id,
        "payment": {
            "customer_id": payment.customer_id,
            "amount_paise": payment.amount_paise,
            "method": payment.method,
            "bank": payment.bank,
            "status": payment.status,
            "failure_code": payment.failure_code,
            "failure_class": payment.failure_class,
            "created_at": payment.created_at.isoformat(),
            "failed_at": payment.failed_at.isoformat() if payment.failed_at else None,
        },
        "events": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for e in event_rows
        ],
        "diagnosis": (
            {
                "diagnosis_id": diagnosis_row["diagnosis_id"],
                "root_cause": diagnosis_row["root_cause"],
                "confidence": (
                    float(diagnosis_row["confidence"])
                    if diagnosis_row["confidence"] is not None
                    else None
                ),
                "confidence_band": diagnosis_row["confidence_band"],
                "is_fallback": diagnosis_row["is_fallback"],
                "model_version": diagnosis_row["model_version"],
                "evidence": diagnosis_row["evidence"],
                "created_at": diagnosis_row["created_at"].isoformat(),
            }
            if diagnosis_row is not None
            else None
        ),
        "candidate_actions": [
            {
                "candidate_id": c["candidate_id"],
                "action_type": c["action_type"],
                "recovery_prob_bps": c["recovery_prob_bps"],
                "expected_value_paise": c["expected_value_paise"],
                "cost_paise": c["cost_paise"],
                "friction_penalty_paise": c["friction_penalty_paise"],
                "risk_penalty_paise": c["risk_penalty_paise"],
                "action_confidence": (
                    float(c["action_confidence"]) if c["action_confidence"] is not None else None
                ),
                "is_selected": (
                    policy_decision_row is not None
                    and c["candidate_id"] == policy_decision_row["candidate_id"]
                ),
            }
            for c in candidate_rows
        ],
        "policy_decision": (
            {
                "decision_id": policy_decision_row["decision_id"],
                "verdict": policy_decision_row["verdict"],
                "rule_trace": policy_decision_row["rule_trace"],
                "created_at": policy_decision_row["created_at"].isoformat(),
                # The real per-merchant policy config that produced this
                # verdict — "stopping rule" on the Payment Detail screen
                # (PRD §45) is this merchant's actual max_retries, not a
                # hardcoded "2 attempts maximum" string.
                "stopping_rule": (
                    f"{policy_config_row['max_retries']} attempts maximum"
                    if policy_config_row is not None
                    else None
                ),
                "max_amount_paise": (
                    policy_config_row["max_amount_paise"] if policy_config_row is not None else None
                ),
                "retry_cooldown_hours": (
                    policy_config_row["retry_cooldown_hours"]
                    if policy_config_row is not None
                    else None
                ),
            }
            if policy_decision_row is not None
            else None
        ),
        "recovery_history": [
            {
                "recovery_id": r["recovery_id"],
                "attempt_number": r["attempt_number"],
                "action_type": r["action_type"],
                "scheduled_for": r["scheduled_for"].isoformat(),
                "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
                "outcome": r["outcome"],
                "recovered_amount_paise": r["recovered_amount_paise"],
                "provider_ref": r["provider_ref"],
                "stopping_rule_triggered": r["stopping_rule_triggered"],
            }
            for r in recovery_history_rows
        ],
    }
