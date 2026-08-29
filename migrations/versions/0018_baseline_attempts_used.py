"""baseline_runs.attempts_used -- Domain Audit finding #6 (fair baseline).

Revision ID: 0018
Revises: 0017

The pre-fix baseline strategy (services/pipeline/baseline.py) modeled
exactly ONE retry attempt, while RecoveryOS's own path can execute up to
policy_configs.max_retries real attempts (CooldownRule/RetryLimitRule/
scheduled_reevaluations) -- an unfair comparison the audit flagged:
"how much of the incremental-revenue number reflects 'we tried more
times' rather than 'we chose better actions'?"

compute_and_persist_fair_baseline_run() (services/pipeline/baseline.py)
gives the naive baseline the SAME attempt budget, keeping its DECISION
POLICY naive (always retry, no smart targeting) -- only the number of
attempts is made fair. `attempts_used` records how many simulated
attempts that run actually consumed before success/exhaustion, needed to
decompose the headline number into "attributable to more attempts" vs
"attributable to better decisions" (see docs/phase8_headline_number.md's
Domain Audit finding #6 section).

Nullable, additive -- existing baseline_runs rows (the original
single-attempt baseline) simply have NULL here, meaning "not applicable
to that run."
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("baseline_runs", sa.Column("attempts_used", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("baseline_runs", "attempts_used")
