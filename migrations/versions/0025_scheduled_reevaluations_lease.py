"""scheduled_reevaluations lease/reclaim -- adversarial sweep finding #50.

Revision ID: 0025
Revises: 0024

Adversarial E2E sweep finding: workers/retry_scheduler.py's claim_reevaluation
flips PENDING -> FIRED with a bare `WHERE status = 'PENDING'` UPDATE and
nothing else ever reclaims a FIRED row. If a worker crashes AFTER winning the
claim but BEFORE process_payment_failure finishes, that row is FIRED forever
-- a permanent orphan, confirmed live by
tests/integration/test_retry_scheduler.py::test_crash_after_claim_leaves_the_row_permanently_fired_no_auto_retry.
Documented in the module's own old comment as intentional ("stays FIRED,
will not be retried automatically"), but this violates the intended
autonomous-recovery reliability guarantee.

Fix: a lease. `lease_expires_at` is set to now + REEVALUATION_LEASE_SECONDS
(services/recovery_engine/scheduling.py) at claim time. A row is now
"claimable" if it is PENDING-and-due OR FIRED-with-an-expired-lease --
claim_reevaluation's atomic UPDATE...WHERE clause is extended to match
either case, so the exact same single-UPDATE concurrency-safety mechanism
covers both a fresh claim and a reclaim. On success, the new terminal status
'COMPLETED' is set (a claimed row that finished its work correctly). Before
reprocessing a RECLAIMED row, workers/retry_scheduler.py checks whether the
row's mission has already moved past OBSERVING_OUTCOME (i.e. the crash
happened after real progress was durably made through some other path) --
if so the row is marked 'CANCELLED' (stale/superseded) instead of being
reprocessed, which is what actually prevents the duplicate-mission-event
hazard a naive "just flip FIRED back to PENDING" fix would have introduced.

Nullable, additive -- every existing row (status PENDING/FIRED/CANCELLED)
gets lease_expires_at = NULL, which is simply never a match for "expired"
comparisons (NULL < now() is NULL, not true), so no pre-existing row is
retroactively treated as reclaimable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_reevaluations",
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_scheduled_reevaluations_status", "scheduled_reevaluations", type_="check")
    op.create_check_constraint(
        "ck_scheduled_reevaluations_status",
        "scheduled_reevaluations",
        "status IN ('PENDING', 'FIRED', 'CANCELLED', 'COMPLETED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_scheduled_reevaluations_status", "scheduled_reevaluations", type_="check")
    op.create_check_constraint(
        "ck_scheduled_reevaluations_status",
        "scheduled_reevaluations",
        "status IN ('PENDING', 'FIRED', 'CANCELLED')",
    )
    op.drop_column("scheduled_reevaluations", "lease_expires_at")
