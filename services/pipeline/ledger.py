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

import uuid
from dataclasses import dataclass

BPS_SCALE = 10_000


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

    await session.execute(
        text(
            """
            INSERT INTO recovery_ledger
                (ledger_id, payment_id, revenue_at_risk_paise, expected_recovery_paise,
                 actual_recovery_paise, baseline_outcome, incremental_recovery_paise,
                 intervention_cost_paise, net_recovery_paise)
            VALUES
                (:ledger_id, :pid, :revenue_at_risk, :expected_recovery, :actual_recovery,
                 :baseline_outcome, :incremental, :intervention_cost, :net_recovery)
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
    await session.commit()


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

    conn.execute(
        text(
            """
            INSERT INTO recovery_ledger
                (ledger_id, payment_id, revenue_at_risk_paise, expected_recovery_paise,
                 actual_recovery_paise, baseline_outcome, incremental_recovery_paise,
                 intervention_cost_paise, net_recovery_paise)
            VALUES
                (:ledger_id, :pid, :revenue_at_risk, :expected_recovery, :actual_recovery,
                 :baseline_outcome, :incremental, :intervention_cost, :net_recovery)
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
    conn.commit()
