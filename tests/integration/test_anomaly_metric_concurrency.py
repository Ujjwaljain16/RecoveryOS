"""
Production Architecture Domain Audit, Finding #5 -- persist_anomaly_window's
severity-transition check (used to gate systemic_degradation_events_total)
was a real TOCTOU: a plain SELECT read, then a separate upsert, with no
lock around the pair. Two genuinely concurrent callers computing the SAME
(scope_type, scope_entity, time_bucket) window could both read "not
previously high" and both increment the counter for one real transition.

Fixed by wrapping the whole read-check-write-increment sequence in
advisory_lock_async, keyed on the window's own identity. This test proves
it directly: two concurrent persist_anomaly_window calls for the SAME
window, both reporting a genuine not-high -> high transition, must
increment systemic_degradation_events_total by exactly 1, not 2.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from prometheus_client import REGISTRY

from recoveryos.database import get_app_session_factory
from services.risk_engine.anomaly import AnomalyResult, persist_anomaly_window


def _high_severity_result(bank: str, time_bucket: datetime) -> AnomalyResult:
    return AnomalyResult(
        scope_type="bank",
        scope_entity=bank,
        time_bucket=time_bucket,
        baseline_rate=0.03,
        observed_rate=0.30,
        z_score=9.0,
        severity="high",
        is_anomaly=True,
        sample_size=40,
        cohort_id=None,
    )


@pytest.mark.asyncio
async def test_concurrent_writers_for_the_same_window_increment_the_counter_exactly_once(
    migrated_db,
):
    bank = f"CONCURRENCY_BANK_{uuid.uuid4().hex[:8]}"
    time_bucket = datetime.now(UTC).replace(second=0, microsecond=0)
    result = _high_severity_result(bank, time_bucket)

    before = REGISTRY.get_sample_value("systemic_degradation_events_total", {"bank": bank}) or 0.0

    session_factory = get_app_session_factory()

    async def write():
        async with session_factory() as session:
            await persist_anomaly_window(session, result)

    # Genuinely concurrent: two independent AsyncSessions (independent
    # Postgres connections) racing on the exact same window identity --
    # the real-world scenario is two pipeline_orchestrator replicas (or a
    # replica racing the demo simulate-degrade endpoint) computing the
    # same bucket at once.
    await asyncio.gather(write(), write())

    after = REGISTRY.get_sample_value("systemic_degradation_events_total", {"bank": bank})
    assert after == before + 1.0, (
        f"two concurrent writers for the SAME window must produce exactly ONE transition "
        f"event, not two -- before={before}, after={after}"
    )


@pytest.mark.asyncio
async def test_a_third_write_for_an_already_high_window_does_not_increment_again(migrated_db):
    """Sanity check the fix didn't just move the race somewhere else --
    a SUBSEQUENT (non-concurrent) write for a window already at 'high'
    must still correctly recognize 'already transitioned' and not
    re-increment, same as before this fix."""
    bank = f"CONCURRENCY_BANK2_{uuid.uuid4().hex[:8]}"
    time_bucket = datetime.now(UTC).replace(second=0, microsecond=0)
    result = _high_severity_result(bank, time_bucket)

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        await persist_anomaly_window(session, result)

    before = REGISTRY.get_sample_value("systemic_degradation_events_total", {"bank": bank})

    async with session_factory() as session:
        await persist_anomaly_window(session, result)

    after = REGISTRY.get_sample_value("systemic_degradation_events_total", {"bank": bank})
    assert after == before, "re-detecting an already-high window must not increment again"
