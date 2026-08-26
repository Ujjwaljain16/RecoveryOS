"""
Unit tests for services/recovery_engine/next_best_action.py's pure
selection logic (select_next_best_action). No DB — CandidateActionResult
tuples are hand-built so these tests isolate the selection rule itself from
cost lookup / propensity / timing.
"""

from __future__ import annotations

from services.recovery_engine.next_best_action import (
    ACTION_TYPES,
    CandidateActionResult,
    compute_action_confidence,
    select_next_best_action,
)


def _candidate(action_type: str, evi_paise: int, prob_bps: int = 5000) -> CandidateActionResult:
    return CandidateActionResult(
        action_type=action_type,
        recovery_prob_bps=prob_bps,
        expected_value_paise=evi_paise,
        cost_paise=0,
        friction_penalty_paise=0,
        risk_penalty_paise=0,
    )


def _all_six(**evi_by_action: int) -> tuple[CandidateActionResult, ...]:
    return tuple(_candidate(a, evi_by_action.get(a, -999_999)) for a in ACTION_TYPES)


def test_best_evi_action_wins():
    candidates = _all_six(
        RETRY_NOW=500, RETRY_LATER=200, ALT_ROUTE=100, REMINDER=50, ESCALATE=10, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert result.chosen_action == "RETRY_NOW"
    assert result.chosen_evi_paise == 500
    assert result.cleared_floor is True


def test_below_floor_actions_rejected_in_favor_of_a_clearing_one():
    candidates = _all_six(
        RETRY_NOW=5, RETRY_LATER=400, ALT_ROUTE=-10, REMINDER=-5, ESCALATE=-1, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=100, propensity_probability_bps=8000
    )
    assert result.chosen_action == "RETRY_LATER"


def test_do_nothing_selected_when_no_action_clears_floor():
    """Every non-DO_NOTHING candidate is at or below the floor -> DO_NOTHING
    must be selected via the explicit fallback branch, not by accident."""
    candidates = _all_six(
        RETRY_NOW=-500,
        RETRY_LATER=-300,
        ALT_ROUTE=-200,
        REMINDER=-150,
        ESCALATE=-9000,
        DO_NOTHING=0,
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=1000
    )
    assert result.chosen_action == "DO_NOTHING"
    assert result.cleared_floor is False
    assert result.chosen_evi_paise == 0


def test_do_nothing_wins_even_when_its_own_evi_is_exactly_the_floor():
    """DO_NOTHING=0 does not need to itself clear the floor (strict
    inequality) — it's a fallback, not a competitor."""
    candidates = _all_six(
        RETRY_NOW=0, RETRY_LATER=0, ALT_ROUTE=0, REMINDER=0, ESCALATE=0, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=1000
    )
    assert result.chosen_action == "DO_NOTHING"
    assert result.cleared_floor is False


def test_retry_later_can_beat_retry_now():
    candidates = _all_six(
        RETRY_NOW=100, RETRY_LATER=900, ALT_ROUTE=50, REMINDER=10, ESCALATE=-100, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert result.chosen_action == "RETRY_LATER"


def test_alt_route_can_beat_retry():
    candidates = _all_six(
        RETRY_NOW=100, RETRY_LATER=50, ALT_ROUTE=900, REMINDER=10, ESCALATE=-100, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert result.chosen_action == "ALT_ROUTE"


def test_reminder_can_beat_retry():
    candidates = _all_six(
        RETRY_NOW=10, RETRY_LATER=5, ALT_ROUTE=1, REMINDER=900, ESCALATE=-100, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert result.chosen_action == "REMINDER"


def test_escalate_can_beat_retry():
    candidates = _all_six(
        RETRY_NOW=10, RETRY_LATER=5, ALT_ROUTE=1, REMINDER=2, ESCALATE=900, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert result.chosen_action == "ESCALATE"


def test_missing_do_nothing_candidate_raises():
    candidates = tuple(_candidate(a, 100) for a in ACTION_TYPES if a != "DO_NOTHING")
    try:
        select_next_best_action(
            candidates, min_expected_value_paise=0, propensity_probability_bps=8000
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_result_carries_full_candidate_set_for_audit():
    candidates = _all_six(
        RETRY_NOW=500, RETRY_LATER=200, ALT_ROUTE=100, REMINDER=50, ESCALATE=10, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert len(result.all_candidates) == 6
    assert {c.action_type for c in result.all_candidates} == set(ACTION_TYPES)
    assert result.propensity_probability_bps == 8000


# ═══════════════════════════════════════════════════════════════════════
# compute_action_confidence — Task AGENT1, deterministic EVI-margin score
# ═══════════════════════════════════════════════════════════════════════


def test_action_confidence_high_when_winner_dominates():
    candidates = _all_six(
        RETRY_NOW=1000, RETRY_LATER=100, ALT_ROUTE=50, REMINDER=10, ESCALATE=-100, DO_NOTHING=0
    )
    confidence = compute_action_confidence("RETRY_NOW", candidates)
    assert confidence == 0.90


def test_action_confidence_low_when_margin_is_thin():
    candidates = _all_six(
        RETRY_NOW=105, RETRY_LATER=100, ALT_ROUTE=50, REMINDER=10, ESCALATE=-100, DO_NOTHING=0
    )
    confidence = compute_action_confidence("RETRY_NOW", candidates)
    assert confidence == 0.30  # margin_ratio ~0.048, under every higher band


def test_action_confidence_medium_band():
    # margin_ratio = (130-100)/130 = 0.2308 -- lands in the [0.20, 0.5) band
    candidates = _all_six(
        RETRY_NOW=130, RETRY_LATER=100, ALT_ROUTE=50, REMINDER=10, ESCALATE=-100, DO_NOTHING=0
    )
    assert compute_action_confidence("RETRY_NOW", candidates) == 0.70


def test_action_confidence_low_medium_band():
    # margin_ratio = (110-100)/110 = 0.0909 -- lands in the [0.05, 0.20) band
    candidates = _all_six(
        RETRY_NOW=110, RETRY_LATER=100, ALT_ROUTE=50, REMINDER=10, ESCALATE=-100, DO_NOTHING=0
    )
    assert compute_action_confidence("RETRY_NOW", candidates) == 0.50


def test_action_confidence_bounded_when_winner_has_no_real_competition():
    """Only one candidate has a sane EVI; the rest are deeply negative --
    still bounded by the same bands, not automatically 1.0 just because
    nothing else came close."""
    candidates = _all_six(
        RETRY_NOW=500, RETRY_LATER=-999999, ALT_ROUTE=-999999, REMINDER=-999999,
        ESCALATE=-999999, DO_NOTHING=-999999,
    )
    confidence = compute_action_confidence("RETRY_NOW", candidates)
    assert confidence == 0.90
    assert confidence <= 1.0


def test_action_confidence_never_exceeds_one_or_drops_below_point_three():
    import itertools

    evis = [-500, -50, -5, 0, 5, 50, 500, 5000]
    for combo in itertools.product(evis, repeat=2):
        candidates = _all_six(RETRY_NOW=combo[0], RETRY_LATER=combo[1])
        confidence = compute_action_confidence("RETRY_NOW", candidates)
        assert 0.30 <= confidence <= 0.90


def test_select_next_best_action_result_carries_action_confidence():
    candidates = _all_six(
        RETRY_NOW=500, RETRY_LATER=200, ALT_ROUTE=100, REMINDER=50, ESCALATE=10, DO_NOTHING=0
    )
    result = select_next_best_action(
        candidates, min_expected_value_paise=0, propensity_probability_bps=8000
    )
    assert result.action_confidence == compute_action_confidence("RETRY_NOW", candidates)
