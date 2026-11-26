"""Shared Redis client (connection pool). Used for health checks now and for
caching / rate limiting in Phase 10."""

from functools import lru_cache

import redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
        decode_responses=True,
    )


def check_redis_connection() -> dict[str, object]:
    try:
        get_redis().ping()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        return {"status": "error", "detail": type(exc).__name__}
