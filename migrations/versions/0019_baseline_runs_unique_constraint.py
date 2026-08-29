"""baseline_runs unique(payment_id, experiment_id) -- Adversarial Audit Verdict.

Revision ID: 0019
Revises: 0018

services/pipeline/baseline.py's two baseline functions have always been
idempotent only at the application level (SELECT existing row, INSERT only
if absent) -- baseline_runs itself carried no DB-level constraint stopping
two concurrent calls for the same (payment_id, experiment_id) from both
passing the SELECT check and both INSERTing, producing duplicate rows that
would double-count in any SUM() over baseline_runs.

Adds the missing unique constraint and switches both functions (services/
pipeline/baseline.py) to this codebase's standard S1 dedup pattern --
INSERT ... ON CONFLICT DO NOTHING ... RETURNING, re-SELECT on conflict --
already used for diagnoses/candidate_actions/policy_decisions elsewhere in
this codebase, instead of a race-prone check-then-insert.
"""

from __future__ import annotations

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_baseline_runs_payment_experiment",
        "baseline_runs",
        ["payment_id", "experiment_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_baseline_runs_payment_experiment", "baseline_runs", type_="unique")
