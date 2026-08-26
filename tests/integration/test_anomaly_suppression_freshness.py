"""
Task S3 (pre-Phase-8 audit): services/recovery_engine/orchestrator.py's
anomaly-context fetch must respect the freshness window
services/risk_engine/anomaly.py::is_cohort_suppressed() was specifically
built to enforce -- a high-severity anomaly_windows row from hours ago must
NOT suppress RETRY_NOW for a bank that's since recovered. Before this fix,
_fetch_anomaly_context() ran an unbounded `ORDER BY time_bucket DESC LIMIT 1`
and is_cohort_suppressed() (the correct, already-written function) had zero
callers and zero test coverage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import create_async_engine

from services.recovery_engine.orchestrator import _fetch_anomaly_context
from tests.integration.conftest import to_async_url


async def _insert_anomaly_window(migrated_db: str, *, bank: str, time_bucket: datetime) -> None:
    engine = create_async_engine(to_async_url(migrated_db))
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO anomaly_windows "
                "(window_id, scope_type, scope_entity, time_bucket, baseline_rate, "
                " observed_rate, z_score, severity, is_anomaly) "
                "VALUES (gen_random_uuid(), 'bank', :bank, :tb, 0.03, 0.15, 7.1, 'high', true)"
            ),
            {"bank": bank, "tb": time_bucket},
        )
    await engine.dispose()


async def test_stale_anomaly_reading_does_not_suppress_retry_now(migrated_db):
    """
    The exact negative case the pre-fix bug got wrong: a high-severity
    window computed 2 hours ago (well outside is_cohort_suppressed's
    30-minute freshness window) must NOT be treated as a currently active
    anomaly -- the bank may have long since recovered.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    bank = f"STALE_BANK_{uuid.uuid4().hex[:8]}"
    stale_time_bucket = datetime.now(UTC) - timedelta(hours=2)
    await _insert_anomaly_window(migrated_db, bank=bank, time_bucket=stale_time_bucket)

    engine = create_async_engine(to_async_url(migrated_db))
    async with AsyncSession(engine) as session:
        context = await _fetch_anomaly_context(session, bank)
    await engine.dispose()

    assert context is None, (
        "a 2-hour-old high-severity anomaly_windows row must NOT suppress RETRY_NOW -- "
        f"got {context!r}. This is the exact staleness bug Task S3 fixes: the old "
        "unbounded ORDER BY time_bucket DESC LIMIT 1 query had no freshness check at all."
    )


async def test_fresh_high_severity_anomaly_still_suppresses_retry_now(migrated_db):
    """
    Sanity/regression: a genuinely fresh, active high-severity window must
    still be detected -- the freshness fix must not have broken the real
    suppression case in the process of fixing the stale one.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    bank = f"FRESH_BANK_{uuid.uuid4().hex[:8]}"
    fresh_time_bucket = datetime.now(UTC) - timedelta(minutes=5)
    await _insert_anomaly_window(migrated_db, bank=bank, time_bucket=fresh_time_bucket)

    engine = create_async_engine(to_async_url(migrated_db))
    async with AsyncSession(engine) as session:
        context = await _fetch_anomaly_context(session, bank)
    await engine.dispose()

    assert context is not None, "a fresh, active high-severity anomaly must still be detected"
    assert context.severity == "high"
    assert context.is_anomaly is True
    assert context.baseline_rate == 0.03
    assert context.observed_rate == 0.15
