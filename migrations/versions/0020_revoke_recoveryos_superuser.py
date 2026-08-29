"""Revoke SUPERUSER from the recoveryos login role -- Adversarial Audit Verdict.

Revision ID: 0020
Revises: 0019

docker-compose.yml's postgres service bootstraps POSTGRES_USER=recoveryos --
the official postgres image always creates that first login role as a
database SUPERUSER. Every app process (api, event_processor,
pipeline_orchestrator, execution_worker, retry_scheduler) then connects AS
that same superuser role for its everyday reads/writes, instead of the
non-privileged app_role membership migration 0002 already grants it
(GRANT app_role TO recoveryos) -- app_role's own carefully-scoped
GRANT/REVOKE matrix (full R/W minus UPDATE/DELETE on audit_log/events) was
never actually the binding constraint in practice, because recoveryos could
always bypass every one of those grants by virtue of being superuser.

This migration self-heals it, in place, with no docker-compose/env changes
and no data loss: it runs under alembic's own recoveryos connection (still
superuser at the moment this migration executes) and strips that same
role's superuser flag for every future connection. A role is permitted to
revoke its own SUPERUSER bit; Postgres does not require a second superuser
to do it. From the next connection onward (the very next container
restart), recoveryos is bound by app_role's grants like every other
non-owner role in this system (diagnoser_role, inference_role) -- and any
FUTURE migration needing elevated privilege (CREATE ROLE, CREATE
EXTENSION, etc.) would need a separate bootstrap-only credential, not this
one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER ROLE recoveryos WITH NOSUPERUSER;"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER ROLE recoveryos WITH SUPERUSER;"))
