"""Bounded AI recovery recommendation + decision fusion trace.

Revision ID: 0021
Revises: 0020

Two new tables, same "separate table, not a JSONB blob on an existing
immutable row" reasoning as migration 0015's diagnosis_hypotheses/
investigation_steps: recovery_recommendations is written by the diagnosis
pipeline (app_role, from services/diagnosis_engine/diagnoser.py's
persist_investigation), decision_fusion_trace is written by the decision
pipeline (app_role, from services/recovery_engine/orchestrator.py's
persist_decision) -- different writers, different times, never UPDATEd.

decision_fusion_trace is written for EVERY decision once
ai_recommendation_fusion_enabled is on (including "no recommendation
available" / "fusion disabled" rows, fusion_reason explains which) -- this
is what lets the AI-on/AI-off ablation (tests/evaluation/ai_ablation_runner.py)
query a uniform provenance trail across both arms, and what lets a judge
trace exactly how much authority the AI recommendation had for any single
payment (a governing invariant of this design).

diagnoser_role needs NO grant on either table -- it only ever reads via
DIAGNOSER_SAFE_TABLES (migration 0002); both new tables are app_role write
targets, same as migration 0015's tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recovery_recommendations",
        sa.Column(
            "recommendation_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "diagnosis_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("diagnoses.diagnosis_id"),
            nullable=False,
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=True,
        ),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("recommended_delay_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("risk_flags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("recovery_rationale", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "recommended_action IN ('RETRY_NOW','RETRY_LATER','ALT_ROUTE','REMINDER',"
            "'ESCALATE','DO_NOTHING')",
            name="ck_recovery_recommendations_action",
        ),
    )
    op.create_index(
        "idx_recovery_recommendations_diagnosis", "recovery_recommendations", ["diagnosis_id"]
    )

    op.create_table(
        "decision_fusion_trace",
        sa.Column(
            "fusion_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("policy_decisions.decision_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "recommendation_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("recovery_recommendations.recommendation_id"),
            nullable=True,
        ),
        sa.Column("deterministic_chosen_action", sa.Text(), nullable=False),
        sa.Column("deterministic_chosen_evi_paise", sa.BigInteger(), nullable=False),
        sa.Column("near_tied_candidates", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("tie_tolerance_bps", sa.Integer(), nullable=False),
        sa.Column("ai_recommended_action", sa.Text(), nullable=True),
        sa.Column("ai_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("ai_risk_flags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("tie_break_applied", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("risk_escalation_applied", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("final_action", sa.Text(), nullable=False),
        sa.Column("fusion_reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_decision_fusion_trace_decision", "decision_fusion_trace", ["decision_id"])

    op.execute("GRANT SELECT, INSERT ON recovery_recommendations, decision_fusion_trace TO app_role;")


def downgrade() -> None:
    op.drop_table("decision_fusion_trace")
    op.drop_table("recovery_recommendations")
