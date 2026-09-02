"""gaps.md sec:A.1 -- audit_log gains a nullable customer_id, for actions that
are about a customer but not about any specific payment.

Revision ID: 0023
Revises: 0022

Building the customer opt-out endpoint (POST /v1/customers/{id}/opt-out,
gaps.md sec:A.1) surfaced a real schema gap: neither existing append-only
trail table has a home for "customer X did Y" when there's no payment to
anchor it to. `events.payment_id` is NOT NULL (recoveryos/models.py's Event
class) -- an opt-out can happen before the customer has ever failed a
payment, so there may be no payment_id to attach to at all. `audit_log`
already has the right shape otherwise (nullable FKs, a free-text `summary`
for the Audit Explorer) but has no customer_id column, only payment/
diagnosis/candidate/decision/recovery FKs -- none of which fit a bare
customer-level action either.

This is the same nullable-FK pattern audit_log already uses for its other
five columns, extended by exactly one more, not a new mechanism.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("customers.customer_id"),
            nullable=True,
        ),
    )
    op.create_index("idx_audit_log_customer", "audit_log", ["customer_id"])


def downgrade() -> None:
    op.drop_index("idx_audit_log_customer", table_name="audit_log")
    op.drop_column("audit_log", "customer_id")
