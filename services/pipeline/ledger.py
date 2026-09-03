"""
recovery_ledger + audit_log population — TRD §2, the terminal step of every
decision chain (whether or not an actual recovery attempt executes).

Two call sites, by design:
  - services/pipeline/consumer.py: writes the ledger/audit row IMMEDIATELY
    for a BLOCK/ESCALATE verdict or a DO_NOTHING chosen action — no
    execution job is ever enqueued for these, so nothing downstream would
    ever write the terminal row otherwise.
  - workers/execution_worker.py: writes the ledger/audit row once a job
    reaches a real terminal outcome (SUCCESS/FAILED) — a PENDING outcome
    (e.g. RazorpayTestAdapter's "order created, not yet paid") is NOT
    terminal and does not get a ledger row yet.

Both call sites share compute_ledger_entry() (pure) so the actual paise
arithmetic exists in exactly one place; each has its own thin I/O wrapper
(async for the pipeline consumer, sync for execution_worker) because the
underlying connection types differ, not because the logic does.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from recoveryos.metrics import (
    incremental_revenue_paise_total,
    revenue_at_risk_paise_total,
    revenue_recovered_paise_total,
)

BPS_SCALE = 10_000

# Task E1 (an evaluation-harness Scenario 4 fix): a payment that reaches
# a real SUCCESS outcome is no longer 'failed' -- distinct from 'success' (which means the
# ORIGINAL authorization succeeded on the first attempt, never failed at
# all; see recoveryos/models.py's Payment.status comment). EligibilityRule
# already blocks anything whose status != 'failed', ordered before
# CooldownRule -- the bug was never a missing rule, it was that nothing
# ever wrote this status transition, so EligibilityRule had nothing to act
# on and CooldownRule's purely-elapsed-time check was the only thing ever
# consulted, incorrectly, past its 12h window.
RECOVERED_STATUS = "recovered"


@dataclass(frozen=True)
class LedgerEntry:
    revenue_at_risk_paise: int
    expected_recovery_paise: int
    actual_recovery_paise: int
    baseline_outcome: str | None
    incremental_recovery_paise: int
    intervention_cost_paise: int
    net_recovery_paise: int


def compute_ledger_entry(
    *,
    amount_paise: int,
    recovery_prob_bps: int,
    actual_recovery_paise: int,
    intervention_cost_paise: int,
    baseline_recovered_amount_paise: int | None,
    baseline_outcome: str | None,
) -> LedgerEntry:
    """
    Pure integer-paise arithmetic (gaps.md §B.4 discipline) — no float ever
    touches a money value here.

    expected_recovery_paise: revenue_at_risk x recovery_prob_bps, the FULL
    transaction amount the merchant would receive back (not scaled by
    RecoveryOS's own take-rate — that margin distinction is EVI's internal
    economics, see services/recovery_engine/evi.py's module docstring;
    the ledger's job is "how much of the at-risk revenue came back," which
    is a merchant-facing number, not a platform-revenue one).
    """
    expected_recovery_paise = (amount_paise * recovery_prob_bps) // BPS_SCALE
    baseline_amount = baseline_recovered_amount_paise or 0
    incremental_recovery_paise = actual_recovery_paise - baseline_amount
    net_recovery_paise = actual_recovery_paise - intervention_cost_paise

    return LedgerEntry(
        revenue_at_risk_paise=amount_paise,
        expected_recovery_paise=expected_recovery_paise,
        actual_recovery_paise=actual_recovery_paise,
        baseline_outcome=baseline_outcome,
        incremental_recovery_paise=incremental_recovery_paise,
        intervention_cost_paise=intervention_cost_paise,
        net_recovery_paise=net_recovery_paise,
    )


def _should_correct_ledger(
    *, existing_actual_recovery_paise: int, new_actual_recovery_paise: int, amount_paise_cap: int
) -> tuple[bool, int]:
    """
    Domain Audit finding #2 -- the invariant for recovery_ledger's one row
    per payment_id, made explicit and pure/testable rather than left as an
    unstated assumption inside a bigger function.

    INVARIANT: recovery_ledger's one row per payment_id represents the
    best-known outcome of recovering THIS PAYMENT'S SINGLE underlying debt
    (payment.amount_paise) -- not a sum across multiple attempts. Only one
    real attempt can ever genuinely collect that debt: EligibilityRule
    blocks any further attempt once payments.status becomes 'recovered',
    so under normal operation two DISTINCT attempts both reporting a real
    SUCCESS for the same payment should be structurally impossible.

    The ONLY correction this function ever authorizes is a one-way
    "not known to be recovered" -> "recovered" transition:
      - existing_actual_recovery_paise > 0 (already known recovered):
        NEVER correct, regardless of what the new attempt reports -- a
        real capture is unambiguous ground truth; a later FAILED/lower
        report must never un-recover money that genuinely came back
        (Finding #1's exact concern, generalized beyond the webhook path).
      - new_actual_recovery_paise <= 0 (this attempt didn't recover
        anything new): NEVER correct -- BLOCK->FAILED, FAILED->FAILED,
        FAILED->BLOCK, or any other non-recovering transition carries no
        new revenue information; the existing row (whichever non-recovery
        terminal state was recorded first) stays authoritative. This is
        the "do not use higher-amount-wins as the sole rule" case: a
        second distinct non-recovering attempt must not silently overwrite
        the first one's audit trail just because it happens to be later.
      - existing_actual_recovery_paise == 0 and new_actual_recovery_paise
        > 0: the only real correction case -- this payment was NOT
        previously known to be recovered, and this new terminal outcome
        reports that it genuinely was. Clamped to amount_paise_cap (the
        payment's own amount_paise) as a sanity bound against a malformed
        or inflated webhook/provider report claiming more was recovered
        than was ever owed.
    """
    if existing_actual_recovery_paise > 0:
        return False, existing_actual_recovery_paise
    if new_actual_recovery_paise <= 0:
        return False, existing_actual_recovery_paise
    return True, min(new_actual_recovery_paise, amount_paise_cap)


def _record_ledger_metrics(entry: LedgerEntry) -> None:
    """TRD §10's three revenue series -- called ONCE per genuinely NEW
    terminal ledger row (guarded by `ledger_row_inserted` at both call
    sites), same dedup discipline as the audit_log write right next to it:
    a redelivered message that finds the row already inserted must not
    double-count these."""
    revenue_at_risk_paise_total.inc(entry.revenue_at_risk_paise)
    revenue_recovered_paise_total.inc(entry.actual_recovery_paise)
    incremental_revenue_paise_total.inc(entry.incremental_recovery_paise)


def build_audit_summary(
    payment_id: str, chosen_action: str, verdict: str, outcome: str | None = None
) -> str:
    if outcome is not None:
        return f"Payment {payment_id}: action={chosen_action}, policy={verdict}, outcome={outcome}"
    return f"Payment {payment_id}: action={chosen_action}, policy={verdict}, no execution attempted"


# Task AGENT1, agent-design review point 4 -- the SAME mapping the
# evaluation harness's AI-eval used (docs/phase8_ai_evaluation.md)
# between the simulator's hidden ground-truth failure type and the
# diagnoser's RootCause vocabulary.
# Duplicated here (not imported from a shared module) deliberately kept as
# a single small literal so this file's only dependency stays app_role SQL
# text() -- no import of services.diagnosis_engine from services.pipeline.
_TRUE_FAILURE_TYPE_TO_ROOT_CAUSE = {
    "PERMANENT_INVALID_CREDS": "permanent_failure",
    "PERMANENT_EXPIRED_INSTRUMENT": "permanent_failure",
    "PERMANENT_ACCOUNT_CLOSED": "permanent_failure",
    "CUSTOMER_INSUFFICIENT_FUNDS": "customer_specific",
    "BANK_DEGRADATION_FAIL": "temporary_bank_degradation",
    "MULTI_RAIL_OUTAGE_FAIL": "systemic_degradation",
    "TEMPORARY_GATEWAY_TIMEOUT": "temporary_bank_degradation",
    "TRANSIENT_NETWORK_DROP": "temporary_bank_degradation",
}


def _derive_outcome_fields(
    *, verdict: str, actual_recovery_paise: int, outcome: str | None
) -> tuple[str, bool | None]:
    """
    observed_outcome, action_effective. action_effective is None (not
    applicable) when no execution was ever attempted (BLOCK/ESCALATE, or
    an ALLOW that chose DO_NOTHING) -- only meaningful when an action
    actually ran and either did or didn't recover money.
    """
    observed_outcome = outcome if outcome is not None else verdict
    if verdict != "ALLOW" or outcome is None:
        return observed_outcome, None
    return observed_outcome, actual_recovery_paise > 0


# ─── Async writer (services/pipeline/consumer.py) ──────────────────────────


async def populate_ledger_and_audit_async(
    session,
    *,
    payment_id: str,
    candidate_id: str,
    decision_id: str,
    verdict: str,
    chosen_action: str,
    recovery_prob_bps: int,
    cost_paise: int,
    actual_recovery_paise: int = 0,
    recovery_id: str | None = None,
    diagnosis_id: str | None = None,
    outcome: str | None = None,
) -> None:
    from sqlalchemy import text

    from services.pipeline.baseline import compute_and_persist_baseline_run

    payment_row = (
        (
            await session.execute(
                text("SELECT amount_paise FROM payments WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    amount_paise = payment_row["amount_paise"] if payment_row else 0

    baseline = await compute_and_persist_baseline_run(session, payment_id)
    baseline_outcome = baseline["outcome"] if baseline else None
    baseline_amount = baseline["recovered_amount_paise"] if baseline else None

    entry = compute_ledger_entry(
        amount_paise=amount_paise,
        recovery_prob_bps=recovery_prob_bps,
        actual_recovery_paise=actual_recovery_paise,
        intervention_cost_paise=cost_paise,
        baseline_recovered_amount_paise=baseline_amount,
        baseline_outcome=baseline_outcome,
    )

    # ON CONFLICT (payment_id) DO NOTHING, not DO UPDATE (Task S1, pre-Phase-8
    # audit): a redelivered pipeline message (e.g. the xack call itself
    # failing right after this exact commit) reprocesses the payment from
    # scratch and would otherwise insert a second row here. The FIRST
    # computed entry is authoritative -- compute_and_persist_baseline_run()
    # is itself idempotent (returns the existing baseline_runs row rather
    # than recomputing), amount_paise is immutable, and nothing else acts on
    # this payment between the two attempts, so a later redelivery's numbers
    # are expected to be identical, not "newer" -- there is no legitimate
    # case where the second attempt's figures should overwrite the first's.
    # RETURNING lets us know whether this attempt actually won, so the
    # audit_log write below (which is NOT itself deduped -- audit_log is an
    # explicit append-only history, gaps.md's own append-only invariant)
    # doesn't record a redundant "new outcome" entry for a write that didn't
    # actually happen.
    result = await session.execute(
        text(
            """
            INSERT INTO recovery_ledger
                (ledger_id, payment_id, revenue_at_risk_paise, expected_recovery_paise,
                 actual_recovery_paise, baseline_outcome, incremental_recovery_paise,
                 intervention_cost_paise, net_recovery_paise)
            VALUES
                (:ledger_id, :pid, :revenue_at_risk, :expected_recovery, :actual_recovery,
                 :baseline_outcome, :incremental, :intervention_cost, :net_recovery)
            ON CONFLICT (payment_id) DO NOTHING
            RETURNING ledger_id
            """
        ),
        {
            "ledger_id": str(uuid.uuid4()),
            "pid": payment_id,
            "revenue_at_risk": entry.revenue_at_risk_paise,
            "expected_recovery": entry.expected_recovery_paise,
            "actual_recovery": entry.actual_recovery_paise,
            "baseline_outcome": entry.baseline_outcome,
            "incremental": entry.incremental_recovery_paise,
            "intervention_cost": entry.intervention_cost_paise,
            "net_recovery": entry.net_recovery_paise,
        },
    )
    ledger_row_inserted = result.first() is not None

    if ledger_row_inserted:
        _record_ledger_metrics(entry)
        await session.execute(
            text(
                "INSERT INTO audit_log (audit_id, payment_id, diagnosis_id, candidate_id, "
                "decision_id, recovery_id, summary) "
                "VALUES (:aid, :pid, :did, :cid, :decid, :rid, :summary)"
            ),
            {
                "aid": str(uuid.uuid4()),
                "pid": payment_id,
                "did": diagnosis_id,
                "cid": candidate_id,
                "decid": decision_id,
                "rid": recovery_id,
                "summary": build_audit_summary(payment_id, chosen_action, verdict, outcome),
            },
        )
        if outcome == "SUCCESS":
            await session.execute(
                text("UPDATE payments SET status = :status WHERE payment_id = :pid"),
                {"status": RECOVERED_STATUS, "pid": payment_id},
            )
        if diagnosis_id is not None:
            await _record_diagnosis_outcome_async(
                session,
                diagnosis_id=diagnosis_id,
                payment_id=payment_id,
                verdict=verdict,
                chosen_action=chosen_action,
                outcome=outcome,
                actual_recovery_paise=entry.actual_recovery_paise,
                baseline_amount=baseline_amount,
                incremental_recovery_paise=entry.incremental_recovery_paise,
            )
        await session.commit()
    else:
        # Domain Audit finding #2: a payment_id can legitimately reach a
        # SECOND real terminal outcome -- not just via services/pipeline/
        # reconciliation.py's webhook-multi-attempt case, but any caller
        # (this one included, via a genuinely later decision cycle for the
        # same payment -- e.g. a merchant re-ingesting a fresh
        # PAYMENT_FAILED event after an earlier terminal write already
        # occupied this payment's one ledger row). Previously this branch
        # did nothing at all, silently discarding a real SUCCESS that
        # arrived after an earlier FAILED/BLOCK/DO_NOTHING. Now delegates
        # to correct_ledger_and_audit_async, which self-enforces
        # _should_correct_ledger's invariant -- most calls here will
        # correctly no-op (e.g. a second FAILED after a first FAILED
        # carries no new revenue information), only a genuine "not known
        # recovered" -> "recovered" transition is ever applied.
        await session.commit()  # release this session's row lock before the correction path re-reads it
        await correct_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=candidate_id,
            decision_id=decision_id,
            verdict=verdict,
            chosen_action=chosen_action,
            recovery_prob_bps=recovery_prob_bps,
            cost_paise=cost_paise,
            actual_recovery_paise=actual_recovery_paise,
            recovery_id=recovery_id,
            diagnosis_id=diagnosis_id,
            outcome=outcome or "UNKNOWN",
            correction_reason=(
                "a later decision cycle for this payment reached a new terminal outcome "
                "after an earlier one already occupied this payment's ledger row"
            ),
        )


async def correct_ledger_and_audit_async(
    session,
    *,
    payment_id: str,
    candidate_id: str | None,
    decision_id: str,
    verdict: str,
    chosen_action: str,
    recovery_prob_bps: int,
    cost_paise: int,
    actual_recovery_paise: int,
    recovery_id: str | None,
    diagnosis_id: str | None,
    outcome: str,
    correction_reason: str,
) -> None:
    """
    Revises an ALREADY-terminal recovery_ledger/recoveries/diagnosis_outcomes
    row — the one case populate_ledger_and_audit_async's ON CONFLICT DO
    NOTHING used to leave completely unhandled (Task S1's "one terminal
    ledger entry per payment" dedup guard is about REDELIVERY of the SAME
    event, not a genuine later real-world outcome). Generalized (Domain
    Audit finding #2) beyond its original webhook-only call site --
    populate_ledger_and_audit_async/_sync now call this too, on ANY
    conflict, not just services/pipeline/reconciliation.py's.

    Self-enforces `_should_correct_ledger`'s invariant (does NOT trust the
    caller to have already checked it) -- multiple callers now reach this
    function, so the safety has to live here, not be re-derived at each
    call site: only a genuine "not known recovered" -> "recovered"
    transition is ever applied, clamped to the payment's own amount_paise.
    A call that doesn't qualify (see the invariant's docstring) is a
    silent, correct no-op, not an error -- this keeps every caller free to
    invoke it "just in case" without needing to pre-check the invariant
    itself.

    Real scenario this exists for (found live-testing Task WEBHOOK1):
    Razorpay lets a customer retry a DIFFERENT payment method on the SAME
    order after a decline (e.g. an international card declined, then
    netbanking succeeds). Both are real webhooks for the same order_id --
    the first (payment.failed) correctly resolves the recovery to FAILED;
    the second (payment.captured) must UPGRADE that same recovery to
    SUCCESS, not be silently dropped as "no PENDING recovery matched."
    """
    from sqlalchemy import text

    from services.pipeline.baseline import compute_and_persist_baseline_run

    payment_row = (
        (
            await session.execute(
                text("SELECT amount_paise FROM payments WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    amount_paise = payment_row["amount_paise"] if payment_row else 0

    old_ledger_row = (
        (
            await session.execute(
                text(
                    "SELECT actual_recovery_paise, incremental_recovery_paise "
                    "FROM recovery_ledger WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if old_ledger_row is None:
        # Nothing to correct -- the original terminal write never
        # happened (a caller invoking this before any populate_* call ever
        # ran for this payment). Fail safe rather than raise.
        return

    should_correct, clamped_actual_recovery_paise = _should_correct_ledger(
        existing_actual_recovery_paise=old_ledger_row["actual_recovery_paise"],
        new_actual_recovery_paise=actual_recovery_paise,
        amount_paise_cap=amount_paise,
    )
    if not should_correct:
        return

    baseline = await compute_and_persist_baseline_run(session, payment_id)
    baseline_outcome = baseline["outcome"] if baseline else None
    baseline_amount = baseline["recovered_amount_paise"] if baseline else None

    entry = compute_ledger_entry(
        amount_paise=amount_paise,
        recovery_prob_bps=recovery_prob_bps,
        actual_recovery_paise=clamped_actual_recovery_paise,
        intervention_cost_paise=cost_paise,
        baseline_recovered_amount_paise=baseline_amount,
        baseline_outcome=baseline_outcome,
    )

    await session.execute(
        text(
            "UPDATE recovery_ledger SET actual_recovery_paise = :actual, "
            "incremental_recovery_paise = :incremental, net_recovery_paise = :net, "
            "baseline_outcome = :baseline_outcome WHERE payment_id = :pid"
        ),
        {
            "actual": entry.actual_recovery_paise,
            "incremental": entry.incremental_recovery_paise,
            "net": entry.net_recovery_paise,
            "baseline_outcome": entry.baseline_outcome,
            "pid": payment_id,
        },
    )

    # audit_log is append-only (gaps.md's own invariant) -- a correction is
    # a NEW entry in the history, not an edit of the original one.
    await session.execute(
        text(
            "INSERT INTO audit_log (audit_id, payment_id, diagnosis_id, candidate_id, "
            "decision_id, recovery_id, summary) "
            "VALUES (:aid, :pid, :did, :cid, :decid, :rid, :summary)"
        ),
        {
            "aid": str(uuid.uuid4()),
            "pid": payment_id,
            "did": diagnosis_id,
            "cid": candidate_id,
            "decid": decision_id,
            "rid": recovery_id,
            "summary": (
                f"Payment {payment_id}: CORRECTED action={chosen_action}, policy={verdict}, "
                f"outcome={outcome} ({correction_reason})"
            ),
        },
    )

    if outcome == "SUCCESS":
        await session.execute(
            text("UPDATE payments SET status = :status WHERE payment_id = :pid"),
            {"status": RECOVERED_STATUS, "pid": payment_id},
        )

    if diagnosis_id is not None:
        observed_outcome, action_effective = _derive_outcome_fields(
            verdict=verdict, actual_recovery_paise=entry.actual_recovery_paise, outcome=outcome
        )
        diag_row = (
            (
                await session.execute(
                    text(
                        "SELECT d.root_cause, l.true_failure_type FROM diagnoses d "
                        "LEFT JOIN simulator_latent_state l ON l.payment_id = d.payment_id "
                        "WHERE d.diagnosis_id = :did"
                    ),
                    {"did": diagnosis_id},
                )
            )
            .mappings()
            .first()
        )
        diagnosis_correct = None
        if diag_row is not None and diag_row["true_failure_type"] is not None:
            expected = _TRUE_FAILURE_TYPE_TO_ROOT_CAUSE.get(diag_row["true_failure_type"])
            diagnosis_correct = expected is not None and expected == diag_row["root_cause"]

        await session.execute(
            text(
                "UPDATE diagnosis_outcomes SET observed_outcome = :observed, "
                "diagnosis_correct = :correct, action_effective = :effective, "
                "counterfactual_result = :counterfactual WHERE diagnosis_id = :did"
            ),
            {
                "observed": observed_outcome,
                "correct": diagnosis_correct,
                "effective": action_effective,
                "counterfactual": json.dumps(
                    {
                        "actual_recovery_paise": entry.actual_recovery_paise,
                        "baseline_recovery_paise": baseline_amount or 0,
                        "incremental_recovery_paise": entry.incremental_recovery_paise,
                    }
                ),
                "did": diagnosis_id,
            },
        )

    await session.commit()

    # Metric deltas -- revenue_recovered_paise_total is a monotonic
    # Counter, so only a non-negative delta is ever recorded. Guaranteed
    # >= 0 here by _should_correct_ledger's own invariant (this code path
    # only runs when should_correct was True, which only happens on a
    # "not recovered" -> "recovered" transition) -- guarded explicitly
    # anyway rather than trust that silently.
    recovered_delta = entry.actual_recovery_paise - old_ledger_row["actual_recovery_paise"]
    if recovered_delta > 0:
        revenue_recovered_paise_total.inc(recovered_delta)
    incremental_revenue_paise_total.inc(
        entry.incremental_recovery_paise - old_ledger_row["incremental_recovery_paise"]
    )


async def _record_diagnosis_outcome_async(
    session,
    *,
    diagnosis_id: str,
    payment_id: str,  # unused -- re-derived via diagnosis_id's own join below; kept so the
    # caller can pass every field it already has without special-casing this one
    verdict: str,
    chosen_action: str,
    outcome: str | None,
    actual_recovery_paise: int,
    baseline_amount: int | None,
    incremental_recovery_paise: int,
) -> None:
    """
    Task AGENT1 point 4 -- closes the diagnosis-to-outcome loop. One row
    per diagnosis_id (unique constraint, migration 0015); a redelivery that
    reaches this same ledger write a second time is already blocked by
    ledger_row_inserted above, so this only ever runs once per diagnosis.

    diagnosis_correct is populated ONLY when simulator_latent_state ground
    truth exists for this payment (app_role has it; this session already
    is app_role) -- NULL for a genuinely live payment, which has no ground
    truth to check the diagnosis against. action_effective is the only
    thing ever answerable in production (see _derive_outcome_fields).
    """
    from sqlalchemy import text

    observed_outcome, action_effective = _derive_outcome_fields(
        verdict=verdict, actual_recovery_paise=actual_recovery_paise, outcome=outcome
    )

    diagnosis_correct = None
    row = (
        (
            await session.execute(
                text(
                    "SELECT d.root_cause, l.true_failure_type FROM diagnoses d "
                    "LEFT JOIN simulator_latent_state l ON l.payment_id = d.payment_id "
                    "WHERE d.diagnosis_id = :did"
                ),
                {"did": diagnosis_id},
            )
        )
        .mappings()
        .first()
    )
    if row is not None and row["true_failure_type"] is not None:
        expected = _TRUE_FAILURE_TYPE_TO_ROOT_CAUSE.get(row["true_failure_type"])
        diagnosis_correct = expected is not None and expected == row["root_cause"]

    await session.execute(
        text(
            "INSERT INTO diagnosis_outcomes "
            "(outcome_id, diagnosis_id, chosen_action, observed_outcome, diagnosis_correct, "
            "action_effective, counterfactual_result) "
            "VALUES (:oid, :did, :action, :observed, :correct, :effective, :counterfactual) "
            "ON CONFLICT (diagnosis_id) DO NOTHING"
        ),
        {
            "oid": str(uuid.uuid4()),
            "did": diagnosis_id,
            "action": chosen_action,
            "observed": observed_outcome,
            "correct": diagnosis_correct,
            "effective": action_effective,
            "counterfactual": json.dumps(
                {
                    "actual_recovery_paise": actual_recovery_paise,
                    "baseline_recovery_paise": baseline_amount or 0,
                    "incremental_recovery_paise": incremental_recovery_paise,
                }
            ),
        },
    )


# ─── Sync writer (workers/execution_worker.py) ─────────────────────────────


def populate_ledger_and_audit_sync(
    conn,
    *,
    payment_id: str,
    candidate_id: str | None,
    decision_id: str,
    verdict: str,
    chosen_action: str,
    recovery_prob_bps: int,
    cost_paise: int,
    actual_recovery_paise: int,
    recovery_id: str | None,
    diagnosis_id: str | None = None,
    outcome: str | None = None,
) -> None:
    from sqlalchemy import text

    payment_row = conn.execute(
        text("SELECT amount_paise FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
    ).first()
    amount_paise = payment_row[0] if payment_row else 0

    baseline_row = (
        conn.execute(
            text(
                "SELECT outcome, recovered_amount_paise FROM baseline_runs "
                "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        )
        .mappings()
        .first()
    )
    baseline_outcome = baseline_row["outcome"] if baseline_row else None
    baseline_amount = baseline_row["recovered_amount_paise"] if baseline_row else None

    entry = compute_ledger_entry(
        amount_paise=amount_paise,
        recovery_prob_bps=recovery_prob_bps,
        actual_recovery_paise=actual_recovery_paise,
        intervention_cost_paise=cost_paise,
        baseline_recovered_amount_paise=baseline_amount,
        baseline_outcome=baseline_outcome,
    )

    # ON CONFLICT DO NOTHING for the same reason as the async writer above
    # (Task S1) -- this path is already protected in practice by
    # execute_with_idempotency's advisory-lock + recoveries-table check
    # (a redelivered execution job finds its result already recorded and
    # never re-enters action_fn at all), but the table-level constraint
    # applies regardless of which writer hits it, so this must not crash
    # with an IntegrityError if it's ever reached twice for the same payment.
    result = conn.execute(
        text(
            """
            INSERT INTO recovery_ledger
                (ledger_id, payment_id, revenue_at_risk_paise, expected_recovery_paise,
                 actual_recovery_paise, baseline_outcome, incremental_recovery_paise,
                 intervention_cost_paise, net_recovery_paise)
            VALUES
                (:ledger_id, :pid, :revenue_at_risk, :expected_recovery, :actual_recovery,
                 :baseline_outcome, :incremental, :intervention_cost, :net_recovery)
            ON CONFLICT (payment_id) DO NOTHING
            RETURNING ledger_id
            """
        ),
        {
            "ledger_id": str(uuid.uuid4()),
            "pid": payment_id,
            "revenue_at_risk": entry.revenue_at_risk_paise,
            "expected_recovery": entry.expected_recovery_paise,
            "actual_recovery": entry.actual_recovery_paise,
            "baseline_outcome": entry.baseline_outcome,
            "incremental": entry.incremental_recovery_paise,
            "intervention_cost": entry.intervention_cost_paise,
            "net_recovery": entry.net_recovery_paise,
        },
    )
    ledger_row_inserted = result.first() is not None

    if ledger_row_inserted:
        _record_ledger_metrics(entry)
        conn.execute(
            text(
                "INSERT INTO audit_log (audit_id, payment_id, diagnosis_id, candidate_id, "
                "decision_id, recovery_id, summary) "
                "VALUES (:aid, :pid, :did, :cid, :decid, :rid, :summary)"
            ),
            {
                "aid": str(uuid.uuid4()),
                "pid": payment_id,
                "did": diagnosis_id,
                "cid": candidate_id,
                "decid": decision_id,
                "rid": recovery_id,
                "summary": build_audit_summary(payment_id, chosen_action, verdict, outcome),
            },
        )
        if outcome == "SUCCESS":
            conn.execute(
                text("UPDATE payments SET status = :status WHERE payment_id = :pid"),
                {"status": RECOVERED_STATUS, "pid": payment_id},
            )
        if diagnosis_id is not None:
            _record_diagnosis_outcome_sync(
                conn,
                diagnosis_id=diagnosis_id,
                payment_id=payment_id,
                verdict=verdict,
                chosen_action=chosen_action,
                outcome=outcome,
                actual_recovery_paise=entry.actual_recovery_paise,
                baseline_amount=baseline_amount,
                incremental_recovery_paise=entry.incremental_recovery_paise,
            )
        conn.commit()
    else:
        # Sync mirror of populate_ledger_and_audit_async's else branch --
        # see that docstring (Domain Audit finding #2). Reachable here via
        # workers/execution_worker.py: a payment whose FIRST decision
        # cycle already wrote a terminal ledger row (e.g. BLOCK via
        # consumer.py) later gets a genuinely NEW executed attempt that
        # reaches SUCCESS -- must correct the existing row, not silently
        # drop the real recovered money.
        conn.commit()  # release this connection's row lock before the correction path re-reads it
        correct_ledger_and_audit_sync(
            conn,
            payment_id=payment_id,
            candidate_id=candidate_id,
            decision_id=decision_id,
            verdict=verdict,
            chosen_action=chosen_action,
            recovery_prob_bps=recovery_prob_bps,
            cost_paise=cost_paise,
            actual_recovery_paise=actual_recovery_paise,
            recovery_id=recovery_id,
            diagnosis_id=diagnosis_id,
            outcome=outcome or "UNKNOWN",
            correction_reason=(
                "a later execution for this payment reached a new terminal outcome "
                "after an earlier one already occupied this payment's ledger row"
            ),
        )


def correct_ledger_and_audit_sync(
    conn,
    *,
    payment_id: str,
    candidate_id: str | None,
    decision_id: str,
    verdict: str,
    chosen_action: str,
    recovery_prob_bps: int,
    cost_paise: int,
    actual_recovery_paise: int,
    recovery_id: str | None,
    diagnosis_id: str | None,
    outcome: str,
    correction_reason: str,
) -> None:
    """Sync mirror of correct_ledger_and_audit_async -- see its docstring
    for the full invariant (Domain Audit finding #2). Self-enforces
    _should_correct_ledger the same way; a call that doesn't qualify is a
    silent, correct no-op."""
    from sqlalchemy import text

    payment_row = conn.execute(
        text("SELECT amount_paise FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
    ).first()
    amount_paise = payment_row[0] if payment_row else 0

    old_ledger_row = (
        conn.execute(
            text(
                "SELECT actual_recovery_paise, incremental_recovery_paise "
                "FROM recovery_ledger WHERE payment_id = :pid"
            ),
            {"pid": payment_id},
        )
        .mappings()
        .first()
    )
    if old_ledger_row is None:
        return

    should_correct, clamped_actual_recovery_paise = _should_correct_ledger(
        existing_actual_recovery_paise=old_ledger_row["actual_recovery_paise"],
        new_actual_recovery_paise=actual_recovery_paise,
        amount_paise_cap=amount_paise,
    )
    if not should_correct:
        return

    baseline_row = (
        conn.execute(
            text(
                "SELECT outcome, recovered_amount_paise FROM baseline_runs "
                "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        )
        .mappings()
        .first()
    )
    baseline_outcome = baseline_row["outcome"] if baseline_row else None
    baseline_amount = baseline_row["recovered_amount_paise"] if baseline_row else None

    entry = compute_ledger_entry(
        amount_paise=amount_paise,
        recovery_prob_bps=recovery_prob_bps,
        actual_recovery_paise=clamped_actual_recovery_paise,
        intervention_cost_paise=cost_paise,
        baseline_recovered_amount_paise=baseline_amount,
        baseline_outcome=baseline_outcome,
    )

    conn.execute(
        text(
            "UPDATE recovery_ledger SET actual_recovery_paise = :actual, "
            "incremental_recovery_paise = :incremental, net_recovery_paise = :net, "
            "baseline_outcome = :baseline_outcome WHERE payment_id = :pid"
        ),
        {
            "actual": entry.actual_recovery_paise,
            "incremental": entry.incremental_recovery_paise,
            "net": entry.net_recovery_paise,
            "baseline_outcome": entry.baseline_outcome,
            "pid": payment_id,
        },
    )

    conn.execute(
        text(
            "INSERT INTO audit_log (audit_id, payment_id, diagnosis_id, candidate_id, "
            "decision_id, recovery_id, summary) "
            "VALUES (:aid, :pid, :did, :cid, :decid, :rid, :summary)"
        ),
        {
            "aid": str(uuid.uuid4()),
            "pid": payment_id,
            "did": diagnosis_id,
            "cid": candidate_id,
            "decid": decision_id,
            "rid": recovery_id,
            "summary": (
                f"Payment {payment_id}: CORRECTED action={chosen_action}, policy={verdict}, "
                f"outcome={outcome} ({correction_reason})"
            ),
        },
    )

    if outcome == "SUCCESS":
        conn.execute(
            text("UPDATE payments SET status = :status WHERE payment_id = :pid"),
            {"status": RECOVERED_STATUS, "pid": payment_id},
        )

    if diagnosis_id is not None:
        observed_outcome, action_effective = _derive_outcome_fields(
            verdict=verdict, actual_recovery_paise=entry.actual_recovery_paise, outcome=outcome
        )
        diag_row = (
            conn.execute(
                text(
                    "SELECT d.root_cause, l.true_failure_type FROM diagnoses d "
                    "LEFT JOIN simulator_latent_state l ON l.payment_id = d.payment_id "
                    "WHERE d.diagnosis_id = :did"
                ),
                {"did": diagnosis_id},
            )
            .mappings()
            .first()
        )
        diagnosis_correct = None
        if diag_row is not None and diag_row["true_failure_type"] is not None:
            expected = _TRUE_FAILURE_TYPE_TO_ROOT_CAUSE.get(diag_row["true_failure_type"])
            diagnosis_correct = expected is not None and expected == diag_row["root_cause"]

        conn.execute(
            text(
                "UPDATE diagnosis_outcomes SET observed_outcome = :observed, "
                "diagnosis_correct = :correct, action_effective = :effective, "
                "counterfactual_result = :counterfactual WHERE diagnosis_id = :did"
            ),
            {
                "observed": observed_outcome,
                "correct": diagnosis_correct,
                "effective": action_effective,
                "counterfactual": json.dumps(
                    {
                        "actual_recovery_paise": entry.actual_recovery_paise,
                        "baseline_recovery_paise": baseline_amount or 0,
                        "incremental_recovery_paise": entry.incremental_recovery_paise,
                    }
                ),
                "did": diagnosis_id,
            },
        )

    conn.commit()

    recovered_delta = entry.actual_recovery_paise - old_ledger_row["actual_recovery_paise"]
    if recovered_delta > 0:
        revenue_recovered_paise_total.inc(recovered_delta)
    incremental_revenue_paise_total.inc(
        entry.incremental_recovery_paise - old_ledger_row["incremental_recovery_paise"]
    )


def _record_diagnosis_outcome_sync(
    conn,
    *,
    diagnosis_id: str,
    payment_id: str,  # unused -- see _record_diagnosis_outcome_async's own comment
    verdict: str,
    chosen_action: str,
    outcome: str | None,
    actual_recovery_paise: int,
    baseline_amount: int | None,
    incremental_recovery_paise: int,
) -> None:
    """Sync mirror of _record_diagnosis_outcome_async -- see its docstring."""
    from sqlalchemy import text

    observed_outcome, action_effective = _derive_outcome_fields(
        verdict=verdict, actual_recovery_paise=actual_recovery_paise, outcome=outcome
    )

    diagnosis_correct = None
    row = (
        conn.execute(
            text(
                "SELECT d.root_cause, l.true_failure_type FROM diagnoses d "
                "LEFT JOIN simulator_latent_state l ON l.payment_id = d.payment_id "
                "WHERE d.diagnosis_id = :did"
            ),
            {"did": diagnosis_id},
        )
        .mappings()
        .first()
    )
    if row is not None and row["true_failure_type"] is not None:
        expected = _TRUE_FAILURE_TYPE_TO_ROOT_CAUSE.get(row["true_failure_type"])
        diagnosis_correct = expected is not None and expected == row["root_cause"]

    conn.execute(
        text(
            "INSERT INTO diagnosis_outcomes "
            "(outcome_id, diagnosis_id, chosen_action, observed_outcome, diagnosis_correct, "
            "action_effective, counterfactual_result) "
            "VALUES (:oid, :did, :action, :observed, :correct, :effective, :counterfactual) "
            "ON CONFLICT (diagnosis_id) DO NOTHING"
        ),
        {
            "oid": str(uuid.uuid4()),
            "did": diagnosis_id,
            "action": chosen_action,
            "observed": observed_outcome,
            "correct": diagnosis_correct,
            "effective": action_effective,
            "counterfactual": json.dumps(
                {
                    "actual_recovery_paise": actual_recovery_paise,
                    "baseline_recovery_paise": baseline_amount or 0,
                    "incremental_recovery_paise": incremental_recovery_paise,
                }
            ),
        },
    )
