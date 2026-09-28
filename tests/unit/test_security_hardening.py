"""
Security-hardening regressions from the repo-wide correction pass:
startup refuses the public default API-key pepper outside demo/test, and
the event ingest schema bounds every client-supplied string.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api import main as api_main
from apps.api.routers.events import EventPayload
from recoveryos.config import INSECURE_DEFAULT_API_KEY_PEPPER, Settings


def _settings(env: str, pepper: str) -> Settings:
    return Settings(env=env, api_key_pepper=pepper)


def test_create_app_refuses_default_pepper_in_staging(monkeypatch):
    monkeypatch.setattr(
        api_main, "get_settings", lambda: _settings("staging", INSECURE_DEFAULT_API_KEY_PEPPER)
    )
    with pytest.raises(RuntimeError, match="API_KEY_PEPPER"):
        api_main.create_app()


def test_create_app_accepts_custom_pepper_in_staging(monkeypatch):
    monkeypatch.setattr(api_main, "get_settings", lambda: _settings("staging", "x" * 32))
    assert api_main.create_app() is not None


def test_create_app_allows_default_pepper_in_demo(monkeypatch):
    monkeypatch.setattr(
        api_main, "get_settings", lambda: _settings("demo", INSECURE_DEFAULT_API_KEY_PEPPER)
    )
    assert api_main.create_app() is not None


_VALID = {
    "payment_id": "p1",
    "merchant_id": "m1",
    "customer_id": "c1",
    "amount_paise": 100,
    "method": "upi",
    "event_type": "PAYMENT_FAILED",
}


def test_event_payload_accepts_valid_body():
    assert EventPayload(**_VALID).event_type == "PAYMENT_FAILED"


@pytest.mark.parametrize(
    "override",
    [
        {"payment_id": "x" * 65},
        {"customer_id": ""},
        {"bank": "b" * 65},
        {"idempotency_key": "k" * 129},
        {"event_type": "payment failed\r\nX-Injected: 1"},
        {"event_type": "lowercase"},
    ],
)
def test_event_payload_rejects_unbounded_or_malformed_fields(override):
    with pytest.raises(ValidationError):
        EventPayload(**{**_VALID, **override})
