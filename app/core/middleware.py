"""HTTP middleware: request id + response timing.

Every response gets:
  X-Request-ID       - echoed from the client (if safe) or generated; also in
                       logs, so one request can be traced across API logs
  X-Response-Time-ms - server-side processing time
Each request is logged as one line (method, path, status, duration) - never
the query string, headers or body (they can contain tokens or personal data).
Requests slower than SLOW_REQUEST_MS are logged as warnings.

Written as a pure ASGI middleware (not BaseHTTPMiddleware) - it adds no
overhead to streaming and does not interfere with background tasks.
"""

import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.access")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
_BASE_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),          # no MIME sniffing of responses
    (b"x-frame-options", b"DENY"),                    # no clickjacking via <iframe>
    (b"referrer-policy", b"no-referrer"),             # URLs (ids) never leak to other sites
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
# The API only returns JSON: it never needs to load scripts, styles or frames.
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")


class SecurityHeadersMiddleware:
    """Defence-in-depth HTTP headers on every API response.

    * API paths: strict CSP and `Cache-Control: no-store` (responses contain
      private document text - browsers/proxies must not cache them).
    * Swagger UI (/docs) is excluded from the strict CSP because it loads its
      own scripts; it is disabled in production anyway.
    * HSTS only when the deployment is HTTPS (HSTS_ENABLED) - on plain HTTP it
      would be ignored at best and lock users out at worst.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_BASE_SECURITY_HEADERS)
                if not path.startswith(_DOCS_PATHS):
                    headers.append(_API_CSP)
                if path.startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                if get_settings().hsts_enabled:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RequestTimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _SAFE_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                elapsed_ms = (time.perf_counter() - started) * 1000
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                headers.append((b"x-response-time-ms", f"{elapsed_ms:.1f}".encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            log = logger.warning if elapsed_ms > get_settings().slow_request_ms else logger.info
            log(
                "%s %s -> %d in %.1fms [%s]%s",
                scope["method"], scope["path"], status_holder["status"], elapsed_ms, request_id,
                "  SLOW" if elapsed_ms > get_settings().slow_request_ms else "",
            )
