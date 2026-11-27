"""Auth endpoints. Routers stay thin: parse HTTP -> call service -> return schema.

Endpoints are plain `def` (not `async def`) because they do blocking work
(Argon2 hashing is intentionally CPU-heavy, DB driver is sync). FastAPI runs
`def` endpoints in a thread pool, so the event loop is never blocked.

Refresh token cookie
--------------------
* httpOnly    -> JavaScript (and therefore XSS) cannot read it
* SameSite=Strict -> the browser never sends it on cross-site requests (CSRF)
* Path=/api/v1/auth -> only sent to the auth endpoints, not with every API call
* Secure      -> HTTPS only (on by default in production)
/refresh and /logout additionally require an `X-Requested-With` header: a
cross-site HTML form cannot set custom headers (defence in depth vs. CSRF).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, DbSession
from app.core.exceptions import PermissionDeniedError
from app.core.rate_limit import limit_by_ip, limit_login_attempts
from app.schemas.auth import Token
from app.schemas.user import UserCreate, UserRead
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _cookie_path() -> str:
    return f"{get_settings().api_v1_prefix}/auth"


def _set_refresh_cookie(response: Response, raw: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.refresh_cookie_name, raw, max_age=s.refresh_token_expire_days * 24 * 3600,
        httponly=True, secure=s.refresh_cookie_secure_effective, samesite="strict", path=_cookie_path(),
    )


def _clear_refresh_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(
        s.refresh_cookie_name, httponly=True, secure=s.refresh_cookie_secure_effective,
        samesite="strict", path=_cookie_path(),
    )


def require_custom_header(x_requested_with: Annotated[str | None, Header()] = None) -> None:
    if not x_requested_with:
        raise PermissionDeniedError("Missing X-Requested-With header")


@router.post(
    "/register", response_model=UserRead, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_ip("register", "rate_limit_register_per_ip", 3600))],
)
def register(data: UserCreate, db: DbSession) -> UserRead:
    return AuthService(db).register(data)  # type: ignore[return-value]


@router.post("/login", response_model=Token)
def login(
    request: Request, response: Response, form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession
) -> Token:
    """OAuth2 password flow: form fields `username` (= email) and `password`.
    Rate limited per IP and per account to slow down password guessing.
    Also sets the refresh token cookie."""
    limit_login_attempts(request, form.username)  # counted BEFORE the (slow) password check
    token, refresh = AuthService(db).login(form.username, form.password)
    _set_refresh_cookie(response, refresh)
    return token


@router.post("/refresh", response_model=Token, dependencies=[Depends(require_custom_header)])
def refresh(request: Request, response: Response, db: DbSession) -> Token:
    """New access token from the refresh cookie (which is rotated).
    401 = log in again; 409 = another tab refreshed at the same moment, retry."""
    token, new_refresh = AuthService(db).refresh(request.cookies.get(get_settings().refresh_cookie_name))
    _set_refresh_cookie(response, new_refresh)
    return token


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_custom_header)])
def logout(request: Request, db: DbSession) -> Response:
    """Ends this login session (revokes its refresh tokens) and clears the cookie."""
    AuthService(db).logout(request.cookies.get(get_settings().refresh_cookie_name))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> UserRead:
    return user  # type: ignore[return-value]
