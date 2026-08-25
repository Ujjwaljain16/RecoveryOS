"""
End-to-end decision test (steering directive §15): a real failed payment
in Postgres -> Phase 2 certified propensity model -> EVI -> 6 candidate
actions -> 7-rule policy engine -> final decision -> persisted
candidate_actions + policy_decisions with full rule_trace. Real Postgres,
real model artifact, zero mocks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from recoveryos.database import get_app_session_factory
from recoveryos.models import CandidateAction, PolicyDecision
from services.recovery_engine.orchestrator import decide_and_persist
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _insert_failed_payment(
    migrated_db: str, merchant_id: str, customer_id: str, *, amount_paise: int = 200_000
) -> str:
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO payments
                    (payment_id, merchant_id, customer_id, amount_paise, method, bank,
                     status, failure_code, failure_class, is_synthetic, created_at, failed_at)
                VALUES
                    (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT',
                     'TEMPORARY', true, :ts, :ts)
                """
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "ts": datetime.now(timezone.utc) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return payment_id


@pytest.mark.asyncio
async def test_end_to_end_decision_persists_full_audit_trail(migrated_db):
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(migrated_db, merchant_id, customer_id)

    result = await decide_and_persist(payment_id)

    print(f"\n[e2e decision] payment_id={payment_id} chosen_action={result['chosen_action']} "
          f"evi={result['chosen_evi_paise']} verdict={result['verdict']} "
          f"prob_bps={result['propensity_probability_bps']}")

    assert result["chosen_action"] in {
        "RETRY_NOW", "RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE", "DO_NOTHING"
    }
    assert result["verdict"] in {"ALLOW", "BLOCK", "ESCALATE"}
    assert len(result["candidate_ids"]) == 6

    async with get_app_session_factory()() as session:
        candidate_rows = (
            await session.execute(
                CandidateAction.__table__.select().where(CandidateAction.payment_id == payment_id)
            )
        ).fetchall()
        decision_rows = (
            await session.execute(
                PolicyDecision.__table__.select().where(PolicyDecision.payment_id == payment_id)
            )
        ).fetchall()

    assert len(candidate_rows) == 6, "all 6 candidate actions must be persisted, not just the chosen one"
    assert len(decision_rows) == 1
    decision_row = decision_rows[0]
    assert decision_row.rule_trace, "rule_trace must be non-empty JSON"
    assert decision_row.verdict == result["verdict"]

    # Every persisted candidate carries real model lineage — not a placeholder.
    from services.recovery_engine.propensity import MODEL_VERSION

    for row in candidate_rows:
        assert row.model_version == MODEL_VERSION


@pytest.mark.asyncio
async def test_decision_is_deterministic_for_the_same_payment(migrated_db):
    """Re-running the pipeline against the SAME persisted DB state must
    produce the same chosen_action and verdict (aside from a fresh
    decision_id/candidate_ids each call, since this isn't idempotent
    persistence — that's Phase 6's concern, not Phase 5's)."""
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(migrated_db, merchant_id, customer_id, amount_paise=500_000)

    from services.recovery_engine.orchestrator import build_decision

    nba1, decision1, _ = await build_decision(payment_id)
    nba2, decision2, _ = await build_decision(payment_id)

    assert nba1.chosen_action == nba2.chosen_action
    assert nba1.chosen_evi_paise == nba2.chosen_evi_paise
    assert decision1.verdict == decision2.verdict
