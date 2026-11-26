"""Auth endpoints. Routers stay thin: parse HTTP -> call service -> return schema.

Endpoints are plain `def` (not `async def`) because they do blocking work
(Argon2 hashing is intentionally CPU-heavy, DB driver is sync). FastAPI runs
`def` endpoints in a thread pool, so the event loop is never blocked.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.dependencies import CurrentUser, DbSession
from app.core.rate_limit import limit_by_ip, limit_login_attempts
from app.schemas.auth import Token
from app.schemas.user import UserCreate, UserRead
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=UserRead, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_ip("register", "rate_limit_register_per_ip", 3600))],
)
def register(data: UserCreate, db: DbSession) -> UserRead:
    return AuthService(db).register(data)  # type: ignore[return-value]


@router.post("/login", response_model=Token)
def login(request: Request, form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession) -> Token:
    """OAuth2 password flow: form fields `username` (= email) and `password`.
    Rate limited per IP and per account to slow down password guessing."""
    limit_login_attempts(request, form.username)  # counted BEFORE the (slow) password check
    return AuthService(db).login(form.username, form.password)


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> UserRead:
    return user  # type: ignore[return-value]
