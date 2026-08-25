"""Column-level grants on merchants for diagnoser_role/inference_role — Task M2.

Revision ID: 0011
Revises: 0010

migrations/0002_db_roles.py and 0008_inference_role.py both grant blanket
table-level `GRANT SELECT ON merchants` to diagnoser_role/inference_role
(merchants is in DIAGNOSER_SAFE_TABLES / INFERENCE_SAFE_TABLES). That grant
predates api_key_hash (added later, 0006_merchant_api_keys.py) — Postgres
applies an existing table-level SELECT grant to columns added afterward via
ALTER TABLE, so both restricted roles have always been able to read a
merchant's credential hash, the exact class of over-grant that
ground_truth_recoverable already got surgical column-level treatment for
via DIAGNOSER_PAYMENT_COLUMNS/INFERENCE_PAYMENT_COLUMNS.

Note: a column-level REVOKE (`REVOKE SELECT (api_key_hash) ON merchants ...`)
would NOT fix this — Postgres checks table-level and column-level
privileges independently, and a role that holds the broader table-level
SELECT grant retains column access regardless of any column-level REVOKE.
The only real fix is what's done here: REVOKE the table-level grant
entirely and re-GRANT SELECT on an explicit safe-column allow-list, the
same pattern already used for payments.
"""

from __future__ import annotations

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None

# Every column diagnoser_role/inference_role legitimately reads today
# (merchant identity + policy lookup) — api_key_hash is deliberately absent.
MERCHANT_SAFE_COLUMNS = "merchant_id, name, policy_config_id, created_at"


def upgrade() -> None:
    for role in ("diagnoser_role", "inference_role"):
        op.execute(f"REVOKE SELECT ON merchants FROM {role};")
        op.execute(f"GRANT SELECT ({MERCHANT_SAFE_COLUMNS}) ON merchants TO {role};")


def downgrade() -> None:
    for role in ("diagnoser_role", "inference_role"):
        op.execute(f"REVOKE SELECT ({MERCHANT_SAFE_COLUMNS}) ON merchants FROM {role};")
        op.execute(f"GRANT SELECT ON merchants TO {role};")
