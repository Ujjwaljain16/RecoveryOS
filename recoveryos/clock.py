"""
The one injectable seam for "what time is it right now" — Task COMPLIANCE1.

Before this, every wall-clock read in the pipeline was a bare
`datetime.now(UTC)` call, which was fine as long as no rule's PASS/FAIL
outcome depended on the absolute hour of day (is_expired/CooldownRule only
ever depend on ELAPSED time, which is time-zone- and clock-skew-agnostic).
AutopayExecutionWindowRule/QuietHoursComplianceRule are the first rules
whose outcome depends on the actual current IST hour, which makes
`datetime.now(UTC)` a real test-determinism hazard: roughly 31% of any
given day falls inside NPCI's peak windows, so a test asserting an ALLOW+
RETRY_NOW verdict would non-deterministically start failing depending on
what real time it happened to run at.

utcnow() is the one place production code should read the clock from (only
services/recovery_engine/orchestrator.py does, today); tests pin it to a
fixed, deliberately-safe time via tests/conftest.py's session-wide autouse
fixture, the same pattern already used for AI_DIAGNOSER_PROVIDER.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
