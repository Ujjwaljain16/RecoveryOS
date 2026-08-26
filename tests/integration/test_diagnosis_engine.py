"""
Integration tests for services/diagnosis_engine/ — TRD §4.2/§9, gaps.md §A.3.

Real Postgres (testcontainers), real diagnoser/app DB role credentials (see
tests/integration/conftest.py:diagnoser_database_url) — role-boundary tests
connect with the actual login users the migration creates, not SET ROLE from
a superuser, so a permission failure here means the DB grant itself is
missing, not just that this test forgot to switch roles.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from recoveryos.database import get_app_session_factory, get_diagnoser_session_factory
from services.diagnosis_engine import llm_diagnoser as llm_diagnoser_module
from services.diagnosis_engine.diagnoser import diagnose, persist_diagnosis
from services.risk_engine.anomaly import AnomalyResult, derive_cohort_id, persist_anomaly_window
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _insert_payment(
    migrated_db: str,
    merchant_id: str,
    customer_id: str,
    *,
    bank: str | None = "HDFC",
    failure_code: str | None = "TIMEOUT",
    status: str = "failed",
) -> str:
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO payments
                    (payment_id, merchant_id, customer_id, amount_paise, method, bank,
                     status, failure_code, is_synthetic, created_at, failed_at)
                VALUES
                    (:pid, :mid, :cid, 100000, 'upi', :bank, :status, :fcode, true, now(), now())
                """
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "bank": bank,
                "status": status,
                "fcode": failure_code,
            },
        )
    await engine.dispose()
    return payment_id


@pytest.mark.asyncio
async def test_diagnoser_role_has_no_write_access(migrated_db):
    """
    Stop condition: diagnoser_role must have ZERO write access to ANY table —
    verified by connecting with the real 'diagnoser' login user's own
    credentials (not a superuser SET ROLE) and attempting real INSERTs.
    """
    diagnoser_session_factory = get_diagnoser_session_factory()

    # Sanity check first: the connection itself must work and be able to
    # read (otherwise a "permission denied" below could just mean "the
    # connection never even worked").
    async with diagnoser_session_factory() as session:
        result = await session.execute(text("SELECT count(*) FROM payments"))
        result.scalar()  # must not raise

    write_attempts = [
        (
            "diagnoses",
            "INSERT INTO diagnoses (diagnosis_id, root_cause, confidence, evidence, "
            "model_version, is_fallback) VALUES (gen_random_uuid(), 'unknown', 0.1, "
            "'[]'::jsonb, 'test', false)",
        ),
        (
            "anomaly_windows",
            "INSERT INTO anomaly_windows (window_id, scope_type, scope_entity, time_bucket, "
            "severity, is_anomaly) VALUES (gen_random_uuid(), 'bank', 'HACKBANK', now(), "
            "'high', true)",
        ),
        (
            "payments",
            "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
            "method, status) VALUES (gen_random_uuid(), gen_random_uuid(), "
            "gen_random_uuid(), 100, 'upi', 'created')",
        ),
    ]

    denied = []
    for table_name, sql in write_attempts:
        async with diagnoser_session_factory() as session:
            try:
                await session.execute(text(sql))
                await session.commit()
            except DBAPIError as exc:
                denied.append(table_name)
                assert (
                    "permission denied" in str(exc).lower()
                ), f"expected a permission-denied error writing to {table_name}, got: {exc}"
            else:
                pytest.fail(
                    f"diagnoser_role was able to INSERT into {table_name} — no write boundary"
                )

    assert denied == [t for t, _ in write_attempts]


@pytest.mark.asyncio
async def test_diagnoser_timeout_falls_back_to_deterministic_rule(migrated_db, monkeypatch):
    """
    Forces a REAL asyncio.wait_for timeout (the LLM call coroutine genuinely
    sleeps longer than settings.ai_diagnoser_timeout_seconds — this is not a
    simulated/mocked timeout signal, wait_for really fires) and confirms the
    full diagnose() pipeline falls back to the deterministic rule table with
    the correct evidence trail, exactly as TRD §4.2's state machine specifies
    (DIAGNOSING --(AI timeout)--> FALLBACK_DIAGNOSIS).
    """
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "openai")  # pin regardless of .env's default
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("AI_DIAGNOSER_TIMEOUT_SECONDS", "0.2")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    async def _hanging_call_llm(diagnosis_input, model, api_key):
        await asyncio.sleep(5.0)  # much longer than the 0.2s timeout above
        raise AssertionError("should never complete — the timeout must fire first")

    monkeypatch.setattr(llm_diagnoser_module, "_call_llm_openai", _hanging_call_llm)

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_payment(
        migrated_db, merchant_id, customer_id, bank="HDFC", failure_code="TIMEOUT"
    )

    output = await diagnose(payment_id)

    print(f"\n[test_diagnoser_timeout_falls_back] output={output!r}")

    assert output is not None
    assert output.is_fallback is True
    assert output.model_version == "fallback-rule-v1"
    assert output.confidence <= 0.6
    assert any(
        "ai_diagnoser_timeout" in e.fact for e in output.evidence
    ), f"expected fallback evidence to name the timeout reason, got: {output.evidence}"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_systemic_degradation_produces_cohort_diagnosis(migrated_db, monkeypatch):
    """
    End-to-end: a persisted high-severity anomaly window for a bank, plus a
    failed payment on that bank, must produce a diagnosis whose root_cause is
    overridden to systemic_degradation with a cohort_id matching an
    independently-computed derive_cohort_id() call — proving the orchestrator's
    uniform cohort-attachment step (diagnoser.py:_attach_cohort_if_systemic)
    actually runs against real persisted data, not just in the unit-level
    manual check done during development.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    bank = f"COHORTBANK-{uuid.uuid4().hex[:8]}"
    time_bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    anomaly_result = AnomalyResult(
        scope_type="bank",
        scope_entity=bank,
        time_bucket=time_bucket,
        baseline_rate=0.03,
        observed_rate=0.18,
        z_score=7.5,
        severity="high",
        is_anomaly=True,
        sample_size=50,
        cohort_id=derive_cohort_id("bank", bank, time_bucket),
    )
    async with get_app_session_factory()() as app_session:
        await persist_anomaly_window(app_session, anomaly_result)

    payment_id = await _insert_payment(
        migrated_db, merchant_id, customer_id, bank=bank, failure_code="TIMEOUT"
    )

    output = await diagnose(payment_id)

    print(f"\n[test_systemic_degradation_produces_cohort_diagnosis] output={output!r}")

    assert output is not None
    assert output.root_cause.value == "systemic_degradation"
    expected_cohort_id = derive_cohort_id("bank", bank, time_bucket)
    assert output.cohort_id == expected_cohort_id

    async with get_app_session_factory()() as app_session:
        diagnosis = await persist_diagnosis(app_session, payment_id, output)
    assert diagnosis.cohort_id == expected_cohort_id

    get_settings.cache_clear()
