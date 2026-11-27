"""Prometheus /metrics: HTTP metrics, RAG metrics, scrape-time state, protection."""

import re
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.core import metrics
from app.core.config import get_settings
from app.tests.test_chat import ask, setup_user  # noqa: F401 - fixture re-used


class SessionContext:
    """`with SessionLocal() as db` around the test session, without closing it."""

    def __init__(self, session) -> None:
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *exc) -> None:
        pass


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, db_session):
    """State metrics read the TEST database and are never served from cache."""
    import app.database.connection as connection

    monkeypatch.setattr(connection, "SessionLocal", SessionContext(db_session))
    monkeypatch.setattr(metrics, "STATE_CACHE_SECONDS", -1)


def scrape(client, **headers) -> str:
    resp = client.get("/metrics", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    return resp.text


def value(text: str, name: str, **labels) -> float:
    """Value of one sample, e.g. value(t, "documind_documents", status="completed")."""
    for line in text.splitlines():
        if not line.startswith(name):
            continue
        rest = line[len(name):]
        if labels:
            if not rest.startswith("{"):
                continue
            found = dict(re.findall(r'(\w+)="([^"]*)"', rest.rsplit("} ", 1)[0]))  # values may contain braces
            if any(found.get(k) != v for k, v in labels.items()):
                continue
        elif not rest.startswith(" "):
            continue
        return float(line.rsplit(" ", 1)[1])
    return 0.0


# ------------------------------------------------------------------ HTTP metrics
def test_http_requests_are_counted_by_route_template(client) -> None:
    before = scrape(client)
    client.get("/api/v1/documents/123e4567-e89b-12d3-a456-426614174000")
    client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000")
    client.get("/totally/unknown/path")
    after = scrape(client)

    route = "/api/v1/documents/{document_id}"
    assert value(after, "documind_http_requests_total", route=route, status="401") - \
        value(before, "documind_http_requests_total", route=route, status="401") == 2  # one series, not one per id
    assert value(after, "documind_http_requests_total", route="unmatched", status="404") >= 1
    assert value(after, "documind_http_request_duration_seconds_count", route=route) >= 2
    assert "123e4567" not in after                           # raw paths never become labels
    assert 'route="/metrics"' not in after                   # the scraper is not measured


def test_route_label() -> None:
    def scope(path: str, template: str | None):
        return {"path": path, "route": SimpleNamespace(path_format=template) if template else None}

    assert metrics.route_label(scope("/api/v1/documents/abc", "/documents/{document_id}")) == "/api/v1/documents/{document_id}"
    assert metrics.route_label(scope("/health/live", "/health/live")) == "/health/live"
    assert metrics.route_label(scope("/", "/")) == "/"
    assert metrics.route_label(scope("/x", None)) == "unmatched"


# ------------------------------------------------------------------ RAG metrics
def test_answers_feed_rag_metrics(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    before = scrape(client)
    ask(client, h, "How many days of paid annual leave?")
    ask(client, h, "How many days of paid annual leave?")  # same first question -> answer cache
    after = scrape(client)
    delta = lambda name, **lb: value(after, name, **lb) - value(before, name, **lb)  # noqa: E731
    assert delta("documind_rag_answers_total", mode="extractive", found="true", cached="false") == 1
    assert delta("documind_rag_answers_total", mode="extractive", found="true", cached="true") == 1
    assert delta("documind_rag_stage_duration_seconds_count", stage="retrieval") == 1
    assert delta("documind_rag_stage_duration_seconds_count", stage="cache") == 1
    assert delta("documind_rag_stage_duration_seconds_count", stage="total") == 2


# ------------------------------------------------------------------ state + cross-process counters
def test_state_from_database_and_redis(client, setup_user, fake_redis) -> None:  # noqa: F811
    setup_user()  # one completed document
    fake_redis.hset("metrics:cache", mapping={"answer_hit": 3, "answer_miss": 5})
    fake_redis.hset("metrics:rate_limited", "login-ip", 2)
    fake_redis.rpush("documents", "job1", "job2")
    metrics.record_processing("completed", 2.5, 10)
    metrics.record_processing("failed", 0.5, 0)

    text = scrape(client)
    assert value(text, "documind_database_up") == 1 and value(text, "documind_redis_up") == 1
    assert value(text, "documind_documents", status="completed") == 1
    assert value(text, "documind_documents", status="failed") == 0
    assert value(text, "documind_users") == 1
    assert value(text, "documind_processing_queue_length") == 2
    assert value(text, "documind_cache_events_total", kind="answer", result="miss") == 5
    assert value(text, "documind_rate_limited_requests_total", scope="login-ip") == 2
    assert value(text, "documind_documents_processed_total", status="completed") == 1
    assert value(text, "documind_documents_processed_total", status="failed") == 1
    assert value(text, "documind_document_processing_seconds_total") == 3.0
    assert value(text, "documind_document_pages_processed_total") == 10


def test_scrape_survives_database_and_redis_outages(client, monkeypatch) -> None:
    import app.database.connection as connection
    from app.database import redis_client

    def broken_session():
        raise OSError("database down")

    class BrokenRedis:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RedisError("down")
            return fail

    monkeypatch.setattr(connection, "SessionLocal", broken_session)
    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())
    text = scrape(client)
    assert value(text, "documind_database_up") == 0
    assert value(text, "documind_redis_up") == 0
    metrics.record_processing("completed", 1, 1)  # best effort: no exception


def test_state_is_cached_between_scrapes(monkeypatch) -> None:
    monkeypatch.setattr(metrics, "STATE_CACHE_SECONDS", 60)
    calls = []
    collector = metrics.StateCollector()
    monkeypatch.setattr(collector, "_build", lambda: calls.append(1) or iter(()))
    list(collector.collect())
    list(collector.collect())
    assert calls == [1]
    assert collector.describe() == []


# ------------------------------------------------------------------ protection
def test_metrics_token(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr("s3cret-token"))
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200


def test_metrics_can_be_disabled(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_enabled", False)
    assert client.get("/metrics").status_code == 404
