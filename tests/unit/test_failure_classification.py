"""
Live E2E smoke test finding (2026-08-29): a real payment ingested through
POST /v1/events -> event_processor never got a failure_class -- EventPayload
has no such field (merchants report a failure CODE, not our internal
TEMPORARY/PERMANENT/CUSTOMER_SPECIFIC taxonomy), and services/event_processor/
repository.py's upsert_payment only ever wrote failure_code. Every real,
non-simulator-seeded payment then hit
services.recovery_engine.propensity.build_propensity_context's hard
ValueError the moment it reached decisioning. Fixed by classify_failure()
deriving a best-effort failure_class from failure_code, defaulting to
TEMPORARY (never NULL) for anything unrecognized.
"""

from __future__ import annotations

from services.event_processor.repository import classify_failure


def test_classify_failure_defaults_to_temporary_for_none():
    assert classify_failure(None) == "TEMPORARY"


def test_classify_failure_defaults_to_temporary_for_unrecognized_code():
    assert classify_failure("GATEWAY_TIMEOUT") == "TEMPORARY"
    assert classify_failure("BANK_DECLINED") == "TEMPORARY"
    assert classify_failure("NETWORK_ERROR") == "TEMPORARY"


def test_classify_failure_recognizes_permanent_keywords():
    assert classify_failure("INVALID_CREDENTIALS") == "PERMANENT"
    assert classify_failure("EXPIRED_CARD") == "PERMANENT"
    assert classify_failure("ACCOUNT_CLOSED") == "PERMANENT"
    assert classify_failure("CARD_BLOCKED") == "PERMANENT"


def test_classify_failure_recognizes_customer_specific_keywords():
    assert classify_failure("INSUFFICIENT_FUNDS") == "CUSTOMER_SPECIFIC"
    assert classify_failure("CUSTOMER_AUTH_EXHAUSTED") == "CUSTOMER_SPECIFIC"
    assert classify_failure("LIMIT_EXCEEDED") == "CUSTOMER_SPECIFIC"


def test_classify_failure_never_returns_none_or_empty():
    for code in [None, "", "TIMEOUT", "WEIRD_UNSEEN_CODE_123"]:
        result = classify_failure(code)
        assert result, f"classify_failure({code!r}) must never return a falsy value"
        assert result in {"TEMPORARY", "PERMANENT", "CUSTOMER_SPECIFIC"}
