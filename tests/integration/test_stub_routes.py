"""
Integration tests for Task 6, Part B — the four domain routes that used to
take a live DB session dependency and never use it, returning hardcoded
empty/zero responses indistinguishable from genuine data.

risk / audit / experiments now 501 honestly (their real implementations
depend on tables nothing writes to yet). payments/{id}/detail got a real,
minimal, merchant-scoped implementation instead, since payments data
already exists from the working ingest path (Task 2/3) — proven separately
in test_auth.py's cross-tenant scoping test, not re-proven here.
"""

from __future__ import annotations

import uuid

import pytest

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key

# Routes expected to be honest 501s today, per Task 6's explicit call:
# "risk-gating is the right call for at least risk/audit/experiments (those
# depend on engines that don't exist yet)."
GATED_ROUTES = [
    ("GET", "/v1/risk/summary"),
    ("GET", "/v1/audit/{payment_id}"),
    ("GET", "/v1/experiments/{run_id}"),
]


async def _seeded_merchant(migrated_db: str) -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, "stub-route-test-merchant", raw_key)
    return merchant_id, raw_key


@pytest.mark.asyncio
async def test_stub_routes_return_501_not_fake_success(async_client, migrated_db):
    """
    Each gated route must respond 501, with a body that actually says it's
    unimplemented — not a 200 with hardcoded zeros/empties that reads as a
    confident, genuine answer to a caller who never inspects the source.
    """
    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    headers = {"X-API-Key": api_key}

    for method, path_template in GATED_ROUTES:
        path = path_template.format(payment_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()))
        resp = await async_client.request(method, path, headers=headers)

        assert resp.status_code == 501, (
            f"{method} {path} must return 501 Not Implemented, "
            f"got {resp.status_code}: {resp.text}"
        )

        body = resp.json()
        # FastAPI's default HTTPException body shape is {"detail": "..."}.
        # The detail must actually communicate non-implementation, not just
        # happen to carry a 501 status code with an empty/generic message —
        # a caller reading logs or an error banner needs the WORDS to make
        # sense, not just the number.
        detail = body.get("detail", "")
        assert "not implemented" in detail.lower(), (
            f"{method} {path}'s 501 body must clearly say it's not " f"implemented, got: {detail!r}"
        )

        # None of the old fake-success response SHAPES should be present —
        # checked as JSON keys (`"field":`), not a bare substring: the 501
        # detail message is allowed to use these words in prose explaining
        # what's missing (e.g. "not an empty-but-plausible audit_chain")
        # without that counting as the field actually being present.
        forbidden_fake_success_keys = [
            "total_revenue_at_risk_paise",
            "recoverable_estimate_paise",
            "audit_chain",
            "incremental_recovery_paise",
        ]
        for key in forbidden_fake_success_keys:
            assert f'"{key}":' not in resp.text, (
                f"{method} {path} still has a {key!r} field — looks like the old "
                f"hardcoded-success shape leaked back in"
            )


@pytest.mark.asyncio
async def test_gated_routes_401_before_ever_reaching_the_501(async_client):
    """
    Auth still runs first: an unauthenticated caller gets 401, not a 501
    that would leak "yes, this route exists and is merely unimplemented" to
    someone who never proved they're allowed to ask at all. (Not a hard
    security boundary here — the routes' existence is discoverable via
    /docs regardless — but the ordering itself is worth pinning down: it
    would be a real bug if a route's un-implementedness were checked BEFORE
    its auth.)
    """
    for method, path_template in GATED_ROUTES:
        path = path_template.format(payment_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()))
        resp = await async_client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} without a key must 401, not 501"


@pytest.mark.asyncio
async def test_payments_detail_is_real_not_gated(async_client, migrated_db):
    """
    payments/{id}/detail is the one route Task 6 gave a real minimal
    implementation instead of a 501 — payments data already exists from the
    working ingest path. Confirms it does NOT 501 (a nonexistent payment_id
    correctly 404s instead — that's real behavior, not "not implemented").
    """
    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    resp = await async_client.get(
        f"/v1/payments/{uuid.uuid4()}/detail", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 404, (
        f"A nonexistent payment should 404 (real lookup, real absence), not "
        f"501 — got {resp.status_code}: {resp.text}"
    )
