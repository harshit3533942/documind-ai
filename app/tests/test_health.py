import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import health

client = TestClient(app)


def test_root() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"


def test_liveness() -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


OK = {"status": "ok"}
DOWN = {"status": "error", "detail": "ConnectionError"}


def test_readiness_ok_when_dependencies_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "check_database_connection", lambda: {"status": "ok", "pgvector": "0.8.0"})
    monkeypatch.setattr(health, "check_redis_connection", lambda: OK)
    response = client.get("/health/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"


@pytest.mark.parametrize(("db", "redis"), [(DOWN, OK), (OK, DOWN), (DOWN, DOWN)])
def test_readiness_503_when_a_dependency_is_down(monkeypatch: pytest.MonkeyPatch, db, redis) -> None:
    monkeypatch.setattr(health, "check_database_connection", lambda: db)
    monkeypatch.setattr(health, "check_redis_connection", lambda: redis)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_readiness_against_real_services() -> None:
    """Integration: PostgreSQL and Redis from docker-compose."""
    body = client.get("/health/ready").json()
    if body["status"] != "ok":
        pytest.skip(f"services not running: {body['checks']}")
    assert body["checks"]["database"]["pgvector"]
    assert body["checks"]["redis"]["status"] == "ok"


def test_unknown_route_returns_404() -> None:
    assert client.get("/does-not-exist").status_code == 404
