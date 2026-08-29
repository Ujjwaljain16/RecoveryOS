"""Drop recovery_episodes — dead table, Task M3.

Revision ID: 0012
Revises: 0011

0004_episodes.py created recovery_episodes to persist generated episode
batches, but the actual episode dataset pipeline (simulator/episodes/
generator.py's RecoveryEpisode dataclass, persisted by
simulator/dataset/builder.py) writes to Parquet, not to this table.
Confirmed via a full-codebase grep: zero INSERT/SELECT statements and zero
ORM model reference this table anywhere outside migrations/ itself — it is
schema for a design that was superseded before ever being wired up. TRD and
gaps.md never mention recovery_episodes, so there's no doc claim to
reconcile either.

Per project convention, 0004 itself is untouched (migrations are historical
record) — this migration undoes it instead.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("recovery_episodes")


def downgrade() -> None:
    op.create_table(
        "recovery_episodes",
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("simulation_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("bank", sa.Text(), nullable=False),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("is_returning_customer", sa.Boolean(), nullable=False),
        sa.Column("customer_ltv_decile", sa.Integer(), nullable=False),
        sa.Column("initial_failure_code", sa.Text(), nullable=True),
        sa.Column("initial_failure_class", sa.Text(), nullable=True),
        sa.Column("hour_of_day", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_episode_duration_sec", sa.Integer(), nullable=True),
        sa.Column("actual_outcome", sa.Text(), nullable=False),
        sa.Column("actual_recovered", sa.Boolean(), nullable=True),
        sa.Column("optimal_recovery_action", sa.Text(), nullable=True),
        sa.Column("expected_value_of_retry_paise", sa.BigInteger(), nullable=True),
        sa.Column("latent_patience_at_decision", sa.Numeric(8, 4), nullable=True),
        sa.Column("latent_bank_health_at_decision", sa.Numeric(8, 4), nullable=True),
        sa.Column("true_recovery_prob_bps_at_decision", sa.Integer(), nullable=True),
        sa.Column("split_name", sa.Text(), nullable=True),
        sa.Column("clock_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("episode_id"),
        sa.ForeignKeyConstraint(
            ["simulation_id"], ["simulator_manifests.simulation_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.payment_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_recovery_episodes_simulation_id", "recovery_episodes", ["simulation_id"])
    op.create_index("ix_recovery_episodes_payment_id", "recovery_episodes", ["payment_id"])
    op.create_index("ix_recovery_episodes_split_name", "recovery_episodes", ["split_name"])
    op.create_index(
        "ix_recovery_episodes_clock_timestamp", "recovery_episodes", ["clock_timestamp"]
    )
    op.execute("REVOKE ALL ON recovery_episodes FROM diagnoser_role;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON recovery_episodes TO app_role;")
