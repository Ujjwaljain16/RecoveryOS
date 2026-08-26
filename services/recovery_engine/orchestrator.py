"""
Decision orchestrator — the ONLY place in Phase 5 that does I/O across all
of propensity, EVI, next-best-action, and policy_engine. Every function in
those modules is pure/testable in isolation; this module wires them
together against real data:

    live payment
        -> inference_role read (propensity features)
        -> Phase 2 certified logistic regression (P(recover) — see
           propensity.py's docstring for why LR, not LightGBM, is correct)
        -> app_role read (anomaly context, retry history, policy config)
        -> EVI-scored candidate actions (6)
        -> next-best-action selection
        -> policy_engine.evaluate() (pure, on the chosen candidate)
        -> persist ALL 6 candidate_actions rows + ONE policy_decision row
           with full rule_trace

Model lineage: every persisted CandidateAction row carries model_version +
feature_schema_version from the propensity prediction, so any decision_id
is traceable back to the exact certified Phase 2 artifact that produced it.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.database import (
    get_app_session_factory,
    get_inference_session_factory,
)
from recoveryos.models import CandidateAction, Merchant, PolicyConfig, PolicyDecision
from services.policy_engine.evaluate import PolicyDecision as PolicyDecisionResult
from services.policy_engine.evaluate import evaluate
from services.policy_engine.rules import CandidateContext, PaymentContext, PolicyConfigContext
from services.recovery_engine.next_best_action import (
    NextBestActionResult,
    generate_candidate_actions,
    select_next_best_action,
)
from services.recovery_engine.propensity import (
    build_propensity_context,
    predict_recovery_probability,
)
from services.recovery_engine.timing import AnomalyContext
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


async def build_decision(
    payment_id: str,
) -> tuple[NextBestActionResult, PolicyDecisionResult, dict]:
    """
    Full read + score + decide pipeline for one payment. Does NOT persist —
    persistence is a separate step (persist_decision) so callers/tests can
    inspect the in-memory result first.

    Returns (next_best_action_result, policy_decision_result, context)
    where `context` carries everything persist_decision() needs (payment
    row fields, prediction metadata, policy_config row).
    """
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

    is_expired = payment_row["failed_at"] is not None and (
        datetime.now(UTC) - payment_row["failed_at"] > timedelta(days=7)
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
        now=datetime.now(UTC),
        is_high_severity_anomaly=is_high_severity_anomaly,
    )
    candidate_ctx = CandidateContext(
        action_type=nba_result.chosen_action, expected_value_paise=nba_result.chosen_evi_paise
    )
    policy_config_ctx = PolicyConfigContext(
        max_retries=policy_config_row.max_retries,
        retry_cooldown_hours=policy_config_row.retry_cooldown_hours,
        max_amount_paise=policy_config_row.max_amount_paise,
        escalate_after_failures=policy_config_row.escalate_after_failures,
        min_expected_value_paise=policy_config_row.min_expected_value_paise,
    )

    decision = evaluate(payment_ctx, candidate_ctx, policy_config_ctx)

    context = {
        "merchant_id": payment_row["merchant_id"],
        "amount_paise": payment_row["amount_paise"],
        "model_version": prediction.model_version,
        "feature_schema_version": prediction.feature_schema_version,
        "policy_config_id": policy_config_row.policy_config_id,
        "is_high_severity_anomaly": is_high_severity_anomaly,
        "blocking_rule": next((e["rule"] for e in decision.rule_trace if not e["passed"]), None),
    }
    return nba_result, decision, context


async def persist_decision(
    payment_id: str,
    nba_result: NextBestActionResult,
    decision: PolicyDecisionResult,
    context: dict,
    source_event_id: str | None = None,
) -> tuple[list[CandidateAction], PolicyDecision]:
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
        if policy_decision_row is None:
            policy_decision_row = (
                await session.execute(
                    select(PolicyDecision).where(
                        PolicyDecision.payment_id == payment_id,
                        PolicyDecision.source_event_id == source_event_id,
                    )
                )
            ).scalar_one()
        await session.commit()

    return candidate_rows, policy_decision_row


async def decide_and_persist(
    payment_id: str, redis_client=None, source_event_id: str | None = None
) -> dict:
    """
    Convenience entry point: full pipeline + persistence for one payment.

    If `redis_client` is given and the verdict is ALLOW for an action that
    actually executes (i.e. not DO_NOTHING — nothing to schedule for it),
    enqueues a job onto stream:recovery_jobs for
    workers/execution_worker.py — Phase 5's decision, Phase 6's execution,
    one continuous path. Omit `redis_client` to decide without enqueueing
    (e.g. tests that only care about the decision).
    """
    nba_result, decision, context = await build_decision(payment_id)
    candidate_rows, policy_decision_row = await persist_decision(
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
        # runs — this was caught by a real end-to-end test in Phase 7, not
        # a hypothetical).
        if inspect.isawaitable(maybe_stream_id):
            stream_id = await maybe_stream_id
        else:
            stream_id = maybe_stream_id
        result["enqueued_stream_id"] = stream_id
        result["idempotency_key"] = idempotency_key

    return result
