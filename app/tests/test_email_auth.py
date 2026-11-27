"""Email verification + password reset."""

import re
import smtplib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import RefreshToken, TokenPurpose, User, UserToken
from app.routers.auth import RESET_REQUESTED
from app.services import email_service
from app.services.auth_service import purge_expired_user_tokens
from app.tests.conftest import TEST_PASSWORD

AUTH = "/api/v1/auth"
NEW_PASSWORD = "BrandNewPassw0rd"


@pytest.fixture
def outbox(monkeypatch) -> list[dict]:
    """Captures emails instead of sending them (BackgroundTasks run inside the TestClient)."""
    sent: list[dict] = []
    monkeypatch.setattr(email_service, "send_email",
                        lambda to, subject, text, html=None: sent.append({"to": to, "subject": subject, "text": text}) or True)
    return sent


def token_from(mail: dict) -> str:
    match = re.search(r"\?token=([A-Za-z0-9_-]+)", mail["text"])
    assert match, mail["text"]
    return match.group(1)


def register(client: TestClient, email: str = "new@example.com"):
    return client.post(f"{AUTH}/register", json={"name": "New User", "email": email, "password": TEST_PASSWORD})


def login(client: TestClient, email: str, password: str = TEST_PASSWORD):
    return client.post(f"{AUTH}/login", data={"username": email, "password": password})


# ------------------------------------------------------------------ verification
def test_register_sends_verification_link_and_link_verifies(client, outbox, db_session: Session) -> None:
    resp = register(client)
    assert resp.status_code == 201 and resp.json()["email_verified_at"] is None
    assert len(outbox) == 1 and outbox[0]["to"] == "new@example.com"
    assert "http://localhost:5173/verify-email?token=" in outbox[0]["text"]
    token = token_from(outbox[0])

    # Only the hash is stored.
    stored = db_session.scalar(select(UserToken))
    assert stored.purpose == TokenPurpose.VERIFY_EMAIL and token not in stored.token_hash

    verified = client.post(f"{AUTH}/verify-email", json={"token": token})
    assert verified.status_code == 200 and verified.json()["email_verified_at"]
    again = client.post(f"{AUTH}/verify-email", json={"token": token})  # single use
    assert again.status_code == 422 and "invalid or has expired" in again.json()["error"]["message"]


def test_verification_link_errors(client, outbox, db_session) -> None:
    register(client)
    token = token_from(outbox[0])
    assert client.post(f"{AUTH}/verify-email", json={"token": "x" * 43}).status_code == 422
    assert client.post(f"{AUTH}/verify-email", json={"token": "short"}).status_code == 422  # schema validation
    db_session.execute(update(UserToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert client.post(f"{AUTH}/verify-email", json={"token": token}).status_code == 422  # expired


def test_reset_token_cannot_verify_email(client, outbox, create_user) -> None:
    create_user("alice@example.com")
    client.post(f"{AUTH}/password-reset/request", json={"email": "alice@example.com"})
    assert client.post(f"{AUTH}/verify-email", json={"token": token_from(outbox[0])}).status_code == 422


def test_resend_invalidates_the_old_link(client, outbox, auth_headers) -> None:
    register(client)
    first = token_from(outbox[0])
    headers = auth_headers("new@example.com")
    resp = client.post(f"{AUTH}/verify-email/resend", headers=headers)
    assert resp.status_code == 202 and len(outbox) == 2
    assert client.post(f"{AUTH}/verify-email", json={"token": first}).status_code == 422
    assert client.post(f"{AUTH}/verify-email", json={"token": token_from(outbox[1])}).status_code == 200
    already = client.post(f"{AUTH}/verify-email/resend", headers=headers)
    assert already.status_code == 409
    assert client.post(f"{AUTH}/verify-email/resend").status_code == 401


def test_resend_is_rate_limited(client, outbox, auth_headers, monkeypatch) -> None:
    register(client)
    headers = auth_headers("new@example.com")
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limit_verification_email_per_user", 2)
    codes = [client.post(f"{AUTH}/verify-email/resend", headers=headers).status_code for _ in range(3)]
    assert codes == [202, 202, 429]


def test_login_can_require_a_verified_email(client, outbox, monkeypatch) -> None:
    register(client)
    monkeypatch.setattr(get_settings(), "require_email_verification", True)
    blocked = login(client, "new@example.com")
    assert blocked.status_code == 403 and "confirm your email" in blocked.json()["error"]["message"]
    wrong_password = login(client, "new@example.com", "WrongPassw0rd")
    assert wrong_password.status_code == 401  # nothing revealed without the right password
    client.post(f"{AUTH}/verify-email", json={"token": token_from(outbox[0])})
    assert login(client, "new@example.com").status_code == 200


# ------------------------------------------------------------------ password reset
def test_password_reset_flow_logs_out_every_session(client, outbox, create_user, db_session) -> None:
    user = create_user("alice@example.com")
    assert login(client, "alice@example.com").status_code == 200  # an existing session (refresh cookie)

    resp = client.post(f"{AUTH}/password-reset/request", json={"email": "  Alice@Example.com "})
    assert resp.status_code == 202 and resp.json()["message"] == RESET_REQUESTED
    assert len(outbox) == 1 and "/reset-password?token=" in outbox[0]["text"]
    token = token_from(outbox[0])

    done = client.post(f"{AUTH}/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD})
    assert done.status_code == 204
    assert login(client, "alice@example.com").status_code == 401           # old password gone
    assert db_session.scalar(select(func.count()).select_from(RefreshToken).where(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))) == 0   # all sessions revoked
    assert client.post(f"{AUTH}/refresh", headers={"X-Requested-With": "t"}).status_code == 401
    assert login(client, "alice@example.com", NEW_PASSWORD).status_code == 200
    db_session.refresh(user)
    assert user.email_verified_at is not None  # the link proved inbox ownership

    reuse = client.post(f"{AUTH}/password-reset/confirm", json={"token": token, "new_password": "Another1Passw0rd"})
    assert reuse.status_code == 422


def test_reset_request_does_not_reveal_accounts(client, outbox, create_user) -> None:
    create_user("disabled@example.com", is_active=False)
    unknown = client.post(f"{AUTH}/password-reset/request", json={"email": "nobody@example.com"})
    disabled = client.post(f"{AUTH}/password-reset/request", json={"email": "disabled@example.com"})
    create_user("real@example.com")
    real = client.post(f"{AUTH}/password-reset/request", json={"email": "real@example.com"})
    assert unknown.status_code == disabled.status_code == real.status_code == 202
    assert unknown.json() == disabled.json() == real.json()
    assert [m["to"] for m in outbox] == ["real@example.com"]


def test_new_reset_request_invalidates_older_links(client, outbox, create_user) -> None:
    create_user("alice@example.com")
    for _ in range(2):
        client.post(f"{AUTH}/password-reset/request", json={"email": "alice@example.com"})
    old, new = token_from(outbox[0]), token_from(outbox[1])
    assert client.post(f"{AUTH}/password-reset/confirm", json={"token": old, "new_password": NEW_PASSWORD}).status_code == 422
    assert client.post(f"{AUTH}/password-reset/confirm", json={"token": new, "new_password": NEW_PASSWORD}).status_code == 204


@pytest.mark.parametrize("password", ["short1", "onlyletters", "12345678901"])
def test_reset_enforces_password_rules(client, outbox, create_user, password) -> None:
    create_user("alice@example.com")
    client.post(f"{AUTH}/password-reset/request", json={"email": "alice@example.com"})
    resp = client.post(f"{AUTH}/password-reset/confirm", json={"token": token_from(outbox[0]), "new_password": password})
    assert resp.status_code == 422


def test_reset_requests_are_rate_limited_per_email(client, outbox, create_user, monkeypatch) -> None:
    create_user("alice@example.com")
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    codes = [client.post(f"{AUTH}/password-reset/request", json={"email": "alice@example.com"}).status_code
             for _ in range(4)]
    assert codes == [202, 202, 202, 429]
    assert len(outbox) == 3


def test_purge_expired_user_tokens(db_session, create_user) -> None:
    user = create_user("alice@example.com")
    now = datetime.now(UTC)
    for i, age in enumerate((timedelta(days=3), timedelta(hours=1))):
        db_session.add(UserToken(user_id=user.id, purpose=TokenPurpose.RESET_PASSWORD, token_hash=f"{i}" * 64,
                                 expires_at=now - age))
    db_session.flush()
    assert purge_expired_user_tokens(db_session) == 1  # only the one expired > 1 day ago
    assert db_session.scalar(select(func.count()).select_from(UserToken)) == 1


# ------------------------------------------------------------------ email backends
class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail = False

    def __init__(self, host, port, timeout) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user, password) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message) -> None:
        if FakeSMTP.fail:
            raise smtplib.SMTPServerDisconnected("gone")
        self.sent.append(message)


def test_smtp_backend(monkeypatch) -> None:
    s = get_settings()
    for name, value in {"email_backend": "smtp", "smtp_host": "mail.test", "smtp_port": 2525, "smtp_starttls": True,
                        "smtp_username": "bot", "smtp_password": "pw"}.items():
        monkeypatch.setattr(s, name, value)
    from pydantic import SecretStr
    monkeypatch.setattr(s, "smtp_password", SecretStr("pw"))
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances.clear()

    assert email_service.send_password_reset_email("alice@example.com", "Alice <b>", "tok-123") is True
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("mail.test", 2525)
    assert smtp.calls == ["starttls", "login:bot:pw"]
    message = smtp.sent[0]
    assert message["To"] == "alice@example.com" and message["Subject"] == "Reset your password"
    html = message.get_body(("html",)).get_content()
    assert "Alice &lt;b&gt;" in html                                # the name is escaped in HTML
    assert "/reset-password?token=tok-123" in message.get_body(("plain",)).get_content()

    FakeSMTP.fail = True
    try:
        assert email_service.send_verification_email("alice@example.com", "Alice", "tok") is False  # logged, not raised
    finally:
        FakeSMTP.fail = False


def test_console_backend_logs_the_email(caplog) -> None:
    with caplog.at_level("INFO"):
        assert email_service.send_verification_email("bob@example.com", "Bob", "tok-abc") is True
    assert "verify-email?token=tok-abc" in caplog.text
