"""
Simulation control router — POST /v1/simulate/degrade (PRD §38 demo hook).
ONLY enabled when ENV=demo (enforced at app factory level in main.py).

Real implementation: inserts real `payments` rows for this bank at the
requested degraded success rate (both a normal trailing-history baseline,
so the z-score has something real to compare against, and a current-bucket
batch at the degraded rate), then calls the ACTUAL anomaly detector
(services/risk_engine/anomaly.py's compute_anomaly_window +
persist_anomaly_window) against those real rows -- not a canned response.
The resulting anomaly_windows row (severity/z_score/is_anomaly) is real
detector output over data this endpoint just wrote, and is returned to the
caller so the dashboard can show what actually fired.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.config import get_settings
from recoveryos.database import get_app_session
from recoveryos.models import Merchant
from services.risk_engine.anomaly import (
    compute_anomaly_window,
    floor_to_bucket,
    persist_anomaly_window,
)

router = APIRouter()

# Real historical baseline needs >= 2 trailing-day buckets with data for
# compute_anomaly_window to produce a z-score at all (see its own
# "len(historical_rates) < 2" guard) — 2 is the minimum, not the target;
# using 2 keeps the demo's synthetic footprint small.
BASELINE_TRAILING_DAYS = 2
NORMAL_FAILURE_RATE = 0.05


class DegradeRequest(BaseModel):
    bank: str
    method: str
    target_success_rate: float = Field(ge=0.0, le=1.0)
    duration_minutes: int = Field(gt=0, le=480)


async def _ensure_synthetic_customer(session: AsyncSession, merchant_id: str) -> str:
    customer_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO customers (customer_id, merchant_id, is_returning) "
            "VALUES (:cid, :mid, true)"
        ),
        {"cid": customer_id, "mid": merchant_id},
    )
    return customer_id


async def _insert_synthetic_payments(
    session: AsyncSession,
    *,
    merchant_id: str,
    customer_id: str,
    bank: str,
    method: str,
    bucket_start: datetime,
    count: int,
    failure_rate: float,
) -> None:
    failed_count = round(count * failure_rate)
    for i in range(count):
        status_value = "failed" if i < failed_count else "success"
        ts = bucket_start + timedelta(seconds=i)
        await session.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, :method, :bank, :status, :fcode, :fclass, "
                "true, :ts, :failed_ts)"
            ),
            {
                "pid": str(uuid.uuid4()),
                "mid": merchant_id,
                "cid": customer_id,
                "amount": 50_000,
                "method": method,
                "bank": bank,
                "status": status_value,
                "fcode": "BANK_DECLINE" if status_value == "failed" else None,
                "fclass": "SYSTEMIC" if status_value == "failed" else None,
                "ts": ts,
                "failed_ts": ts if status_value == "failed" else None,
            },
        )


@router.post("/degrade", summary="[DEMO ONLY] Inject bank degradation scenario")
async def simulate_degrade(
    payload: DegradeRequest,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Writes real synthetic `payments` rows for `payload.bank`/`payload.method`
    at the requested degraded success rate, then runs the REAL anomaly
    detector over them. `duration_minutes` sets how many current-bucket
    payments to inject (proportional, capped) so a longer injection reads
    as a bigger, not just longer, incident -- matching PRD §38's framing
    that a demo degradation should visibly move the dashboard's numbers.
    """
    settings = get_settings()
    bucket_minutes = settings.anomaly_bucket_minutes
    min_sample_size = settings.anomaly_min_sample_size

    customer_id = await _ensure_synthetic_customer(session, merchant.merchant_id)

    now = datetime.now(UTC)
    current_bucket = floor_to_bucket(now, bucket_minutes)

    # Real trailing-day baseline history at a normal failure rate, so the
    # z-score has genuine historical variance to compare against instead
    # of hitting the "insufficient_data" guard every time.
    for days_ago in range(1, BASELINE_TRAILING_DAYS + 1):
        hist_bucket = current_bucket - timedelta(days=days_ago)
        await _insert_synthetic_payments(
            session,
            merchant_id=merchant.merchant_id,
            customer_id=customer_id,
            bank=payload.bank,
            method=payload.method,
            bucket_start=hist_bucket,
            count=min_sample_size,
            failure_rate=NORMAL_FAILURE_RATE,
        )

    # Current-bucket degraded batch — size scales with duration_minutes
    # (capped) so a longer/bigger injection visibly moves more of the
    # dashboard's numbers, per PRD §38.
    current_count = min(min_sample_size + payload.duration_minutes, 500)
    await _insert_synthetic_payments(
        session,
        merchant_id=merchant.merchant_id,
        customer_id=customer_id,
        bank=payload.bank,
        method=payload.method,
        bucket_start=current_bucket,
        count=current_count,
        failure_rate=1.0 - payload.target_success_rate,
    )
    await session.commit()

    # The real detector, over the rows just written — not a canned result.
    result = await compute_anomaly_window(session, "bank", payload.bank, current_bucket)
    await persist_anomaly_window(session, result)

    return {
        "status": "degradation_injected",
        "bank": payload.bank,
        "method": payload.method,
        "target_success_rate": payload.target_success_rate,
        "duration_minutes": payload.duration_minutes,
        "synthetic_payments_injected": current_count + BASELINE_TRAILING_DAYS * min_sample_size,
        "anomaly_detection_result": {
            "scope_type": result.scope_type,
            "scope_entity": result.scope_entity,
            "time_bucket": result.time_bucket.isoformat(),
            "baseline_rate": result.baseline_rate,
            "observed_rate": result.observed_rate,
            "z_score": result.z_score,
            "severity": result.severity,
            "is_anomaly": result.is_anomaly,
            "sample_size": result.sample_size,
        },
    }
