"""
gaps.md §B.1's own named test, previously enforced only as a CI shell step
(.github/workflows/ci.yml's "Gate: no SELECT * in inference/training code"),
never as a pytest test a developer can run locally without pushing to CI.
Same exact directories and pattern as that shell gate — this is the pytest
mirror of it, not a different/looser check.

Why this matters (gaps.md §B.1): `SELECT *` anywhere reachable from the
inference/training pipeline risks pulling `ground_truth_recoverable` (or
any other latent simulator column) into a feature-building code path as an
unused-but-present dict key — silent until something downstream logs,
caches, or re-serializes that dict and the leak becomes real. Banning the
pattern outright is cheaper than auditing every call site for whether the
extra column is actually read.
"""

from __future__ import annotations

import re
from pathlib import Path

_SELECT_STAR = re.compile(r"SELECT \*", re.IGNORECASE)

# Exactly the directories the CI gate greps -- keep these in sync with
# .github/workflows/ci.yml's "no SELECT *" step if either ever changes.
_SCANNED_DIRS = ("services/recovery_engine", "models", "services/diagnosis_engine")


def _repo_root() -> Path:
    # tests/unit/this_file.py -> repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def test_no_select_star_in_inference_reachable_code():
    root = _repo_root()
    violations: list[str] = []
    for rel_dir in _SCANNED_DIRS:
        scan_dir = root / rel_dir
        if not scan_dir.exists():
            continue
        for path in scan_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _SELECT_STAR.search(line):
                    violations.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")

    assert not violations, (
        "SELECT * is banned in any code path reachable from the inference/training "
        "pipeline (gaps.md §B.1) -- found:\n" + "\n".join(violations)
    )
