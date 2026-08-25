"""
Negative-control test for services/recovery_engine/next_best_action.py +
timing.py, real Postgres: RETRY_LATER must beat RETRY_NOW during a
high-severity systemic anomaly with cost/friction forced EQUAL between the
two — so a win can only be coming from the probability adjustment itself,
not from a cost/friction difference in the test fixture (same negative-
control discipline as Task 3's concurrency proof).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from services.recovery_engine.next_best_action import generate_candidate_actions, select_next_best_action
from services.recovery_engine.timing import AnomalyContext
from tests.integration.conftest import to_async_url


@pytest.mark.asyncio
async def test_retry_later_beats_retry_now_on_probability_alone_during_high_anomaly(migrated_db):
    engine = create_async_engine(to_async_url(migrated_db))
    merchant_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, 'negctrl-merchant')"),
            {"mid": merchant_id},
        )
        # Force RETRY_NOW and RETRY_LATER to IDENTICAL cost/friction — any
        # win for RETRY_LATER can only come from the probability adjustment.
        for action_type in ("RETRY_NOW", "RETRY_LATER"):
            await conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                    "VALUES (:mid, :action_type, 100, 10)"
                ),
                {"mid": merchant_id, "action_type": action_type},
            )
        # Make every OTHER action deliberately uneconomical for this
        # merchant, so the overall selection isolates the RETRY_NOW vs
        # RETRY_LATER comparison specifically rather than being decided by
        # some unrelated action's unrelated cost structure.
        for action_type in ("ALT_ROUTE", "REMINDER", "ESCALATE"):
            await conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                    "VALUES (:mid, :action_type, 10000000, 0)"
                ),
                {"mid": merchant_id, "action_type": action_type},
            )

    high_anomaly = AnomalyContext(severity="high", is_anomaly=True, observed_rate=0.30, baseline_rate=0.03)

    async with AsyncSession(engine) as session:
        candidates = await generate_candidate_actions(
            session,
            merchant_id=merchant_id,
            amount_paise=500_000,
            customer_is_returning=True,
            base_propensity_prob_bps=8200,
            anomaly_context=high_anomaly,
        )

    by_action = {c.action_type: c for c in candidates}
    retry_now = by_action["RETRY_NOW"]
    retry_later = by_action["RETRY_LATER"]

    # Prove the fixture is a genuine negative control: cost/friction equal.
    assert retry_now.cost_paise == retry_later.cost_paise == 100
    assert retry_now.friction_penalty_paise == retry_later.friction_penalty_paise == 10

    # The ONLY thing that can differ is the timing-adjusted probability.
    assert retry_later.recovery_prob_bps > retry_now.recovery_prob_bps
    assert retry_later.expected_value_paise > retry_now.expected_value_paise

    result = select_next_best_action(candidates, min_expected_value_paise=0, propensity_probability_bps=8200)
    assert result.chosen_action == "RETRY_LATER"

    print(
        f"\n[negative control] RETRY_NOW prob_bps={retry_now.recovery_prob_bps} "
        f"evi={retry_now.expected_value_paise} | RETRY_LATER prob_bps={retry_later.recovery_prob_bps} "
        f"evi={retry_later.expected_value_paise} | chosen={result.chosen_action}"
    )

    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_later_does_not_beat_retry_now_absent_an_anomaly(migrated_db):
    """
    Same equal-cost fixture, no anomaly context at all -> RETRY_LATER must
    NOT beat RETRY_NOW on probability (both get the unadjusted base
    propensity) — proves the mechanism is narrowly scoped to the systemic
    case, not a blanket 'later is always better' bias.
    """
    engine = create_async_engine(to_async_url(migrated_db))
    merchant_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, 'negctrl-merchant-2')"),
            {"mid": merchant_id},
        )
        for action_type in ("RETRY_NOW", "RETRY_LATER"):
            await conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                    "VALUES (:mid, :action_type, 100, 10)"
                ),
                {"mid": merchant_id, "action_type": action_type},
            )

    async with AsyncSession(engine) as session:
        candidates = await generate_candidate_actions(
            session,
            merchant_id=merchant_id,
            amount_paise=500_000,
            customer_is_returning=True,
            base_propensity_prob_bps=8200,
            anomaly_context=None,
        )

    by_action = {c.action_type: c for c in candidates}
    assert by_action["RETRY_NOW"].recovery_prob_bps == by_action["RETRY_LATER"].recovery_prob_bps
    assert by_action["RETRY_NOW"].expected_value_paise == by_action["RETRY_LATER"].expected_value_paise

    await engine.dispose()
