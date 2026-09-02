"""services/customer_engine/opt_out.py::apply_customer_opt_out -- pure logic,
no DB. The end-to-end proof (real HTTP, real policy effect, real
simulator-vs-endpoint identity check) lives in
tests/integration/test_customer_opt_out.py; this file is the cheap,
fast-running unit-level companion for the function's own contract."""

from __future__ import annotations

from datetime import UTC, datetime

from recoveryos.models import AuditLog, Customer
from services.customer_engine.opt_out import apply_customer_opt_out


def _fresh_customer() -> Customer:
    return Customer(
        customer_id="11111111-1111-1111-1111-111111111111",
        merchant_id="22222222-2222-2222-2222-222222222222",
        is_returning=False,
        lifetime_value_paise=0,
        opted_out_at=None,
    )


def test_sets_opted_out_at_and_returns_an_audit_row():
    customer = _fresh_customer()
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)

    audit_row = apply_customer_opt_out(customer, now=now, reason="too many texts", channel="sms")

    assert customer.opted_out_at == now
    assert isinstance(audit_row, AuditLog)
    assert audit_row.customer_id == customer.customer_id
    assert audit_row.payment_id is None
    assert "sms" in audit_row.summary
    assert "too many texts" in audit_row.summary


def test_omits_reason_from_summary_when_not_given():
    customer = _fresh_customer()
    audit_row = apply_customer_opt_out(
        customer, now=datetime(2026, 9, 2, tzinfo=UTC), reason=None, channel="email"
    )
    assert "email" in audit_row.summary
    assert "—" not in audit_row.summary


def test_falls_back_to_unspecified_channel_when_none_given():
    customer = _fresh_customer()
    audit_row = apply_customer_opt_out(customer, now=datetime(2026, 9, 2, tzinfo=UTC))
    assert "unspecified channel" in audit_row.summary


def test_idempotent_second_call_is_a_no_op():
    customer = _fresh_customer()
    first = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
    later = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)

    first_audit_row = apply_customer_opt_out(customer, now=first, channel="sms")
    assert first_audit_row is not None

    second_audit_row = apply_customer_opt_out(customer, now=later, channel="email", reason="again")

    assert (
        second_audit_row is None
    ), "a customer already opted out must not produce a second audit row"
    assert (
        customer.opted_out_at == first
    ), "the original opt-out timestamp must never be overwritten"
