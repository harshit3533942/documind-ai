"""Authentication & authorisation tests: happy paths AND attack attempts."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.models import User, UserRole

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
ADMIN_USERS = "/api/v1/admin/users"

VALID = {"name": "Alice", "email": "alice@example.com", "password": "Sup3rSecret"}


# ------------------------------------------------------------------ hashing
def test_password_hash_is_argon2_and_salted() -> None:
    h1, h2 = hash_password("Sup3rSecret"), hash_password("Sup3rSecret")
    assert h1.startswith("$argon2id$")
    assert h1 != h2  # random salt -> same password, different hash
    assert verify_password("Sup3rSecret", h1)
    assert not verify_password("wrong", h1)
    assert not verify_password("Sup3rSecret", "not-a-hash")


# ------------------------------------------------------------------ register
def test_register_success(client: TestClient, db_session: Session) -> None:
    resp = client.post(REGISTER, json=VALID)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert body["role"] == "user"
    assert "password" not in body and "hashed_password" not in body

    stored = db_session.get(User, uuid.UUID(body["id"]))
    assert stored.hashed_password != VALID["password"]
    assert stored.hashed_password.startswith("$argon2id$")


def test_register_normalises_email(client: TestClient) -> None:
    resp = client.post(REGISTER, json={**VALID, "email": "  Alice@Example.COM "})
    assert resp.status_code == 201
    assert resp.json()["email"] == "alice@example.com"


def test_register_duplicate_email_case_insensitive(client: TestClient) -> None:
    assert client.post(REGISTER, json=VALID).status_code == 201
    resp = client.post(REGISTER, json={**VALID, "email": "ALICE@example.com"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_register_race_condition_returns_409(client: TestClient, monkeypatch) -> None:
    """Simulate two simultaneous sign-ups: the pre-check misses, the UNIQUE index catches it."""
    from app.repositories.user_repository import UserRepository

    assert client.post(REGISTER, json=VALID).status_code == 201
    monkeypatch.setattr(UserRepository, "get_by_email", lambda self, email: None)
    resp = client.post(REGISTER, json=VALID)
    assert resp.status_code == 409


def test_register_cannot_self_assign_admin_role(client: TestClient) -> None:
    resp = client.post(REGISTER, json={**VALID, "role": "admin"})
    assert resp.status_code == 201
    assert resp.json()["role"] == "user"


def test_register_validation_errors(client: TestClient) -> None:
    cases = [
        {**VALID, "email": "not-an-email"},
        {**VALID, "password": "short1"},           # too short
        {**VALID, "password": "onlyletters"},      # no digit
        {**VALID, "password": "a1" * 100},         # too long (DoS guard)
        {**VALID, "name": "   "},
        {"email": "x@example.com"},                # missing fields
    ]
    for payload in cases:
        assert client.post(REGISTER, json=payload).status_code == 422, payload


# ------------------------------------------------------------------ login
def test_login_success_returns_valid_jwt(client: TestClient) -> None:
    user_id = client.post(REGISTER, json=VALID).json()["id"]
    resp = client.post(LOGIN, data={"username": VALID["email"], "password": VALID["password"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_expire_minutes * 60
    payload = decode_access_token(body["access_token"])
    assert payload["sub"] == user_id
    assert payload["role"] == "user"


def test_login_is_case_insensitive_for_email(client: TestClient) -> None:
    client.post(REGISTER, json=VALID)
    resp = client.post(LOGIN, data={"username": "ALICE@EXAMPLE.COM", "password": VALID["password"]})
    assert resp.status_code == 200


def test_login_wrong_password_and_unknown_email_look_identical(client: TestClient) -> None:
    """Same status + message -> attacker cannot tell which emails are registered."""
    client.post(REGISTER, json=VALID)
    wrong_pw = client.post(LOGIN, data={"username": VALID["email"], "password": "Wr0ngPassword"})
    no_user = client.post(LOGIN, data={"username": "nobody@example.com", "password": "Wr0ngPassword"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()
    assert wrong_pw.headers["WWW-Authenticate"] == "Bearer"


def test_login_inactive_user_rejected(client: TestClient, create_user: Callable[..., User]) -> None:
    create_user("disabled@example.com", is_active=False)
    resp = client.post(LOGIN, data={"username": "disabled@example.com", "password": "Str0ngPassw0rd"})
    assert resp.status_code == 401


# ------------------------------------------------------------------ protected endpoint
def test_me_requires_token(client: TestClient) -> None:
    resp = client.get(ME)
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Not authenticated"


def test_me_with_valid_token(client: TestClient, create_user: Callable[..., User], auth_headers) -> None:
    create_user("bob@example.com")
    resp = client.get(ME, headers=auth_headers("bob@example.com"))
    assert resp.status_code == 200
    assert resp.json()["email"] == "bob@example.com"


def test_me_rejects_garbage_token(client: TestClient) -> None:
    resp = client.get(ME, headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_me_rejects_expired_token(client: TestClient, create_user: Callable[..., User]) -> None:
    user = create_user()
    settings = get_settings()
    past = datetime.now(UTC) - timedelta(hours=2)
    token = jwt.encode(
        {"sub": str(user.id), "role": "user", "type": "access", "iss": settings.jwt_issuer,
         "iat": past, "exp": past + timedelta(minutes=5)},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    assert client.get(ME, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_me_rejects_token_signed_with_other_secret(client: TestClient, create_user: Callable[..., User]) -> None:
    """A forged token (attacker does not know our secret) must fail signature check."""
    user = create_user()
    now = datetime.now(UTC)
    forged = jwt.encode(
        {"sub": str(user.id), "role": "admin", "type": "access", "iss": get_settings().jwt_issuer,
         "iat": now, "exp": now + timedelta(minutes=5)},
        "attacker-secret-attacker-secret-attacker-secret",
        algorithm="HS256",
    )
    assert client.get(ME, headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_me_rejects_alg_none_token(client: TestClient, create_user: Callable[..., User]) -> None:
    user = create_user()
    now = datetime.now(UTC)
    unsigned = jwt.encode(
        {"sub": str(user.id), "role": "admin", "type": "access", "iss": get_settings().jwt_issuer,
         "iat": now, "exp": now + timedelta(minutes=5)},
        key=None,
        algorithm="none",
    )
    assert client.get(ME, headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_token_for_deleted_user_rejected(client: TestClient, create_user: Callable[..., User], db_session: Session) -> None:
    user = create_user()
    token = create_access_token(user.id, user.role.value)
    db_session.delete(user)
    db_session.flush()
    assert client.get(ME, headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ------------------------------------------------------------------ RBAC
def test_admin_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get(ADMIN_USERS).status_code == 401


def test_admin_endpoint_forbidden_for_normal_user(client: TestClient, create_user, auth_headers) -> None:
    create_user("user@example.com")
    resp = client.get(ADMIN_USERS, headers=auth_headers("user@example.com"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "permission_denied"


def test_role_claim_in_token_is_not_trusted(client: TestClient, create_user: Callable[..., User]) -> None:
    """Token says admin, DB says user -> must be denied (role is re-read from DB)."""
    user = create_user("sneaky@example.com", role=UserRole.USER)
    token = create_access_token(user.id, role="admin")
    assert client.get(ADMIN_USERS, headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_admin_can_list_users_without_password_hashes(client: TestClient, create_user, auth_headers) -> None:
    create_user("admin@example.com", role=UserRole.ADMIN)
    create_user("someone@example.com")
    resp = client.get(ADMIN_USERS, headers=auth_headers("admin@example.com"), params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {u["email"] for u in body["items"]} == {"admin@example.com", "someone@example.com"}
    assert all("hashed_password" not in u for u in body["items"])


def test_admin_pagination_limits_validated(client: TestClient, create_user, auth_headers) -> None:
    create_user("admin@example.com", role=UserRole.ADMIN)
    headers = auth_headers("admin@example.com")
    assert client.get(ADMIN_USERS, headers=headers, params={"limit": 1000}).status_code == 422
    assert client.get(ADMIN_USERS, headers=headers, params={"offset": -1}).status_code == 422
