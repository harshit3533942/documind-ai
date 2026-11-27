"""Refresh tokens: httpOnly cookie, rotation, reuse detection, logout, cleanup."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.security import hash_refresh_token
from app.models import RefreshToken
from app.services.auth_service import purge_expired_refresh_tokens
from app.tests.conftest import TEST_PASSWORD

LOGIN, REFRESH, LOGOUT, ME = "/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout", "/api/v1/auth/me"
XRW = {"X-Requested-With": "documind"}
COOKIE = get_settings().refresh_cookie_name


def login(client, email="user@example.com"):
    resp = client.post(LOGIN, data={"username": email, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp


def refresh_with(client, raw: str | None = None):
    """Refresh with the client's cookie jar, or with an explicit raw token."""
    if raw is None:
        return client.post(REFRESH, headers=XRW)
    client.cookies.clear()
    return client.post(REFRESH, headers={**XRW, "Cookie": f"{COOKIE}={raw}"})


def test_login_sets_httponly_strict_cookie_and_stores_only_a_hash(client, create_user, db_session) -> None:
    create_user()
    resp = login(client)
    header = resp.headers["set-cookie"]
    assert f"{COOKIE}=" in header and "HttpOnly" in header and "SameSite=strict" in header
    assert "Path=/api/v1/auth" in header and "Secure" not in header  # not production
    raw = resp.cookies[COOKIE]
    stored = db_session.scalar(select(RefreshToken))
    assert stored.token_hash == hash_refresh_token(raw) and raw not in stored.token_hash
    assert "refresh" not in resp.json()  # never exposed to JavaScript


def test_refresh_rotates_token_and_returns_working_access_token(client, create_user, db_session) -> None:
    create_user()
    old_raw = login(client).cookies[COOKIE]
    resp = refresh_with(client)
    assert resp.status_code == 200, resp.text
    new_raw = resp.cookies[COOKIE]
    assert new_raw != old_raw
    assert client.get(ME, headers={"Authorization": f"Bearer {resp.json()['access_token']}"}).status_code == 200

    old = db_session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(old_raw)))
    new = db_session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(new_raw)))
    assert old.revoked_at is not None and new.revoked_at is None and old.family_id == new.family_id


def test_reuse_of_rotated_token_revokes_the_whole_session(client, create_user, monkeypatch) -> None:
    create_user()
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    stolen = login(client).cookies[COOKIE]
    current = refresh_with(client).cookies[COOKIE]  # the real user refreshes

    assert refresh_with(client, stolen).status_code == 401  # the thief replays the old token...
    assert refresh_with(client, current).status_code == 401  # ...and the victim's session dies too


def test_concurrent_refresh_within_grace_is_a_conflict_not_theft(client, create_user) -> None:
    create_user()
    raw = login(client).cookies[COOKIE]
    newer = refresh_with(client).cookies[COOKIE]
    resp = refresh_with(client, raw)  # second tab, same old cookie, same moment
    assert resp.status_code == 409
    assert refresh_with(client, newer).status_code == 200  # session still alive


def test_logout_revokes_session_and_clears_cookie(client, create_user) -> None:
    create_user()
    raw = login(client).cookies[COOKIE]
    resp = client.post(LOGOUT, headers=XRW)
    assert resp.status_code == 204
    assert f'{COOKIE}=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]
    assert refresh_with(client, raw).status_code == 401


def test_logout_only_ends_this_session(client, create_user) -> None:
    create_user()
    laptop = login(client).cookies[COOKIE]
    phone = login(client).cookies[COOKIE]
    client.cookies.clear()
    client.post(LOGOUT, headers={**XRW, "Cookie": f"{COOKIE}={laptop}"})
    assert refresh_with(client, phone).status_code == 200


def test_logout_without_cookie_is_fine(client) -> None:
    assert client.post(LOGOUT, headers=XRW).status_code == 204
    assert client.post(LOGOUT, headers={**XRW, "Cookie": f"{COOKIE}=unknown"}).status_code == 204


@pytest.mark.parametrize("path", [REFRESH, LOGOUT])
def test_custom_header_required(client, path) -> None:
    assert client.post(path).status_code == 403


def test_refresh_without_or_with_unknown_cookie(client) -> None:
    assert refresh_with(client).status_code == 401
    assert refresh_with(client, "not-a-real-token").status_code == 401


def test_expired_refresh_token_rejected(client, create_user, db_session) -> None:
    create_user()
    raw = login(client).cookies[COOKIE]
    db_session.execute(update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert refresh_with(client, raw).status_code == 401


def test_disabled_user_cannot_refresh(client, create_user, db_session) -> None:
    user = create_user()
    raw = login(client).cookies[COOKIE]
    user.is_active = False
    db_session.flush()
    assert refresh_with(client, raw).status_code == 401


def test_purge_deletes_only_long_expired_tokens(client, create_user, db_session) -> None:
    create_user()
    login(client)
    login(client)
    first = db_session.scalars(select(RefreshToken)).first()
    first.expires_at = datetime.now(UTC) - timedelta(days=2)
    db_session.flush()
    assert purge_expired_refresh_tokens(db_session) == 1
    assert db_session.scalar(select(func.count()).select_from(RefreshToken)) == 1


def test_purge_task_uses_session_factory(client, create_user, db_session, monkeypatch) -> None:
    from app.tasks import maintenance_tasks

    create_user()
    login(client)
    db_session.execute(update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(days=3)))
    monkeypatch.setattr(maintenance_tasks, "_session_factory", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    assert maintenance_tasks.purge_refresh_tokens_task.run() == 1


def test_cookie_secure_flag_follows_environment(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "refresh_cookie_secure", None)
    monkeypatch.setattr(s, "environment", "production")
    assert s.refresh_cookie_secure_effective is True
    monkeypatch.setattr(s, "refresh_cookie_secure", False)
    assert s.refresh_cookie_secure_effective is False
