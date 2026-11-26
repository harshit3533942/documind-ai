"""Redis fixed-window rate limiting, used as FastAPI dependencies.

Algorithm: key = "rl:<scope>:<identity>:<window number>"; INCR it and set the
TTL on first use (EXPIRE ... NX), both in one MULTI/EXEC transaction, so
concurrent requests on several API servers are counted correctly. Over the
limit -> HTTP 429 with a Retry-After header.

Trade-off: a fixed window allows a burst of up to 2x the limit around a window
boundary (end of one minute + start of the next). A sliding-window log (Redis
sorted set) is exact but costs more memory per user - fine-grained precision is
not needed for abuse protection.

Fail-open: if Redis is unavailable the request is allowed (and logged). For
abuse protection, availability of the product matters more than a few
unlimited requests during an outage.
"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger
from app.database import redis_client

logger = get_logger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    count: int
    limit: int
    retry_after: int  # seconds until the window resets


def hit(scope: str, identity: str, *, limit: int, window_seconds: int) -> RateLimitResult:
    now = int(time.time())
    window = now // window_seconds
    key = f"rl:{scope}:{identity}:{window}"
    retry_after = window_seconds - (now % window_seconds)
    try:
        pipe = redis_client.get_redis().pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count = int(pipe.execute()[0])
    except RedisError:
        logger.warning("Rate limiter unavailable (Redis error); allowing request", exc_info=True)
        return RateLimitResult(True, 0, limit, 0)
    allowed = count <= limit
    if not allowed:
        _count_rejection(scope)
    return RateLimitResult(allowed, count, limit, retry_after)


def _count_rejection(scope: str) -> None:
    try:
        redis_client.get_redis().hincrby("metrics:rate_limited", scope, 1)
    except RedisError:
        pass


def client_ip(request: Request) -> str:
    """Client address as seen by uvicorn. Behind a reverse proxy, uvicorn must
    run with --proxy-headers --forwarded-allow-ips=<proxy ip> so this is the
    real client and not the proxy (and clients cannot spoof X-Forwarded-For)."""
    return request.client.host if request.client else "unknown"


def _hash(value: str) -> str:
    """Identities such as emails are hashed: no personal data in Redis keys."""
    return hashlib.sha256(value.lower().encode()).hexdigest()[:24]


def enforce(scope: str, identity: str, *, limit: int, window_seconds: int, message: str) -> None:
    if not get_settings().rate_limit_enabled:
        return
    result = hit(scope, identity, limit=limit, window_seconds=window_seconds)
    if not result.allowed:
        logger.info("Rate limit exceeded scope=%s", scope)  # identity not logged
        raise RateLimitedError(message, headers={"Retry-After": str(result.retry_after)})


# ------------------------------------------------------------------ dependencies
def limit_by_ip(scope: str, limit_setting: str, window_seconds: int) -> Callable[[Request], None]:
    """Limit read from settings at request time (so it is configurable/testable)."""

    def dependency(request: Request) -> None:
        enforce(
            scope, _hash(client_ip(request)),
            limit=getattr(get_settings(), limit_setting), window_seconds=window_seconds,
            message="Too many requests. Please wait and try again.",
        )

    return dependency


def limit_by_user(scope: str, limit_setting: str, window_seconds: int) -> Callable[..., None]:
    from app.core.dependencies import CurrentUser  # avoid import cycle

    def dependency(user: CurrentUser) -> None:
        enforce(
            scope, str(user.id),
            limit=getattr(get_settings(), limit_setting), window_seconds=window_seconds,
            message="You are sending requests too quickly. Please wait and try again.",
        )

    return dependency


def limit_login_attempts(request: Request, email: str) -> None:
    """Two limits on login: per IP (one attacker, many accounts) and per email
    (many IPs guessing ONE account's password). Called from the login route
    because the email arrives in the form body."""
    s = get_settings()
    enforce("login-ip", _hash(client_ip(request)), limit=s.rate_limit_login_per_ip, window_seconds=60,
            message="Too many login attempts. Please wait a minute and try again.")
    enforce("login-email", _hash(email.strip()), limit=s.rate_limit_login_per_email, window_seconds=15 * 60,
            message="Too many login attempts for this account. Please try again later.")
