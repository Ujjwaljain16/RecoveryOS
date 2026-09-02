"""
Unit tests for services/recovery_engine/ai_fusion.py's pure tie-break math.
No DB — CandidateActionResult tuples are hand-built, same style as
tests/unit/test_next_best_action.py.
"""

from __future__ import annotations

from services.recovery_engine.ai_fusion import find_near_tied_candidates
from services.recovery_engine.next_best_action import ACTION_TYPES, CandidateActionResult


def _candidate(action_type: str, evi_paise: int) -> CandidateActionResult:
    return CandidateActionResult(
        action_type=action_type,
        recovery_prob_bps=5000,
        expected_value_paise=evi_paise,
        cost_paise=0,
        friction_penalty_paise=0,
        risk_penalty_paise=0,
    )


def _all_six(**evi_by_action: int) -> tuple[CandidateActionResult, ...]:
    return tuple(_candidate(a, evi_by_action.get(a, -999_999)) for a in ACTION_TYPES)


def test_near_tied_candidate_within_tolerance_is_included():
    """The user's own worked example: RETRY_NOW ₹82.00 vs ALT_ROUTE ₹81.70 —
    a 0.37% delta, well within a 1% (100 bps) tolerance."""
    candidates = _all_six(RETRY_NOW=8_200, ALT_ROUTE=8_170, REMINDER=4_400, ESCALATE=1_000)
    near_tied = find_near_tied_candidates(
        candidates, winner_evi_paise=8_200, min_expected_value_paise=0, tie_tolerance_bps=100
    )
    action_types = {c.action_type for c in near_tied}
    assert "RETRY_NOW" in action_types  # winner always included (distance 0)
    assert "ALT_ROUTE" in action_types
    assert "REMINDER" not in action_types
    assert "ESCALATE" not in action_types


def test_decisive_winner_excludes_a_clearly_worse_candidate():
    """The user's own second worked example: RETRY_NOW ₹82 vs ALT_ROUTE ₹64 —
    a ~22% delta, decisively outside any reasonable tolerance."""
    candidates = _all_six(RETRY_NOW=8_200, ALT_ROUTE=6_400)
    near_tied = find_near_tied_candidates(
        candidates, winner_evi_paise=8_200, min_expected_value_paise=0, tie_tolerance_bps=100
    )
    assert {c.action_type for c in near_tied} == {"RETRY_NOW"}


def test_candidate_that_does_not_clear_the_floor_is_excluded_even_if_numerically_close():
    """Floor eligibility (strict > min_expected_value_paise, matching
    select_next_best_action()'s own rule) is checked BEFORE tolerance —
    a candidate at or below the floor never qualifies for tie-break,
    regardless of how close its EVI is to the winner's."""
    candidates = _all_six(RETRY_NOW=100, ALT_ROUTE=100)
    near_tied = find_near_tied_candidates(
        candidates, winner_evi_paise=100, min_expected_value_paise=100, tie_tolerance_bps=10_000
    )
    assert near_tied == []


def test_zero_tolerance_requires_an_exact_tie():
    candidates = _all_six(RETRY_NOW=1_000, ALT_ROUTE=999)
    near_tied = find_near_tied_candidates(
        candidates, winner_evi_paise=1_000, min_expected_value_paise=0, tie_tolerance_bps=0
    )
    assert {c.action_type for c in near_tied} == {"RETRY_NOW"}

    candidates_exact = _all_six(RETRY_NOW=1_000, ALT_ROUTE=1_000)
    near_tied_exact = find_near_tied_candidates(
        candidates_exact, winner_evi_paise=1_000, min_expected_value_paise=0, tie_tolerance_bps=0
    )
    assert {c.action_type for c in near_tied_exact} == {"RETRY_NOW", "ALT_ROUTE"}


def test_do_nothing_never_included_regardless_of_tolerance():
    candidates = _all_six(RETRY_NOW=0, DO_NOTHING=0)
    near_tied = find_near_tied_candidates(
        candidates, winner_evi_paise=0, min_expected_value_paise=-1, tie_tolerance_bps=10_000
    )
    assert "DO_NOTHING" not in {c.action_type for c in near_tied}


def test_zero_winner_evi_only_matches_exact_zero_candidates():
    """No meaningful 'relative' tolerance around a zero winner — only an
    exact-0 candidate can qualify, regardless of tie_tolerance_bps."""
    candidates = _all_six(RETRY_NOW=0, ALT_ROUTE=0, REMINDER=1)
    near_tied = find_near_tied_candidates(
        candidates, winner_evi_paise=0, min_expected_value_paise=-1, tie_tolerance_bps=10_000
    )
    assert {c.action_type for c in near_tied} == {"RETRY_NOW", "ALT_ROUTE"}
