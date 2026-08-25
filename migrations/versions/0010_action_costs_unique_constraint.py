"""Real UNIQUE constraint on action_costs — Task M1, pre-Phase-8 audit.

Revision ID: 0010
Revises: 0009

gaps.md §A.2 always specified a COALESCE-trick partial/functional unique
index on (merchant_id, action_type, version) — merchant_id is nullable
(NULL = platform default), so a plain UNIQUE(merchant_id, action_type,
version) wouldn't work: Postgres treats every NULL as distinct, so it
would silently allow unlimited duplicate platform-default rows. This was
never actually implemented in 0001 (only a plain, non-unique btree index) —
flagged during the apps/ audit, re-flagged during the migrations/ audit,
fixed here.

Without this, a duplicate (merchant_id, action_type, version) row fails at
READ time instead of INSERT time: services/recovery_engine/evi.py's
get_action_cost() uses .scalar_one_or_none(), which raises
MultipleResultsFound the first time a duplicate happens to exist — an
ugly, hard-to-diagnose failure mid-decision, instead of a clean
IntegrityError at the moment the bad row is written.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None

SENTINEL_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_action_cost_merchant_action;")
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_action_cost_merchant_action
        ON action_costs (
            COALESCE(merchant_id, '{SENTINEL_UUID}'::uuid),
            action_type,
            version
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_action_cost_merchant_action;")
    op.create_index(
        "idx_action_cost_merchant_action",
        "action_costs",
        ["merchant_id", "action_type", "version"],
    )
