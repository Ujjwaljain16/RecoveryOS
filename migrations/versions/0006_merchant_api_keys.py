"""
Migration 0006 — merchants.api_key_hash

Closes the "zero authentication exists anywhere" gap (Task 4). Adds the
column apps/api/dependencies/auth.py:verify_api_key looks up against.

Stores a hash, never the raw key — see auth.py's module docstring for why
SHA-256+pepper rather than bcrypt/argon2 was chosen for this specific case
(high-entropy server-generated keys, not low-entropy human passwords).

Nullable: existing merchants (simulator-seeded, or manually inserted during
prior tasks' testing) don't get a key automatically. One must be issued via
auth.generate_api_key() + hash_api_key() before that merchant can
authenticate — there is no key recovery, only reissue (hashing is one-way).
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("merchants", sa.Column("api_key_hash", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_merchants_api_key_hash", "merchants", ["api_key_hash"])


def downgrade() -> None:
    op.drop_constraint("uq_merchants_api_key_hash", "merchants", type_="unique")
    op.drop_column("merchants", "api_key_hash")
