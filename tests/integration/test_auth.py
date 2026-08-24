"""
Integration tests for API key authentication (Task 4).

Before this task, X-Merchant-ID was a self-reported, unverified header used
for BOTH rate-limit identity and an "anti-spoofing" check that protected
against nothing — there was no verified identity on either side of that
comparison, only two client-controlled values checked against each other.

These tests prove the replacement: a real X-API-Key, resolved to a Merchant
row by hashed lookup (apps/api/dependencies/auth.py), is now the ONLY
source of merchant identity anywhere in the request path.

Requirements:
  - Real Redis (testcontainers) + real Postgres (testcontainers via the
    root conftest.py) — shared fixtures in tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid

import pytest

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key


def _valid_event_payload(merchant_id: str) -> dict:
    return {
        "payment_id": str(uuid.uuid4()),
        "merchant_id": merchant_id,
        "customer_id": str(uuid.uuid4()),
        "amount_paise": 50000,
        "method": "upi",
        "bank": "HDFC",
        "event_type": "PAYMENT_FAILED",
        "failure_code": "BANK_TIMEOUT",
    }


async def _seeded_merchant(migrated_db: str, name: str) -> tuple[str, str]:
    """Seed a fresh merchant with a real API key. Returns (merchant_id, raw_api_key)."""
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, name, raw_key)
    return merchant_id, raw_key


@pytest.mark.asyncio
async def test_missing_api_key_rejected_401(async_client, migrated_db):
    """
    No X-API-Key header at all → 401, before the request body is even
    meaningfully processed. Applies to /v1/events (POST) and /v1/risk/summary
    (GET) — every route that resolves a merchant identity, not just one.
    """
    merchant_id, _key = await _seeded_merchant(migrated_db, "missing-key-test")
    payload = _valid_event_payload(merchant_id)

    resp = await async_client.post("/v1/events", json=payload)
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"
    assert "X-API-Key" in resp.json()["detail"]

    resp2 = await async_client.get("/v1/risk/summary")
    assert resp2.status_code == 401, f"Expected 401, got {resp2.status_code}: {resp2.text}"


@pytest.mark.asyncio
async def test_invalid_api_key_rejected_401(async_client, migrated_db):
    """
    A syntactically plausible but nonexistent API key → 401, distinctly
    from the "missing" case above but the same status code (never leak
    whether a key format is merely wrong vs. simply not on file).
    """
    merchant_id, _real_key = await _seeded_merchant(migrated_db, "invalid-key-test")
    payload = _valid_event_payload(merchant_id)

    fake_key = generate_api_key()  # well-formed, but never seeded/persisted anywhere
    resp = await async_client.post("/v1/events", json=payload, headers={"X-API-Key": fake_key})
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"
    assert resp.json()["detail"] == "Invalid API key."

    # A key that's merely a garbage string (not even a real key shape) must
    # also 401, not 500 — hash_api_key/verify_api_key must not assume any
    # particular input shape beyond "a string".
    resp2 = await async_client.post(
        "/v1/events", json=payload, headers={"X-API-Key": "not-a-real-key-at-all"}
    )
    assert resp2.status_code == 401


@pytest.mark.asyncio
async def test_valid_api_key_resolves_correct_merchant_scoping(async_client, migrated_db):
    """
    Two real merchants, two real keys. Merchant A's key must:
      - see ONLY its own payment via GET /v1/payments/{id}/detail (Task 6's
        real, merchant-scoped implementation) — this is the direct proof of
        resolution AND of resource-level authorization, not just identity
        echoing. (/v1/risk/summary was the original vehicle for this proof,
        before Task 6 changed it to an honest 501 — see that router.)
      - be rejected (403) if used to submit an event claiming to be a
        DIFFERENT merchant (the anti-spoofing check, now backed by a real
        verified identity instead of two unverified client-supplied values)
      - never be usable to exhaust or read merchant B's rate-limit bucket
        or vice versa (covered in depth by
        test_rate_limit_now_keyed_on_verified_merchant_not_raw_header below;
        asserted again here as a basic sanity check from the "scoping" angle)

    This is the test proving merchant A's key can never see/affect merchant
    B's data via any route currently wired to real auth.
    """
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.integration.conftest import to_async_url

    merchant_a, key_a = await _seeded_merchant(migrated_db, "scoping-test-merchant-a")
    merchant_b, key_b = await _seeded_merchant(migrated_db, "scoping-test-merchant-b")

    # Seed one real payment owned by merchant A directly (bypassing the HTTP
    # ingest path — that path is already covered end-to-end by
    # test_ingest.py; this test's job is auth/scoping, not re-proving ingest).
    payment_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            _text(
                "INSERT INTO customers (customer_id, merchant_id) VALUES (:cid, :mid) "
                "ON CONFLICT DO NOTHING"
            ),
            {"cid": customer_id, "mid": merchant_a},
        )
        await conn.execute(
            _text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, status) VALUES (:pid, :mid, :cid, 75000, 'upi', 'failed')"
            ),
            {"pid": payment_id, "mid": merchant_a, "cid": customer_id},
        )
    await engine.dispose()

    # ── "see": A's key sees its own payment; B's key gets 404, not the data ──
    resp_a = await async_client.get(
        f"/v1/payments/{payment_id}/detail", headers={"X-API-Key": key_a}
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["payment_id"] == payment_id
    assert resp_a.json()["payment"]["amount_paise"] == 75000

    resp_b = await async_client.get(
        f"/v1/payments/{payment_id}/detail", headers={"X-API-Key": key_b}
    )
    assert resp_b.status_code == 404, (
        f"Merchant B's key must NOT be able to see merchant A's payment — "
        f"got {resp_b.status_code}: {resp_b.text}"
    )

    # A nonexistent payment_id 404s identically — a caller can't distinguish
    # "not yours" from "doesn't exist" by status code alone (no enumeration).
    resp_missing = await async_client.get(
        f"/v1/payments/{uuid.uuid4()}/detail", headers={"X-API-Key": key_a}
    )
    assert resp_missing.status_code == 404

    # ── "affect": merchant A's key cannot submit an event AS merchant B ────
    payload_as_b = _valid_event_payload(merchant_b)
    resp_spoof = await async_client.post(
        "/v1/events", json=payload_as_b, headers={"X-API-Key": key_a}
    )
    assert resp_spoof.status_code == 403
    assert resp_spoof.json()["error"] == "merchant_identity_mismatch"

    # ── sanity: A's key legitimately submitting AS A still works ───────────
    payload_as_a = _valid_event_payload(merchant_a)
    resp_legit = await async_client.post(
        "/v1/events", json=payload_as_a, headers={"X-API-Key": key_a}
    )
    assert resp_legit.status_code == 202


@pytest.mark.asyncio
async def test_rate_limit_now_keyed_on_verified_merchant_not_raw_header(
    async_client, redis_client, migrated_db
):
    """
    Before Task 4: rate-limit identity came straight from X-Merchant-ID, an
    unverified header — a caller could rotate that header per request to
    dodge its own rate limit entirely (each rotated value gets a fresh,
    full bucket), or deliberately set it to someone else's merchant_id to
    burn through THEIR bucket. Neither is possible once the bucket key is
    derived from a verified API key instead: the bucket is tied to
    `merchant.merchant_id` from verify_api_key (apps/api/dependencies/rate_limit.py),
    which cannot be forged or rotated without a different real key.
    """
    import time

    merchant_id, api_key = await _seeded_merchant(migrated_db, "rate-limit-identity-test")

    # Pre-exhaust the bucket keyed on the REAL merchant_id (what the limiter
    # actually uses now) — see test_ingest.py's rate-limit test for why
    # last_ms is seeded into the future rather than "now".
    bucket_key = f"rate_limit:events:{merchant_id}"
    now_ms = int(time.time() * 1000) + 5000
    await redis_client.hset(bucket_key, mapping={"tokens": "0", "last_ms": str(now_ms)})

    payload = _valid_event_payload(merchant_id)

    # The caller's real key resolves to the exhausted bucket → 429, no
    # matter what (nonexistent) X-Merchant-ID header does or doesn't say —
    # proving the header is no longer consulted for identity at all.
    resp = await async_client.post(
        "/v1/events",
        json=payload,
        headers={"X-API-Key": api_key, "X-Merchant-ID": str(uuid.uuid4())},
    )
    assert resp.status_code == 429, (
        f"Rate limit must trigger based on the verified API key's merchant, "
        f"ignoring any X-Merchant-ID header entirely — got {resp.status_code}: {resp.text}"
    )

    # Conversely: attempting to dodge the limit by ROTATING X-Merchant-ID
    # per request (the old attack — a fresh header value used to mean a
    # fresh, full bucket) must NOT bypass anything now. Re-seed the same
    # exhausted state first: the Lua script updates last_ms on every call,
    # allow OR deny (apps/api/dependencies/rate_limit.py), so real wall-clock
    # time passing between these two requests legitimately refills a few
    # tokens — that's correct token-bucket behavior, not a gap, and re-seeding
    # isolates THIS assertion to what it's actually testing: that a rotated
    # header value doesn't grant a different (fresh) bucket.
    now_ms2 = int(time.time() * 1000) + 5000
    await redis_client.hset(bucket_key, mapping={"tokens": "0", "last_ms": str(now_ms2)})
    resp2 = await async_client.post(
        "/v1/events",
        json=payload,
        headers={"X-API-Key": api_key, "X-Merchant-ID": "totally-unrelated-value"},
    )
    assert resp2.status_code == 429, (
        f"Rotating X-Merchant-ID must not grant a different bucket — "
        f"identity is the verified key's merchant, always — got {resp2.status_code}"
    )

    # A DIFFERENT real merchant (its own key, its own fresh bucket) is
    # unaffected — the exhaustion is genuinely scoped to the verified
    # merchant, not global and not header-driven.
    other_merchant_id, other_key = await _seeded_merchant(
        migrated_db, "rate-limit-identity-test-other"
    )
    other_payload = _valid_event_payload(other_merchant_id)
    resp3 = await async_client.post(
        "/v1/events", json=other_payload, headers={"X-API-Key": other_key}
    )
    assert resp3.status_code == 202, (
        f"A different verified merchant must have its own fresh bucket — "
        f"got {resp3.status_code}: {resp3.text}"
    )
