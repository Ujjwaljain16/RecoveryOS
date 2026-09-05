"""
Decision orchestrator — the ONLY place in the recovery decision pipeline
that does I/O across all of propensity, EVI, next-best-action, and
policy_engine. Every function in those modules is pure/testable in
isolation; this module wires them together against real data:

    live payment
        -> inference_role read (propensity features)
        -> the certified recovery-propensity logistic regression
           (P(recover) — see propensity.py's docstring for why LR, not
           LightGBM, is correct)
        -> app_role read (anomaly context, retry history, policy config)
        -> EVI-scored candidate actions (6)
        -> next-best-action selection (pure argmax, always AI-blind)
        -> policy_engine.evaluate() (pure, on the chosen candidate)
        -> bounded AI-recommendation fusion (_apply_ai_fusion), gated
           behind Settings.ai_recommendation_fusion_enabled -- can change
           chosen_action ONLY via an economic near-tie already
           independently policy-ALLOWED, or via a closed-set risk_flags
           signal a real PolicyRule (AIRiskSignalEscalationRule) interprets
           into ESCALATE. See _apply_ai_fusion's docstring for the full
           boundary; off (the default) reproduces the pre-AI-fusion
           behavior exactly.
        -> persist ALL 6 candidate_actions rows + ONE policy_decision row
           with full rule_trace + ONE decision_fusion_trace row when fusion
           ran

Model lineage: every persisted CandidateAction row carries model_version +
feature_schema_version from the propensity prediction, so any decision_id
is traceable back to the exact certified propensity-model artifact that
produced it.
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos import clock
from recoveryos.config import get_settings
from recoveryos.database import (
    get_app_session_factory,
    get_inference_session_factory,
)
from recoveryos.metrics import (
    ai_outcome_delta_total,
    ai_recommendation_available_total,
    ai_risk_escalations_total,
    ai_tie_break_applied_total,
    ai_tie_break_rejected_total,
    policy_blocks_total,
)
from recoveryos.models import (
    CandidateAction,
    DecisionFusionTrace,
    Merchant,
    PolicyConfig,
    PolicyDecision,
)
from services.policy_engine.evaluate import PolicyDecision as PolicyDecisionResult
from services.policy_engine.evaluate import evaluate
from services.policy_engine.rules import CandidateContext, PaymentContext, PolicyConfigContext
from services.recovery_engine.ai_fusion import find_near_tied_candidates
from services.recovery_engine.next_best_action import (
    CandidateActionResult,
    NextBestActionResult,
    compute_action_confidence,
    generate_candidate_actions,
    select_next_best_action,
)
from services.recovery_engine.propensity import (
    build_propensity_context,
    predict_recovery_probability,
)
from services.recovery_engine.timing import AnomalyContext, compute_retry_delay
from services.risk_engine.anomaly import is_cohort_suppressed

# inference_role's allow-listed payment columns (matches
# migrations/0008_inference_role.py's grant exactly).
_PAYMENT_SAFE_COLUMNS = (
    "payment_id, merchant_id, customer_id, amount_paise, method, bank, "
    "status, failure_code, failure_class, is_synthetic, created_at, failed_at"
)

# Fixed, well-known id for the platform-default policy config — mirrors
# action_costs' merchant_id-IS-NULL sentinel pattern, but policy_configs has
# no merchant_id column, so a deterministic id is the equivalent mechanism.
PLATFORM_DEFAULT_POLICY_CONFIG_ID = "00000000-0000-0000-0000-000000000001"


async def _get_or_create_default_policy_config(session: AsyncSession) -> PolicyConfig:
    row = await session.get(PolicyConfig, PLATFORM_DEFAULT_POLICY_CONFIG_ID)
    if row is not None:
        return row
    row = PolicyConfig(policy_config_id=PLATFORM_DEFAULT_POLICY_CONFIG_ID)
    session.add(row)
    await session.commit()
    return row


async def _resolve_policy_config(session: AsyncSession, merchant_id: str) -> PolicyConfig:
    merchant = await session.get(Merchant, merchant_id)
    if merchant is not None and merchant.policy_config_id is not None:
        config = await session.get(PolicyConfig, merchant.policy_config_id)
        if config is not None:
            return config
    return await _get_or_create_default_policy_config(session)


async def _fetch_anomaly_context(session: AsyncSession, bank: str | None) -> AnomalyContext | None:
    """
    Whether a FRESH, active, high-severity anomaly currently applies to this
    bank — via is_cohort_suppressed() (services/risk_engine/anomaly.py),
    the freshness-bounded helper built specifically to feed this decision
    (Task S3, pre-Phase-8 audit; previously dead code with zero callers).

    Every downstream consumer of the returned AnomalyContext
    (services/policy_engine/rules.py:SystemicSuppressionRule,
    services/recovery_engine/timing.py, services/recovery_engine/evi.py's
    risk_penalty_paise) only ever branches on severity == "high" and
    is_anomaly — a "low"/"medium"/"insufficient_data" reading has
    identical effect to None everywhere it's consumed, so returning None
    whenever there's no fresh HIGH-severity window is exactly equivalent,
    not an approximation.

    The previous version took the single most-recent anomaly_windows row
    for this bank with NO freshness bound at all — a high-severity window
    computed hours ago (anomaly detection is documented as "a single
    callable batch pass, not a standing service", i.e. not guaranteed to
    re-run on any schedule) would still read as "currently anomalous" even
    long after the underlying condition resolved, incorrectly suppressing
    RETRY_NOW based on stale data.
    """
    if bank is None:
        return None

    suppression = await is_cohort_suppressed(session, bank=bank)
    if suppression is None:
        return None

    # is_cohort_suppressed() only returns the fields needed to know THAT a
    # window is fresh/active; observed_rate/baseline_rate (needed for
    # timing.py's probability-penalty ratio) come from that exact same
    # window, looked up by its own (scope_type, scope_entity, time_bucket) —
    # guaranteed to exist since is_cohort_suppressed just read it.
    row = (
        (
            await session.execute(
                text(
                    "SELECT observed_rate, baseline_rate FROM anomaly_windows "
                    "WHERE scope_type = :scope_type AND scope_entity = :scope_entity "
                    "AND time_bucket = :time_bucket"
                ),
                {
                    "scope_type": suppression.scope_type,
                    "scope_entity": suppression.scope_entity,
                    "time_bucket": suppression.time_bucket,
                },
            )
        )
        .mappings()
        .first()
    )
    return AnomalyContext(
        severity="high",
        is_anomaly=True,
        observed_rate=(
            float(row["observed_rate"]) if row and row["observed_rate"] is not None else None
        ),
        baseline_rate=(
            float(row["baseline_rate"]) if row and row["baseline_rate"] is not None else None
        ),
    )


def resolve_decision_now(
    *,
    is_synthetic: bool,
    failed_at: datetime | None,
    last_attempt_at: datetime | None,
) -> datetime:
    """
    The ONE authoritative "now" for a decision -- feeds is_expired AND
    PaymentContext.now, so every time-dependent policy rule (EligibilityRule,
    CooldownRule, AutopayExecutionWindowRule, QuietHoursComplianceRule) reads
    from the exact same value. Production traffic (is_synthetic=False)
    always gets the real clock -- this function, and every rule downstream
    of it, is completely unaware that "synthetic" is even a concept; only
    THIS call site branches on it.

    For a synthetic payment's FIRST decision (last_attempt_at is None -- no
    row in `recoveries` yet), use the payment's own simulated failed_at
    instead of the real clock: a canonical/evaluation run seeds thousands of
    payments spanning simulated days, then makes all of their first
    decisions within a few real minutes, so checking the real clock would
    make every one of them share the same real hour-of-day regardless of
    when, in the simulated world, they actually failed.

    Every decision AFTER the first (last_attempt_at is not None -- a real
    retry already executed) keeps using the real clock, synthetic or not:
    that attempt was genuinely scheduled and executed in real time by
    workers/retry_scheduler.py / execution_worker.py, so CooldownRule's
    `now - last_attempt_at` must stay consistent with how it actually
    happened, not jump back to the payment's original failure moment.
    """
    if is_synthetic and last_attempt_at is None and failed_at is not None:
        return failed_at
    return clock.utcnow()


async def _fetch_retry_history(
    session: AsyncSession, payment_id: str
) -> tuple[datetime | None, int]:
    """(last_attempt_at, next_attempt_number) from the recoveries table."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT executed_at, attempt_number FROM recoveries "
                    "WHERE payment_id = :pid ORDER BY attempt_number DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None, 1
    return row["executed_at"], row["attempt_number"] + 1


async def _fetch_money_exposure_context(
    session: AsyncSession, payment_id: str, amount_paise: int
) -> tuple[int, int]:
    """
    (pending_exposure_paise, max_money_exposure_paise) for
    MoneyExposureLimitRule (services/policy_engine/rules.py, Re-Audit
    MEDIUM finding). pending_exposure_paise: amount_paise summed once per
    currently-outstanding (outcome='PENDING') recovery attempt for this
    payment -- each one, if it later resolves SUCCESS, would claim the
    full payment amount. max_money_exposure_paise: the payment's active
    recovery_missions row's own cap; UNBOUNDED_EXPOSURE_PAISE if no active
    mission exists (e.g. a decision computed before a mission was ever
    created -- the rule then trivially passes, matching pre-fix behavior).
    """
    from services.policy_engine.rules import UNBOUNDED_EXPOSURE_PAISE

    pending_count_row = (
        await session.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid AND outcome = 'PENDING'"),
            {"pid": payment_id},
        )
    ).first()
    pending_count = pending_count_row[0] if pending_count_row else 0

    mission_row = (
        await session.execute(
            text(
                "SELECT max_money_exposure_paise FROM recovery_missions WHERE payment_id = :pid "
                "AND state NOT IN ('RECOVERED','ESCALATED','TERMINATED') "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        )
    ).first()
    max_money_exposure_paise = (
        mission_row[0] if mission_row is not None else UNBOUNDED_EXPOSURE_PAISE
    )

    return pending_count * amount_paise, max_money_exposure_paise


@dataclass(frozen=True)
class _RecommendationContext:
    """Pure-data view of the latest recovery_recommendations row for one
    diagnosis_id -- same hydrate-once-then-pass-a-dataclass discipline as
    PaymentContext/CandidateContext."""

    recommendation_id: str
    recommended_action: str
    confidence: float
    risk_flags: frozenset[str]


async def _fetch_recommendation(
    session: AsyncSession, diagnosis_id: str
) -> _RecommendationContext | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT recommendation_id, recommended_action, confidence, risk_flags "
                    "FROM recovery_recommendations WHERE diagnosis_id = :did "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"did": diagnosis_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return _RecommendationContext(
        recommendation_id=str(row["recommendation_id"]),
        recommended_action=row["recommended_action"],
        confidence=float(row["confidence"]),
        risk_flags=frozenset(row["risk_flags"] or []),
    )


def _apply_ai_fusion(
    *,
    candidates: tuple[CandidateActionResult, ...],
    nba_result: NextBestActionResult,
    decision: PolicyDecisionResult,
    payment_ctx: PaymentContext,
    policy_config_ctx: PolicyConfigContext,
    policy_config_row: PolicyConfig,
    recommendation: _RecommendationContext | None,
    ai_risk_flags: frozenset[str],
    tie_tolerance_bps: int,
    min_confidence: float,
) -> tuple[NextBestActionResult, PolicyDecisionResult, dict]:
    """
    The ONLY function allowed to change chosen_action based on an AI
    recommendation, and only in the two ways the design's invariants allow:

      (a) risk-flag escalation: already happened, if at all, INSIDE the
          `decision` this function receives -- ai_risk_flags was already
          threaded into the CandidateContext evaluate() was called with
          before this function runs, so AIRiskSignalEscalationRule (a real
          PolicyRule, services/policy_engine/rules.py) has already forced
          ESCALATE if a flag was present. This function only detects and
          reports that outcome; it never itself decides ESCALATE.

      (b) tie-break: only entered when the deterministic verdict is ALLOW
          (i.e. no risk escalation and nothing else blocked it), the
          recommendation's confidence clears min_confidence (AI Architecture
          Gap Audit gap, closed here -- confidence used to be persisted but
          never actually load-bearing), and the AI's recommended_action
          lands inside find_near_tied_candidates()'s set -- candidates that
          already cleared the EVI floor -- AND that exact candidate is ALSO
          individually policy-ALLOWED on its own re-evaluation. AI can never
          select a candidate policy has rejected, and can never change a
          decisive (non-near-tied) winner.

    Always returns a complete fusion_provenance dict (never None), even when
    nothing about the outcome changed, so persist_decision can write a
    uniform decision_fusion_trace row for every decision once the feature
    is enabled (design doc invariant 7).
    """
    near_tied = find_near_tied_candidates(
        candidates,
        nba_result.chosen_evi_paise,
        policy_config_row.min_expected_value_paise,
        tie_tolerance_bps,
    )
    risk_escalation_applied = decision.verdict == "ESCALATE" and bool(ai_risk_flags)

    provenance = {
        "recommendation_id": recommendation.recommendation_id if recommendation else None,
        "deterministic_chosen_action": nba_result.chosen_action,
        "deterministic_chosen_evi_paise": nba_result.chosen_evi_paise,
        "near_tied_candidates": [
            {"action_type": c.action_type, "evi_paise": c.expected_value_paise} for c in near_tied
        ],
        "tie_tolerance_bps": tie_tolerance_bps,
        "ai_recommended_action": recommendation.recommended_action if recommendation else None,
        "ai_confidence": recommendation.confidence if recommendation else None,
        "ai_risk_flags": sorted(ai_risk_flags),
        "tie_break_applied": False,
        "risk_escalation_applied": risk_escalation_applied,
        "final_action": nba_result.chosen_action,
        "fusion_reason": "no_recommendation_available",
        # Not persisted to decision_fusion_trace (that table only stores
        # fusion_reason's human-readable text) -- used by decide_and_persist
        # to label ai_tie_break_rejected_total{reason=...} without having to
        # pattern-match fusion_reason's prose.
        "reject_reason": None,
    }

    if recommendation is None:
        return nba_result, decision, provenance
    if risk_escalation_applied:
        provenance["fusion_reason"] = (
            f"AI risk signal(s) {sorted(ai_risk_flags)} forced ESCALATE via "
            "AIRiskSignalEscalationRule"
        )
        return nba_result, decision, provenance
    if decision.verdict != "ALLOW":
        provenance["fusion_reason"] = (
            f"deterministic verdict={decision.verdict} -- tie-break not considered"
        )
        return nba_result, decision, provenance
    if recommendation.recommended_action == nba_result.chosen_action:
        provenance["fusion_reason"] = "AI recommendation matches the deterministic winner"
        return nba_result, decision, provenance

    if recommendation.confidence < min_confidence:
        provenance["reject_reason"] = "confidence_below_floor"
        provenance["fusion_reason"] = (
            f"AI confidence {recommendation.confidence:.2f} below floor "
            f"{min_confidence:.2f} -- tie-break not considered"
        )
        return nba_result, decision, provenance

    near_tied_actions = {c.action_type for c in near_tied}
    if recommendation.recommended_action not in near_tied_actions:
        provenance["reject_reason"] = "outside_tolerance"
        provenance["fusion_reason"] = (
            f"AI recommended {recommendation.recommended_action}, not within "
            f"{tie_tolerance_bps} bps of the winner's EVI -- deterministic winner stands"
        )
        return nba_result, decision, provenance

    candidate_row = next(c for c in near_tied if c.action_type == recommendation.recommended_action)
    candidate_ctx = CandidateContext(
        action_type=candidate_row.action_type,
        expected_value_paise=candidate_row.expected_value_paise,
        ai_risk_flags=ai_risk_flags,
    )
    candidate_decision = evaluate(payment_ctx, candidate_ctx, policy_config_ctx)
    if candidate_decision.verdict != "ALLOW":
        provenance["reject_reason"] = "tie_break_rejected_policy"
        provenance["fusion_reason"] = (
            f"AI recommended {recommendation.recommended_action}, economically near-tied, but "
            f"individually policy verdict={candidate_decision.verdict} -- deterministic winner stands"
        )
        return nba_result, decision, provenance

    evi_delta_pct = (
        abs(candidate_row.expected_value_paise - nba_result.chosen_evi_paise)
        * 100.0
        / max(abs(nba_result.chosen_evi_paise), 1)
    )
    fused_nba_result = dataclasses.replace(
        nba_result,
        chosen_action=candidate_row.action_type,
        chosen_evi_paise=candidate_row.expected_value_paise,
        cleared_floor=True,
        action_confidence=compute_action_confidence(candidate_row.action_type, candidates),
    )
    provenance["tie_break_applied"] = True
    provenance["final_action"] = candidate_row.action_type
    provenance["fusion_reason"] = (
        f"AI recommendation accepted: {candidate_row.action_type} EVI delta "
        f"{evi_delta_pct:.2f}% within {tie_tolerance_bps / 100:.2f}% tolerance"
    )
    return fused_nba_result, candidate_decision, provenance


async def build_decision(
    payment_id: str,
    diagnosis_id: str | None = None,
) -> tuple[NextBestActionResult, PolicyDecisionResult, dict]:
    """
    Full read + score + decide pipeline for one payment. Does NOT persist —
    persistence is a separate step (persist_decision) so callers/tests can
    inspect the in-memory result first.

    Returns (next_best_action_result, policy_decision_result, context)
    where `context` carries everything persist_decision() needs (payment
    row fields, prediction metadata, policy_config row).

    diagnosis_id (optional, defaults to None): when given AND
    recoveryos.config.Settings.ai_recommendation_fusion_enabled is True, the
    latest recovery_recommendations row for that diagnosis is fetched and
    passed through the bounded fusion step (_apply_ai_fusion) -- see that
    function's docstring for the exact two ways it can influence
    chosen_action. Omitting diagnosis_id (every pre-Phase-11 caller/test)
    reproduces the exact prior behavior: no recommendation is ever fetched,
    _apply_ai_fusion is never called.
    """
    settings = get_settings()
    async with get_inference_session_factory()() as inf_session:
        payment_row = (
            (
                await inf_session.execute(
                    text(f"SELECT {_PAYMENT_SAFE_COLUMNS} FROM payments WHERE payment_id = :pid"),
                    {"pid": payment_id},
                )
            )
            .mappings()
            .first()
        )
        if payment_row is None:
            raise ValueError(
                f"payment_id={payment_id} not found (or not visible under inference_role)"
            )

        customer_row = (
            (
                await inf_session.execute(
                    text(
                        "SELECT is_returning, lifetime_value_paise, opted_out_at "
                        "FROM customers WHERE customer_id = :cid"
                    ),
                    {"cid": payment_row["customer_id"]},
                )
            )
            .mappings()
            .first()
        )

    propensity_context = build_propensity_context(
        amount_paise=payment_row["amount_paise"],
        method=payment_row["method"],
        bank=payment_row["bank"],
        is_returning_customer=bool(customer_row["is_returning"]) if customer_row else False,
        lifetime_value_paise=customer_row["lifetime_value_paise"] if customer_row else 0,
        initial_failure_code=payment_row["failure_code"],
        initial_failure_class=payment_row["failure_class"],
        created_at=payment_row["created_at"],
        merchant_id=payment_row["merchant_id"],
    )
    prediction = predict_recovery_probability(propensity_context)

    async with get_app_session_factory()() as app_session:
        anomaly_context = await _fetch_anomaly_context(app_session, payment_row["bank"])
        last_attempt_at, attempt_number = await _fetch_retry_history(app_session, payment_id)
        policy_config_row = await _resolve_policy_config(app_session, payment_row["merchant_id"])
        pending_exposure_paise, max_money_exposure_paise = await _fetch_money_exposure_context(
            app_session, payment_id, payment_row["amount_paise"]
        )

        recommendation = None
        if diagnosis_id is not None and settings.ai_recommendation_fusion_enabled:
            recommendation = await _fetch_recommendation(app_session, diagnosis_id)

        candidates = await generate_candidate_actions(
            app_session,
            merchant_id=payment_row["merchant_id"],
            amount_paise=payment_row["amount_paise"],
            customer_is_returning=bool(customer_row["is_returning"]) if customer_row else False,
            base_propensity_prob_bps=prediction.probability_bps,
            anomaly_context=anomaly_context,
        )

    nba_result = select_next_best_action(
        candidates,
        min_expected_value_paise=policy_config_row.min_expected_value_paise,
        propensity_probability_bps=prediction.probability_bps,
    )

    now = resolve_decision_now(
        is_synthetic=payment_row["is_synthetic"],
        failed_at=payment_row["failed_at"],
        last_attempt_at=last_attempt_at,
    )

    is_expired = payment_row["failed_at"] is not None and (
        now - payment_row["failed_at"] > timedelta(days=7)
    )
    is_high_severity_anomaly = bool(
        anomaly_context is not None
        and anomaly_context.severity == "high"
        and anomaly_context.is_anomaly
    )

    payment_ctx = PaymentContext(
        payment_id=payment_id,
        status=payment_row["status"],
        is_expired=is_expired,
        opted_out_at=customer_row["opted_out_at"] if customer_row else None,
        last_attempt_at=last_attempt_at,
        attempt_number=attempt_number,
        amount_paise=payment_row["amount_paise"],
        now=now,
        method=payment_row["method"],
        is_high_severity_anomaly=is_high_severity_anomaly,
        pending_exposure_paise=pending_exposure_paise,
        max_money_exposure_paise=max_money_exposure_paise,
    )
    # ai_risk_flags is empty whenever recommendation is None (flag off, no
    # diagnosis_id, or no recommendation row found) -- AIRiskSignalEscalationRule
    # (services/policy_engine/rules.py) then passes trivially, so this
    # evaluate() call is decision-identical to the pre-AI-fusion behavior in
    # that case, modulo one additional always-passing rule_trace entry.
    ai_risk_flags = recommendation.risk_flags if recommendation is not None else frozenset()
    candidate_ctx = CandidateContext(
        action_type=nba_result.chosen_action,
        expected_value_paise=nba_result.chosen_evi_paise,
        ai_risk_flags=ai_risk_flags,
    )
    policy_config_ctx = PolicyConfigContext(
        max_retries=policy_config_row.max_retries,
        retry_cooldown_hours=policy_config_row.retry_cooldown_hours,
        max_amount_paise=policy_config_row.max_amount_paise,
        escalate_after_failures=policy_config_row.escalate_after_failures,
        min_expected_value_paise=policy_config_row.min_expected_value_paise,
    )

    decision = evaluate(payment_ctx, candidate_ctx, policy_config_ctx)

    fusion_provenance = None
    if settings.ai_recommendation_fusion_enabled:
        nba_result, decision, fusion_provenance = _apply_ai_fusion(
            candidates=candidates,
            nba_result=nba_result,
            decision=decision,
            payment_ctx=payment_ctx,
            policy_config_ctx=policy_config_ctx,
            policy_config_row=policy_config_row,
            recommendation=recommendation,
            ai_risk_flags=ai_risk_flags,
            tie_tolerance_bps=settings.ai_tie_break_tolerance_bps,
            min_confidence=settings.ai_tie_break_min_confidence,
        )

    context = {
        "merchant_id": payment_row["merchant_id"],
        "amount_paise": payment_row["amount_paise"],
        "model_version": prediction.model_version,
        "feature_schema_version": prediction.feature_schema_version,
        "policy_config_id": policy_config_row.policy_config_id,
        "retry_cooldown_hours": policy_config_row.retry_cooldown_hours,
        "is_high_severity_anomaly": is_high_severity_anomaly,
        "blocking_rule": next((e["rule"] for e in decision.rule_trace if not e["passed"]), None),
        "fusion_provenance": fusion_provenance,
    }
    return nba_result, decision, context


async def persist_decision(
    payment_id: str,
    nba_result: NextBestActionResult,
    decision: PolicyDecisionResult,
    context: dict,
    source_event_id: str | None = None,
) -> tuple[list[CandidateAction], PolicyDecision, bool]:
    """
    Persist ALL 6 candidate_actions rows + ONE policy_decision row (pointing
    at the CHOSEN candidate's row) with the full rule_trace. app_role
    session — the only role with write access to these tables.

    source_event_id (Task S1, pre-Phase-8 audit): the triggering
    stream:risk_engine message's id, threaded through by
    services/pipeline/consumer.py. Deduped via migrations/0013's UNIQUE
    constraints — a redelivered message for the SAME triggering event (e.g.
    the pipeline consumer's xack call failing right after a fully
    successful run) returns the already-persisted rows instead of inserting
    duplicates. None (no event context — tests, direct invocation) never
    collides with anything, so every existing caller is unaffected.
    """
    async with get_app_session_factory()() as session:
        candidate_rows: list[CandidateAction] = []
        chosen_row: CandidateAction | None = None
        for candidate in nba_result.all_candidates:
            stmt = (
                pg_insert(CandidateAction)
                .values(
                    candidate_id=str(uuid.uuid4()),
                    payment_id=payment_id,
                    source_event_id=source_event_id,
                    action_type=candidate.action_type,
                    recovery_prob_bps=candidate.recovery_prob_bps,
                    expected_value_paise=candidate.expected_value_paise,
                    cost_paise=candidate.cost_paise,
                    friction_penalty_paise=candidate.friction_penalty_paise,
                    risk_penalty_paise=candidate.risk_penalty_paise,
                    model_version=context["model_version"],
                    # Only the CHOSEN action gets a real action_confidence
                    # (Task AGENT1) -- the other 5 candidates were never
                    # acted on, so "how confident are we in this action"
                    # doesn't apply to them.
                    action_confidence=(
                        nba_result.action_confidence
                        if candidate.action_type == nba_result.chosen_action
                        else None
                    ),
                )
                .on_conflict_do_nothing(
                    index_elements=["payment_id", "source_event_id", "action_type"]
                )
                .returning(CandidateAction)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                # Redelivery of the same triggering event -- this exact
                # (payment_id, source_event_id, action_type) row already
                # exists from the first attempt.
                row = (
                    await session.execute(
                        select(CandidateAction).where(
                            CandidateAction.payment_id == payment_id,
                            CandidateAction.source_event_id == source_event_id,
                            CandidateAction.action_type == candidate.action_type,
                        )
                    )
                ).scalar_one()
            candidate_rows.append(row)
            if candidate.action_type == nba_result.chosen_action:
                chosen_row = row

        assert chosen_row is not None
        policy_stmt = (
            pg_insert(PolicyDecision)
            .values(
                decision_id=str(uuid.uuid4()),
                payment_id=payment_id,
                source_event_id=source_event_id,
                candidate_id=chosen_row.candidate_id,
                policy_config_id=context["policy_config_id"],
                verdict=decision.verdict,
                rule_trace=list(decision.rule_trace),
            )
            .on_conflict_do_nothing(index_elements=["payment_id", "source_event_id"])
            .returning(PolicyDecision)
        )
        policy_decision_row = (await session.execute(policy_stmt)).scalar_one_or_none()
        was_inserted = policy_decision_row is not None
        if policy_decision_row is None:
            policy_decision_row = (
                await session.execute(
                    select(PolicyDecision).where(
                        PolicyDecision.payment_id == payment_id,
                        PolicyDecision.source_event_id == source_event_id,
                    )
                )
            ).scalar_one()
        # One decision_fusion_trace row per policy_decision, only when
        # fusion actually ran (context["fusion_provenance"] is None
        # whenever ai_recommendation_fusion_enabled was off for this
        # decision -- see build_decision). Guarded on was_inserted, same
        # dedup discipline as policy_blocks_total below: a redelivered
        # triggering event reuses the already-persisted policy_decision_row,
        # so it must not attempt a second insert against decision_id's own
        # UNIQUE constraint.
        fusion_provenance = context.get("fusion_provenance")
        if was_inserted and fusion_provenance is not None:
            session.add(
                DecisionFusionTrace(
                    decision_id=policy_decision_row.decision_id,
                    recommendation_id=fusion_provenance["recommendation_id"],
                    deterministic_chosen_action=fusion_provenance["deterministic_chosen_action"],
                    deterministic_chosen_evi_paise=fusion_provenance[
                        "deterministic_chosen_evi_paise"
                    ],
                    near_tied_candidates=fusion_provenance["near_tied_candidates"],
                    tie_tolerance_bps=fusion_provenance["tie_tolerance_bps"],
                    ai_recommended_action=fusion_provenance["ai_recommended_action"],
                    ai_confidence=fusion_provenance["ai_confidence"],
                    ai_risk_flags=fusion_provenance["ai_risk_flags"],
                    tie_break_applied=fusion_provenance["tie_break_applied"],
                    risk_escalation_applied=fusion_provenance["risk_escalation_applied"],
                    final_action=fusion_provenance["final_action"],
                    fusion_reason=fusion_provenance["fusion_reason"],
                )
            )
        await session.commit()

    return candidate_rows, policy_decision_row, was_inserted


async def decide_and_persist(
    payment_id: str,
    redis_client=None,
    source_event_id: str | None = None,
    diagnosis_id: str | None = None,
    before_enqueue: Callable[[dict], Awaitable[None]] | None = None,
) -> dict:
    """
    Convenience entry point: full pipeline + persistence for one payment.

    Three outcomes for the verdict/action pair:
      - ALLOW + RETRY_LATER: no execution job is enqueued at all. A
        scheduled_reevaluations row (Task REPLAN1) is written instead, with
        a real future scheduled_for computed by
        services.recovery_engine.timing.compute_retry_delay(). This is the
        continuous-replanning path -- workers/retry_scheduler.py re-runs
        the FULL decision from scratch once that time arrives.
      - ALLOW + any other executing action (not DO_NOTHING): if
        `redis_client` is given, enqueues a job onto stream:recovery_jobs
        for workers/execution_worker.py -- decision and execution, one
        continuous path. Omit `redis_client` to decide
        without enqueueing (e.g. tests that only care about the decision).
      - Anything else (BLOCK/ESCALATE, or ALLOW + DO_NOTHING): nothing is
        enqueued or scheduled; the caller (services/pipeline/consumer.py)
        writes the terminal ledger/audit row itself.

    `before_enqueue`, if given, is awaited with the in-progress `result`
    dict (already carrying decision_id/chosen_action/attempt_number) right
    before the job actually becomes visible on stream:recovery_jobs -- and
    ONLY in the branch that's about to enqueue one. This closes a real race
    (found live-testing the demo scenario endpoints against a genuinely
    separate, always-running execution_worker container, which the
    in-process test suite never exercises): workers/execution_worker.py's
    own mission_trackable check reads the mission's state as soon as it
    picks the job up off the stream, which can happen within ~1s of the
    enqueue (it's already blocked on XREADGROUP) -- faster than
    services/pipeline/consumer.py's OWN follow-up transition of the mission
    into EXECUTING used to commit, since that transition used to run AFTER
    this function returned, i.e. AFTER the enqueue. execution_worker would
    then read the mission still in AWAITING_AUTHORIZATION, conclude
    mission_trackable=False, and silently skip ALL mission-tracking for
    that attempt (no attempt increment, no OBSERVING_OUTCOME transition,
    no replan on a later failure) -- a stalled mission with zero exception
    ever raised. The caller uses this hook to commit that EXECUTING
    transition itself, synchronously, before the enqueue -- so by the time
    the job is visible to any consumer, the mission is already
    provably in EXECUTING. Never called for RETRY_LATER (returns earlier,
    above) or verdict!=ALLOW/DO_NOTHING (no enqueue branch at all) --
    those transitions carry no such race and stay exactly where they were.
    """
    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)
    candidate_rows, policy_decision_row, was_inserted = await persist_decision(
        payment_id, nba_result, decision, context, source_event_id
    )

    result = {
        "payment_id": payment_id,
        "chosen_action": nba_result.chosen_action,
        "chosen_evi_paise": nba_result.chosen_evi_paise,
        "propensity_probability_bps": nba_result.propensity_probability_bps,
        "verdict": decision.verdict,
        "rule_trace": decision.rule_trace,
        "blocking_rule": context["blocking_rule"],
        "decision_id": policy_decision_row.decision_id,
        "candidate_ids": [c.candidate_id for c in candidate_rows],
    }

    # AI fusion metrics, same was_inserted-guarded discipline as
    # policy_blocks_total below -- a redelivered triggering event must not
    # double-count against an outcome that was already recorded.
    fusion_provenance = context.get("fusion_provenance")
    if was_inserted and fusion_provenance is not None:
        if fusion_provenance["ai_recommended_action"] is not None:
            ai_recommendation_available_total.inc()
        if fusion_provenance["risk_escalation_applied"]:
            ai_risk_escalations_total.inc()
            ai_outcome_delta_total.labels(cause="risk_escalation").inc()
        elif fusion_provenance["tie_break_applied"]:
            ai_tie_break_applied_total.inc()
            ai_outcome_delta_total.labels(cause="tie_break").inc()
        elif fusion_provenance["reject_reason"] is not None:
            ai_tie_break_rejected_total.labels(reason=fusion_provenance["reject_reason"]).inc()

    if was_inserted and decision.verdict != "ALLOW" and context["blocking_rule"] is not None:
        # TRD §10: policy_blocks_total{rule} -- labeled with the SPECIFIC
        # rule that blocked/escalated (services.policy_engine.evaluate's
        # own short-circuit already identifies exactly one), not a generic
        # "blocked" bucket. Guarded on was_inserted (same dedup discipline
        # as services/pipeline/ledger.py's ledger_row_inserted guard) so a
        # redelivered triggering event -- which reuses the ALREADY-persisted
        # decision instead of creating a new one -- doesn't double-count.
        policy_blocks_total.labels(rule=context["blocking_rule"]).inc()

    if decision.verdict == "ALLOW" and nba_result.chosen_action == "RETRY_LATER":
        # Task REPLAN1: RETRY_LATER no longer enqueues an immediate
        # execution job (that was the pre-existing bug -- scheduled_for
        # was always now(), so RETRY_LATER executed identically to
        # RETRY_NOW). Instead, write a real deferred re-evaluation --
        # workers/retry_scheduler.py re-runs the FULL decision at that
        # future time, not a replay of this stale one.
        from services.recovery_engine.scheduling import schedule_reevaluation

        delay = compute_retry_delay(
            is_high_severity_anomaly=context["is_high_severity_anomaly"],
            retry_cooldown_hours=context["retry_cooldown_hours"],
        )
        reevaluation_id = await schedule_reevaluation(
            payment_id=payment_id,
            decision_id=policy_decision_row.decision_id,
            diagnosis_id=diagnosis_id,
            source_event_id=source_event_id,
            scheduled_for=clock.utcnow() + delay,
        )
        result["scheduled_reevaluation_id"] = reevaluation_id
        result["scheduled_for"] = (clock.utcnow() + delay).isoformat()
        return result

    if (
        redis_client is not None
        and decision.verdict == "ALLOW"
        and nba_result.chosen_action != "DO_NOTHING"
    ):
        from services.execution_engine.publisher import enqueue_recovery_job

        async with get_app_session_factory()() as session:
            attempt_number = (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM recoveries WHERE payment_id = :pid"
                    ),
                    {"pid": payment_id},
                )
            ).scalar_one()

        idempotency_key = f"recovery:{payment_id}:{nba_result.chosen_action}:{attempt_number}"
        result["attempt_number"] = attempt_number
        result["idempotency_key"] = idempotency_key
        if before_enqueue is not None:
            # Must fully commit before the job becomes visible below --
            # see this function's own docstring on the race this closes.
            await before_enqueue(result)
        maybe_stream_id = enqueue_recovery_job(
            redis_client,
            payment_id=payment_id,
            decision_id=policy_decision_row.decision_id,
            idempotency_key=idempotency_key,
            action_type=nba_result.chosen_action,
            attempt_number=attempt_number,
            amount_paise=context.get("amount_paise") or 0,
        )
        # enqueue_recovery_job calls redis_client.xadd(), which is a plain
        # (synchronous-looking) call on a sync `redis.Redis` client but
        # returns an unawaited coroutine on an async `redis.asyncio.Redis`
        # client (services/pipeline/consumer.py's XREADGROUP-based consumer
        # uses the latter). Awaiting only when awaitable lets this one
        # publisher function serve both callers without silently dropping
        # the enqueue (a coroutine that's never awaited never actually
        # runs — this was caught by a real end-to-end test, not a
        # hypothetical).
        if inspect.isawaitable(maybe_stream_id):
            stream_id = await maybe_stream_id
        else:
            stream_id = maybe_stream_id
        result["enqueued_stream_id"] = stream_id

    return result
