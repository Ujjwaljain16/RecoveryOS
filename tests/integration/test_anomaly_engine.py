"""
Integration tests for services/risk_engine/anomaly.py — TRD §3.2.

Real Postgres (testcontainers via tests/conftest.py + tests/integration/conftest.py).
Payments are seeded directly via raw SQL rather than through the full
simulator pipeline: the simulator's own timing model ticks 5-45s/payment
(see run.py), so building 7 real trailing days of history through it would
mean generating tens of thousands of payments per test. Direct seeding
gives the exact bucket/rate control these tests need in milliseconds — the
REAL simulator scenario is exercised separately (see the manual verification
run in PHASE4_VERIFICATION.md-equivalent acceptance-criteria output, using
simulator/run.py's actual BankDegradationScenario).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from services.risk_engine.anomaly import (
    SEVERITY_HIGH,
    SEVERITY_INSUFFICIENT_DATA,
    compute_anomaly_window,
    floor_to_bucket,
    persist_anomaly_window,
)
from tests.integration.conftest import seed_merchant_and_customer, to_async_url

BUCKET_MINUTES = 15


async def _seed_payments(
    engine,
    merchant_id: str,
    customer_id: str,
    bank: str,
    bucket_start: datetime,
    total: int,
    failed: int,
) -> None:
    """Insert `total` payments into one 15-min bucket for one bank, `failed`
    of them status='failed', spread across the bucket so they unambiguously
    fall inside [bucket_start, bucket_start + 15min)."""
    async with engine.begin() as conn:
        for i in range(total):
            ts = bucket_start + timedelta(seconds=i % 800)  # stays within the 15-min window
            status = "failed" if i < failed else "success"
            await conn.execute(
                text(
                    """
                    INSERT INTO payments
                        (payment_id, merchant_id, customer_id, amount_paise, method, bank,
                         status, failure_code, is_synthetic, created_at, failed_at)
                    VALUES
                        (:pid, :mid, :cid, 50000, 'upi', :bank,
                         :status, :fcode, true, :ts, :failed_at)
                    """
                ),
                {
                    "pid": str(uuid.uuid4()),
                    "mid": merchant_id,
                    "cid": customer_id,
                    "bank": bank,
                    "status": status,
                    "fcode": "TIMEOUT" if status == "failed" else None,
                    "ts": ts,
                    "failed_at": ts if status == "failed" else None,
                },
            )


@pytest.mark.asyncio
async def test_zscore_flags_known_injected_spike(migrated_db):
    """
    7 trailing days at ~3% baseline, current bucket at 18% (PRD §32 Scenario
    B's exact numbers) -> must be flagged severity=high within one bucket,
    with a cohort_id attached.
    """
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    bank = f"TESTBANK-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(to_async_url(migrated_db))

    now = datetime.now(timezone.utc)
    current_bucket = floor_to_bucket(now, BUCKET_MINUTES)

    # 7 days of clean baseline history at the SAME time-of-day bucket, ~3% failure.
    for days_ago in range(1, 8):
        hist_bucket = current_bucket - timedelta(days=days_ago)
        await _seed_payments(
            engine, merchant_id, customer_id, bank, hist_bucket, total=50, failed=1
        )  # 1/50 = 2%, close to the 3% baseline PRD/TRD describe

    # Current bucket: real degradation, 18% failure (PRD §32 Scenario B: 3% -> 18%).
    await _seed_payments(engine, merchant_id, customer_id, bank, current_bucket, total=50, failed=9)

    async with AsyncSession(engine) as session:
        result = await compute_anomaly_window(session, "bank", bank, current_bucket)
        await persist_anomaly_window(session, result)

    print(
        f"\n[test_zscore_flags_known_injected_spike] bank={bank} "
        f"observed_rate={result.observed_rate:.3f} baseline_rate={result.baseline_rate:.3f} "
        f"z_score={result.z_score:.3f} severity={result.severity} "
        f"is_anomaly={result.is_anomaly} cohort_id={result.cohort_id}"
    )

    assert result.severity == SEVERITY_HIGH, (
        f"expected severity=high for an 18%-vs-3% spike, got {result.severity} "
        f"(z={result.z_score})"
    )
    assert result.z_score is not None and result.z_score > 3.0
    assert result.is_anomaly is True
    assert result.cohort_id is not None

    # Confirm it actually landed in anomaly_windows (persist_anomaly_window
    # ran on app_role — the only role that can write it).
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT severity, z_score, is_anomaly FROM anomaly_windows "
                    "WHERE scope_type='bank' AND scope_entity=:bank AND time_bucket=:bucket"
                ),
                {"bank": bank, "bucket": current_bucket},
            )
        ).mappings().first()
    assert row is not None, "anomaly window was not persisted"
    assert row["severity"] == SEVERITY_HIGH
    assert row["is_anomaly"] is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_insufficient_data_guard_prevents_false_positive_on_low_traffic(migrated_db):
    """
    A bucket with n < anomaly_min_sample_size (30) must be marked
    insufficient_data and NEVER flagged as an anomaly, no matter how extreme
    the observed rate looks (here: 8/10 = 80% failed) — TRD §3.2's explicit
    guard against false positives on low-traffic banks.
    """
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    bank = f"SPARSEBANK-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(to_async_url(migrated_db))

    now = datetime.now(timezone.utc)
    current_bucket = floor_to_bucket(now, BUCKET_MINUTES)

    # Only 10 payments (< min_sample_size=30), 8 of them "failed" — an
    # 80% rate that would trivially be "high" if the sample-size guard
    # didn't exist.
    await _seed_payments(engine, merchant_id, customer_id, bank, current_bucket, total=10, failed=8)

    async with AsyncSession(engine) as session:
        result = await compute_anomaly_window(session, "bank", bank, current_bucket)

    print(
        f"\n[test_insufficient_data_guard] bank={bank} sample_size={result.sample_size} "
        f"severity={result.severity} is_anomaly={result.is_anomaly}"
    )

    assert result.severity == SEVERITY_INSUFFICIENT_DATA
    assert result.is_anomaly is False
    assert result.z_score is None, "z-score must not even be computed below the sample-size floor"
    assert result.sample_size == 10

    await engine.dispose()
