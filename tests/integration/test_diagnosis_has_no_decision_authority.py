"""
Domain Audit finding F1, deliberately superseded by Phase 11 (Option B, per
this file's own original docstring: "if this is intentional, this test must
be deliberately updated/removed, not silently broken").

F1 originally established: services/recovery_engine/orchestrator.py's
build_decision() never reads anything the AI Diagnoser produces --
deleting services/diagnosis_engine entirely wouldn't change a single
outcome. Phase 11 deliberately changes that, but bounded: build_decision()
now accepts an OPTIONAL diagnosis_id, and only when it's given AND
recoveryos.config.Settings.ai_recommendation_fusion_enabled is True does a
RecoveryRecommendation get fetched and passed through
_apply_ai_fusion() -- which can change chosen_action ONLY via an economic
near-tie that's already independently policy-ALLOWED, or via a closed-set
risk_flags signal a real PolicyRule interprets into ESCALATE. See
services/recovery_engine/ai_fusion.py and _apply_ai_fusion's docstring.

The invariant this file now proves has TWO parts instead of one:

  1. What must STILL be true, unconditionally (the part of F1 that
     survives): the pure argmax in services/recovery_engine/
     next_best_action.py, and the ORIGINAL 10 PolicyRule.check() methods in
     services/policy_engine/rules.py, remain completely AI-blind -- zero
     reference to diagnosis/confidence/recommendation in their source. This
     is what makes it true that "AI can only ever change an outcome via a
     mechanism the deterministic engine has already independently cleared,"
     not "AI can quietly influence the core economics."

  2. What must be true by DEFAULT (the backward-compatible part): calling
     build_decision(payment_id) exactly as every pre-Phase-11 caller does
     (diagnosis_id omitted) reproduces the exact prior behavior, byte-
     identical, regardless of what diagnosis/recommendation rows exist for
     that payment -- fusion never activates unless a caller explicitly
     opts in by passing diagnosis_id AND the feature flag is on.

The POSITIVE case -- proving AI recommendation fusion DOES change outcomes
in the two bounded ways it's designed to -- lives in the sibling file
tests/integration/test_ai_recommendation_bounded_influence.py, not here.
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


def test_select_next_best_action_source_never_references_ai_or_diagnosis():
    """Phase 11 replacement for the old build_decision-level scan (which
    necessarily now references diagnosis/recommendation by name -- see this
    file's module docstring). The invariant that's actually still true and
    load-bearing lives one layer down: the PURE argmax itself
    (select_next_best_action/generate_candidate_actions,
    services/recovery_engine/next_best_action.py) must remain completely
    AI-blind -- Phase 11's fusion happens entirely in orchestrator.py,
    AFTER this pure selection has already run, never inside it."""
    import services.recovery_engine.next_best_action as nba_module

    source = inspect.getsource(nba_module.select_next_best_action) + inspect.getsource(
        nba_module.generate_candidate_actions
    )
    # NOT "confidence" here -- compute_action_confidence()'s own
    # action_confidence is a pre-existing, deterministic EVI-margin
    # heuristic (Task AGENT1), legitimately unrelated to AI/diagnosis
    # confidence, and this module calls it. "diagnos"/"recommend"/"ai_risk"
    # are the actually AI-specific identifiers this scan cares about.
    hits = _source_contains_any(source, ("diagnos", "recommend", "ai_risk"))
    assert not hits, (
        f"select_next_best_action/generate_candidate_actions now reference {hits} -- the pure "
        "argmax must stay AI-blind by construction; fusion belongs in orchestrator.py's "
        "_apply_ai_fusion, never here"
    )


def test_policy_engine_original_rules_never_reference_diagnosis_or_confidence():
    """Same structural proof as before, scoped to the ORIGINAL 10
    PolicyRule classes -- AIRiskSignalEscalationRule (Phase 11) is
    deliberately excluded, named explicitly here rather than silently
    carved out, and covered by its own assertion below instead."""
    import services.policy_engine.rules as rules_module

    original_rule_classes = [
        cls
        for cls in vars(rules_module).values()
        if isinstance(cls, type)
        and issubclass(cls, rules_module.PolicyRule)
        and cls is not rules_module.PolicyRule
        and cls is not rules_module.AIRiskSignalEscalationRule
    ]
    assert len(original_rule_classes) == 10, (
        f"expected exactly the 10 original PolicyRule classes (excluding "
        f"AIRiskSignalEscalationRule), found {len(original_rule_classes)} -- update this test "
        "deliberately if the original rule set itself changed"
    )

    forbidden = {"diagnosis", "diagnoses", "diagnosis_id", "confidence", "root_cause", "recommend"}
    for cls in original_rule_classes:
        tree = ast.parse(inspect.getsource(cls))
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
        hits = identifiers & forbidden
        assert not hits, (
            f"{cls.__name__} now references identifiers {hits} -- if this is intentional, "
            "this test must be deliberately updated"
        )


def test_ai_risk_signal_escalation_rule_is_the_only_ai_aware_rule():
    """The explicit, named counterpart to the exclusion above: exactly ONE
    rule is allowed to reference an AI-related identifier
    (ai_risk_flags) -- AIRiskSignalEscalationRule -- and it's a
    deterministic rule INTERPRETING a bounded signal, not the AI itself
    deciding anything (Phase 11 design doc, invariant 4)."""
    import services.policy_engine.rules as rules_module

    all_rule_classes = [
        cls
        for cls in vars(rules_module).values()
        if isinstance(cls, type)
        and issubclass(cls, rules_module.PolicyRule)
        and cls is not rules_module.PolicyRule
    ]
    ai_aware = [
        cls
        for cls in all_rule_classes
        if "ai_risk" in inspect.getsource(cls).lower()
    ]
    assert [cls.__name__ for cls in ai_aware] == ["AIRiskSignalEscalationRule"]


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
