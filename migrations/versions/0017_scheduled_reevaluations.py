"""scheduled_reevaluations -- continuous replanning for RETRY_LATER
decisions (Task REPLAN1).

Revision ID: 0017
Revises: 0016

Before this: RETRY_LATER was a label with no behavior behind it --
workers/execution_worker.py set scheduled_for=now() regardless of which
action was chosen, so RETRY_LATER executed immediately, identically to
RETRY_NOW. This table is the real deferred-execution mechanism: when
RETRY_LATER is chosen, orchestrator.py writes a row here with a genuinely
future scheduled_for (services/recovery_engine/timing.py's
compute_retry_delay()) instead of enqueueing an execution job at all.
workers/retry_scheduler.py polls for rows whose time has come and
RE-EVALUATES the payment from scratch (fresh anomaly state, fresh attempt
count, fresh cooldown/time-of-day compliance checks) rather than blindly
executing the stale earlier decision -- the whole point of deferring is
that the world might be different by then.

Deliberately a SEPARATE table from `recoveries`, not a new outcome value
on it: `recoveries` means "an attempt that executed or is executing" --
overloading it with "intent for a future attempt" would corrupt
_fetch_retry_history's MAX(attempt_number)+1 counting (a not-yet-fired
scheduled row would get double-counted as an attempt that never actually
happened). Same separation-of-concerns principle as diagnosis_hypotheses/
investigation_steps being kept separate from diagnoses (migration 0015).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_reevaluations",
        sa.Column(
            "reevaluation_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("policy_decisions.decision_id"),
            nullable=False,
        ),
        sa.Column(
            "diagnosis_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("diagnoses.diagnosis_id"),
            nullable=True,
        ),
        # The triggering event that produced the RETRY_LATER decision --
        # dedup key (same S1 discipline as diagnoses/candidate_actions/
        # policy_decisions, migration 0013): a redelivered message for the
        # SAME triggering event must not create a second schedule entry.
        sa.Column("source_event_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("scheduled_for", postgresql.TIMESTAMP(timezone=True), nullable=False),
        # PENDING -> FIRED (the scheduler claimed and re-evaluated it) or
        # CANCELLED (a future hook -- e.g. a webhook resolves the payment
        # before its scheduled time; not populated by anything yet).
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        # Set atomically by the scheduler's own claim UPDATE (status='PENDING'
        # -> 'FIRED' WHERE reevaluation_id=:id AND status='PENDING' RETURNING
        # *) -- this IS the concurrency-safety mechanism (two scheduler
        # replicas racing on the same row: only one UPDATE actually matches
        # the WHERE clause), not a separate advisory lock.
        sa.Column("claimed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        # The fresh event_id minted when this row fires, threaded into the
        # new decision cycle exactly like a real risk-engine trigger event --
        # lets the new diagnoses/candidate_actions/policy_decisions rows
        # dedup correctly against a redelivery of THIS firing, same as any
        # other pipeline entry point.
        sa.Column("fired_source_event_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'FIRED', 'CANCELLED')", name="ck_scheduled_reevaluations_status"
        ),
        sa.UniqueConstraint(
            "payment_id", "source_event_id", name="uq_scheduled_reevaluations_payment_event"
        ),
    )
    op.create_index(
        "idx_scheduled_reevaluations_due",
        "scheduled_reevaluations",
        ["status", "scheduled_for"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON scheduled_reevaluations TO app_role;"
    )


def downgrade() -> None:
    op.drop_table("scheduled_reevaluations")
