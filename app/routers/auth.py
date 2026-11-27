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

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, DbSession
from app.core.exceptions import PermissionDeniedError
from app.core.rate_limit import limit_by_email, limit_by_ip, limit_by_user, limit_login_attempts
from app.schemas.auth import EmailTokenRequest, MessageResponse, PasswordResetConfirm, PasswordResetRequest, Token
from app.schemas.user import UserCreate, UserRead
from app.services import email_service
from app.services.auth_service import AuthService

RESET_REQUESTED = "If an account exists for this email, we have sent a link to reset the password."

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
def register(data: UserCreate, db: DbSession, background: BackgroundTasks) -> UserRead:
    """Creates the account and emails a confirmation link (sent after the response)."""
    service = AuthService(db)
    user = service.register(data)
    token = service.start_email_verification(user)
    background.add_task(email_service.send_verification_email, user.email, user.name, token)
    return user  # type: ignore[return-value]


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


@router.post("/verify-email", response_model=UserRead)
def verify_email(body: EmailTokenRequest, db: DbSession) -> UserRead:
    """Confirm the email address with the token from the link. Works without
    being logged in (the link may be opened on another device). 422 = invalid,
    used or expired link."""
    return AuthService(db).verify_email(body.token)  # type: ignore[return-value]


@router.post(
    "/verify-email/resend", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_user("verify-resend", "rate_limit_verification_email_per_user", 3600))],
)
def resend_verification_email(user: CurrentUser, db: DbSession, background: BackgroundTasks) -> MessageResponse:
    """Send a new confirmation link (older links stop working). 409 if already confirmed."""
    token = AuthService(db).start_email_verification(user)
    background.add_task(email_service.send_verification_email, user.email, user.name, token)
    return MessageResponse(message="We have sent you a new confirmation link.")


@router.post(
    "/password-reset/request", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_ip("reset-ip", "rate_limit_password_reset_per_ip", 3600))],
)
def request_password_reset(body: PasswordResetRequest, db: DbSession, background: BackgroundTasks) -> MessageResponse:
    """Email a reset link. ALWAYS the same 202 answer - whether the account
    exists or not - so this endpoint cannot be used to find registered emails.
    The email itself is sent after the response (same response time too)."""
    s = get_settings()
    limit_by_email("reset-email", body.email, limit=s.rate_limit_password_reset_per_email, window_seconds=3600,
                   message="Too many reset requests for this email. Please try again later.")
    found = AuthService(db).start_password_reset(body.email)
    if found is not None:
        user, token = found
        background.add_task(email_service.send_password_reset_email, user.email, user.name, token)
    return MessageResponse(message=RESET_REQUESTED)


@router.post(
    "/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_ip("reset-confirm-ip", "rate_limit_password_reset_per_ip", 3600))],
)
def confirm_password_reset(body: PasswordResetConfirm, db: DbSession) -> Response:
    """Set a new password with the token from the email. Every existing
    session of the account is logged out. 422 = invalid, used or expired link."""
    AuthService(db).reset_password(body.token, body.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> UserRead:
    return user  # type: ignore[return-value]
