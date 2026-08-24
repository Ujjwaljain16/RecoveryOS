"""
API key authentication.

Replaces "trust whatever X-Merchant-ID the client sends" (the old model:
apps/api/routers/events.py's "anti-spoofing" check compared two
client-controlled values against EACH OTHER — a header and a body field the
same untrusted caller supplied both of. There was nothing to spoof FROM,
because neither value was ever verified against anything real).

Design: one API key per merchant, presented via X-API-Key, resolved to a
verified Merchant row by hashed lookup against merchants.api_key_hash. From
here on, the merchant_id used for rate limiting AND for scoping any
merchant-owned data is ALWAYS `merchant.merchant_id` from this dependency —
never a client-supplied merchant_id, in a header or in a request body.

Hashing choice: HMAC-SHA256(pepper, raw_key), not bcrypt/argon2. Those slow
KDFs exist to blunt brute-forcing LOW-entropy secrets (human passwords
drawn from a small effective keyspace). API keys here are generated
server-side from 256 bits of os.urandom (see generate_api_key) — already
far outside brute-force range — so a deliberately slow hash only taxes
every authenticated request's latency for no matching security benefit.
This is the same tradeoff Stripe/GitHub/AWS make for their API key schemes,
as distinct from their password-login paths. The pepper
(settings.api_key_pepper) is a server-side secret folded into the hash so a
leaked api_key_hash column alone (e.g. a read-only SQLi, without RCE or env
access) isn't sufficient to derive a forgeable key even in principle.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.config import get_settings
from recoveryos.database import get_app_session
from recoveryos.models import Merchant

API_KEY_PREFIX = "rk_live_"


def generate_api_key() -> str:
    """
    Generate a new, high-entropy API key for a merchant.

    Server-side only. The raw value must be shown to the operator ONCE
    (e.g. in a provisioning script's output) and never stored — only its
    hash (see hash_api_key) is persisted, so a lost key means reissue, not
    recovery.
    """
    return f"{API_KEY_PREFIX}{secrets.token_hex(32)}"


def hash_api_key(raw_key: str) -> str:
    """HMAC-SHA256(pepper, raw_key) — see module docstring for why not bcrypt/argon2."""
    pepper = get_settings().api_key_pepper
    return hmac.new(pepper.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


async def verify_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_app_session),
) -> Merchant:
    """
    FastAPI dependency: resolves the caller's X-API-Key to a verified
    Merchant row, or raises 401.

    Use this — never a client-supplied merchant_id header/body field — as
    the sole source of truth for "which merchant is this request acting
    as" in every route that touches merchant-scoped data. FastAPI caches
    dependency results per-request, so depending on this from multiple
    places in one route's dependency chain (e.g. both the rate limiter and
    the route body) resolves it once, not once per use.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header is required.",
        )

    key_hash = hash_api_key(x_api_key)
    result = await session.execute(select(Merchant).where(Merchant.api_key_hash == key_hash))
    merchant = result.scalar_one_or_none()

    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    return merchant
