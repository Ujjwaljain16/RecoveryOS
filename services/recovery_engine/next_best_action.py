"""
Next-best-action selection — TRD §3.1's decision rule, gaps.md §A.2.

    select action = argmax(EVI) subject to EVI > policy.min_expected_value_paise
    If no action clears the floor -> DO_NOTHING

Two functions, split deliberately by I/O boundary:
  - generate_candidate_actions(): does I/O (fetches action_costs rows per
    action type) — builds all 6 CandidateActionResult scores.
  - select_next_best_action(): PURE. Takes already-scored candidates and a
    floor, returns the winner. This is the function every "DO_NOTHING
    selected", "RETRY_LATER beats RETRY_NOW", etc. test exercises directly,
    with hand-built candidate tuples — no DB needed to prove the selection
    logic itself is correct.

DO_NOTHING is excluded from the argmax competition on purpose: TRD's rule is
"argmax among actions that clear the floor, else DO_NOTHING" — DO_NOTHING is
the fallback, not a competitor. With the platform-default policy config
(min_expected_value_paise=0), DO_NOTHING's own EVI is exactly 0 (zero
recovery probability applied, zero cost/friction/risk) — 0 does not clear a
floor of 0 (strict inequality), so DO_NOTHING never wins by accidentally
"clearing its own floor"; it only wins via the explicit fallback branch,
which is what makes this a real, exercised code path rather than a
theoretical one (see test_do_nothing_selected_when_no_action_clears_floor).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from services.recovery_engine.evi import (
    calculate_evi,
    friction_penalty_paise,
    get_action_cost,
    risk_penalty_paise,
)
from services.recovery_engine.timing import AnomalyContext, expected_recovery_prob_bps

ACTION_TYPES: tuple[str, ...] = (
    "RETRY_NOW",
    "RETRY_LATER",
    "ALT_ROUTE",
    "REMINDER",
    "ESCALATE",
    "DO_NOTHING",
)


@dataclass(frozen=True)
class CandidateActionResult:
    """Mirrors candidate_actions' columns (TRD §2) exactly — this dataclass
    IS what gets persisted, one row per action_type per payment."""

    action_type: str
    recovery_prob_bps: int
    expected_value_paise: int
    cost_paise: int
    friction_penalty_paise: int
    risk_penalty_paise: int


@dataclass(frozen=True)
class NextBestActionResult:
    chosen_action: str
    chosen_evi_paise: int
    all_candidates: tuple[CandidateActionResult, ...]
    propensity_probability_bps: int
    cleared_floor: bool
    action_confidence: float


def compute_action_confidence(
    chosen_action: str, candidates: tuple[CandidateActionResult, ...]
) -> float:
    """
    Task AGENT1 -- deterministic, EVI-margin-based confidence that the
    CHOSEN action is genuinely the best one, separate from (and computed
    completely independently of) diagnosis confidence. Not a probability:
    a disclosed heuristic bucketing of "how much better is the winner than
    its closest real competitor," same philosophy as the investigative
    diagnoser's confidence_band (point 1 of the agent-design review) --
    honest qualitative bands, not a fake-calibrated float. No LLM call:
    this is exactly the kind of decision EVI's own deterministic economics
    should answer, not a language model's guess.
    """
    chosen = next(c for c in candidates if c.action_type == chosen_action)
    other_evis = [c.expected_value_paise for c in candidates if c.action_type != chosen_action]
    runner_up_evi = max(other_evis) if other_evis else chosen.expected_value_paise

    denom = max(abs(chosen.expected_value_paise), abs(runner_up_evi), 1)
    margin_ratio = (chosen.expected_value_paise - runner_up_evi) / denom
    margin_ratio = max(0.0, min(1.0, margin_ratio))

    if margin_ratio >= 0.5:
        return 0.90
    if margin_ratio >= 0.20:
        return 0.70
    if margin_ratio >= 0.05:
        return 0.50
    return 0.30


async def generate_candidate_actions(
    session: AsyncSession,
    merchant_id: str | None,
    amount_paise: int,
    customer_is_returning: bool,
    base_propensity_prob_bps: int,
    anomaly_context: AnomalyContext | None,
) -> tuple[CandidateActionResult, ...]:
    """
    Score all 6 candidate actions for one payment. The ONLY I/O in this
    module: one get_action_cost() call per action type.
    """
    candidates = []
    for action_type in ACTION_TYPES:
        action_cost = await get_action_cost(session, merchant_id, action_type)
        adjusted_prob_bps = expected_recovery_prob_bps(
            base_propensity_prob_bps, action_type, anomaly_context
        )
        friction = friction_penalty_paise(
            action_type, action_cost.friction_base_paise, customer_is_returning
        )
        risk = risk_penalty_paise(action_type, anomaly_context)
        evi = calculate_evi(adjusted_prob_bps, amount_paise, action_cost.cost_paise, friction, risk)

        candidates.append(
            CandidateActionResult(
                action_type=action_type,
                recovery_prob_bps=adjusted_prob_bps,
                expected_value_paise=evi,
                cost_paise=action_cost.cost_paise,
                friction_penalty_paise=friction,
                risk_penalty_paise=risk,
            )
        )
    return tuple(candidates)


def select_next_best_action(
    candidates: tuple[CandidateActionResult, ...],
    min_expected_value_paise: int,
    propensity_probability_bps: int,
) -> NextBestActionResult:
    """
    Pure selection logic — TRD §3.1's decision rule. No I/O, hand-testable
    with any tuple of CandidateActionResult.
    """
    do_nothing = next((c for c in candidates if c.action_type == "DO_NOTHING"), None)
    if do_nothing is None:
        raise ValueError("candidates must always include a DO_NOTHING entry")

    competing = [c for c in candidates if c.action_type != "DO_NOTHING"]
    eligible = [c for c in competing if c.expected_value_paise > min_expected_value_paise]

    if eligible:
        chosen = max(eligible, key=lambda c: c.expected_value_paise)
        cleared_floor = True
    else:
        chosen = do_nothing
        cleared_floor = False

    return NextBestActionResult(
        chosen_action=chosen.action_type,
        chosen_evi_paise=chosen.expected_value_paise,
        all_candidates=candidates,
        propensity_probability_bps=propensity_probability_bps,
        cleared_floor=cleared_floor,
        action_confidence=compute_action_confidence(chosen.action_type, candidates),
    )
