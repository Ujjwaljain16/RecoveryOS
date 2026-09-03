"""Fix RETRY_LATER's platform-default friction seed — a real sanity-check finding.

Revision ID: 0009
Revises: 0008

Found running the 20-payment manual sanity check (steering directive §16):
every one of 20 real payments selected RETRY_LATER, including a PERMANENT/
CARD_EXPIRED failure at 17.71% probability, because migrations/0001's seed
gave RETRY_LATER cost=0/friction_base=0 while RETRY_NOW got friction_base=10.
Outside an active systemic anomaly, services/recovery_engine/timing.py
correctly gives both actions the SAME probability — so with equal
probability, RETRY_LATER was strictly cheaper by construction and won every
single time, for every payment, regardless of whether waiting genuinely
helps. That's not the timing mechanism doing its job; it's a seed-data gap
making RETRY_NOW structurally unable to ever win.

This sets RETRY_LATER's platform-default friction_base_paise to a small
nonzero "cost of delay" (20, double RETRY_NOW's 10) — waiting has a real,
if modest, economic cost. RETRY_NOW now wins the normal case (equal
probability, RETRY_LATER costs more to choose), and RETRY_LATER can still
win specifically when timing.py's anomaly-driven probability adjustment
gives it a big enough edge to overcome this gap — which is the ONLY
grounded case this system claims RETRY_LATER should win on (see gaps.md
§C.1). Merchant-specific action_costs overrides are untouched by this
migration (COALESCE... merchant_id IS NULL only).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

OLD_FRICTION_BASE_PAISE = 0
NEW_FRICTION_BASE_PAISE = 20


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE action_costs SET friction_base_paise = :new_val "
            "WHERE merchant_id IS NULL AND action_type = 'RETRY_LATER'"
        ),
        {"new_val": NEW_FRICTION_BASE_PAISE},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE action_costs SET friction_base_paise = :old_val "
            "WHERE merchant_id IS NULL AND action_type = 'RETRY_LATER'"
        ),
        {"old_val": OLD_FRICTION_BASE_PAISE},
    )
