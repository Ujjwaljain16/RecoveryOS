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

BPS_SCALE = 10_000

# Task E1 (Phase 8 Scenario 4 fix): a payment that reaches a real SUCCESS
# outcome is no longer 'failed' -- distinct from 'success' (which means the
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


def build_audit_summary(
    payment_id: str, chosen_action: str, verdict: str, outcome: str | None = None
) -> str:
    if outcome is not None:
        return f"Payment {payment_id}: action={chosen_action}, policy={verdict}, outcome={outcome}"
    return f"Payment {payment_id}: action={chosen_action}, policy={verdict}, no execution attempted"


# Task AGENT1, agent-design review point 4 -- the SAME mapping Phase 8's
# AI-eval used (docs/phase8_ai_evaluation.md) between the simulator's
# hidden ground-truth failure type and the diagnoser's RootCause vocabulary.
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


async def _record_diagnosis_outcome_async(
    session,
    *,
    diagnosis_id: str,
    payment_id: str,
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
        await session.execute(
            text(
                "SELECT d.root_cause, l.true_failure_type FROM diagnoses d "
                "LEFT JOIN simulator_latent_state l ON l.payment_id = d.payment_id "
                "WHERE d.diagnosis_id = :did"
            ),
            {"did": diagnosis_id},
        )
    ).mappings().first()
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


def _record_diagnosis_outcome_sync(
    conn,
    *,
    diagnosis_id: str,
    payment_id: str,
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
