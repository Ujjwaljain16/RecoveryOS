"""
Migration 0005 — events.idempotency_key

Closes the gap between the events-router docstring's long-standing claim of a
UNIQUE constraint backing idempotency and the schema, which never actually had
one: dedup previously happened only via a check-then-insert SELECT on
event_id (the server-minted, per-POST-unique id), so a client that retried a
request with the same client-supplied idempotency_key produced two rows.

Scoping: UNIQUE(payment_id, idempotency_key), NOT idempotency_key alone.
A global constraint would wrongly reject a legitimate event on a different
payment if a client (or a bug) ever reused a key across payments — e.g. a
naive client deriving idempotency_key from something coarser than the
payment itself. Scoping to the payment means "this payment's retry of this
logical event" is deduplicated, without silently dropping unrelated events
on other payments that happen to collide on the key string alone.

Backfills existing rows with idempotency_key = event_id (their only unique
identity today) before enforcing NOT NULL + UNIQUE, so this is safe to run
against a populated table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("idempotency_key", sa.Text(), nullable=True),
    )
    op.execute("UPDATE events SET idempotency_key = event_id::text WHERE idempotency_key IS NULL")
    op.alter_column("events", "idempotency_key", nullable=False)
    op.create_unique_constraint(
        "uq_events_payment_idempotency_key", "events", ["payment_id", "idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_events_payment_idempotency_key", "events", type_="unique")
    op.drop_column("events", "idempotency_key")
