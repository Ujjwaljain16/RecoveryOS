"""investigative diagnosis -- multi-hypothesis tracking, tool-call trace,
action confidence, and outcome feedback for the diagnoser (Task AGENT1).

Revision ID: 0015
Revises: 0014

Three additions, deliberately separate tables rather than JSONB blobs on
diagnoses, for the same reason migrations 0013/0014 kept dedup/publish
state in their own tables: each of these is written by a DIFFERENT step
of the pipeline at a DIFFERENT time (hypotheses + investigation steps
during diagnosis; the confidence during recovery strategy selection;
the outcome only once a terminal result exists, which can be minutes to
hours after the diagnosis row was written) -- cramming them into one
mutable JSONB column on an otherwise-immutable diagnoses row would mean
either allowing UPDATEs on a table that's supposed to stay append-only
in spirit, or losing the per-step history entirely.

diagnoser_role needs NO new grants here: DIAGNOSER_SAFE_TABLES (migration
0002) already covers every table an investigation tool would read from
(customers, anomaly_windows, candidate_actions, policy_decisions,
recoveries, recovery_ledger, action_costs, baseline_runs) -- these three
new tables are write targets for app_role only, populated AFTER an
investigation/decision/outcome is final, never read mid-investigation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── diagnoses gets a qualitative confidence band alongside the existing
    # numeric `confidence` column -- Point 1 of the agent-design review:
    # don't let an LLM-generated float pretend to be a calibrated
    # probability. Numeric `confidence` is kept as-is (existing EVI/policy
    # consumers already read it, and it's still meaningful as "how strongly
    # did the model commit"); confidence_band is the new honest label the
    # investigative loop actually reasons in.
    op.add_column(
        "diagnoses",
        sa.Column("confidence_band", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_diagnoses_confidence_band",
        "diagnoses",
        "confidence_band IN ('CONFIDENT','LIKELY','AMBIGUOUS',"
        "'INSUFFICIENT_EVIDENCE','CONFLICTING_SIGNALS','ESCALATE') "
        "OR confidence_band IS NULL",
    )

    # ── diagnosis_hypotheses: every candidate cause considered, not just the
    # winner. support_score/contradict_score/evidence_count are plain
    # integers the investigation loop increments as tool results come in --
    # not a probability, per Point 1.
    op.create_table(
        "diagnosis_hypotheses",
        sa.Column(
            "hypothesis_id",
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
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("support_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contradict_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "unresolved_questions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_diagnosis_hypotheses_diagnosis", "diagnosis_hypotheses", ["diagnosis_id"]
    )

    # ── investigation_steps: the tool-call trace, in order, with the
    # information-gain score that justified each choice -- Points 2 & 3.
    # expected_uncertainty_reduction is an LLM-ESTIMATED score (documented
    # as such at the call site, not claimed to be true entropy math);
    # tool_cost/latency_ms come from the ToolRegistry's own declared,
    # real constants.
    op.create_table(
        "investigation_steps",
        sa.Column(
            "step_id",
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
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_inputs", postgresql.JSONB(), nullable=False),
        sa.Column("tool_output_summary", postgresql.JSONB(), nullable=True),
        sa.Column("expected_uncertainty_reduction", sa.Numeric(6, 3), nullable=False),
        sa.Column("tool_cost", sa.Numeric(10, 4), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("investigation_score", sa.Numeric(8, 3), nullable=False),
        sa.Column(
            "called_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("diagnosis_id", "step_number"),
    )
    op.create_index("idx_investigation_steps_diagnosis", "investigation_steps", ["diagnosis_id"])

    # ── candidate_actions gets action_confidence -- the recovery
    # strategist's own confidence that THIS action's expected value will
    # actually be realized, separate from how confident the diagnosis was.
    op.add_column(
        "candidate_actions",
        sa.Column("action_confidence", sa.Numeric(4, 3), nullable=True),
    )

    # ── diagnosis_outcomes: closes the loop (Point 4). One row per
    # diagnosis, written once a terminal outcome exists. diagnosis_correct
    # is nullable and ONLY ever populated in the simulator/offline-eval
    # context (via app_role reading simulator_latent_state.true_failure_type,
    # exactly like Phase 8's AI-eval already does) -- a real production
    # case has no ground truth to check the diagnosis against, only whether
    # the chosen action worked (action_effective).
    op.create_table(
        "diagnosis_outcomes",
        sa.Column(
            "outcome_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "diagnosis_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("diagnoses.diagnosis_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("chosen_action", sa.Text(), nullable=False),
        sa.Column("observed_outcome", sa.Text(), nullable=False),
        sa.Column("diagnosis_correct", sa.Boolean(), nullable=True),
        sa.Column("action_effective", sa.Boolean(), nullable=True),
        sa.Column("counterfactual_result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "recorded_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.execute(
        "GRANT SELECT, INSERT ON diagnosis_hypotheses, investigation_steps, "
        "diagnosis_outcomes TO app_role;"
    )


def downgrade() -> None:
    op.drop_table("diagnosis_outcomes")
    op.drop_table("investigation_steps")
    op.drop_table("diagnosis_hypotheses")
    op.drop_column("candidate_actions", "action_confidence")
    op.drop_constraint("ck_diagnoses_confidence_band", "diagnoses", type_="check")
    op.drop_column("diagnoses", "confidence_band")
