"""Authentication business logic (no HTTP details here).

Sessions = short-lived JWT access token + rotating refresh token
-----------------------------------------------------------------
* login()   -> access token (15 min) + refresh token (14 days, new "family")
* refresh() -> the presented refresh token is revoked and replaced by a new
               one (rotation) + a fresh access token
* logout()  -> the whole family (= this login session) is revoked

Reuse detection: a refresh token can be used exactly once. If a revoked token
comes back, either two tabs raced (within a few seconds -> 409, the client
simply retries with the newer cookie) or the token was stolen and the thief
or the victim is using an old copy -> the entire family is revoked, so the
attacker's stolen session dies too.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import (
    burn_verification_time,
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.models import RefreshToken, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import Token
from app.schemas.user import UserCreate

logger = get_logger(__name__)

INVALID_CREDENTIALS = "Incorrect email or password"
INVALID_SESSION = "Session expired. Please log in again."


class AuthService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.users = UserRepository(db)

    def register(self, data: UserCreate) -> User:
        if self.users.get_by_email(data.email):
            raise ConflictError("An account with this email already exists")
        try:
            # Role is NEVER taken from the request: public sign-up always creates
            # a normal user. Admins are created with scripts/create_admin.py.
            user = self.users.create(
                name=data.name, email=data.email, hashed_password=hash_password(data.password)
            )
            self.db.commit()
        except IntegrityError:
            # Two simultaneous sign-ups with the same email: the pre-check above
            # passes for both, but the UNIQUE index rejects the second.
            self.db.rollback()
            raise ConflictError("An account with this email already exists") from None
        logger.info("User registered id=%s", user.id)  # log id, never email/password
        return user

    def authenticate(self, email: str, password: str) -> User:
        user = self.users.get_by_email(email.strip())
        if user is None:
            burn_verification_time()
            raise UnauthorizedError(INVALID_CREDENTIALS)
        if not verify_password(password, user.hashed_password):
            logger.info("Failed login for user id=%s", user.id)
            raise UnauthorizedError(INVALID_CREDENTIALS)
        if not user.is_active:
            raise UnauthorizedError("Account is disabled")

        if password_needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(password)
            self.db.commit()
        return user

    def login(self, email: str, password: str) -> tuple[Token, str]:
        """(access token, raw refresh token for the cookie)."""
        user = self.authenticate(email, password)
        raw, _ = self._issue_refresh_token(user.id, family_id=uuid.uuid4())
        self.db.commit()
        return self._access_token(user), raw

    # ------------------------------------------------------------ refresh tokens
    def refresh(self, raw: str | None) -> tuple[Token, str]:
        """Rotate: revoke the presented refresh token, issue a new one."""
        if not raw:
            raise UnauthorizedError(INVALID_SESSION)
        settings = get_settings()
        now = datetime.now(UTC)
        # FOR UPDATE: two concurrent refreshes with the same token are
        # serialised - the second one waits and then sees it already revoked.
        token = self.db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)).with_for_update()
        )
        if token is None:
            raise UnauthorizedError(INVALID_SESSION)

        if token.revoked_at is not None:
            just_rotated = token.replaced_by_id is not None and (
                now - token.revoked_at <= timedelta(seconds=settings.refresh_reuse_grace_seconds)
            )
            if just_rotated:
                raise ConflictError("Session was just refreshed by another tab; retry")
            self._revoke_family(token.family_id, now)
            self.db.commit()
            logger.warning("Refresh token reuse detected for user %s - session family revoked", token.user_id)
            raise UnauthorizedError(INVALID_SESSION)

        if token.expires_at <= now:
            raise UnauthorizedError(INVALID_SESSION)
        user = self.users.get_by_id(token.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError(INVALID_SESSION)

        raw_new, new_id = self._issue_refresh_token(user.id, family_id=token.family_id)
        token.revoked_at, token.replaced_by_id = now, new_id
        self.db.commit()
        return self._access_token(user), raw_new

    def logout(self, raw: str | None) -> None:
        """Revoke this login session. Unknown/expired tokens: nothing to do."""
        if not raw:
            return
        token = self.db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)))
        if token is not None:
            self._revoke_family(token.family_id, datetime.now(UTC))
            self.db.commit()

    def _issue_refresh_token(self, user_id: uuid.UUID, *, family_id: uuid.UUID) -> tuple[str, uuid.UUID]:
        raw, digest = new_refresh_token()
        days = get_settings().refresh_token_expire_days
        token = RefreshToken(
            user_id=user_id, token_hash=digest, family_id=family_id,
            expires_at=datetime.now(UTC) + timedelta(days=days),
        )
        self.db.add(token)
        self.db.flush()
        return raw, token.id

    def _revoke_family(self, family_id: uuid.UUID, when: datetime) -> None:
        self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=when)
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    def _access_token(user: User) -> Token:
        minutes = get_settings().access_token_expire_minutes
        return Token(access_token=create_access_token(user.id, user.role.value), expires_in=minutes * 60)


def purge_expired_refresh_tokens(db: Session, *, older_than_days: int = 1) -> int:
    """Delete tokens that EXPIRED more than `older_than_days` ago. Revoked but
    unexpired tokens are kept on purpose: if a stolen copy shows up later,
    reuse detection still recognises it and kills the session family."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = db.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff))
    db.commit()
    return result.rowcount or 0
