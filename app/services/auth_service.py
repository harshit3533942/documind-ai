"""Authentication business logic (no HTTP details here)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import (
    burn_verification_time,
    create_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.models import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import Token
from app.schemas.user import UserCreate

logger = get_logger(__name__)

INVALID_CREDENTIALS = "Incorrect email or password"


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

    def login(self, email: str, password: str) -> Token:
        user = self.authenticate(email, password)
        minutes = get_settings().access_token_expire_minutes
        return Token(access_token=create_access_token(user.id, user.role.value), expires_in=minutes * 60)
