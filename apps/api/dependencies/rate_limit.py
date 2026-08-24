"""
Rate limiter dependency for RecoveryOS API.

Implements a Redis-backed token bucket per merchant_id.

SECURITY NOTE (Task 4): the merchant_id used for rate-limiting comes from
`verify_api_key` — a real, hashed-lookup-verified identity — never from a
client-supplied header or body field. Before Task 4 this depended on
`X-Merchant-ID`, an entirely unverified, self-reported header: any caller
could exhaust a bucket keyed to a merchant_id that wasn't theirs, or dodge
rate limiting on their own traffic by rotating the header value per
request. Buckets are still keyed per-merchant in Redis (Merchant A cannot
exhaust Merchant B's bucket), but "per-merchant" now means something real.

Architecture: Token Bucket via Redis Lua script.
  - Lua script runs atomically in Redis (no race conditions).
  - Capacity: max burst allowed.
  - Refill rate: tokens added per second.
  - Returns: (allowed: bool, remaining: int, retry_after_ms: int)
"""

import time

# NOTE: deliberately NOT using `from __future__ import annotations` here.
# RateLimiter is used as a callable-CLASS-INSTANCE dependency (Depends(_rate_limiter)
# in events.py, calling __call__). FastAPI resolves Annotated[...] metadata (Header())
# via typing.get_type_hints(call), and for a bound method of an *instance* that
# resolution needs call.__globals__ — which only exists on functions, not on
# instances. With postponed evaluation on, every annotation here is a bare string
# with nothing to resolve it against, so FastAPI silently fails to see a Header()
# param (dependant.header_params ends up empty) and it always resolves to None.
# Keeping annotations un-postponed means they're real Annotated[...] objects
# already, so no resolution is needed. (Moot for the Merchant param below since
# it's a plain Depends(), not a Header() — but kept off for consistency with the
# rest of this file and to not re-trip this exact footgun on a future edit.)
import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status

from apps.api.dependencies.auth import verify_api_key
from recoveryos.models import Merchant
from recoveryos.redis import get_redis

# ─── Token bucket constants ────────────────────────────────────────────────────
# These are the per-merchant defaults. In a multi-tenant setup, they'd come from
# the merchant's policy_config. For Phase 3, they're global defaults.
BUCKET_CAPACITY: int = 1000  # max burst tokens (generous for demo)
REFILL_RATE: int = 500  # tokens added per second (TRD §8 target)
BUCKET_TTL_SECONDS: int = 60  # Redis key TTL to GC inactive merchants

# Lua script: atomically checks and consumes one token from a merchant's bucket.
# Algorithm:
#   1. Get (tokens, last_refill_ts) from Redis hash.
#   2. Calculate elapsed time since last refill.
#   3. Add (elapsed * refill_rate) tokens, capped at capacity.
#   4. If tokens >= 1: decrement and allow; else deny.
# Returns: [allowed (0|1), remaining tokens, retry_after_ms]
_RATE_LIMIT_LUA = """
local key        = KEYS[1]
local capacity   = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms     = tonumber(ARGV[3])
local ttl        = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_ms')
local tokens  = tonumber(data[1]) or capacity
local last_ms = tonumber(data[2]) or now_ms

-- Refill proportional to elapsed time
local elapsed_sec = math.max(0, (now_ms - last_ms) / 1000.0)
tokens = math.min(capacity, tokens + elapsed_sec * refill_rate)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_ms', now_ms)
    redis.call('EXPIRE', key, ttl)
    return {1, math.floor(tokens), 0}
else
    -- retry_after: how long until one token is available
    local wait_ms = math.ceil((1 - tokens) / refill_rate * 1000)
    redis.call('HMSET', key, 'tokens', tokens, 'last_ms', now_ms)
    redis.call('EXPIRE', key, ttl)
    return {0, 0, wait_ms}
end
"""


class RateLimiter:
    """
    FastAPI dependency factory for merchant-scoped token bucket rate limiting.

    Usage:
        @router.post(...)
        async def endpoint(
            _: None = Depends(RateLimiter()),
        ): ...
    """

    def __init__(
        self,
        capacity: int = BUCKET_CAPACITY,
        refill_rate: int = REFILL_RATE,
    ):
        self.capacity = capacity
        self.refill_rate = refill_rate

    async def __call__(
        self,
        merchant: Merchant = Depends(verify_api_key),
        redis: aioredis.Redis = Depends(get_redis),
    ) -> None:
        """
        Check the token bucket for the caller's VERIFIED merchant identity.

        `merchant` comes from verify_api_key — invalid/missing API keys
        already 401 before this method's body ever runs, so there's no
        "anonymous bucket" case to handle here anymore.
        """
        merchant_id = merchant.merchant_id
        bucket_key = f"rate_limit:events:{merchant_id}"
        # time.time() (wall clock, UTC epoch), NOT time.monotonic() — the
        # bucket lives in Redis and is read/written by every worker process
        # sharing it. monotonic()'s zero point is arbitrary PER PROCESS (time
        # since that process started), so two workers' clocks are unrelated
        # to each other; only masked here by the API Dockerfile pinning
        # `--workers 1`. The instant a second worker/replica shares this
        # Redis instance, one process's "now" becomes a meaningless number to
        # the other, silently corrupting the elapsed-time refill math (either
        # over-refilling, so the limit never actually bites, or
        # under-refilling, so it wrongly denies real traffic) — and it would
        # do so silently, not with an error, making it a specifically nasty
        # one to diagnose after the fact. time.time() has the same epoch
        # (UTC) everywhere, so this holds regardless of process count.
        now_ms = int(time.time() * 1000)

        # Deliberately NOT using redis.register_script()/Script.__call__ here:
        # a Script object bound at registration time to whichever client
        # instance first called it, and RateLimiter itself is a per-process
        # singleton (events.py: `_rate_limiter = RateLimiter(...)` at module
        # scope) — if the Redis pool is ever rebuilt during the process's
        # life (pool reconnect, or per-test pool rotation in the test suite),
        # the cached Script keeps calling EVALSHA against the *old*, possibly
        # dead, client/event loop instead of the one just injected via
        # Depends(get_redis). Evaluating against `redis` (the current
        # request's client) directly avoids that class of bug entirely, at
        # the cost of Redis re-parsing ~20 lines of Lua per call — negligible
        # next to the network round-trip itself.
        result = await redis.eval(
            _RATE_LIMIT_LUA,
            1,
            bucket_key,
            self.capacity,
            self.refill_rate,
            now_ms,
            BUCKET_TTL_SECONDS,
        )

        allowed, remaining, retry_after_ms = int(result[0]), int(result[1]), int(result[2])

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "merchant_id": merchant_id,
                    "retry_after_ms": retry_after_ms,
                    "message": (
                        f"Rate limit exceeded for merchant {merchant_id}. "
                        f"Retry after {retry_after_ms}ms."
                    ),
                },
                headers={"Retry-After": str(retry_after_ms // 1000 + 1)},
            )
