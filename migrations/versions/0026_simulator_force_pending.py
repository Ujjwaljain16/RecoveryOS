"""Simulator forced-pending override for demo scenarios

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-02

Adds simulator_latent_state.force_pending_until_reconciled (boolean,
default false). Purely additive and opt-in: every existing row and every
row the real simulator/episode/benchmark pipeline ever inserts gets the
default (false) and is completely unaffected -- SimulatorAdapter.retry()
(integrations/razorpay/adapter.py) only takes the new short-circuit branch
when this is explicitly set true, which today only apps/api/routers/
simulate.py's "world_changed" demo scenario does. Table-level GRANT/REVOKE
from migration 0003 already covers this new column (Postgres grants are
table-scoped here, no column-level privileges in play) -- diagnoser_role
still has zero access to this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "simulator_latent_state",
        sa.Column(
            "force_pending_until_reconciled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("simulator_latent_state", "force_pending_until_reconciled")
