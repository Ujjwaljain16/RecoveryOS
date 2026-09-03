"""
Timing-adjusted recovery probability — the ONLY place action timing changes
the propensity model's raw P(recover) estimate.

Grounding (see the design discussion this file resulted from): the
episode simulator has NO real data to calibrate an hours-scale "wait and
recovery improves" curve for an individual, non-systemic temporary failure —
its retry-chain delay tops out at MIN_RETRY_DELAY_SEC..MAX_RETRY_DELAY_SEC
(1-5 minutes, simulator/episodes/generator.py), nowhere near production's
retry_cooldown_hours=12 default. The only latent time-decay that DOES exist
(LatentRecoverabilityFunction's customer-patience exp decay, keyed on
attempt_number) is explicitly hidden ground truth — using it here would be
the exact non-circularity leak the simulator's ground-truth separation
was built to prevent.

The one genuinely real, non-latent, already-measured signal for "is right
now worse than normal" is the anomaly detector: observed_rate vs
baseline_rate for a bank, computed from real payment outcomes (see
services/risk_engine/anomaly.py). So the mechanism here is narrow and
honest: RETRY_NOW gets penalized during an active HIGH-severity systemic
anomaly, by the ACTUAL measured ratio of how depressed the bank's current
success rate is versus its own baseline — never a guessed decay curve.
RETRY_LATER and ALT_ROUTE are not penalized under that same condition
(they route around/wait out the exact condition being measured).

COVERAGE LIMIT (documented here and in gaps.md — read before claiming more
than this proves): this mechanism only makes RETRY_LATER win on probability
during an active systemic anomaly. Outside one, RETRY_LATER can only win on
cost/friction — an individual customer's non-systemic temporary timeout
(PRD §32 Scenario D) has NO calibrated wait-benefit in this system, because
the simulator never generated data at that timescale. Lean any pitch of "waiting
helps" on the systemic-degradation scenario specifically; it is not a
general claim.

Zero I/O: this module takes an already-computed AnomalyContext (the caller
fetches anomaly_windows state, if any, and passes it in) — no DB/network
calls happen here, matching the same purity discipline as
services/policy_engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

BPS_SCALE = 10_000

# Task REPLAN1 -- how long to wait before re-evaluating a RETRY_LATER
# decision. Two grounded cases, same honesty discipline as this module's
# own COVERAGE LIMIT above (never a guessed decay curve):
#
#   - Deferred because of an active high-severity systemic anomaly: wait
#     the SAME 30-minute re-evaluation window services/risk_engine/
#     anomaly.py's is_cohort_suppressed() already uses to decide whether an
#     anomaly reading is still "fresh" -- reusing an existing, already-
#     justified constant, not inventing a new one just for this.
#   - Deferred for any other reason (cost/friction, no active anomaly):
#     wait the platform's own configured retry_cooldown_hours -- the same
#     number CooldownRule already uses to gate a next attempt, so a
#     RETRY_LATER decision waits exactly as long as the policy engine
#     would have required anyway.
ANOMALY_REEVALUATION_MINUTES = 30

# Actions that route around (or wait out) the exact bank condition an active
# systemic anomaly measures — these are NOT penalized when RETRY_NOW is.
BYPASS_ACTIONS = frozenset({"RETRY_LATER", "ALT_ROUTE"})

# Only a HIGH-severity, sufficiently-sampled anomaly triggers the penalty —
# matches TRD §3.2's own high-severity threshold (the one that also forms a
# cohort / triggers SystemicSuppressionRule) and its n<30 insufficient-data
# guard. A "medium" (flagged-only, no auto-suppression) or "low" reading, or
# a reading the detector itself couldn't compute a real z-score for, must
# not change action-selection outcomes.
PENALIZED_SEVERITY = "high"


@dataclass(frozen=True)
class AnomalyContext:
    """
    Minimal, pure-data view of a bank's current anomaly state — built by the
    caller from services.risk_engine.anomaly.AnomalyResult (or None if no
    window exists yet for this bank/bucket). Deliberately does NOT import
    that module: this dataclass has zero dependency on SQLAlchemy/DB code,
    keeping services/recovery_engine importable with no I/O surface of its
    own, same discipline as services/policy_engine.
    """

    severity: str  # "insufficient_data" | "low" | "medium" | "high"
    is_anomaly: bool
    observed_rate: float | None  # fraction [0,1], None if not computed
    baseline_rate: float | None  # fraction [0,1], None if not computed

    @property
    def has_sufficient_data(self) -> bool:
        return self.severity != "insufficient_data"


def _anomaly_penalty_bps(context: AnomalyContext) -> int:
    """
    Real, measured penalty factor (in bps, capped at BPS_SCALE i.e. 1.0) for
    how depressed a bank's CURRENT success rate is versus its own baseline —
    never a boost. Returns BPS_SCALE (no-op multiplier) whenever the
    condition for applying a penalty at all isn't met.
    """
    if not context.has_sufficient_data:
        return BPS_SCALE
    if context.severity != PENALIZED_SEVERITY or not context.is_anomaly:
        return BPS_SCALE
    if not context.observed_rate or not context.baseline_rate or context.baseline_rate <= 0:
        # Can't compute a real ratio from this — don't fabricate one.
        return BPS_SCALE

    # observed_rate/baseline_rate here are FAILURE rates (higher = worse).
    # The corresponding SUCCESS-rate ratio a customer actually experiences
    # is the complement: (1 - observed_failure) / (1 - baseline_failure).
    observed_success = 1.0 - context.observed_rate
    baseline_success = 1.0 - context.baseline_rate
    if baseline_success <= 0:
        return BPS_SCALE

    ratio = observed_success / baseline_success
    # Condition 1: clamp to [0, 1.0] — this function is a penalty ONLY,
    # never a boost past what the certified model itself estimated.
    ratio = max(0.0, min(1.0, ratio))
    return int(round(ratio * BPS_SCALE))


def expected_recovery_prob_bps(
    base_prob_bps: int,
    action_type: str,
    anomaly_context: AnomalyContext | None,
) -> int:
    """
    The propensity model's base_prob_bps, adjusted ONLY for the narrow,
    grounded case described in this module's docstring. Pure function, no
    I/O, integer bps in and out.

    - No anomaly_context (caller has no anomaly_windows row for this bank
      yet) -> unmodified base_prob_bps.
    - Insufficient-data / not currently a high-severity anomaly -> unmodified.
    - RETRY_LATER / ALT_ROUTE during an active high-severity anomaly ->
      unmodified (they bypass the exact condition being measured).
    - RETRY_NOW during an active high-severity anomaly -> penalized by the
      real observed-vs-baseline success-rate ratio, never boosted.
    - Every other action (REMINDER, ESCALATE, DO_NOTHING) -> unmodified;
      their recovery odds aren't about retrying through the bank rail at
      all, so this bank-health signal doesn't apply to them.
    """
    if anomaly_context is None:
        return base_prob_bps
    if action_type in BYPASS_ACTIONS:
        return base_prob_bps
    if action_type != "RETRY_NOW":
        return base_prob_bps

    penalty_bps = _anomaly_penalty_bps(anomaly_context)
    if penalty_bps >= BPS_SCALE:
        return base_prob_bps
    return (base_prob_bps * penalty_bps) // BPS_SCALE


def compute_retry_delay(
    is_high_severity_anomaly: bool,
    retry_cooldown_hours: int,
) -> timedelta:
    """
    How long to wait before re-evaluating a RETRY_LATER decision (Task
    REPLAN1 -- the continuous-replanning scheduler). Pure function, no I/O.
    See the module-level constant's comment for why these two specific
    durations, not an invented curve.
    """
    if is_high_severity_anomaly:
        return timedelta(minutes=ANOMALY_REEVALUATION_MINUTES)
    return timedelta(hours=retry_cooldown_hours)
