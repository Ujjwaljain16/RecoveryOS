"""event_publications -- decouple "is this event newly inserted" from "has
it been published downstream yet" -- Task S4, pre-Phase-8 audit.

Revision ID: 0014
Revises: 0013

services/event_processor/processor.py's own module docstring claims a
publish failure gets retried on redelivery ("Publisher fails: exception
propagates, no XACK -> message stays pending... publish retried"). In
reality, insert_event_idempotent()'s is_new flag is what gates the publish
call -- once the Event row commits, a redelivered message finds is_new=False
and the publish is skipped, permanently, even though it never actually
succeeded. A single transient Redis blip during the publish call drops that
event's downstream notification forever.

The fix needs its own persisted state, not a re-derivation of is_new -- but
`events` has UPDATE/DELETE revoked from app_role at the DB level
(migrations/0002_db_roles.py's APPEND_ONLY_TABLES, TRD Sec.9's immutability
guarantee: "so even a bug can't silently rewrite history"). Adding a
mutable published_at column directly on events would either require
loosening that grant (undermining the append-only invariant) or fail at
runtime with a permission error the moment anyone tried to UPDATE it. A
separate, INSERT-only table sidesteps this entirely: marking an event
published is an INSERT (once, ever, per event_id), never an UPDATE, so
app_role only needs SELECT + INSERT here -- the exact same append-only
discipline events itself already follows, just on a second table instead
of a mutable column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_publications",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column(
            "published_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"], ondelete="CASCADE"),
    )
    op.execute("GRANT SELECT, INSERT ON event_publications TO app_role;")
    # No UPDATE/DELETE grant at all -- this table is INSERT-only by
    # construction, same discipline as events itself, so there's nothing to
    # additionally revoke the way APPEND_ONLY_TABLES does for events/audit_log.


def downgrade() -> None:
    op.drop_table("event_publications")
