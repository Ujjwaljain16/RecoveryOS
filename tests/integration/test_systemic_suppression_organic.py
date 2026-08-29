"""
Adversarial Audit Verdict, Blocker #8: every existing SystemicSuppressionRule
test (tests/unit/test_policy_engine.py) calls the rule directly against a
hand-built PaymentContext(is_high_severity_anomaly=True) fixture, and
test_decision_e2e.py's own full-pipeline E2E test never forces a real
anomaly window at all -- so there was no evidence the rule ever actually
fires when driven by the REAL detection pipeline (a genuine anomaly_windows
row read through services/recovery_engine/orchestrator.py's
_fetch_anomaly_context) rather than a fixture asserting the code path
exists.

Investigating this with a real end-to-end run (real Postgres, real
propensity model, real EVI, real NBA selection, real 10-rule policy engine)
surfaced a genuine structural finding, RESOLVED as intentional layered
defense rather than a bug -- see the two-part proof below.

Part 1 (empirical, any realistic anomaly): timing.py's probability haircut
during a high-severity anomaly applies ONLY to RETRY_NOW's
recovery_prob_bps -- RETRY_LATER, ALT_ROUTE, REMINDER, ESCALATE, and
DO_NOTHING all keep the UNPENALIZED probability. RETRY_LATER's EVI beats
RETRY_NOW's at every realistic (amount, degradation-ratio) pair tried.

Part 2 (mathematical, the actual reason it's airtight): even at the
degenerate edge case observed_rate == baseline_rate (i.e. the anomaly
detector flagged severity='high' but the bank's success rate isn't
ACTUALLY depressed at all, so timing.py's ratio penalty is a no-op and
RETRY_NOW's recovery_prob_bps is numerically IDENTICAL to RETRY_LATER's),
RETRY_LATER's EVI still exceeds RETRY_NOW's -- by exactly
SYSTEMIC_RISK_PENALTY_PAISE(500) - (RETRY_LATER.friction_base_paise(20) -
RETRY_NOW.friction_base_paise(10)) = 490 paise, flat, independent of
amount_paise (verified at ₹3,000 and ₹5,00,000 -- the gap is identical
because it comes entirely from EVI's own fixed per-attempt terms, which
don't scale with amount the way the probability*amount*margin term does).
Since probability can only ever be lower or equal for RETRY_NOW relative to
RETRY_LATER during a high-severity anomaly (timing.py's penalty is
one-directional, never a boost), and the fixed-cost gap is nonzero in
RETRY_LATER's favor at every probability ratio, RETRY_NOW CANNOT win NBA
selection while is_high_severity_anomaly=True, for any amount or any real
degradation severity. This is not "usually true" -- it's guaranteed by the
current EVI ((SYSTEMIC_RISK_PENALTY_PAISE, friction_base_paise) and timing
(one-directional probability penalty) design taken together.

Resolution: EVI (services/recovery_engine/evi.py) and timing
(services/recovery_engine/timing.py) already jointly implement TRD §3.1's
"bias the argmax away from RETRY_NOW during systemic anomalies"
requirement so completely that SystemicSuppressionRule
(services/policy_engine/rules.py) -- a THIRD, independent mechanism
enforcing the identical policy at the rules layer -- never needs to fire.
That is intentional layered defense (three independent code paths agreeing
"not RETRY_NOW right now"), not dead code: if a future change to EVI's
risk_penalty or timing's probability curve ever weakened either primary
mechanism, SystemicSuppressionRule is what would catch the gap. Removing
it to "clean up dead code" would delete that safety net. Changing EVI/
timing's economics to deliberately make room for it to fire would be
manufacturing a test result, not fixing a bug -- and touches the same
protected economics discipline as the headline number itself. This test
suite is the permanent, mathematical proof of why leaving it alone is
correct, not a placeholder pending a decision.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.recovery_engine.orchestrator import build_decision
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _insert_failed_payment(
    migrated_db: str, merchant_id: str, customer_id: str, *, bank: str, amount_paise: int
) -> str:
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', :bank, 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "bank": bank,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return payment_id


async def _seed_fresh_high_severity_anomaly_window(
    migrated_db: str, *, bank: str, observed_rate: float = 0.42, baseline_rate: float = 0.03
) -> None:
    """The real schema/freshness contract is_cohort_suppressed() reads --
    not a fixture, an actual anomaly_windows row a real detection pass would
    produce. observed_rate/baseline_rate default to a clearly-degraded
    window; pass observed_rate == baseline_rate to construct the degenerate
    edge case where severity='high' but the bank's success rate isn't
    ACTUALLY depressed at all (timing.py's ratio penalty becomes a no-op)."""
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO anomaly_windows (window_id, scope_type, scope_entity, time_bucket, "
                "baseline_rate, observed_rate, z_score, severity, is_anomaly) "
                "VALUES (gen_random_uuid(), 'bank', :bank, :bucket, :br, :orr, 5.8, 'high', true)"
            ),
            {"bank": bank, "bucket": datetime.now(UTC), "br": baseline_rate, "orr": observed_rate},
        )
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount_paise", [50_000, 2_000_000, 2_400_000])
async def test_retry_later_structurally_beats_retry_now_during_a_real_anomaly_at_any_amount(
    migrated_db, amount_paise
):
    """
    Documents Blocker #8's real finding: with a genuine high-severity
    anomaly window (not a fixture) driving a real build_decision() call,
    RETRY_LATER's EVI beats RETRY_NOW's at every amount tried -- amount
    cannot close the gap because both scale identically with it, only
    RETRY_NOW carries the probability haircut + fixed risk penalty.
    SystemicSuppressionRule is provably unreachable via NBA selection as
    currently wired; see this module's docstring for why this is being
    tracked rather than silently patched.
    """
    bank = f"TESTBANK{uuid.uuid4().hex[:6]}"
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(
        migrated_db, merchant_id, customer_id, bank=bank, amount_paise=amount_paise
    )
    await _seed_fresh_high_severity_anomaly_window(migrated_db, bank=bank)

    nba_result, decision, context = await build_decision(payment_id)

    assert context["is_high_severity_anomaly"] is True, (
        "the real anomaly_windows row seeded above must be read back as a high-severity "
        "anomaly by orchestrator._fetch_anomaly_context"
    )

    by_action = {c.action_type: c for c in nba_result.all_candidates}
    retry_now, retry_later = by_action["RETRY_NOW"], by_action["RETRY_LATER"]

    assert retry_now.recovery_prob_bps < retry_later.recovery_prob_bps, (
        "RETRY_NOW must carry a genuine probability haircut relative to RETRY_LATER during a "
        "real high-severity anomaly (timing.py's penalty) -- if this ever fails, the haircut "
        "logic itself changed and SystemicSuppressionRule's reachability should be re-examined"
    )
    assert retry_later.expected_value_paise >= retry_now.expected_value_paise, (
        f"RETRY_LATER's EVI ({retry_later.expected_value_paise}) no longer dominates RETRY_NOW's "
        f"({retry_now.expected_value_paise}) at amount_paise={amount_paise} -- SystemicSuppressionRule "
        f"may now be reachable via real NBA selection; if so, extend this test to prove it actually "
        f"fires and blocks, instead of asserting the dominance that currently prevents it"
    )
    assert nba_result.chosen_action != "RETRY_NOW", (
        "given RETRY_LATER's EVI dominance above, NBA selection must not choose RETRY_NOW here -- "
        "SystemicSuppressionRule never gets a (RETRY_NOW, is_high_severity_anomaly=True) pair to "
        "evaluate against a real candidate set"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("amount_paise", [300_000, 50_000_000])
async def test_retry_later_still_wins_even_when_the_anomaly_causes_zero_probability_penalty(
    migrated_db, amount_paise
):
    """
    The mathematical half of the proof (see module docstring Part 2): the
    degenerate edge case observed_rate == baseline_rate makes timing.py's
    ratio penalty a complete no-op -- RETRY_NOW's recovery_prob_bps comes
    back numerically IDENTICAL to RETRY_LATER's, not merely close. If
    RETRY_LATER can still win here, the dominance isn't a probability-haircut
    artifact that a well-chosen amount or a mild anomaly could someday
    close -- it's EVI's own fixed per-attempt terms (SYSTEMIC_RISK_PENALTY_
    PAISE vs the friction_base_paise difference) doing it alone, unconditionally,
    for every amount and every real degradation severity.
    """
    bank = f"TESTBANK{uuid.uuid4().hex[:6]}"
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = await _insert_failed_payment(
        migrated_db, merchant_id, customer_id, bank=bank, amount_paise=amount_paise
    )
    await _seed_fresh_high_severity_anomaly_window(
        migrated_db, bank=bank, observed_rate=0.03, baseline_rate=0.03
    )

    nba_result, decision, context = await build_decision(payment_id)
    assert context["is_high_severity_anomaly"] is True

    by_action = {c.action_type: c for c in nba_result.all_candidates}
    retry_now, retry_later = by_action["RETRY_NOW"], by_action["RETRY_LATER"]

    assert retry_now.recovery_prob_bps == retry_later.recovery_prob_bps, (
        f"expected timing.py's ratio penalty to be a complete no-op when observed_rate == "
        f"baseline_rate (ratio == 1.0) -- got RETRY_NOW={retry_now.recovery_prob_bps} vs "
        f"RETRY_LATER={retry_later.recovery_prob_bps}; if these differ, timing.py's penalty "
        f"logic changed and this test's premise no longer holds"
    )
    assert retry_later.expected_value_paise > retry_now.expected_value_paise, (
        f"at amount_paise={amount_paise} with IDENTICAL recovery probability for both actions, "
        f"RETRY_LATER's EVI ({retry_later.expected_value_paise}) no longer beats RETRY_NOW's "
        f"({retry_now.expected_value_paise}) -- EVI's fixed risk_penalty/friction terms changed; "
        f"SystemicSuppressionRule may now be reachable and this finding should be re-examined"
    )
    assert nba_result.chosen_action != "RETRY_NOW"
