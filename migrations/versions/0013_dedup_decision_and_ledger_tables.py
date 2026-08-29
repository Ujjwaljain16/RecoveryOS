"""Dedup constraints on recovery_ledger, diagnoses, candidate_actions,
policy_decisions -- Task S1, pre-Phase-8 audit.

Revision ID: 0013
Revises: 0012

recovery_ledger had NO unique constraint on payment_id and its writer
(services/pipeline/ledger.py::populate_ledger_and_audit_async) did a bare
INSERT -- combined with services/pipeline/consumer.py's retry-on-any-
exception pattern (a message stays pending, and gets redelivered, even if
the ONLY failure was the xack call itself, which runs AFTER a fully
successful commit), a redelivered message reprocesses the payment from
scratch and can insert a SECOND recovery_ledger row for the same payment.
TRD Sec.7's incremental-revenue number is a raw SQL SUM() over exactly this
table -- a duplicate row silently inflates the one number the whole
evaluation harness (and the pitch) rests on.

diagnoses/candidate_actions/policy_decisions have the same non-idempotent
write pattern, lower urgency (downstream readers all use
ORDER BY created_at DESC LIMIT 1, so they tolerate duplicates functionally)
but fixed here while touching the same retry-safety surface. Unlike
recovery_ledger, a payment CAN legitimately have multiple diagnoses/
decisions across multiple real retry attempts over its lifetime -- so
these are NOT deduped on payment_id alone. Each gets a new nullable
source_event_id column (the triggering stream:risk_engine message's
source_event_id, threaded through from services/pipeline/consumer.py),
deduped on (payment_id, source_event_id[, action_type]). Postgres treats
every NULL as distinct for uniqueness purposes, so existing callers that
don't have an event context (tests, any direct invocation) keep working
unchanged -- they simply never collide with each other, exactly as before
this migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_recovery_ledger_payment", "recovery_ledger", ["payment_id"])

    op.add_column("diagnoses", sa.Column("source_event_id", sa.UUID(), nullable=True))
    op.create_unique_constraint(
        "uq_diagnoses_payment_event", "diagnoses", ["payment_id", "source_event_id"]
    )

    op.add_column("candidate_actions", sa.Column("source_event_id", sa.UUID(), nullable=True))
    op.create_unique_constraint(
        "uq_candidate_actions_payment_event_action",
        "candidate_actions",
        ["payment_id", "source_event_id", "action_type"],
    )

    op.add_column("policy_decisions", sa.Column("source_event_id", sa.UUID(), nullable=True))
    op.create_unique_constraint(
        "uq_policy_decisions_payment_event", "policy_decisions", ["payment_id", "source_event_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_policy_decisions_payment_event", "policy_decisions", type_="unique")
    op.drop_column("policy_decisions", "source_event_id")

    op.drop_constraint(
        "uq_candidate_actions_payment_event_action", "candidate_actions", type_="unique"
    )
    op.drop_column("candidate_actions", "source_event_id")

    op.drop_constraint("uq_diagnoses_payment_event", "diagnoses", type_="unique")
    op.drop_column("diagnoses", "source_event_id")

    op.drop_constraint("uq_recovery_ledger_payment", "recovery_ledger", type_="unique")
