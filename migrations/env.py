"""
Alembic env.py — RecoveryOS
==============================
Reads DATABASE_URL_SYNC from settings (not the INI file) so migrations
always use the correct environment without manual editing of alembic.ini.

Runs in offline mode for generating SQL scripts and online mode for
applying migrations against a live Postgres instance.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Allow importing recoveryos package from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recoveryos.config import get_settings
from recoveryos.models import Base  # import all models so Alembic sees them

# ─── Alembic Config object ────────────────────────────────────────────────────
config = context.config

# Override the sqlalchemy.url from settings (beats the INI value)
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a DB connection — emits SQL to stdout."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the live Postgres instance."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
