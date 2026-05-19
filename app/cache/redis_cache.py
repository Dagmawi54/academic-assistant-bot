"""Redis cache layer for hot-path data."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.config import settings
from app.logging import get_logger

logger = get_logger("cache")

_redis: aioredis.Redis | None = None

# Default TTL: 5 minutes
DEFAULT_TTL = 300


async def init_cache() -> None:
    """Initialize the Redis connection."""
    global _redis
    try:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis.ping()
        logger.info("redis_connected", url=settings.redis_url)
    except Exception:
        logger.warning("redis_unavailable", msg="Cache disabled — running without Redis")
        _redis = None


async def close_cache() -> None:
    """Close the Redis connection."""
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None


def _available() -> bool:
    return _redis is not None


# ---------------------------------------------------------------------------
# Generic cache operations
# ---------------------------------------------------------------------------


async def get(key: str) -> Any | None:
    """Get a value from cache. Returns None if miss or Redis unavailable."""
    if not _available():
        return None
    try:
        raw = await _redis.get(key)  # type: ignore[union-attr]
        return json.loads(raw) if raw else None
    except Exception:
        logger.warning("cache_get_failed", key=key)
        return None


async def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """Set a value in cache with TTL."""
    if not _available():
        return
    try:
        await _redis.set(key, json.dumps(value), ex=ttl)  # type: ignore[union-attr]
    except Exception:
        logger.warning("cache_set_failed", key=key)


async def delete(key: str) -> None:
    """Delete a key from cache."""
    if not _available():
        return
    try:
        await _redis.delete(key)  # type: ignore[union-attr]
    except Exception:
        logger.warning("cache_delete_failed", key=key)


async def invalidate_pattern(pattern: str) -> None:
    """Delete all keys matching a pattern (e.g. 'group:123:*')."""
    if not _available():
        return
    try:
        keys = []
        async for key in _redis.scan_iter(match=pattern):  # type: ignore[union-attr]
            keys.append(key)
        if keys:
            await _redis.delete(*keys)  # type: ignore[union-attr]
    except Exception:
        logger.warning("cache_invalidate_failed", pattern=pattern)


# ---------------------------------------------------------------------------
# Domain-specific cache keys
# ---------------------------------------------------------------------------


def topic_key(chat_id: int) -> str:
    return f"topics:{chat_id}"


def course_key(group_id: int) -> str:
    return f"courses:{group_id}"


def routing_key(chat_id: int, thread_id: int) -> str:
    return f"route:{chat_id}:{thread_id}"
