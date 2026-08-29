"""
Domain Audit finding #2 -- the invariant services/pipeline/ledger.py's
_should_correct_ledger encodes, tested as a pure function in isolation
(no DB) before any end-to-end proof. Per the explicit instruction this
exists to satisfy: "do not use higher-amount-wins as the sole correctness
rule" -- these tests specifically probe the cases a naive "higher wins"
rule would get wrong.
"""

from __future__ import annotations

from services.pipeline.ledger import _should_correct_ledger


def test_not_recovered_to_recovered_is_the_one_real_correction():
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=0, new_actual_recovery_paise=50_000, amount_paise_cap=100_000
    )
    assert should_correct is True
    assert amount == 50_000


def test_never_downgrades_an_already_recovered_payment():
    """Finding #1's exact concern, generalized: once a payment is known
    recovered, NOTHING corrects it again -- not even a 'higher' amount,
    since a payment can only owe what it owes."""
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=50_000,
        new_actual_recovery_paise=999_999,
        amount_paise_cap=999_999,
    )
    assert should_correct is False
    assert amount == 50_000


def test_never_downgrades_to_a_lower_or_zero_amount():
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=50_000, new_actual_recovery_paise=0, amount_paise_cap=100_000
    )
    assert should_correct is False
    assert amount == 50_000


def test_two_distinct_non_recovering_outcomes_never_correct_each_other():
    """The 'do not use higher-amount-wins as the sole rule' case: a SECOND
    non-recovering attempt (still 0 recovered) must not be treated as a
    correction just because it's later -- there is no new revenue
    information here, regardless of which specific non-recovery outcome
    (BLOCK, FAILED, DO_NOTHING) either one was."""
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=0, new_actual_recovery_paise=0, amount_paise_cap=100_000
    )
    assert should_correct is False
    assert amount == 0


def test_correction_amount_is_clamped_to_the_payment_amount_cap():
    """Sanity bound against a malformed/inflated webhook or provider
    report claiming more was recovered than was ever owed -- 'higher wins'
    alone would accept an arbitrarily large number verbatim."""
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=0,
        new_actual_recovery_paise=10_000_000,
        amount_paise_cap=50_000,
    )
    assert should_correct is True
    assert (
        amount == 50_000
    ), "must clamp to the payment's own amount_paise, not accept the raw report"


def test_correction_amount_within_cap_is_not_clamped():
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=0, new_actual_recovery_paise=30_000, amount_paise_cap=50_000
    )
    assert should_correct is True
    assert amount == 30_000


def test_negative_new_amount_never_corrects():
    """Defensive: a negative actual_recovery_paise should never be
    possible upstream, but this function must not treat it as 'new
    information' if it somehow arrives."""
    should_correct, amount = _should_correct_ledger(
        existing_actual_recovery_paise=0, new_actual_recovery_paise=-100, amount_paise_cap=100_000
    )
    assert should_correct is False
    assert amount == 0
