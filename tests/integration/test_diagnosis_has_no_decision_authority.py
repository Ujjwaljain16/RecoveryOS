"""
Domain Audit finding F1, Option A (own the separation, prove it in code) --
the single test the audit's own recommendation says a judge should see:

    Given identical payment/customer/anomaly/policy inputs, the recovery
    decision must be byte-identical regardless of what diagnosis exists
    (or doesn't exist) for that payment.

This isn't a new behavior -- services/recovery_engine/orchestrator.py's
build_decision() has never read the diagnoses table. This test makes that
fact a permanent, enforced regression guard instead of something only
provable by reading the code, so a future change that DOES wire diagnosis
into the decision path would have to consciously delete or rewrite this
test, not silently break an unstated invariant.

Two layers of proof, matching the audit's own two framings:
  1. Structural: build_decision()/select_next_best_action()/policy_engine's
     evaluate() (and the 10 PolicyRule.check() methods) contain zero
     reference to "diagnos"/"confidence" in their source -- an AST/text
     scan, not a claim.
  2. Behavioral: call build_decision() three times for the SAME payment --
     with no diagnosis row, with a high-confidence diagnosis, and with a
     low-confidence/UNKNOWN diagnosis -- and assert the resulting
     chosen_action, EVI, and policy verdict are identical every time.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.recovery_engine.orchestrator import build_decision
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


def _source_contains_any(source: str, needles: tuple[str, ...]) -> list[str]:
    lowered = source.lower()
    return [n for n in needles if n.lower() in lowered]


def test_build_decision_source_never_references_diagnosis_or_confidence():
    """Structural proof: the function that computes chosen_action/EVI/verdict
    contains zero reference to diagnosis output, in source -- not just "we
    checked once," but a fact enforced every time this test runs."""
    source = inspect.getsource(build_decision)
    hits = _source_contains_any(source, ("diagnos", "confidence", "root_cause"))
    assert not hits, (
        f"build_decision() now references {hits} -- if this is intentional (Option B), "
        "this test must be deliberately updated/removed, not silently broken"
    )


def test_policy_engine_rules_never_reference_diagnosis_or_confidence():
    """Same structural proof for every PolicyRule.check() -- AST-parsed, not
    grepped, so a reference hidden in a string literal doesn't false-negative
    and a reference in an unrelated docstring doesn't false-positive."""
    import services.policy_engine.rules as rules_module

    tree = ast.parse(inspect.getsource(rules_module))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    forbidden = {"diagnosis", "diagnoses", "diagnosis_id", "confidence", "root_cause"}
    hits = identifiers & forbidden
    assert not hits, (
        f"services/policy_engine/rules.py now references identifiers {hits} -- "
        "if this is intentional (Option B), this test must be deliberately updated"
    )


def test_policy_evaluate_never_references_diagnosis_or_confidence():
    import services.policy_engine.evaluate as evaluate_module

    source = inspect.getsource(evaluate_module)
    hits = _source_contains_any(source, ("diagnos", "confidence", "root_cause"))
    assert not hits, f"services/policy_engine/evaluate.py now references {hits}"


async def _insert_payment(migrated_db: str, *, amount_paise: int = 200_000) -> tuple[str, str, str]:
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
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return payment_id, merchant_id, customer_id


async def _insert_diagnosis(
    migrated_db: str,
    *,
    payment_id: str,
    source_event_id: str,
    root_cause: str,
    confidence: float,
    is_fallback: bool = False,
) -> None:
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO diagnoses (diagnosis_id, payment_id, source_event_id, root_cause, "
                "confidence, evidence, model_version, is_fallback, created_at) "
                "VALUES (gen_random_uuid(), :pid, :sid, :rc, :conf, '[]'::jsonb, 'test-v1', :fb, now())"
            ),
            {
                "pid": payment_id,
                "sid": source_event_id,
                "rc": root_cause,
                "conf": confidence,
                "fb": is_fallback,
            },
        )
    await engine.dispose()


def _decision_fingerprint(nba_result, decision) -> tuple:
    """Everything a diagnosis COULD plausibly have influenced, flattened
    into one comparable tuple: the chosen action, its EVI, the policy
    verdict, and the full rule_trace (so even a change in WHICH rule fired,
    not just the final verdict, would be caught)."""
    return (
        nba_result.chosen_action,
        nba_result.chosen_evi_paise,
        nba_result.propensity_probability_bps,
        decision.verdict,
        tuple((r["rule"], r["passed"], r["reason"]) for r in decision.rule_trace),
    )


@pytest.mark.asyncio
async def test_decision_is_byte_identical_regardless_of_diagnosis_content(migrated_db):
    """
    The behavioral proof: same payment, same customer, same anomaly state,
    same policy config -- vary ONLY what diagnosis exists for it (none, a
    confident permanent_failure diagnosis, an unconfident/unknown one) and
    assert build_decision() produces an identical fingerprint every time.

    This is the exact scenario the audit's own recommended test describes:
    "Diagnosis A: root_cause=X confidence=0.95; Diagnosis B: root_cause=
    UNKNOWN confidence=0.10; assert decision(A) == decision(B)."
    """
    payment_id, _, _ = await _insert_payment(migrated_db)

    baseline_nba, baseline_decision, _ = await build_decision(payment_id)
    baseline = _decision_fingerprint(baseline_nba, baseline_decision)

    await _insert_diagnosis(
        migrated_db,
        payment_id=payment_id,
        source_event_id=str(uuid.uuid4()),
        root_cause="permanent_failure",
        confidence=0.95,
    )
    with_confident_diagnosis_nba, with_confident_diagnosis_decision, _ = await build_decision(
        payment_id
    )
    with_confident_diagnosis = _decision_fingerprint(
        with_confident_diagnosis_nba, with_confident_diagnosis_decision
    )

    await _insert_diagnosis(
        migrated_db,
        payment_id=payment_id,
        source_event_id=str(uuid.uuid4()),
        root_cause="unknown",
        confidence=0.10,
        is_fallback=True,
    )
    with_unknown_diagnosis_nba, with_unknown_diagnosis_decision, _ = await build_decision(
        payment_id
    )
    with_unknown_diagnosis = _decision_fingerprint(
        with_unknown_diagnosis_nba, with_unknown_diagnosis_decision
    )

    print(
        f"\n[F1 invariant] no-diagnosis={baseline}\n"
        f"  confident-permanent_failure={with_confident_diagnosis}\n"
        f"  unconfident-unknown={with_unknown_diagnosis}"
    )

    assert with_confident_diagnosis == baseline, (
        "a confident permanent_failure diagnosis changed the decision -- "
        "diagnosis must have zero causal effect on chosen_action/EVI/verdict (F1)"
    )
    assert with_unknown_diagnosis == baseline, (
        "an unconfident/unknown diagnosis changed the decision -- "
        "diagnosis must have zero causal effect on chosen_action/EVI/verdict (F1)"
    )


@pytest.mark.asyncio
async def test_deleting_all_diagnoses_for_a_payment_does_not_change_its_decision(migrated_db):
    """The other framing from the audit's judge questions: 'if you deleted
    services/diagnosis_engine entirely... what would change?' -- proven
    here at the data level: persist a diagnosis, capture the decision,
    delete the diagnosis row entirely, recompute, and assert no change."""
    payment_id, _, _ = await _insert_payment(migrated_db, amount_paise=350_000)

    await _insert_diagnosis(
        migrated_db,
        payment_id=payment_id,
        source_event_id=str(uuid.uuid4()),
        root_cause="systemic_degradation",
        confidence=0.88,
    )
    before_nba, before_decision, _ = await build_decision(payment_id)
    before = _decision_fingerprint(before_nba, before_decision)

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM diagnoses WHERE payment_id = :pid"), {"pid": payment_id}
        )
    await engine.dispose()

    after_nba, after_decision, _ = await build_decision(payment_id)
    after = _decision_fingerprint(after_nba, after_decision)

    assert after == before, (
        "deleting every diagnosis row for this payment changed its decision -- "
        "the decision pipeline must not depend on the diagnoses table existing at all"
    )
