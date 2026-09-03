"""
Audit explorer router — GET /v1/audit/{payment_id} (PRD §48).

Replays the full decision chain PAYMENT -> FAILURE -> ANOMALY -> DIAGNOSIS
-> PROPENSITY -> ACTIONS -> EVI -> POLICY -> EXECUTION -> OUTCOME as a
single query over real, already-populated tables (audit_log itself,
diagnoses, candidate_actions, policy_decisions, recoveries,
recovery_ledger, anomaly_windows). Any step whose underlying row genuinely
doesn't exist for this payment (e.g. no anomaly was active, or execution
hasn't happened yet) is represented as null/empty, never fabricated.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.database import get_app_session
from recoveryos.models import Merchant
from services.risk_engine.anomaly import floor_to_bucket

router = APIRouter()


@router.get("/{payment_id}", summary="Full audit trail for a payment (replayable)")
async def audit_trail(
    payment_id: str,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    payment_row = (
        (
            await session.execute(
                text(
                    "SELECT payment_id, merchant_id, customer_id, amount_paise, method, bank, "
                    "status, failure_code, failure_class, created_at, failed_at "
                    "FROM payments WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    # Raw text() SQL returns asyncpg's native uuid.UUID for a UUID column,
    # not the plain str the ORM's UUID(as_uuid=False) type decorator
    # produces (see recoveryos/models.py) — str() both sides so this
    # comparison isn't silently always-false against a str merchant_id.
    if payment_row is None or str(payment_row["merchant_id"]) != str(merchant.merchant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")

    # ─── ANOMALY ────────────────────────────────────────────────────────
    # Real lookup, not a guess: the same 15-minute bucket
    # services/risk_engine/anomaly.py buckets on, for this payment's bank,
    # at the moment it was created. Null if no window was ever computed
    # for that bucket -- never fabricated.
    anomaly_row = None
    if payment_row["bank"] is not None:
        bucket = floor_to_bucket(payment_row["created_at"], 15)
        anomaly_row = (
            (
                await session.execute(
                    text(
                        "SELECT window_id, scope_type, scope_entity, time_bucket, baseline_rate, "
                        "observed_rate, z_score, severity, is_anomaly "
                        "FROM anomaly_windows WHERE scope_type = 'bank' AND scope_entity = :bank "
                        "AND time_bucket = :bucket"
                    ),
                    {"bank": payment_row["bank"], "bucket": bucket},
                )
            )
            .mappings()
            .first()
        )

    # ─── DIAGNOSIS ──────────────────────────────────────────────────────
    diagnosis_row = (
        (
            await session.execute(
                text(
                    "SELECT diagnosis_id, source_event_id, cohort_id, root_cause, confidence, "
                    "confidence_band, is_fallback, model_version, evidence, created_at "
                    "FROM diagnoses WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )

    # ─── ACTIONS + EVI + PROPENSITY ─────────────────────────────────────
    # candidate_actions scoped to the same triggering event as the latest
    # policy_decision -- see payments.py's payment_detail for why
    # source_event_id, not payment_id alone, pins down "this decision
    # cycle's" 6 candidates.
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

    candidate_rows = []
    policy_config_row = None
    if policy_decision_row is not None:
        source_event_id = policy_decision_row["source_event_id"]
        sid_clause = (
            "source_event_id = :sid" if source_event_id is not None else "source_event_id IS NULL"
        )
        candidate_rows = (
            (
                await session.execute(
                    text(
                        f"SELECT candidate_id, action_type, recovery_prob_bps, expected_value_paise, "
                        f"cost_paise, friction_penalty_paise, risk_penalty_paise, action_confidence, "
                        f"model_version FROM candidate_actions "
                        f"WHERE payment_id = :pid AND {sid_clause} ORDER BY recovery_prob_bps DESC"
                    ),
                    {"pid": payment_id, "sid": source_event_id},
                )
            )
            .mappings()
            .all()
        )
        policy_config_row = (
            (
                await session.execute(
                    text(
                        "SELECT max_retries, retry_cooldown_hours, max_amount_paise "
                        "FROM policy_configs WHERE policy_config_id = :pcid"
                    ),
                    {"pcid": policy_decision_row["policy_config_id"]},
                )
            )
            .mappings()
            .first()
        )

    chosen_candidate = next(
        (
            c
            for c in candidate_rows
            if policy_decision_row is not None
            and c["candidate_id"] == policy_decision_row["candidate_id"]
        ),
        None,
    )
    retry_now_candidate = next((c for c in candidate_rows if c["action_type"] == "RETRY_NOW"), None)

    # ─── EXECUTION + OUTCOME ────────────────────────────────────────────
    recovery_rows = (
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

    ledger_row = (
        (
            await session.execute(
                text(
                    "SELECT revenue_at_risk_paise, expected_recovery_paise, actual_recovery_paise, "
                    "baseline_outcome, incremental_recovery_paise, intervention_cost_paise, "
                    "net_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )

    # ─── AI FUSION ────────────────────────────────────────────────────────
    # decision_fusion_trace exists only when ai_recommendation_fusion_enabled
    # was on for this decision -- null (not fabricated) otherwise, same
    # discipline as every other step in this chain.
    fusion_row = None
    if policy_decision_row is not None:
        fusion_row = (
            (
                await session.execute(
                    text(
                        "SELECT deterministic_chosen_action, deterministic_chosen_evi_paise, "
                        "near_tied_candidates, tie_tolerance_bps, ai_recommended_action, "
                        "ai_confidence, ai_risk_flags, tie_break_applied, risk_escalation_applied, "
                        "final_action, fusion_reason "
                        "FROM decision_fusion_trace WHERE decision_id = :did"
                    ),
                    {"did": policy_decision_row["decision_id"]},
                )
            )
            .mappings()
            .first()
        )

    # ─── The audit_log rows themselves (real, insert-only table) ───────
    audit_log_rows = (
        (
            await session.execute(
                text(
                    "SELECT audit_id, diagnosis_id, candidate_id, decision_id, recovery_id, "
                    "summary, created_at FROM audit_log WHERE payment_id = :pid ORDER BY created_at"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .all()
    )

    return {
        "payment_id": payment_id,
        "chain": {
            "payment": {
                "amount_paise": payment_row["amount_paise"],
                "method": payment_row["method"],
                "bank": payment_row["bank"],
                "status": payment_row["status"],
                "created_at": payment_row["created_at"].isoformat(),
            },
            "failure": {
                "failure_code": payment_row["failure_code"],
                "failure_class": payment_row["failure_class"],
                "failed_at": (
                    payment_row["failed_at"].isoformat() if payment_row["failed_at"] else None
                ),
            },
            "anomaly": (
                {
                    "scope_type": anomaly_row["scope_type"],
                    "scope_entity": anomaly_row["scope_entity"],
                    "time_bucket": anomaly_row["time_bucket"].isoformat(),
                    "baseline_rate": (
                        float(anomaly_row["baseline_rate"])
                        if anomaly_row["baseline_rate"] is not None
                        else None
                    ),
                    "observed_rate": (
                        float(anomaly_row["observed_rate"])
                        if anomaly_row["observed_rate"] is not None
                        else None
                    ),
                    "z_score": (
                        float(anomaly_row["z_score"])
                        if anomaly_row["z_score"] is not None
                        else None
                    ),
                    "severity": anomaly_row["severity"],
                    "is_anomaly": anomaly_row["is_anomaly"],
                }
                if anomaly_row is not None
                else None
            ),
            "diagnosis": (
                {
                    "diagnosis_id": diagnosis_row["diagnosis_id"],
                    "cohort_id": diagnosis_row["cohort_id"],
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
                }
                if diagnosis_row is not None
                else None
            ),
            "propensity": (
                {
                    "recovery_prob_bps": retry_now_candidate["recovery_prob_bps"],
                    "model_version": retry_now_candidate["model_version"],
                    "source": "RETRY_NOW candidate's recovery_prob_bps — the closest real "
                    "persisted proxy for base recovery propensity (no separate raw "
                    "propensity column is persisted; this IS the certified LR model's "
                    "scored probability for this payment, before timing/anomaly "
                    "adjustments that apply to other action types)",
                }
                if retry_now_candidate is not None
                else None
            ),
            "actions": [
                {
                    "candidate_id": c["candidate_id"],
                    "action_type": c["action_type"],
                    "recovery_prob_bps": c["recovery_prob_bps"],
                    "expected_value_paise": c["expected_value_paise"],
                    "is_selected": chosen_candidate is not None
                    and c["candidate_id"] == chosen_candidate["candidate_id"],
                }
                for c in candidate_rows
            ],
            "evi": (
                {
                    "candidate_id": chosen_candidate["candidate_id"],
                    "action_type": chosen_candidate["action_type"],
                    "expected_value_paise": chosen_candidate["expected_value_paise"],
                    "cost_paise": chosen_candidate["cost_paise"],
                    "friction_penalty_paise": chosen_candidate["friction_penalty_paise"],
                    "risk_penalty_paise": chosen_candidate["risk_penalty_paise"],
                    "action_confidence": (
                        float(chosen_candidate["action_confidence"])
                        if chosen_candidate["action_confidence"] is not None
                        else None
                    ),
                }
                if chosen_candidate is not None
                else None
            ),
            "policy": (
                {
                    "decision_id": policy_decision_row["decision_id"],
                    "verdict": policy_decision_row["verdict"],
                    "rule_trace": policy_decision_row["rule_trace"],
                    "stopping_rule": (
                        f"{policy_config_row['max_retries']} attempts maximum"
                        if policy_config_row is not None
                        else None
                    ),
                }
                if policy_decision_row is not None
                else None
            ),
            "execution": [
                {
                    "recovery_id": r["recovery_id"],
                    "attempt_number": r["attempt_number"],
                    "action_type": r["action_type"],
                    "scheduled_for": r["scheduled_for"].isoformat(),
                    "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
                    "outcome": r["outcome"],
                    "provider_ref": r["provider_ref"],
                }
                for r in recovery_rows
            ],
            "outcome": (
                {
                    "revenue_at_risk_paise": ledger_row["revenue_at_risk_paise"],
                    "expected_recovery_paise": ledger_row["expected_recovery_paise"],
                    "actual_recovery_paise": ledger_row["actual_recovery_paise"],
                    "baseline_outcome": ledger_row["baseline_outcome"],
                    "incremental_recovery_paise": ledger_row["incremental_recovery_paise"],
                    "net_recovery_paise": ledger_row["net_recovery_paise"],
                }
                if ledger_row is not None
                else None
            ),
            "ai_fusion": (
                {
                    "deterministic_chosen_action": fusion_row["deterministic_chosen_action"],
                    "deterministic_chosen_evi_paise": fusion_row["deterministic_chosen_evi_paise"],
                    "near_tied_candidates": fusion_row["near_tied_candidates"],
                    "tie_tolerance_bps": fusion_row["tie_tolerance_bps"],
                    "ai_recommended_action": fusion_row["ai_recommended_action"],
                    "ai_confidence": (
                        float(fusion_row["ai_confidence"])
                        if fusion_row["ai_confidence"] is not None
                        else None
                    ),
                    "ai_risk_flags": fusion_row["ai_risk_flags"],
                    "tie_break_applied": fusion_row["tie_break_applied"],
                    "risk_escalation_applied": fusion_row["risk_escalation_applied"],
                    "final_action": fusion_row["final_action"],
                    "fusion_reason": fusion_row["fusion_reason"],
                }
                if fusion_row is not None
                else None
            ),
        },
        "audit_log": [
            {
                "audit_id": a["audit_id"],
                "summary": a["summary"],
                "created_at": a["created_at"].isoformat(),
            }
            for a in audit_log_rows
        ],
    }
