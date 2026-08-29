"""
Task AGENT1 -- end-to-end: diagnose_and_persist() actually wires the
investigative loop through and persists diagnosis_hypotheses/
investigation_steps (migration 0015). Gemini itself is mocked (this is a
wiring/persistence test, not a live-network test), but everything else is
real: real diagnoser_role read, real app_role write, real diagnoser_role
tool query mid-investigation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.diagnosis_engine.diagnoser import diagnose, diagnose_and_persist
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_failed_payment(migrated_db: str) -> str:
    """
    Uses a unique bank string per call -- migrated_db is session-scoped
    (same accumulation issue documented in test_chaos_fault_injection.py
    and test_investigation_tools.py). A shared 'HDFC' here previously
    picked up a stale anomaly_windows row left by an unrelated test
    elsewhere in the suite, tripping the CONFLICTING_SIGNALS guard on a
    BANK_DOWN failure_code that had nothing to do with this test.
    """
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    bank = f"TESTBANK_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 150000, 'upi', :bank, 'failed', 'BANK_DOWN', 'SYSTEMIC', "
                "true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "bank": bank},
        )
    await engine.dispose()
    return payment_id


def _mock_investigation_responses():
    async def fake_generate_json(*, system_prompt, user_content, response_schema, model, api_key):
        if "final_hypotheses" in user_content:
            return {
                "selected_cause": "systemic_degradation",
                "confidence_band": "LIKELY",
                "evidence": [
                    {"fact": "cohort failure rate elevated", "source": "get_cohort_failure_rate"}
                ],
            }
        if user_content["round_number"] == 1:
            return {
                "hypotheses": [
                    {
                        "cause": "customer_specific",
                        "support_score": 2,
                        "contradict_score": 1,
                        "unresolved_questions": ["cohort data?"],
                    },
                    {
                        "cause": "systemic_degradation",
                        "support_score": 3,
                        "contradict_score": 1,
                        "unresolved_questions": [],
                    },
                ],
                "action": "call_tool",
                "tool_name": "get_cohort_failure_rate",
                "tool_inputs": {"bank": "HDFC", "method": "upi"},
                "expected_uncertainty_reduction": 7.0,
                "reasoning": "distinguish customer-specific vs systemic",
            }
        return {
            "hypotheses": [
                {
                    "cause": "systemic_degradation",
                    "support_score": 6,
                    "contradict_score": 1,
                    "unresolved_questions": [],
                },
            ],
            "action": "finalize",
            "reasoning": "cohort data confirms systemic",
        }

    return fake_generate_json


@pytest.mark.asyncio
async def test_diagnose_runs_investigator_and_returns_investigation_result(
    migrated_db, monkeypatch
):
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-not-real")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json",
        _mock_investigation_responses(),
    )

    payment_id = await _seed_failed_payment(migrated_db)
    result = await diagnose(payment_id)
    get_settings.cache_clear()

    assert result is not None
    output, investigation = result
    assert investigation is not None
    assert output.root_cause.value == "systemic_degradation"
    assert output.model_version.startswith("investigator-gemini-")
    assert len(investigation.steps) == 1
    assert investigation.steps[0].tool_name == "get_cohort_failure_rate"


@pytest.mark.asyncio
async def test_diagnose_and_persist_writes_hypotheses_and_investigation_steps(
    migrated_db, monkeypatch
):
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-not-real")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json",
        _mock_investigation_responses(),
    )

    payment_id = await _seed_failed_payment(migrated_db)
    diagnosis = await diagnose_and_persist(payment_id)
    get_settings.cache_clear()

    assert diagnosis is not None
    assert diagnosis.root_cause == "systemic_degradation"
    assert diagnosis.confidence_band == "LIKELY"

    import sqlalchemy as sa

    from recoveryos.config import get_settings as _gs

    sync_url = _gs().database_url_sync
    sync_engine = sa.create_engine(sync_url, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        hyp_count = conn.execute(
            text("SELECT count(*) FROM diagnosis_hypotheses WHERE diagnosis_id = :did"),
            {"did": diagnosis.diagnosis_id},
        ).scalar_one()
        step_count = conn.execute(
            text("SELECT count(*) FROM investigation_steps WHERE diagnosis_id = :did"),
            {"did": diagnosis.diagnosis_id},
        ).scalar_one()
        selected = conn.execute(
            text(
                "SELECT cause FROM diagnosis_hypotheses "
                "WHERE diagnosis_id = :did AND is_selected = true"
            ),
            {"did": diagnosis.diagnosis_id},
        ).scalar_one()
        step_row = (
            conn.execute(
                text(
                    "SELECT tool_name, tool_cost, latency_ms, investigation_score "
                    "FROM investigation_steps WHERE diagnosis_id = :did"
                ),
                {"did": diagnosis.diagnosis_id},
            )
            .mappings()
            .first()
        )
    sync_engine.dispose()

    assert hyp_count == 1  # only the final round's hypothesis set is persisted
    assert step_count == 1
    assert selected == "systemic_degradation"
    assert step_row["tool_name"] == "get_cohort_failure_rate"
    assert float(step_row["tool_cost"]) == pytest.approx(1.5)  # real registry constant, not guessed
    assert step_row["latency_ms"] is not None
    assert float(step_row["investigation_score"]) == pytest.approx(7.0 - 1.5 - 0.025)


@pytest.mark.asyncio
async def test_redelivery_does_not_duplicate_investigation_rows(migrated_db, monkeypatch):
    """S1's dedup discipline extended to the new tables: a redelivered
    diagnosis (same payment_id + source_event_id) must not create a second
    set of hypotheses/investigation_steps rows."""
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-not-real")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json",
        _mock_investigation_responses(),
    )

    payment_id = await _seed_failed_payment(migrated_db)
    source_event_id = str(uuid.uuid4())

    diagnosis_1 = await diagnose_and_persist(payment_id, source_event_id=source_event_id)
    diagnosis_2 = await diagnose_and_persist(payment_id, source_event_id=source_event_id)
    get_settings.cache_clear()

    assert diagnosis_1.diagnosis_id == diagnosis_2.diagnosis_id

    import sqlalchemy as sa

    from recoveryos.config import get_settings as _gs

    sync_engine = sa.create_engine(_gs().database_url_sync, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        hyp_count = conn.execute(
            text("SELECT count(*) FROM diagnosis_hypotheses WHERE diagnosis_id = :did"),
            {"did": diagnosis_1.diagnosis_id},
        ).scalar_one()
        step_count = conn.execute(
            text("SELECT count(*) FROM investigation_steps WHERE diagnosis_id = :did"),
            {"did": diagnosis_1.diagnosis_id},
        ).scalar_one()
    sync_engine.dispose()

    assert hyp_count == 1
    assert step_count == 1
