"""
Task R2 (pre-Phase-8 audit): verify recoveryos/models.py's declared column
types actually match the real Postgres schema, for every table and every
column -- not just presence. The existing test_config_and_models.py::
test_base_metadata_has_all_tables only checks that tables/columns exist by
NAME; it would never have caught AnomalyWindow.baseline_rate/observed_rate/
z_score and Diagnosis.confidence silently defaulting to FLOAT while the
real schema (and TRD §2) declares NUMERIC(5,4)/(5,4)/(6,3)/(4,3). This test
closes that whole category, not just today's instance.
"""

from __future__ import annotations

from sqlalchemy import inspect

from recoveryos.models import Base


def _normalize_type_str(type_obj, dialect) -> str:
    """
    Render a SQLAlchemy type object as a Postgres-flavored string with
    whitespace/case normalized, so a reflected column's type (from
    Inspector.get_columns()) can be compared against the ORM's declared
    type (from Base.metadata) even though they come from different code
    paths in SQLAlchemy.
    """
    return " ".join(str(type_obj.compile(dialect=dialect)).split()).upper()


def test_model_column_types_match_migration_schema(sync_engine):
    """
    For every table/column recoveryos/models.py declares, introspect the
    real Postgres schema and assert the compiled type strings match. This
    is the actual gap the audit found -- existing tests check column
    presence, never type -- and would have caught the FLOAT/NUMERIC drift
    (Task R2) had it existed when those tests were written.
    """
    dialect = sync_engine.dialect
    inspector = inspect(sync_engine)
    mismatches: list[str] = []

    for table_name, table in Base.metadata.tables.items():
        reflected_columns = {c["name"]: c for c in inspector.get_columns(table_name)}
        for column in table.columns:
            reflected = reflected_columns.get(column.name)
            if reflected is None:
                mismatches.append(f"{table_name}.{column.name}: missing from live DB schema")
                continue

            declared_str = _normalize_type_str(column.type, dialect)
            reflected_str = _normalize_type_str(reflected["type"], dialect)

            if declared_str != reflected_str:
                mismatches.append(
                    f"{table_name}.{column.name}: ORM declares {declared_str!r}, "
                    f"live DB schema has {reflected_str!r}"
                )

    assert not mismatches, (
        "recoveryos/models.py's declared column types drifted from the real schema "
        "(migrations/ + TRD §2) -- this would confuse a future `alembic revision "
        "--autogenerate` into proposing to revert the real schema back to the ORM's "
        "wrong type:\n" + "\n".join(mismatches)
    )
