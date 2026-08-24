"""
Static checks for Task 6, Part A — no live DB/Redis needed, these are pure
source inspection.

Context: migrations/versions/0002_db_roles.py used to contain
`CREATE USER recoveryos WITH PASSWORD 'recoveryos'` and
`CREATE USER diagnoser WITH PASSWORD 'diagnoser_pass'` — plaintext,
committed, and (per `git log`) pushed to origin, meaning both strings are
permanently part of this repo's public history regardless of any later fix.
"""

from __future__ import annotations

import pathlib
import re

MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent.parent / "migrations" / "versions" / "0002_db_roles.py"
)

# The two literal, historically-committed passwords this task is about.
# Checked for as exact quoted-string literals (not merely "does the
# substring 'recoveryos' appear" — that word is also the project name,
# the DB name, and the role/user name throughout this file legitimately;
# only a PASSWORD '...' literal containing these values is the actual bug).
COMPROMISED_PASSWORD_LITERALS = ["recoveryos", "diagnoser_pass"]


def test_migration_reads_password_from_env_not_hardcoded():
    """
    The migration must contain no PASSWORD '<literal>' SQL fragment at all
    — passwords are read from os.environ and interpolated at apply time,
    never written as a string literal in the source.
    """
    content = MIGRATION_PATH.read_text(encoding="utf-8")

    # Catches any `PASSWORD '...'` where the quoted content is a fixed
    # literal rather than an f-string-interpolated variable. An f-string
    # placeholder like `PASSWORD '{app_role_password}'` is fine and
    # expected — it's not a hardcoded value, it's a formatting slot; this
    # regex only flags quotes containing something OTHER than a single
    # `{identifier}` expression.
    hardcoded_password_pattern = re.compile(
        r"PASSWORD\s+'(?!\{[A-Za-z_][A-Za-z0-9_]*\}')[^']*'", re.IGNORECASE
    )
    matches = hardcoded_password_pattern.findall(content)
    assert not matches, (
        f"Found hardcoded PASSWORD literal(s) in {MIGRATION_PATH.name}: {matches} — "
        f"passwords must be read from os.environ, never a string literal in source."
    )

    # Specifically confirm the two historically-compromised values are gone
    # AS PASSWORD LITERALS — not merely mentioned. This file's own docstring
    # legitimately names both values in prose (documenting why the fix
    # exists), so a bare "does this quoted string appear anywhere" check
    # would flag its own explanation; check the dangerous SQL shape instead.
    for compromised in COMPROMISED_PASSWORD_LITERALS:
        dangerous = re.compile(rf"PASSWORD\s+'{re.escape(compromised)}'", re.IGNORECASE)
        assert not dangerous.search(content), (
            f"The historically-compromised literal '{compromised}' is still present "
            f"as an actual PASSWORD literal in {MIGRATION_PATH.name}"
        )

    # And confirm the real mechanism (env var read) is actually present —
    # a file that removed the hardcoded password AND the env-var read
    # (e.g. replaced with a different hardcoded value, or nothing at all)
    # would wrongly pass the checks above alone.
    assert (
        "os.environ" in content
    ), f"{MIGRATION_PATH.name} must read the DB role passwords from os.environ"
    assert "RECOVERYOS_APP_ROLE_PASSWORD" in content
    assert "RECOVERYOS_DIAGNOSER_ROLE_PASSWORD" in content


def test_no_plaintext_credentials_anywhere_in_tracked_source():
    """
    Broader sweep than the one migration file — Task 6's acceptance
    criterion is explicit: "grep the whole repo, not just the one
    migration." Walks every tracked-source-like file (.py, .yml, .yaml,
    .ini, .toml — deliberately excluding .env itself, which is gitignored
    and is where real local secrets are SUPPOSED to live) for the two
    historically-compromised literal password strings, used AS A
    CREDENTIAL — not merely mentioned. "recoveryos" is also this project's
    name, its DB name, and a username used legitimately throughout the
    codebase (including in this very file's own docstrings, and in other
    files' comments explaining this exact fix by naming the old value) — a
    bare substring match would flag all of that as if it were live secret
    material. Only two shapes are actually dangerous: the password position
    of a connection-string userinfo (":password@") and a SQL
    `PASSWORD '<value>'` literal.
    """
    repo_root = pathlib.Path(__file__).parent.parent.parent
    exts = {".py", ".yml", ".yaml", ".ini", ".toml"}
    skip_dirs = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "data",
        "models",  # trained model artifacts, not source
    }
    # This file necessarily names the compromised values (it's what defines
    # COMPROMISED_PASSWORD_LITERALS above) — excluded from the credential-
    # shape sweep, not because its content is exempt from scrutiny, but
    # because a list of strings to search for is not itself a credential.
    self_path = pathlib.Path(__file__).resolve()

    dangerous_patterns = [
        re.compile(rf":{re.escape(pw)}@") for pw in COMPROMISED_PASSWORD_LITERALS
    ] + [
        re.compile(rf"PASSWORD\s+'{re.escape(pw)}'", re.IGNORECASE)
        for pw in COMPROMISED_PASSWORD_LITERALS
    ]

    offenders: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for pattern in dangerous_patterns:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(repo_root)}: matches {pattern.pattern!r}")

    assert not offenders, "Plaintext credential(s) found:\n" + "\n".join(offenders)
