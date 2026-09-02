"""baseline_runs.blocked_by_rule -- compliance-aware fair baseline comparator.

Revision ID: 0024
Revises: 0023

The evaluation-only compliance-aware baseline (services/pipeline/baseline.py's
compute_and_persist_compliance_aware_baseline_run) runs the SAME
services.policy_engine.evaluate() compliance-rule chain RecoveryOS itself
obeys against each simulated attempt, instead of the compliance-blind
"retry everything except a known-hopeless failure" heuristic the existing
fair baseline uses. When a BLOCK/ESCALATE verdict stops that run short of
max_retries, this column names the rule that stopped it (e.g.
"AutopayExecutionWindowRule", "OptOutRule") -- needed to decompose the
comparator's own gap vs RecoveryOS into "blocked by the same compliance
constraint" vs "a genuine decision-quality difference."

Nullable, additive -- every existing baseline_runs row (the single-attempt
baseline, the existing compliance-blind fair baseline) has no concept of a
blocking rule and simply has NULL here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("baseline_runs", sa.Column("blocked_by_rule", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("baseline_runs", "blocked_by_rule")
