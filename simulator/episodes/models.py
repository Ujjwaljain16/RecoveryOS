"""
Recovery Episode data models for RecoveryOS Simulator (TRD §6, Phase 2).

An "episode" is the core unit of the recovery decision problem:
    1. A payment fails (attempt 1)
    2. The system must decide: RETRY_NOW or DO_NOT_RETRY
    3. If retried: more attempts follow until SUCCESS, patience exhausted, or MAX_RETRIES
    4. Two ground-truth labels are derived:
         - actual_recovered: did the episode actually produce a SUCCESS?
         - optimal_recovery_action: was retrying economically optimal at decision time?

CRITICAL: Both labels are HIDDEN from the model. The model sees only visible_features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ─── Economics constants (configurable — also in calibration/parameters.yaml) ──
FIXED_RETRY_COST_PAISE: int = 100        # ₹1 flat per retry attempt
VARIABLE_RETRY_COST_RATE: float = 0.001  # 0.10% of transaction amount
RECOVERY_MARGIN: float = 0.15           # 15% platform margin on recovered transaction


def compute_retry_cost(amount_paise: int) -> int:
    """Total cost to execute one retry attempt (fixed + proportional)."""
    return FIXED_RETRY_COST_PAISE + int(amount_paise * VARIABLE_RETRY_COST_RATE)


def compute_expected_retry_value(
    true_recovery_prob: float,
    amount_paise: int,
) -> int:
    """
    E[retry] = P(recovery | latent state) × amount × margin − retry_cost

    This is the economic value of initiating recovery at decision time.
    Returns integer paise. Positive → RETRY_NOW is optimal.

    NOTE: This uses latent state — it is NEVER exposed to the model.
    """
    retry_cost = compute_retry_cost(amount_paise)
    expected_value = true_recovery_prob * amount_paise * RECOVERY_MARGIN - retry_cost
    return int(expected_value)


def derive_optimal_action(
    true_recovery_prob: float,
    amount_paise: int,
) -> Literal["RETRY_NOW", "DO_NOT_RETRY"]:
    """
    Phase 2 binary decision.
    Phase 3 will introduce WAIT with optimal timing.
    """
    return "RETRY_NOW" if compute_expected_retry_value(true_recovery_prob, amount_paise) > 0 else "DO_NOT_RETRY"


# ─── Episode Data Models ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RetryAttempt:
    """
    A single retry attempt within a recovery episode.
    Visible fields: what the system can observe about this attempt.
    Latent fields: the hidden state at this attempt — evaluator/simulator only.
    """
    attempt_number: int                            # 2, 3, 4 (attempt 1 is the initial failure)
    delay_seconds: int                             # time waited before this retry (visible)
    # Visible telemetry:
    observed_failure_code: str | None              # None if succeeded
    observed_failure_class: str | None             # None if succeeded
    outcome: Literal["FAILED", "SUCCESS"]
    occurred_at: datetime
    # Latent state at this attempt (HIDDEN — evaluator only):
    latent_patience_at_attempt: float
    latent_bank_health_at_attempt: float
    true_failure_type_at_attempt: str             # TrueFailureType.value or "SUCCESS"


@dataclass(frozen=True)
class RecoveryEpisode:
    """
    A complete recovery episode: initial failure + retry chain + outcome + labels.

    Visible at decision time (what the model gets):
        - All fields in the "Visible" section below
        - retry_count, time-since-failure from retries list (derived features)

    Hidden (evaluator only):
        - actual_recovered
        - optimal_recovery_action
        - expected_value_of_retry_paise
        - All latent_* fields in retries

    The split between visible and hidden is enforced by DatasetBuilder when writing
    features.parquet vs labels.parquet.
    """
    episode_id: str
    simulation_id: str
    payment_id: str

    # ── Visible: Payment context at decision time ──────────────────────────────
    amount_paise: int
    method: str
    bank: str
    merchant_id: str
    customer_id: str
    is_returning_customer: bool
    customer_ltv_decile: int                       # [1..10], binned on train only
    # Initial failure telemetry (visible):
    initial_failure_code: str
    initial_failure_class: str
    # Temporal context (visible):
    hour_of_day: int                               # 0–23
    day_of_week: int                               # 0=Monday, 6=Sunday
    created_at: datetime
    clock_timestamp: datetime                      # simulator virtual time (for temporal split)

    # ── Retry chain (visible outcomes, hidden latent fields) ──────────────────
    retries: tuple[RetryAttempt, ...]              # attempts 2, 3, 4...
    retry_count: int                               # len(retries)
    total_episode_duration_sec: int                # from attempt 1 to final outcome

    # ── Actual simulated outcome ───────────────────────────────────────────────
    actual_outcome: Literal["RECOVERED", "ABANDONED", "MAX_RETRIES_REACHED"]

    # ── Ground truth labels (HIDDEN — evaluator/latent world only) ─────────────
    actual_recovered: bool
    """
    Factual: did the episode actually produce a SUCCESS within the simulated horizon?
    NOTE: actual_recovered=False does NOT prove the payment was unrecoverable —
    it only means it did not recover within max_retries attempts.
    """
    optimal_recovery_action: Literal["RETRY_NOW", "DO_NOT_RETRY"]
    """
    Prescriptive counterfactual: was initiating recovery economically optimal
    at decision time (after attempt 1, using latent state)?
    Derived: E[retry | attempt-1 latent] > 0 → RETRY_NOW
    """
    expected_value_of_retry_paise: int
    """
    E[retry] = P(recovery | latent) × amount × RECOVERY_MARGIN − retry_cost
    Hidden — used to derive optimal_recovery_action. Evaluator-only.
    """
    # Latent state at decision time (attempt 1) — HIDDEN:
    latent_patience_at_decision: float
    latent_bank_health_at_decision: float
    true_recovery_prob_bps_at_decision: int        # basis points [0, 10000]

    # ── Split metadata ─────────────────────────────────────────────────────────
    split_name: str                                # train | val_random | val_temporal | test_random | test_temporal


@dataclass
class EpisodeBatchResult:
    """Collected output from EpisodeGenerator.generate_episodes()."""
    simulation_id: str
    episodes: list[RecoveryEpisode]
    # Summary statistics (computed post-generation)
    total_failed_payments: int = 0
    actual_recovered_count: int = 0
    retry_now_optimal_count: int = 0
    max_retries_reached_count: int = 0
    abandoned_count: int = 0
