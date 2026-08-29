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

CORRECTED (found running this migration for real against a live
docker-compose stack, not just testcontainers): a role CANNOT always revoke
its own SUPERUSER bit. PostgreSQL specifically protects the very first
bootstrap role (the one initdb creates, exactly what POSTGRES_USER=recoveryos
produces here) -- "ALTER ROLE ... NOSUPERUSER" on that specific role raises
"permission denied to alter role: The bootstrap user must have the SUPERUSER
attribute", unconditionally, regardless of who issues it. This was NOT
caught by this migration's own regression test
(tests/integration/test_schema_and_roles.py) because testcontainers'
bootstrap username there is "recoveryos_test", not "recoveryos" -- that test
exercises revoking superuser from an ordinary (non-bootstrap) role that
happens to be superuser, which genuinely does work; it never exercised the
true bootstrap-role case docker-compose actually produces.

The real, complete fix is architectural: give docker-compose's postgres
service a SEPARATE bootstrap-only login (distinct from the app's own
`recoveryos` connection role), so `recoveryos` is never the protected
bootstrap role in the first place -- tracked as a real follow-up, not done
here (it needs a new env var, a docker-compose change, and doesn't apply
retroactively to an already-initialized data volume without a full
Postgres reinit regardless).

Until then, this migration degrades gracefully instead of crashing the
whole `alembic upgrade head` run: it attempts the revoke inside a
SAVEPOINT, and where Postgres refuses because recoveryos IS the protected
bootstrap role, it logs a clear warning and moves on rather than blocking
every other migration/deployment behind an unfixable-in-place constraint.
Wherever recoveryos is NOT the true bootstrap role (a properly separated
setup, or the CI testcontainer), the revoke still applies for real.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    try:
        with conn.begin_nested():
            conn.execute(sa.text("ALTER ROLE recoveryos WITH NOSUPERUSER;"))
    except sa.exc.DBAPIError as exc:
        if "bootstrap user" not in str(exc).lower():
            raise
        logger.warning(
            "[0020] recoveryos is Postgres's protected bootstrap role in this "
            "deployment -- SUPERUSER cannot be revoked from it in place (see this "
            "migration's own docstring for the real fix: a separate bootstrap-only "
            "login). Skipping, not failing the migration run."
        )


def downgrade() -> None:
    conn = op.get_bind()
    try:
        with conn.begin_nested():
            conn.execute(sa.text("ALTER ROLE recoveryos WITH SUPERUSER;"))
    except sa.exc.DBAPIError as exc:
        if "bootstrap user" not in str(exc).lower():
            raise
