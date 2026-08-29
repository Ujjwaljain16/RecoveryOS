"""Simulator latent state and manifests — TRD §6, gaps.md §B.1

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20

What this migration does:
  1. Creates simulator_manifests table to track simulation runs (seed, versions, configs).
  2. Creates simulator_latent_state table with hidden parameters for ground truth generation.
  3. Hardens permissions:
       - app_role gets SELECT, INSERT, UPDATE, DELETE on both tables.
       - diagnoser_role and any inference-related roles get ZERO GRANTS on both tables.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Create simulator_manifests ──────────────────────────────────────────
    op.create_table(
        "simulator_manifests",
        sa.Column("simulation_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column("scenario_config", postgresql.JSONB(), nullable=False),
        sa.Column("latent_function_version", sa.Text(), nullable=False),
        sa.Column("total_payments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── 2. Create simulator_latent_state ───────────────────────────────────────
    op.create_table(
        "simulator_latent_state",
        sa.Column("latent_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "simulation_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("simulator_manifests.simulation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("customer_patience_score", sa.Numeric(6, 4), nullable=False),
        sa.Column("bank_latent_health", sa.Numeric(6, 4), nullable=False),
        sa.Column("latent_network_noise", sa.Numeric(6, 4), nullable=False),
        sa.Column("latent_customer_propensity", sa.Numeric(6, 4), nullable=False),
        sa.Column("true_recovery_prob_bps", sa.BigInteger(), nullable=False),
        sa.Column("true_failure_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "idx_latent_simulation",
        "simulator_latent_state",
        ["simulation_id"],
    )

    # ── 3. DB Role Permissions Hardening ──────────────────────────────────────
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
        GRANT SELECT, INSERT, UPDATE, DELETE ON simulator_manifests TO app_role;
        GRANT SELECT, INSERT, UPDATE, DELETE ON simulator_latent_state TO app_role;

        -- diagnoser_role gets NO privileges on simulator_latent_state or simulator_manifests
        REVOKE ALL ON simulator_manifests FROM diagnoser_role;
        REVOKE ALL ON simulator_latent_state FROM diagnoser_role;
    """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
        REVOKE ALL ON simulator_latent_state FROM app_role;
        REVOKE ALL ON simulator_manifests FROM app_role;
    """
        )
    )
    op.drop_index("idx_latent_simulation", table_name="simulator_latent_state")
    op.drop_table("simulator_latent_state")
    op.drop_table("simulator_manifests")
