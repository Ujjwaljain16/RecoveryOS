"""Recovery Mission -- explicit, code-owned state machine.

Revision ID: 0022
Revises: 0021

Two new tables:

  recovery_missions: ONE mutable row per payment's mission lifecycle (state,
  budget, current progress). Mutable in place -- same exception to this
  system's otherwise append-only discipline as scheduled_reevaluations'
  status/claimed_at columns (migration 0017), for the same reason: a
  mission's CURRENT state is a real thing calling code needs to read/update
  atomically, not a fact only ever reconstructable from history.

  mission_events: the append-only, ordered trace of everything that
  happened to a mission (PAYMENT_FAILED, MISSION_CREATED,
  INVESTIGATION_STARTED, HYPOTHESIS_UPDATED, AI_RECOMMENDATION,
  POLICY_AUTHORIZED, ACTION_EXECUTING, RECOVERY_SUCCEEDED/FAILED,
  REINVESTIGATION_STARTED, MISSION_RECOVERED/ESCALATED/TERMINATED, ...) --
  this is the artifact a judge opens to see one payment's entire autonomous
  trajectory as a literal ordered list. Never UPDATEd/DELETEd (matches
  audit_log/events' own REVOKE UPDATE, DELETE FROM app_role pattern,
  migration 0002).

scheduled_reevaluations also gains a nullable mission_id column: this
generalizes services/recovery_engine/scheduling.py's RETRY_LATER-only
closed loop to every action outcome (a FAILED RETRY_NOW/ALT_ROUTE now also
schedules a re-evaluation, from workers/execution_worker.py's sync path) --
workers/retry_scheduler.py needs to know WHICH mission a firing re-
evaluation belongs to, to correctly reuse (not recreate) that mission on
re-entry.

app_role gets SELECT/INSERT/UPDATE on recovery_missions (the state/budget
columns are updated in place) and SELECT/INSERT only on mission_events
(append-only). diagnoser_role needs nothing new -- neither table is ever
read mid-investigation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels = None
depends_on = None

MISSION_STATES = (
    "OBSERVED",
    "INVESTIGATING",
    "PLANNING",
    "AWAITING_AUTHORIZATION",
    "EXECUTING",
    "OBSERVING_OUTCOME",
    "RECOVERED",
    "ESCALATED",
    "TERMINATED",
)


def upgrade() -> None:
    op.create_table(
        "recovery_missions",
        sa.Column(
            "mission_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default="OBSERVED"),
        # Fixed, disclosed objective string -- never LLM-generated (the AI-
        # fusion "AI supplies a signal, never authority" principle extended: here,
        # AI doesn't even get to phrase its own goal).
        sa.Column(
            "objective",
            sa.Text(),
            nullable=False,
            server_default=(
                "maximize expected recovered revenue subject to deterministic "
                "safety, policy, and budget constraints"
            ),
        ),
        # ── Hard envelope -- code-set at mission creation, never modified by
        # anything downstream of that (see services/recovery_engine/mission.py's
        # check_budget(), the sole reader). ──
        sa.Column("max_investigation_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_mission_duration_seconds", sa.Integer(), nullable=False, server_default="604800"),
        sa.Column("max_money_exposure_paise", sa.BigInteger(), nullable=False),
        sa.Column("current_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN (" + ",".join(f"'{s}'" for s in MISSION_STATES) + ")",
            name="ck_recovery_missions_state",
        ),
    )
    op.create_index("idx_recovery_missions_payment", "recovery_missions", ["payment_id"])
    # Partial unique index: at most ONE non-terminal (active) mission per
    # payment at a time -- a payment can legitimately accumulate multiple
    # TERMINAL missions over time (e.g. escalated once, later a brand new
    # failure starts a fresh mission), but never two concurrently active ones.
    op.execute(
        "CREATE UNIQUE INDEX uq_recovery_missions_one_active_per_payment "
        "ON recovery_missions (payment_id) "
        "WHERE state NOT IN ('RECOVERED', 'ESCALATED', 'TERMINATED')"
    )

    op.create_table(
        "mission_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "mission_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("recovery_missions.mission_id"),
            nullable=False,
        ),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        # The mission's state AFTER this event -- makes the trace directly
        # readable without joining back to recovery_missions' current row.
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("mission_id", "sequence_number", name="uq_mission_events_sequence"),
    )
    op.create_index("idx_mission_events_mission", "mission_events", ["mission_id", "sequence_number"])

    op.add_column(
        "scheduled_reevaluations",
        sa.Column(
            "mission_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("recovery_missions.mission_id"),
            nullable=True,
        ),
    )

    op.execute("GRANT SELECT, INSERT, UPDATE ON recovery_missions TO app_role;")
    op.execute("GRANT SELECT, INSERT ON mission_events TO app_role;")


def downgrade() -> None:
    op.drop_column("scheduled_reevaluations", "mission_id")
    op.drop_table("mission_events")
    op.execute("DROP INDEX IF EXISTS uq_recovery_missions_one_active_per_payment")
    op.drop_table("recovery_missions")
