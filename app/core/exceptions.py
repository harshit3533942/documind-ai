"""Centralised exception handling.

Services raise `AppError` subclasses; one handler converts them into a
consistent JSON shape. Unexpected errors are logged with a traceback but the
client only sees a generic message (no internal details leak).
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"

    def __init__(self, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers = headers


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"


class UnauthorizedError(AppError):
    """401 = 'who are you?' (missing/invalid credentials). 403 = 'I know you, but no.'"""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class PayloadTooLargeError(AppError):
    status_code = 413  # Content Too Large
    code = "payload_too_large"


class UnsupportedMediaTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"


class UnprocessableError(AppError):
    """Request is well-formed but the content cannot be processed (e.g. corrupt PDF)."""

    status_code = 422  # Unprocessable Content
    code = "unprocessable"


class ServiceUnavailableError(AppError):
    """A dependency (broker, mail server) is down; retrying later can succeed."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def _safe_validation_errors(exc: RequestValidationError) -> list[dict]:
    """FastAPI's default 422 body echoes the submitted value (`input`) - e.g. the
    password from a failed registration - into responses that browsers, proxies
    and error trackers may store. Return only where and why validation failed."""
    return [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _safe_validation_errors(exc)})

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = dict(exc.headers or {})
        if isinstance(exc, UnauthorizedError):
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "internal_error", "message": "Internal server error"}},
        )
