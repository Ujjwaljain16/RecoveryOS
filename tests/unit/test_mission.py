"""
Unit tests for services/recovery_engine/mission.py's pure state machine --
validate_transition() and check_budget(). No DB; the I/O wrappers
(get_or_create_mission_*/transition_mission_*) are covered by
tests/integration/test_recovery_mission_lifecycle.py against a real
Postgres, since their whole point is atomic locking/persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.recovery_engine.mission import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    BudgetStatus,
    check_budget,
    validate_transition,
)

ALL_STATES = (
    "OBSERVED",
    "INVESTIGATING",
    "PLANNING",
    "AWAITING_AUTHORIZATION",
    "EXECUTING",
    "OBSERVING_OUTCOME",
    "RECOVERED",
    "ESCALATED",
    "TERMINATED",
)


def test_every_state_has_an_entry_in_the_transition_table():
    assert set(ALLOWED_TRANSITIONS.keys()) == set(ALL_STATES)


def test_terminal_states_have_zero_outgoing_transitions():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_the_full_happy_path_is_allowed_step_by_step():
    path = [
        "OBSERVED",
        "INVESTIGATING",
        "PLANNING",
        "AWAITING_AUTHORIZATION",
        "EXECUTING",
        "OBSERVING_OUTCOME",
        "RECOVERED",
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert validate_transition(a, b), f"{a} -> {b} should be allowed"


def test_observing_outcome_can_loop_back_to_investigating():
    """The Phase 13 closed-loop transition -- a FAILED attempt (or a
    RETRY_LATER window elapsing) re-enters investigation."""
    assert validate_transition("OBSERVING_OUTCOME", "INVESTIGATING")


def test_backwards_or_skipping_transitions_are_rejected():
    assert not validate_transition("EXECUTING", "OBSERVED")
    assert not validate_transition("OBSERVED", "EXECUTING")  # can't skip investigation/planning
    assert not validate_transition("RECOVERED", "INVESTIGATING")  # terminal, no way back
    assert not validate_transition("PLANNING", "OBSERVING_OUTCOME")


def test_every_declared_target_state_is_itself_a_real_state():
    """Catches a typo'd target state that would silently never match
    anything -- every value in the transition table must be a key too."""
    for targets in ALLOWED_TRANSITIONS.values():
        for target in targets:
            assert target in ALLOWED_TRANSITIONS


# ── check_budget ─────────────────────────────────────────────────────────

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def _budget(**overrides) -> dict:
    defaults = {
        "current_round": 1,
        "max_investigation_rounds": 3,
        "current_attempt": 1,
        "max_attempts": 3,
        "started_at": NOW - timedelta(hours=1),
        "max_mission_duration_seconds": 604_800,
        "now": NOW,
    }
    defaults.update(overrides)
    return defaults


def test_within_every_limit_is_not_exhausted():
    result = check_budget(**_budget())
    assert result == BudgetStatus(False, None)


def test_round_limit_exceeded():
    result = check_budget(**_budget(current_round=4, max_investigation_rounds=3))
    assert result.exhausted is True
    assert result.reason == "MAX_ROUNDS_EXCEEDED"


def test_round_limit_exactly_at_limit_is_not_exceeded():
    """Strict inequality, matching MinExpectedValueRule/RetryLimitRule's own
    boundary-inclusive convention elsewhere in this codebase."""
    result = check_budget(**_budget(current_round=3, max_investigation_rounds=3))
    assert result.exhausted is False


def test_attempt_limit_exceeded():
    result = check_budget(**_budget(current_attempt=4, max_attempts=3))
    assert result.exhausted is True
    assert result.reason == "MAX_ATTEMPTS_EXCEEDED"


def test_attempt_limit_exactly_at_limit_is_not_exceeded():
    result = check_budget(**_budget(current_attempt=3, max_attempts=3))
    assert result.exhausted is False


def test_duration_exceeded():
    result = check_budget(
        **_budget(started_at=NOW - timedelta(days=8), max_mission_duration_seconds=604_800)
    )
    assert result.exhausted is True
    assert result.reason == "MISSION_DURATION_EXCEEDED"


def test_duration_exactly_at_limit_is_not_exceeded():
    result = check_budget(
        **_budget(started_at=NOW - timedelta(seconds=604_800), max_mission_duration_seconds=604_800)
    )
    assert result.exhausted is False


def test_rounds_checked_before_attempts_before_duration_when_multiple_exceeded():
    """Deterministic priority order when more than one limit is blown at
    once -- rounds first, matching check_budget's own docstring."""
    result = check_budget(
        **_budget(
            current_round=10,
            max_investigation_rounds=3,
            current_attempt=10,
            max_attempts=3,
            started_at=NOW - timedelta(days=30),
            max_mission_duration_seconds=604_800,
        )
    )
    assert result.reason == "MAX_ROUNDS_EXCEEDED"
