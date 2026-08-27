"""
Incidents router — GET /v1/incidents/active (PRD §46 System Incident
screen). Not in TRD §5's representative subset -- added because the
System Incident screen needs a dedicated real feed of currently-active
high-severity anomalies, which no existing endpoint serves.

Triggers off the same real anomaly_windows rows services/risk_engine/
anomaly.py writes (including ones written by /v1/simulate/degrade in demo
mode) — never a canned incident payload.
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

# Same freshness window as apps/api/routers/risk.py's bank health grid and
# services/risk_engine/anomaly.py::is_cohort_suppressed's default — an
# "active" incident is one whose window is still within the suppression
# horizon, not a stale reading from hours ago.
INCIDENT_FRESHNESS_MINUTES = 30


@router.get("/active", summary="Currently active high-severity anomalies (System Incident screen)")
async def active_incidents(
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    cutoff = datetime.now(UTC) - timedelta(minutes=INCIDENT_FRESHNESS_MINUTES)

    windows = (
        (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (scope_entity)
                        window_id, scope_type, scope_entity, time_bucket, baseline_rate,
                        observed_rate, z_score, severity
                    FROM anomaly_windows
                    WHERE scope_type = 'bank' AND severity = 'high' AND time_bucket >= :cutoff
                    ORDER BY scope_entity, time_bucket DESC
                    """
                ),
                {"cutoff": cutoff},
            )
        )
        .mappings()
        .all()
    )

    incidents = []
    for w in windows:
        bucket_end = w["time_bucket"] + timedelta(minutes=15)
        affected_row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT
                            COUNT(*) AS affected_payment_count,
                            COALESCE(SUM(p.amount_paise), 0) AS revenue_at_risk_paise,
                            COALESCE(SUM(rl.expected_recovery_paise), 0) AS expected_recovery_paise
                        FROM payments p
                        LEFT JOIN recovery_ledger rl ON rl.payment_id = p.payment_id
                        WHERE p.merchant_id = :merchant_id AND p.bank = :bank
                          AND p.status = 'failed'
                          AND p.created_at >= :bucket_start AND p.created_at < :bucket_end
                        """
                    ),
                    {
                        "merchant_id": merchant.merchant_id,
                        "bank": w["scope_entity"],
                        "bucket_start": w["time_bucket"],
                        "bucket_end": bucket_end,
                    },
                )
            )
            .mappings()
            .one()
        )

        incidents.append(
            {
                "window_id": w["window_id"],
                "bank": w["scope_entity"],
                "time_bucket": w["time_bucket"].isoformat(),
                "baseline_success_rate": (
                    1.0 - float(w["baseline_rate"]) if w["baseline_rate"] is not None else None
                ),
                "observed_success_rate": (
                    1.0 - float(w["observed_rate"]) if w["observed_rate"] is not None else None
                ),
                "z_score": float(w["z_score"]) if w["z_score"] is not None else None,
                "affected_payment_count": int(affected_row["affected_payment_count"]),
                "revenue_at_risk_paise": int(affected_row["revenue_at_risk_paise"]),
                "expected_recovery_paise": int(affected_row["expected_recovery_paise"]),
                # Real policy behavior, not editorial copy: high-severity
                # anomalies suppress RETRY_NOW via SystemicSuppressionRule
                # (services/policy_engine/rules.py) and RETRY_LATER defers
                # via scheduled_reevaluations (Task REPLAN1) — "defer
                # retries" is literally what the system is already doing
                # for these payments, not a suggestion invented for the UI.
                "recommended_action": "Defer retries (RETRY_LATER) until the cohort re-evaluation window passes",
                "root_cause": f"Bank-level degradation: {w['scope_entity']}",
            }
        )

    return {"incidents": incidents}
