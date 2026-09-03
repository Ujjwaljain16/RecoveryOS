"""
Risk summary router — GET /v1/risk/summary (PRD §44 Control Tower).

Every field is a real aggregate over recovery_ledger/anomaly_windows/
candidate_actions/policy_decisions/recoveries/scheduled_reevaluations —
no placeholder numbers. TRD §5's contract for this endpoint is a smaller
subset ({total_revenue_at_risk_paise, recoverable_estimate_paise,
affected_payment_count}); this response is a superset because the Control
Tower screen (PRD §44) needs the bank health grid and live recovery queue
too, and TRD §5 itself is scoped as "a representative subset," not the
full contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.database import get_app_session
from recoveryos.models import Merchant

router = APIRouter()

# Same 30-minute "is this anomaly still active" window
# services/risk_engine/anomaly.py::is_cohort_suppressed already uses —
# reused here so the bank health grid agrees with what the policy engine
# is actually suppressing on, not a separately invented staleness rule.
BANK_HEALTH_FRESHNESS_MINUTES = 30
RECOVERY_QUEUE_LIMIT = 20


@router.get("/summary", summary="Revenue-at-risk summary for the authenticated merchant")
async def risk_summary(
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Real, wired implementation. Every number below is a live
    aggregate scoped to `merchant.merchant_id` — never a client-supplied
    merchant_id (see apps/api/dependencies/auth.py).
    """
    ledger_row = (
        (
            await session.execute(
                text(
                    """
                SELECT
                    COALESCE(SUM(rl.revenue_at_risk_paise), 0) AS revenue_at_risk_paise,
                    COALESCE(SUM(rl.actual_recovery_paise), 0) AS recovered_paise,
                    COALESCE(SUM(rl.incremental_recovery_paise), 0) AS incremental_recovery_paise
                FROM recovery_ledger rl
                JOIN payments p ON p.payment_id = rl.payment_id
                WHERE p.merchant_id = :merchant_id
                """
                ),
                {"merchant_id": merchant.merchant_id},
            )
        )
        .mappings()
        .one()
    )

    revenue_at_risk_paise = int(ledger_row["revenue_at_risk_paise"])
    recovered_paise = int(ledger_row["recovered_paise"])
    incremental_recovery_paise = int(ledger_row["incremental_recovery_paise"])
    recovery_rate_bps = (
        (recovered_paise * 10_000) // revenue_at_risk_paise if revenue_at_risk_paise > 0 else 0
    )

    # ─── Bank health grid ───────────────────────────────────────────────
    # anomaly_windows.scope_type='bank' is a platform-wide dimension (see
    # services/risk_engine/anomaly.py's module docstring — no merchant
    # column on that scope), so this reads the latest window per bank that
    # has ANY anomaly history, real DB rows only.
    bank_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT DISTINCT ON (scope_entity)
                    scope_entity AS bank, severity, is_anomaly, observed_rate,
                    baseline_rate, time_bucket
                FROM anomaly_windows
                WHERE scope_type = 'bank'
                ORDER BY scope_entity, time_bucket DESC
                """
                )
            )
        )
        .mappings()
        .all()
    )

    freshness_cutoff = datetime.now(UTC) - timedelta(minutes=BANK_HEALTH_FRESHNESS_MINUTES)
    bank_health = [
        {
            "bank": row["bank"],
            "status": (
                "DEGRADED"
                if row["severity"] == "high" and row["time_bucket"] >= freshness_cutoff
                else "HEALTHY"
            ),
            "severity": row["severity"],
            "observed_rate": (
                float(row["observed_rate"]) if row["observed_rate"] is not None else None
            ),
            "baseline_rate": (
                float(row["baseline_rate"]) if row["baseline_rate"] is not None else None
            ),
            "time_bucket": row["time_bucket"].isoformat(),
        }
        for row in bank_rows
    ]

    # ─── Live recovery queue ────────────────────────────────────────────
    # Two real in-flight states: an execution job that hasn't reached a
    # terminal outcome yet, and a RETRY_LATER decision deferred to
    # scheduled_reevaluations (Task REPLAN1) awaiting its future re-run.
    queue_rows = (
        (
            await session.execute(
                text(
                    """
                (
                    SELECT r.payment_id, p.amount_paise, r.action_type AS chosen_action,
                           ca.recovery_prob_bps, 'EXECUTING' AS status, r.created_at AS ts
                    FROM recoveries r
                    JOIN payments p ON p.payment_id = r.payment_id
                    JOIN policy_decisions pd ON pd.decision_id = r.decision_id
                    JOIN candidate_actions ca ON ca.candidate_id = pd.candidate_id
                    WHERE p.merchant_id = :merchant_id
                      AND (r.outcome IS NULL OR r.outcome = 'PENDING')
                )
                UNION ALL
                (
                    SELECT sr.payment_id, p.amount_paise, ca.action_type AS chosen_action,
                           ca.recovery_prob_bps, 'SCHEDULED' AS status, sr.created_at AS ts
                    FROM scheduled_reevaluations sr
                    JOIN payments p ON p.payment_id = sr.payment_id
                    JOIN policy_decisions pd ON pd.decision_id = sr.decision_id
                    JOIN candidate_actions ca ON ca.candidate_id = pd.candidate_id
                    WHERE p.merchant_id = :merchant_id AND sr.status = 'PENDING'
                )
                ORDER BY ts DESC
                LIMIT :limit
                """
                ),
                {"merchant_id": merchant.merchant_id, "limit": RECOVERY_QUEUE_LIMIT},
            )
        )
        .mappings()
        .all()
    )

    recovery_queue = [
        {
            "payment_id": row["payment_id"],
            "amount_paise": row["amount_paise"],
            "chosen_action": row["chosen_action"],
            "recovery_prob_bps": row["recovery_prob_bps"],
            "status": row["status"],
            "updated_at": row["ts"].isoformat(),
        }
        for row in queue_rows
    ]

    return {
        "revenue_at_risk_paise": revenue_at_risk_paise,
        "recovered_paise": recovered_paise,
        "incremental_recovery_paise": incremental_recovery_paise,
        "recovery_rate_bps": recovery_rate_bps,
        "bank_health": bank_health,
        "recovery_queue": recovery_queue,
    }
