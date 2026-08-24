"""
RecoveryOS — Redis Connection Management
=======================================
Provides async Redis connection pool.
"""

from __future__ import annotations

import redis.asyncio as redis
from recoveryos.config import get_settings

_redis_pool: redis.Redis | None = None


def get_redis_pool() -> redis.Redis:
    global _redis_pool
    if _redis_pool is None:
        settings = get_settings()
        _redis_pool = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_pool


async def close_redis_pool():
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


async def get_redis():
    """FastAPI dependency for Redis."""
    return get_redis_pool()
