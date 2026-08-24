"""Quick smoke test for POST /v1/events API."""
import httpx, uuid, time
import redis as syncredis

BASE = "http://localhost:8000"
merchant = str(uuid.uuid4())

payload = {
    "payment_id": str(uuid.uuid4()),
    "merchant_id": merchant,
    "customer_id": str(uuid.uuid4()),
    "amount_paise": 150000,
    "method": "upi",
    "bank": "HDFC",
    "event_type": "PAYMENT_FAILED",
    "failure_code": "BANK_TIMEOUT",
}
headers = {"X-Merchant-ID": merchant}

print("=" * 55)
print("PHASE 3 — LIVE API SMOKE TEST")
print("=" * 55)

# 1. Valid event → 202
r = httpx.post(f"{BASE}/v1/events", json=payload, headers=headers)
print(f"\n[1] Valid event")
print(f"    curl -X POST {BASE}/v1/events \\")
print(f"         -H 'X-Merchant-ID: {merchant}' \\")
print(f"         -d '{{payment_id: ..., amount_paise: 150000, method: upi, ...}}'")
print(f"    → HTTP {r.status_code}")
print(f"    → {r.text}")
assert r.status_code == 202, f"Expected 202, got {r.status_code}"

# 2. Malformed → 422 (amount=0)
bad = dict(payload, amount_paise=0, payment_id=str(uuid.uuid4()))
r2 = httpx.post(f"{BASE}/v1/events", json=bad, headers=headers)
print(f"\n[2] Malformed (amount_paise=0) → HTTP {r2.status_code}")
body2 = r2.json()
msgs = [e["msg"] for e in body2["detail"]] if isinstance(body2.get("detail"), list) else body2
print(f"    → {msgs}")
assert r2.status_code == 422, f"Expected 422, got {r2.status_code}"

# 3. Malformed → 422 (method=cash)
bad3 = dict(payload, method="cash", payment_id=str(uuid.uuid4()))
r3 = httpx.post(f"{BASE}/v1/events", json=bad3, headers=headers)
print(f"\n[3] Malformed (method=cash) → HTTP {r3.status_code}")
body3 = r3.json()
msgs3 = [e["msg"] for e in body3["detail"]] if isinstance(body3.get("detail"), list) else body3
print(f"    → {msgs3}")
assert r3.status_code == 422, f"Expected 422, got {r3.status_code}"

# 4. Merchant identity mismatch → 403
r4 = httpx.post(f"{BASE}/v1/events", json=payload, headers={"X-Merchant-ID": str(uuid.uuid4())})
print(f"\n[4] Merchant identity mismatch → HTTP {r4.status_code}")
print(f"    → {r4.text[:150]}")
assert r4.status_code == 403, f"Expected 403, got {r4.status_code}"

# 5. Rate limit → 429 (exhaust bucket via Redis)
r_client = syncredis.from_url("redis://localhost:6379/0", decode_responses=True)
bucket_key = f"rate_limit:events:{merchant}"
r_client.hset(bucket_key, mapping={"tokens": "0", "last_ms": str(int(time.monotonic() * 1000))})
r5 = httpx.post(f"{BASE}/v1/events", json=dict(payload, payment_id=str(uuid.uuid4())), headers=headers)
print(f"\n[5] Rate limit exhausted → HTTP {r5.status_code}")
print(f"    → {r5.text[:200]}")
assert r5.status_code == 429, f"Expected 429, got {r5.status_code}"
assert r5.json()["detail"]["error"] == "rate_limit_exceeded"

# 6. Verify Redis stream received messages
stream_len = r_client.xlen("stream:payment_failed")
print(f"\n[6] Redis stream:payment_failed length = {stream_len}")
assert stream_len >= 1, "Expected at least 1 message in stream"

print("\n" + "=" * 55)
print("ALL CHECKS PASSED ✓")
print("=" * 55)
