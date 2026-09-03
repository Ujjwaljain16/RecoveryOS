"""
Bounded AI-recommendation tie-break math. Pure, zero-I/O, same
purity discipline as services/recovery_engine/next_best_action.py and
services/policy_engine/rules.py: a rule must be a deterministic function of
its inputs, hand-testable with plain tuples, no DB/network/clock reads.

This module answers exactly one question: "which of the candidates the
deterministic engine already scored are economically indistinguishable from
the winner?" It does NOT decide whether AI's pick wins -- that decision
(does the AI's recommendation land in this set, and is that specific
candidate ALSO individually policy-ALLOWED) lives in
services/recovery_engine/orchestrator.py, the one module in this phase
allowed to do I/O and call services.policy_engine.evaluate() a second time.

Integer arithmetic only, matching services/recovery_engine/evi.py's own
non-negotiable discipline (gaps.md §B.4): the tolerance check below never
touches a float.
"""

from __future__ import annotations

from services.recovery_engine.evi import BPS_SCALE
from services.recovery_engine.next_best_action import CandidateActionResult


def find_near_tied_candidates(
    candidates: tuple[CandidateActionResult, ...],
    winner_evi_paise: int,
    min_expected_value_paise: int,
    tie_tolerance_bps: int,
) -> list[CandidateActionResult]:
    """
    Non-DO_NOTHING candidates that (a) cleared the EVI floor
    (expected_value_paise > min_expected_value_paise, same strict
    inequality select_next_best_action() uses) and (b) sit within
    tie_tolerance_bps of winner_evi_paise, expressed as a fraction of
    |winner_evi_paise|.

    tie_tolerance_bps=0 means "only an exact tie" (never invented for a
    demo, always a caller-supplied, disclosed constant --
    recoveryos.config.Settings.ai_tie_break_tolerance_bps). The deterministic
    winner itself always qualifies (distance 0), so this list is never empty
    when the winner itself cleared the floor.

    abs(c.expected_value_paise - winner_evi_paise) * BPS_SCALE
        <= abs(winner_evi_paise) * tie_tolerance_bps

    is the integer-only equivalent of
    |c - winner| / |winner| <= tie_tolerance_bps / BPS_SCALE, cross-multiplied
    to avoid any float division. When winner_evi_paise == 0, only an exact-0
    candidate can satisfy this (0 <= 0), which is correct: there is no
    meaningful "relative" tolerance around a zero winner.
    """
    near_tied = []
    for c in candidates:
        if c.action_type == "DO_NOTHING":
            continue
        if c.expected_value_paise <= min_expected_value_paise:
            continue
        distance = abs(c.expected_value_paise - winner_evi_paise)
        if distance * BPS_SCALE <= abs(winner_evi_paise) * tie_tolerance_bps:
            near_tied.append(c)
    return near_tied
