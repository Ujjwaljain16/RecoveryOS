"""
End-to-end decision test (steering directive §15): a real failed payment
in Postgres -> Phase 2 certified propensity model -> EVI -> 6 candidate
actions -> 10-rule policy engine (7 original + 3 real regulatory
compliance rules, Task COMPLIANCE1) -> final decision -> persisted
candidate_actions + policy_decisions with full rule_trace. Real Postgres,
real model artifact, zero mocks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from recoveryos.database import get_app_session_factory
from recoveryos.models import CandidateAction, PolicyDecision
from services.recovery_engine.orchestrator import decide_and_persist
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _insert_failed_payment(
    migrated_db: str,
    merchant_id: str,
    customer_id: str,
    *,
    amount_paise: int = 200_000,
    is_synthetic: bool = True,
    failed_at: datetime | None = None,
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
                     'TEMPORARY', :is_synthetic, :ts, :ts)
                """
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "is_synthetic": is_synthetic,
                "ts": failed_at if failed_at is not None else datetime.now(UTC) - timedelta(hours=1),
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

    print(
        f"\n[e2e decision] payment_id={payment_id} chosen_action={result['chosen_action']} "
        f"evi={result['chosen_evi_paise']} verdict={result['verdict']} "
        f"prob_bps={result['propensity_probability_bps']}"
    )

    assert result["chosen_action"] in {
        "RETRY_NOW",
        "RETRY_LATER",
        "ALT_ROUTE",
        "REMINDER",
        "ESCALATE",
        "DO_NOTHING",
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

    assert (
        len(candidate_rows) == 6
    ), "all 6 candidate actions must be persisted, not just the chosen one"
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
    payment_id = await _insert_failed_payment(
        migrated_db, merchant_id, customer_id, amount_paise=500_000
    )

    from services.recovery_engine.orchestrator import build_decision

    nba1, decision1, _ = await build_decision(payment_id)
    nba2, decision2, _ = await build_decision(payment_id)

    assert nba1.chosen_action == nba2.chosen_action
    assert nba1.chosen_evi_paise == nba2.chosen_evi_paise
    assert decision1.verdict == decision2.verdict


@pytest.mark.asyncio
async def test_real_pipeline_blocks_retry_now_during_npci_peak_window_for_production_traffic(
    migrated_db, monkeypatch
):
    """
    Production safety (gaps.md sec:C.4): Task COMPLIANCE1's
    AutopayExecutionWindowRule, proven through the REAL
    orchestrator.build_decision() path (real Postgres, real propensity
    model, real EVI) for a NON-synthetic (is_synthetic=False, i.e. real
    production traffic) payment -- confirms real traffic still gets
    real-clock-based execution-window enforcement, wired through
    resolve_decision_now() -> PaymentContext.now, exactly as before the
    synthetic-payment fix below. A real payment has no simulated moment to
    defer to, so it must always be evaluated against the actual current
    time, peak-hour blocking included.
    """
    import recoveryos.clock as clock_module

    peak_ist_11am = datetime(
        2026, 8, 25, 5, 30, 0, tzinfo=UTC
    )  # 11:00 IST -- inside NPCI's peak window
    monkeypatch.setattr(clock_module, "utcnow", lambda: peak_ist_11am)

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(
        migrated_db, merchant_id, customer_id, is_synthetic=False
    )

    from services.recovery_engine.orchestrator import build_decision

    nba_result, decision, _ = await build_decision(payment_id)

    if nba_result.chosen_action != "RETRY_NOW":
        pytest.skip(
            f"chosen_action={nba_result.chosen_action!r}, not RETRY_NOW -- the execution-window "
            f"rule only applies to RETRY_NOW, nothing to assert for this payment's EVI scores"
        )
    assert decision.verdict == "BLOCK"
    assert decision.rule_trace[-1]["rule"] == "AutopayExecutionWindowRule"


@pytest.mark.asyncio
async def test_synthetic_payment_outside_peak_window_not_blocked_despite_real_clock_in_peak(
    migrated_db, monkeypatch
):
    """
    The exact discovered bug, as a regression test (gaps.md sec:C.4): a
    synthetic payment whose SIMULATED decision time (failed_at) is OUTSIDE
    the NPCI peak window must NOT be blocked by AutopayExecutionWindowRule,
    even when the REAL wall clock happens to be INSIDE the peak window --
    this is precisely what went wrong in the live canonical-run evaluation
    (93% of one seed's BLOCKs traced to this rule, purely because the real
    clock was inside peak hours while the run executed, independent of the
    simulated scenario).
    """
    import recoveryos.clock as clock_module

    real_clock_in_peak = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)  # 11:00 IST -- peak
    monkeypatch.setattr(clock_module, "utcnow", lambda: real_clock_in_peak)

    simulated_time_outside_peak = datetime(2026, 8, 25, 4, 0, 0, tzinfo=UTC)  # 09:30 IST -- clear

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(
        migrated_db,
        merchant_id,
        customer_id,
        is_synthetic=True,
        failed_at=simulated_time_outside_peak,
    )

    from services.recovery_engine.orchestrator import build_decision

    nba_result, decision, _ = await build_decision(payment_id)

    if nba_result.chosen_action != "RETRY_NOW":
        pytest.skip(
            f"chosen_action={nba_result.chosen_action!r}, not RETRY_NOW -- the execution-window "
            f"rule only applies to RETRY_NOW, nothing to assert for this payment's EVI scores"
        )
    assert decision.rule_trace[-1]["rule"] != "AutopayExecutionWindowRule", (
        "blocked by the execution-window rule despite the SIMULATED decision time being "
        "outside the peak window -- the real clock leaked into a synthetic payment's decision"
    )


@pytest.mark.asyncio
async def test_synthetic_payment_inside_peak_window_still_blocked_despite_real_clock_outside_peak(
    migrated_db, monkeypatch
):
    """The inverse of the regression test above: a synthetic payment whose
    SIMULATED decision time IS inside the NPCI peak window must still be
    correctly blocked, even when the real wall clock happens to be outside
    it -- proving the fix didn't just make the rule inert for synthetic
    payments, it made the rule see the right clock."""
    import recoveryos.clock as clock_module

    real_clock_outside_peak = datetime(2026, 8, 25, 4, 0, 0, tzinfo=UTC)  # 09:30 IST -- clear
    monkeypatch.setattr(clock_module, "utcnow", lambda: real_clock_outside_peak)

    simulated_time_in_peak = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)  # 11:00 IST -- peak

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(
        migrated_db,
        merchant_id,
        customer_id,
        is_synthetic=True,
        failed_at=simulated_time_in_peak,
    )

    from services.recovery_engine.orchestrator import build_decision

    nba_result, decision, _ = await build_decision(payment_id)

    if nba_result.chosen_action != "RETRY_NOW":
        pytest.skip(
            f"chosen_action={nba_result.chosen_action!r}, not RETRY_NOW -- the execution-window "
            f"rule only applies to RETRY_NOW, nothing to assert for this payment's EVI scores"
        )
    assert decision.verdict == "BLOCK"
    assert decision.rule_trace[-1]["rule"] == "AutopayExecutionWindowRule"


@pytest.mark.asyncio
async def test_synthetic_payment_decision_is_identical_regardless_of_real_execution_time(
    migrated_db, monkeypatch
):
    """
    Determinism proof: the SAME synthetic payment (same seed data, same
    simulated failed_at, same model, same policies), decided at two
    DIFFERENT real wall-clock times, must produce byte-identical policy
    outcomes. Before the fix, this would have non-deterministically flipped
    depending on which of the two real times fell inside an NPCI peak
    window -- exactly the "depends on when you happen to run it" hazard
    this checkpoint exists to eliminate.
    """
    import recoveryos.clock as clock_module

    simulated_time_in_peak = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)  # 11:00 IST -- peak

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(
        migrated_db,
        merchant_id,
        customer_id,
        is_synthetic=True,
        failed_at=simulated_time_in_peak,
    )

    from services.recovery_engine.orchestrator import build_decision

    monkeypatch.setattr(
        clock_module, "utcnow", lambda: datetime(2026, 9, 1, 8, 0, 0, tzinfo=UTC)
    )
    nba_1, decision_1, _ = await build_decision(payment_id)

    monkeypatch.setattr(
        clock_module, "utcnow", lambda: datetime(2026, 9, 15, 20, 30, 0, tzinfo=UTC)
    )
    nba_2, decision_2, _ = await build_decision(payment_id)

    assert nba_1.chosen_action == nba_2.chosen_action
    assert nba_1.chosen_evi_paise == nba_2.chosen_evi_paise
    assert decision_1.verdict == decision_2.verdict
    assert decision_1.rule_trace == decision_2.rule_trace
