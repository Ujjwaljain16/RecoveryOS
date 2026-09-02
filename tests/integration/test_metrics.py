"""
Phase 10 — Prometheus metrics (TRD §10), real Postgres/Redis, zero mocks.

Two mandatory tests (deliverable spec):
  - the /metrics endpoint exposes all 9 required series
  - the AI-diagnoser fallback counter genuinely increments when the LLM
    path fails (reusing Phase 4's real fallback trigger: an empty
    GEMINI_API_KEY, the same mechanism
    tests/integration/test_pipeline_e2e.py's outage test uses)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import seed_merchant_and_customer, to_async_url

REQUIRED_METRIC_NAMES = (
    "recovery_attempts_total",
    "recovery_success_total",
    "revenue_at_risk_paise_total",
    "revenue_recovered_paise_total",
    "incremental_revenue_paise_total",
    "policy_blocks_total",
    "stopping_rule_triggers_total",
    "systemic_degradation_events_total",
    "diagnosis_latency_seconds",
    "ai_diagnoser_fallback_total",
)


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_all_required_series(async_client, migrated_db):
    """
    Scrape /metrics (unauthenticated, same as every real Prometheus scrape
    -- see apps/api/routers/health.py) and assert every TRD §10 series name
    is present in the exposition text. recoveryos/metrics.py pre-registers
    every known label combination at import time specifically so this is
    true even against a freshly-started process that hasn't yet recorded a
    single real event of each kind.
    """
    resp = await async_client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]

    body = resp.text
    missing = [name for name in REQUIRED_METRIC_NAMES if name not in body]
    assert not missing, f"metric series missing from /metrics: {missing}\n\nFull body:\n{body}"

    # Histogram-specific components must also actually be there, not just
    # the base name matching some unrelated substring.
    assert "diagnosis_latency_seconds_bucket" in body
    assert "diagnosis_latency_seconds_count" in body
    assert "diagnosis_latency_seconds_sum" in body


@pytest.mark.asyncio
async def test_fallback_counter_increments_on_diagnoser_timeout(migrated_db, monkeypatch):
    """
    Reuses Phase 4's real fallback trigger (an empty GEMINI_API_KEY --
    same mechanism test_pipeline_e2e.py::test_pipeline_handles_ai_diagnoser_outage_gracefully
    uses) and asserts ai_diagnoser_fallback_total genuinely increments by
    exactly 1 -- not just that the resulting Diagnosis row has
    is_fallback=True (already proven elsewhere), but that the METRIC
    actually moved.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    from services.diagnosis_engine.diagnoser import diagnose_and_persist

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 100000, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()

    before = REGISTRY.get_sample_value("ai_diagnoser_fallback_total") or 0.0

    diagnosis = await diagnose_and_persist(payment_id)

    assert diagnosis is not None
    assert diagnosis.is_fallback is True, "test setup must genuinely trigger the fallback path"

    after = REGISTRY.get_sample_value("ai_diagnoser_fallback_total")
    assert after == before + 1.0, (
        f"ai_diagnoser_fallback_total must increment by exactly 1 on a real fallback -- "
        f"before={before}, after={after}"
    )

    get_settings.cache_clear()
