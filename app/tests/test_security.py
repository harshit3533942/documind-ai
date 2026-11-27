"""Phase 13 security hardening: safe error bodies, security headers, production
config guard, docs exposure, injection attempts and secret hygiene."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import BASE_DIR, Settings, get_settings
from app.database.connection import get_db
from app.main import app as fastapi_app
from app.main import create_app
from app.models import User
from app.tests import pdf_factory

GOOD_SECRET = "Zq8-k2LmP0x_Rt7vWn4YbC9eHs1JdF6uGa3iKo5"  # random-looking, 39 chars


# ------------------------------------------------------------------ error responses
def test_validation_errors_do_not_echo_submitted_values(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/register", json={"name": "A", "email": "a@example.com", "password": "Sh0rt"})
    assert resp.status_code == 422
    assert "Sh0rt" not in resp.text  # the (wrong) password is never sent back
    [error] = resp.json()["detail"]
    assert set(error) == {"type", "loc", "msg"} and error["loc"] == ["body", "password"]


def test_unexpected_errors_return_generic_500(client: TestClient) -> None:
    def broken_db():
        raise RuntimeError("connection to db-prod-7.internal:5432 failed for user admin")

    fastapi_app.dependency_overrides[get_db] = broken_db
    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        resp = c.post("/api/v1/auth/register", json={"name": "A", "email": "a@example.com", "password": "Passw0rd123"})
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}
    assert "db-prod-7" not in resp.text and "Traceback" not in resp.text


# ------------------------------------------------------------------ headers
def test_security_headers_on_api_responses(client: TestClient) -> None:
    h = client.get("/api/v1/auth/me").headers  # even on a 401
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert h["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert h["Cache-Control"] == "no-store"  # private data must not be cached
    assert "Strict-Transport-Security" not in h  # off unless HTTPS


def test_hsts_only_when_enabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hsts_enabled", True)
    assert client.get("/health/live").headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_swagger_ui_not_blocked_by_strict_csp(client: TestClient) -> None:
    resp = client.get("/docs")
    assert resp.status_code == 200 and "Content-Security-Policy" not in resp.headers
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_api_docs_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "api_docs_enabled", False)
    with TestClient(create_app()) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/health/live").status_code == 200


def test_docs_default_off_in_production() -> None:
    assert Settings(environment="development").docs_enabled is True
    prod = Settings(environment="production", jwt_secret_key=GOOD_SECRET,
                    database_url="postgresql+psycopg://app:S3cure-Pa55@db:5432/documind")
    assert prod.docs_enabled is False


# ------------------------------------------------------------------ production guard
@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"database_url": Settings.model_fields["database_url"].default},
         "development password"),
        ({"database_url": "postgresql+psycopg://app:S3cure@db/x", "debug": True}, "DEBUG"),
        ({"database_url": "postgresql+psycopg://app:S3cure@db/x", "cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"database_url": "postgresql+psycopg://app:S3cure@db/x",
          "jwt_secret_key": "change-me-change-me-change-me-change-me"}, "JWT_SECRET_KEY"),
    ],
)
def test_production_refuses_unsafe_config(overrides, problem) -> None:
    with pytest.raises(ValidationError, match=problem):
        Settings(**{"environment": "production", "jwt_secret_key": GOOD_SECRET, **overrides})


def test_production_accepts_safe_config() -> None:
    Settings(environment="production", jwt_secret_key=GOOD_SECRET,
             database_url="postgresql+psycopg://app:S3cure-Pa55@db:5432/documind",
             cors_origins=["https://documind.example.com"])


def test_jwt_secret_is_never_printed() -> None:
    s = Settings(jwt_secret_key=GOOD_SECRET)
    assert GOOD_SECRET not in repr(s) and GOOD_SECRET not in str(s.model_dump())


def test_jwt_secret_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_secret_key="too-short")


# ------------------------------------------------------------------ injection attempts
INJECTIONS = ["'; DROP TABLE users; --", "\" OR 1=1 --", "<script>alert(1)</script>", "{{7*7}}", "../../etc/passwd"]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_in_question_is_just_text(client: TestClient, create_user, auth_headers, db_session, payload) -> None:
    create_user("alice@example.com")
    resp = client.post("/api/v1/chat/ask", headers=auth_headers("alice@example.com"), json={"question": payload})
    assert resp.status_code == 200
    assert resp.json()["user_message"]["content"] == payload  # stored and returned verbatim, as data
    assert db_session.scalar(select(func.count()).select_from(User)) == 1  # users table intact


def test_malicious_filename_is_stored_as_plain_text(client: TestClient, create_user, auth_headers) -> None:
    create_user("alice@example.com")
    name = "<img src=x onerror=alert(1)>'; DROP TABLE documents;--.pdf"
    resp = client.post("/api/v1/documents", headers=auth_headers("alice@example.com"),
                       files={"file": (name, pdf_factory.text_pdf(["hello"]), "application/pdf")})
    assert resp.status_code == 201
    assert resp.json()["filename"] == name  # JSON-encoded data; React renders it as text, never HTML
    assert resp.headers["Content-Type"].startswith("application/json")


# ------------------------------------------------------------------ secret hygiene
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, check=False)


def test_env_file_is_git_ignored() -> None:
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git checkout")
    assert _git("check-ignore", ".env").returncode == 0, ".env must be ignored by git"
    assert _git("check-ignore", "storage/uploads/some-user/file.pdf").returncode == 0, "uploads must be ignored"
    assert ".env" not in _git("ls-files").stdout.split()


def test_env_example_contains_no_real_secrets() -> None:
    example = (Path(BASE_DIR) / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if line.startswith(("JWT_SECRET_KEY=", "OPENAI_API_KEY=")):
            assert line.split("=", 1)[1].strip() == "", f"{line.split('=')[0]} must be empty in .env.example"
