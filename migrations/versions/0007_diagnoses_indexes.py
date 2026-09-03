"""
Migration 0007 — indexes on diagnoses

diagnoses had zero indexes beyond its primary key. The diagnosis
engine and the audit explorer both need "latest diagnosis for payment X"
and "all diagnoses for cohort Y" — without an index those are sequential
scans over every diagnosis ever written.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_diagnoses_payment", "diagnoses", ["payment_id", "created_at"])
    op.create_index("idx_diagnoses_cohort", "diagnoses", ["cohort_id"])


def downgrade() -> None:
    op.drop_index("idx_diagnoses_cohort", table_name="diagnoses")
    op.drop_index("idx_diagnoses_payment", table_name="diagnoses")
