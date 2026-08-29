"""
Domain Audit Judge Question 4: since the LLM has zero authority over
action selection (F1), the actual worst case for this system is the
propensity-LR + EVI formula itself selecting a bad-but-policy-legal
action. This is the "independent of the AI-diagnoser story" adversarial
pass the audit asked for -- pure numeric edge cases against calculate_evi(),
the one function that turns (probability, amount, cost, friction, risk)
into the number select_next_best_action() argmaxes over.
"""

from __future__ import annotations

from services.recovery_engine.evi import calculate_evi

BPS_SCALE = 10_000


def test_evi_never_crashes_on_a_maximal_amount_paise():
    """A merchant-controlled amount_paise near the practical limit of a
    real payment (RBI's own e-mandate/UPI ceilings run into single-digit
    crores at most, but the DB column is BIGINT with no upper bound) must
    not overflow or crash calculate_evi -- pure Python ints are arbitrary
    precision, so this is really a 'does the formula stay sane' check,
    not an overflow check."""
    huge_amount = 10**15  # ₹1000 crore-equivalent paise -- absurdly large
    result = calculate_evi(
        recovery_prob_bps=8200,
        amount_paise=huge_amount,
        cost_paise=0,
        friction_paise=0,
        risk_paise=0,
    )
    assert isinstance(result, int)
    assert result > 0


def test_evi_at_zero_amount_is_never_positive():
    """A zero-amount payment (a merchant supplying amount_paise=0, or a
    payment record corrupted to 0) must never produce a positive EVI that
    could make a real action look economically justified when there is
    nothing to recover."""
    result = calculate_evi(
        recovery_prob_bps=10_000, amount_paise=0, cost_paise=0, friction_paise=0, risk_paise=0
    )
    assert result <= 0


def test_evi_at_zero_recovery_probability_is_never_positive():
    """recovery_prob_bps=0 (the model is certain this will never recover)
    must floor expected_recovery_paise at exactly 0 -- any positive EVI
    here would mean cost/friction/risk went negative somewhere, which
    would itself be a bug."""
    result = calculate_evi(
        recovery_prob_bps=0, amount_paise=500_000, cost_paise=0, friction_paise=0, risk_paise=0
    )
    assert result <= 0


def test_evi_at_max_recovery_probability_matches_hand_computed_value():
    """recovery_prob_bps=10000 (100%, the maximum the model can output --
    propensity.py's classifier output is itself bounded to [0,10000] before
    it ever reaches EVI) must not silently clamp/behave oddly at its own
    ceiling -- exact hand-computed check, not just 'is positive.'"""
    from services.recovery_engine.evi import RECOVERY_MARGIN_BPS

    amount_paise = 1_000_000
    expected = (amount_paise * 10_000 * RECOVERY_MARGIN_BPS) // (BPS_SCALE * BPS_SCALE)
    result = calculate_evi(
        recovery_prob_bps=10_000,
        amount_paise=amount_paise,
        cost_paise=0,
        friction_paise=0,
        risk_paise=0,
    )
    assert result == expected


def test_evi_out_of_range_recovery_prob_bps_does_not_silently_invert_sign():
    """calculate_evi() itself has no upper-bound guard on recovery_prob_bps
    (that validation, if any, lives upstream in propensity.py/the
    classifier's own output clamp) -- an out-of-contract value (e.g. a
    caller bug passing 15000 instead of a valid 0-10000 bps value) must
    still only ever scale expected_recovery_paise UP, never flip its sign
    or produce a nonsensical negative-from-a-positive-input result. This
    documents the formula's actual behavior at an out-of-contract input
    rather than assuming upstream validation is airtight everywhere."""
    amount_paise = 500_000
    in_range = calculate_evi(
        recovery_prob_bps=10_000,
        amount_paise=amount_paise,
        cost_paise=0,
        friction_paise=0,
        risk_paise=0,
    )
    out_of_range = calculate_evi(
        recovery_prob_bps=15_000,
        amount_paise=amount_paise,
        cost_paise=0,
        friction_paise=0,
        risk_paise=0,
    )
    assert out_of_range > in_range >= 0, (
        "calculate_evi() has no defensive upper-bound clamp on recovery_prob_bps -- "
        "documented here as a real gap: a caller bug passing an out-of-contract bps "
        "value would inflate EVI rather than being rejected"
    )


def test_evi_cost_larger_than_recovery_correctly_forces_negative():
    """A merchant-configured cost/friction/risk that legitimately exceeds
    the expected recovery (e.g. ESCALATE's real ₹150 human-review cost
    against a ₹50 payment) must produce a genuinely negative EVI -- this
    is what makes DO_NOTHING/BLOCK possible at all; a formula that floors
    at 0 here would silently hide uneconomical actions instead of letting
    MinExpectedValueRule catch them."""
    result = calculate_evi(
        recovery_prob_bps=9_500,
        amount_paise=5_000,
        cost_paise=15_000,
        friction_paise=0,
        risk_paise=0,
    )
    assert result < 0


def test_evi_scales_linearly_with_amount_no_hidden_amount_gaming_bonus():
    """Merchant-controlled amount_paise gaming: a merchant inflating the
    reported amount to force a marginal payment's EVI above
    min_expected_value_paise is a REAL lever (amount_paise is trusted,
    unvalidated input from the ingest boundary, per apps/api/routers/
    events.py's EventPayload.amount_paise -- only `gt=0` is enforced, no
    upper bound or cross-check against any external ground truth). This
    test doesn't claim to fix that (a merchant's own reported transaction
    amount has no independent oracle to validate against in this system's
    design) -- it documents that the relationship is EXACTLY linear (2x
    amount -> exactly 2x expected_recovery_paise contribution), so a
    merchant CAN reliably force any target EVI by choosing amount_paise
    accordingly. Real gap, not a false-positive concern: MinExpectedValueRule
    (services/policy_engine/rules.py) is the only thing standing between an
    inflated amount and an approved retry, and it has no independent
    amount-plausibility check of its own."""
    base = calculate_evi(
        recovery_prob_bps=8_200, amount_paise=100_000, cost_paise=0, friction_paise=0, risk_paise=0
    )
    doubled = calculate_evi(
        recovery_prob_bps=8_200, amount_paise=200_000, cost_paise=0, friction_paise=0, risk_paise=0
    )
    assert doubled == base * 2, (
        "expected_recovery_paise must scale exactly linearly with amount_paise -- "
        "confirming amount_paise is a direct, unclamped lever on EVI with no independent "
        "plausibility check anywhere in this formula"
    )
