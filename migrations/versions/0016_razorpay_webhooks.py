"""raw_webhook_events -- real, signature-verified Razorpay webhook ingestion
(Task WEBHOOK1).

Revision ID: 0016
Revises: 0015

Scope, deliberately bounded: this closes the ALREADY-DISCOVERED dead end in
integrations/razorpay/adapter.py's RazorpayTestAdapter.retry() -- it creates
a real Razorpay order and returns outcome="PENDING" with provider_ref=the
real order id, but nothing in the codebase ever resolved that PENDING to a
real terminal outcome ("resolving to SUCCESS/FAILED is a webhook/polling
concern outside this call's scope" -- adapter.py's own docstring). This
migration + the webhook receiver it supports reconciles exactly that: a
real webhook arrives, we match it to the PENDING recovery via
recoveries.provider_ref (already stores the real order id -- no new
payment-identity mapping needed), and write the terminal ledger row that
should have existed all along.

Explicitly OUT of scope: routing an inbound webhook for a payment
RecoveryOS never itself initiated (no merchant/account_id resolution, no
customer creation from webhook data) -- that's the bigger "merchant intent
API" / entity-state-store surface, deferred on purpose. This migration only
supports reconciling RecoveryOS's OWN outbound Razorpay orders.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_webhook_events",
        sa.Column(
            "webhook_event_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        # Razorpay's own event name, e.g. "payment.failed" / "order.paid" --
        # NULL if the raw body couldn't even be parsed as JSON (still stored
        # for audit -- see module docstring's "never throw away the
        # original payload" principle).
        sa.Column("event_type", sa.Text(), nullable=True),
        # The VERBATIM raw body, parsed as JSON for storage -- signature
        # verification itself happens against the raw bytes BEFORE this
        # parse (apps/api/routers/razorpay_webhooks.py), so this column is
        # evidence of what was received, not what was trusted.
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        sa.Column("headers", postgresql.JSONB(), nullable=False),
        sa.Column("signature_verified", sa.Boolean(), nullable=False),
        # SHA-256 of the raw body bytes -- Razorpay's webhook payloads carry
        # no universal unique event-id field (unlike Stripe's `id: evt_xxx`),
        # so the idempotency key is derived from the payload's own content:
        # an identical redelivery (Razorpay retries on any non-2xx response)
        # hashes identically, a genuinely different event never collides.
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "matched_recovery_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("recoveries.recovery_id"),
            nullable=True,
        ),
        sa.Column("reconciliation_note", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_raw_webhook_events_matched_recovery", "raw_webhook_events", ["matched_recovery_id"]
    )

    # Unverified-signature rows must still be storable (audit trail of a
    # rejected/forged attempt) -- app_role gets full R/W here, same as any
    # other operational table (NOT append-only: reconciliation legitimately
    # updates matched_recovery_id/processed_at/reconciliation_note after
    # the initial insert).
    op.execute("GRANT SELECT, INSERT, UPDATE ON raw_webhook_events TO app_role;")


def downgrade() -> None:
    op.drop_table("raw_webhook_events")
