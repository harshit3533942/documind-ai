"""Password hashing (Argon2) and JWT access tokens.

Argon2id is the winner of the Password Hashing Competition. It is deliberately
slow AND memory-hard, so attackers cannot cheaply brute-force leaked hashes on
GPUs. Each hash contains its own random salt + parameters, e.g.
    $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher()  # library defaults follow OWASP recommendations

# Used when a login email does not exist: we still run one verification so the
# response time is the same as for a wrong password. Otherwise an attacker
# could discover which emails are registered by measuring response time.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")


class TokenError(Exception):
    """Raised for any invalid, expired or malformed token."""


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    """True if the hash was made with weaker, older parameters -> upgrade on login."""
    return _hasher.check_needs_rehash(hashed)


def burn_verification_time() -> None:
    verify_password("not-the-password", _DUMMY_HASH)


# ---------------------------------------------------------------- JWT
def create_access_token(subject: uuid.UUID, role: str, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expires_minutes or settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(subject),  # who the token belongs to
        "role": role,         # convenience only - authorisation re-reads role from DB
        "type": "access",
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": expire,
        "jti": uuid.uuid4().hex,  # unique token id (enables a revocation list later)
    }
    return jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            # Pin the algorithm list: never trust the token's own "alg" header
            # (prevents the classic "alg: none" attack).
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "iss", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("type") != "access":
        raise TokenError("Wrong token type")
    return payload
