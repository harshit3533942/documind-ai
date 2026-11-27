"""Phase 10: rate limiting, Redis caching (with invalidation), request timing
middleware and admin metrics. Uses the per-test fake Redis from conftest."""

import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.orm import Session

from app.core import rate_limit
from app.core.config import get_settings
from app.core.dependencies import get_llm_dependency
from app.database import redis_client
from app.main import app as fastapi_app
from app.models import User, UserRole
from app.services import cache_service
from app.services.cache_service import CachedEmbedder
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import HashingEmbedder
from app.services.llm_service import LLMError
from app.tests import pdf_factory
from app.tests.conftest import TEST_PASSWORD
from app.tests.fakes import FakeLLM

ASK = "/api/v1/chat/ask"
LOGIN = "/api/v1/auth/login"
QUESTION = "What is the daily meal allowance for domestic travel?"
PAGES = ["Leave policy. Every employee receives 24 days of paid annual leave.",
         "Travel policy. The daily meal allowance for domestic travel is 1500 rupees."]


class BrokenRedis:
    """Every Redis call fails, like a Redis outage."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RedisConnectionError("Redis is down")
        return fail


@pytest.fixture
def limits_on(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def _set(**limits: int) -> None:
        monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
        # Freeze the limiter's clock 20 s into a window: with the real clock a
        # slow test could cross a minute boundary, the counter would reset and
        # the expected 429 would never come (flaky). Only rate_limit's own
        # reference to `time` is replaced, not the global module.
        monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: 60 * 16_667 + 20.0))
        for name, value in limits.items():
            monkeypatch.setattr(get_settings(), name, value)
    return _set


@pytest.fixture
def user_with_docs(client, create_user, auth_headers, db_session: Session, storage, fake_embedder):
    def _make(email: str = "alice@example.com", pages: list[str] = PAGES):
        user = create_user(email)
        h = auth_headers(email)
        resp = client.post("/api/v1/documents", headers=h, files={"file": ("handbook.pdf", pdf_factory.text_pdf(pages), "application/pdf")})
        doc_id = uuid.UUID(resp.json()["id"])
        DocumentProcessor(db_session, storage, TextChunker(ChunkingConfig(60, 10)), embedder=fake_embedder).process(doc_id)
        return user, h, doc_id
    return _make


def login(client: TestClient, email: str, password: str = TEST_PASSWORD):
    return client.post(LOGIN, data={"username": email, "password": password})


# ------------------------------------------------------------------ rate limiting
def test_chat_rate_limit_per_user(client, user_with_docs, create_user, auth_headers, limits_on) -> None:
    _, alice, _ = user_with_docs()
    limits_on(rate_limit_chat_per_user=2)
    assert client.post(ASK, headers=alice, json={"question": "leave"}).status_code == 200
    assert client.post(ASK, headers=alice, json={"question": "travel"}).status_code == 200
    resp = client.post(ASK, headers=alice, json={"question": "again"})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(resp.headers["Retry-After"]) <= 60

    create_user("bob@example.com")  # limits are per user
    assert client.post(ASK, headers=auth_headers("bob@example.com"), json={"question": "leave"}).status_code == 200


def test_login_rate_limit_per_ip_counts_failed_attempts(client, create_user, limits_on) -> None:
    create_user("alice@example.com")
    limits_on(rate_limit_login_per_ip=3, rate_limit_login_per_email=100)
    for _ in range(3):
        assert login(client, "alice@example.com", "Wr0ngPassword").status_code == 401
    blocked = login(client, "alice@example.com")  # even the CORRECT password is now refused
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers


def test_login_rate_limit_per_account(client, create_user, limits_on) -> None:
    create_user("victim@example.com")
    create_user("other@example.com")
    limits_on(rate_limit_login_per_ip=100, rate_limit_login_per_email=2)
    for _ in range(2):
        login(client, "victim@example.com", "Guess1234x")
    assert login(client, "VICTIM@example.com", "Guess5678x").status_code == 429  # case-insensitive
    assert login(client, "other@example.com").status_code == 200  # other accounts unaffected


def test_register_rate_limit_per_ip(client, limits_on) -> None:
    limits_on(rate_limit_register_per_ip=2)
    for i in range(2):
        r = client.post("/api/v1/auth/register", json={"name": "U", "email": f"u{i}@example.com", "password": "Passw0rd123"})
        assert r.status_code == 201
    r = client.post("/api/v1/auth/register", json={"name": "U", "email": "u9@example.com", "password": "Passw0rd123"})
    assert r.status_code == 429


def test_upload_rate_limit_per_user(client, create_user, auth_headers, limits_on) -> None:
    create_user("alice@example.com")
    h = auth_headers("alice@example.com")
    limits_on(rate_limit_upload_per_user=1)
    up = lambda i: client.post("/api/v1/documents", headers=h, files={"file": (f"{i}.pdf", pdf_factory.text_pdf([f"doc {i}"]), "application/pdf")})  # noqa: E731
    assert up(1).status_code == 201
    assert up(2).status_code == 429


def test_fixed_window_resets(monkeypatch, limits_on) -> None:
    limits_on()
    now = [60 * 16_667 + 20.0]  # exactly 20 s into a 60 s window
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: now[0]))
    assert rate_limit.hit("t", "id", limit=1, window_seconds=60).allowed
    second = rate_limit.hit("t", "id", limit=1, window_seconds=60)
    assert not second.allowed and second.retry_after == 40
    now[0] += 40  # next window
    assert rate_limit.hit("t", "id", limit=1, window_seconds=60).allowed


def test_rate_limiter_fails_open_when_redis_is_down(client, user_with_docs, limits_on, monkeypatch) -> None:
    _, h, _ = user_with_docs()
    limits_on(rate_limit_chat_per_user=1)
    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())
    for _ in range(3):  # over the limit, but Redis is down -> allowed, never a 500
        assert client.post(ASK, headers=h, json={"question": QUESTION}).status_code == 200


def test_rejections_are_counted_without_personal_data(client, create_user, limits_on, fake_redis) -> None:
    create_user("alice@example.com")
    limits_on(rate_limit_login_per_ip=1, rate_limit_login_per_email=100)
    login(client, "alice@example.com")
    login(client, "alice@example.com")
    assert fake_redis.hgetall("metrics:rate_limited") == {"login-ip": "1"}
    assert not any("alice" in k for k in fake_redis.keys("*"))  # emails/IPs are hashed in keys


# ------------------------------------------------------------------ embedding cache
def test_query_embedding_cache() -> None:
    inner = HashingEmbedder()
    calls = []
    real = inner.embed_query
    inner.embed_query = lambda t: calls.append(t) or real(t)  # type: ignore[method-assign]
    cached = CachedEmbedder(inner)

    first, second = cached.embed_query("annual leave"), cached.embed_query("annual leave")
    assert first == second and calls == ["annual leave"]  # model ran once
    cached.embed_query("travel")
    assert calls == ["annual leave", "travel"]
    assert cache_service.cache_stats() == {"embedding_miss": 2, "embedding_hit": 1}
    assert cached.model_name == inner.model_name and cached.count_tokens("a b") == 2  # delegation


# ------------------------------------------------------------------ answer cache
def ask(client, h, question=QUESTION, **extra):
    resp = client.post(ASK, headers=h, json={"question": question, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_repeated_question_served_from_cache(client, user_with_docs, fake_embedder) -> None:
    _, h, _ = user_with_docs()
    first = ask(client, h)
    embedded_before = fake_embedder.embedded_texts
    second = ask(client, h)
    assert first["cached"] is False and second["cached"] is True
    assert fake_embedder.embedded_texts == embedded_before  # no retrieval / sentence scoring work
    assert second["assistant_message"]["content"] == first["assistant_message"]["content"]
    assert second["assistant_message"]["citations"] == first["assistant_message"]["citations"]
    assert set(second["timings_ms"]) == {"cache", "total"}
    assert second["conversation_id"] != first["conversation_id"]  # still saved as its own conversation


def test_cache_key_normalises_whitespace_and_case(client, user_with_docs) -> None:
    _, h, _ = user_with_docs()
    ask(client, h, QUESTION)
    assert ask(client, h, "  what is THE daily meal allowance   for domestic travel? ")["cached"] is True


def test_follow_ups_are_never_cached(client, user_with_docs) -> None:
    _, h, _ = user_with_docs()
    conv = ask(client, h)["conversation_id"]
    assert ask(client, h, "and the leave?", conversation_id=conv)["cached"] is False
    assert ask(client, h, "and the leave?", conversation_id=conv)["cached"] is False


def test_cache_invalidated_when_documents_change(client, user_with_docs, db_session, storage, fake_embedder) -> None:
    user, h, doc_id = user_with_docs()
    ask(client, h)
    assert ask(client, h)["cached"] is True

    # New document processed -> version bump -> fresh answer that can include it.
    resp = client.post("/api/v1/documents", headers=h, files={"file": ("extra.pdf", pdf_factory.text_pdf(["Meal allowance update."]), "application/pdf")})
    DocumentProcessor(db_session, storage, TextChunker(ChunkingConfig(60, 10)), embedder=fake_embedder).process(uuid.UUID(resp.json()["id"]))
    assert ask(client, h)["cached"] is False
    assert ask(client, h)["cached"] is True

    # Document deleted -> must not keep citing it from cache.
    assert client.delete(f"/api/v1/documents/{doc_id}", headers=h).status_code == 204
    after = ask(client, h)
    assert after["cached"] is False
    assert all(c["document_id"] != str(doc_id) for c in after["assistant_message"]["citations"])


def test_cache_is_per_user(client, user_with_docs) -> None:
    _, alice, _ = user_with_docs("alice@example.com")
    ask(client, alice)
    _, bob, _ = user_with_docs("bob@example.com", ["Bob travel notes: meal allowance unknown."])
    bob_answer = ask(client, bob)
    assert bob_answer["cached"] is False  # never Alice's cached answer
    assert all(c["filename"] == "handbook.pdf" and "1500" not in c["excerpt"] for c in bob_answer["assistant_message"]["citations"])


def test_cache_respects_document_selection(client, user_with_docs) -> None:
    _, h, doc_id = user_with_docs()
    ask(client, h)
    assert ask(client, h, document_ids=[str(doc_id)])["cached"] is False  # different key


def test_degraded_llm_fallback_answers_are_not_cached(client, user_with_docs) -> None:
    _, h, _ = user_with_docs()
    fastapi_app.dependency_overrides[get_llm_dependency] = lambda: FakeLLM(LLMError("down"), LLMError("down"))
    assert ask(client, h)["assistant_message"]["mode"] == "extractive"
    assert ask(client, h)["cached"] is False


def test_chat_works_when_redis_is_down(client, user_with_docs, monkeypatch) -> None:
    _, h, _ = user_with_docs()
    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())
    body = ask(client, h)
    assert body["cached"] is False and body["assistant_message"]["found"] is True


def test_cache_can_be_disabled(client, user_with_docs, monkeypatch) -> None:
    _, h, _ = user_with_docs()
    monkeypatch.setattr(get_settings(), "cache_enabled", False)
    ask(client, h)
    assert ask(client, h)["cached"] is False


# ------------------------------------------------------------------ middleware
def test_response_time_and_request_id_headers(client) -> None:
    resp = client.get("/health/live")
    assert float(resp.headers["X-Response-Time-ms"]) >= 0
    assert len(resp.headers["X-Request-ID"]) == 32


def test_safe_incoming_request_id_is_propagated_unsafe_replaced(client) -> None:
    assert client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    evil = client.get("/health/live", headers={"X-Request-ID": "x\r\nSet-Cookie: a=b"}).headers["X-Request-ID"]
    assert evil != "x\r\nSet-Cookie: a=b" and len(evil) == 32


def test_access_log_line_has_no_query_string(client, caplog) -> None:
    caplog.set_level("INFO", logger="app.access")
    client.get("/health/live?token=SECRET123")
    lines = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any("GET /health/live -> 200" in line for line in lines)
    assert not any("SECRET123" in line for line in lines)


# ------------------------------------------------------------------ admin metrics
def test_admin_metrics(client, user_with_docs, create_user, auth_headers) -> None:
    _, h, _ = user_with_docs()
    ask(client, h)
    ask(client, h)
    create_user("admin@example.com", role=UserRole.ADMIN)
    m = client.get("/api/v1/admin/metrics", headers=auth_headers("admin@example.com")).json()
    assert m["app"]["answer_mode"] == "extractive" and m["app"]["uptime_seconds"] >= 0
    assert m["database"]["users"] == 2 and m["database"]["conversations"] == 2 and m["database"]["messages"] == 4
    assert m["database"]["pool_size"] >= 1
    assert m["documents"]["by_status"]["completed"] == 1
    assert m["redis"]["status"] == "ok"
    assert m["cache"]["answer_hit"] == 1 and m["cache"]["answer_miss"] == 1
    assert client.get("/api/v1/admin/metrics", headers=h).status_code == 403
