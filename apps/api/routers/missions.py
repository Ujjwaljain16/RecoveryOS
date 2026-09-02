"""
Missions router — GET /v1/missions/active. Powers the Control Tower's
"Active Recovery Missions" list — every payment this merchant currently has
an in-flight (non-terminal) RecoveryMission for (migration 0022,
services/recovery_engine/mission.py). Same merchant-scoped,
verify_api_key-gated pattern as apps/api/routers/incidents.py.

Per-payment mission detail (the full ordered mission_events trace) lives on
apps/api/routers/payments.py's GET /{payment_id}/mission instead -- payment-
scoped reads already live there (see /{payment_id}/detail).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.database import get_app_session
from recoveryos.models import Merchant

router = APIRouter()


@router.get("/active", summary="Currently active (non-terminal) recovery missions")
async def active_missions(
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    rows = (
        (
            await session.execute(
                text(
                    "SELECT m.mission_id, m.payment_id, m.state, m.current_round, "
                    "m.current_attempt, m.max_attempts, m.started_at, "
                    "p.amount_paise, p.bank, p.method "
                    "FROM recovery_missions m "
                    "JOIN payments p ON p.payment_id = m.payment_id "
                    "WHERE p.merchant_id = :merchant_id "
                    "AND m.state NOT IN ('RECOVERED', 'ESCALATED', 'TERMINATED') "
                    "ORDER BY m.started_at DESC"
                ),
                {"merchant_id": merchant.merchant_id},
            )
        )
        .mappings()
        .all()
    )

    return {
        "missions": [
            {
                "mission_id": r["mission_id"],
                "payment_id": r["payment_id"],
                "state": r["state"],
                "current_round": r["current_round"],
                "current_attempt": r["current_attempt"],
                "max_attempts": r["max_attempts"],
                "started_at": r["started_at"].isoformat(),
                "amount_paise": r["amount_paise"],
                "bank": r["bank"],
                "method": r["method"],
            }
            for r in rows
        ]
    }
