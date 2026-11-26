"""Infrastructure code that other tests replace with fakes: real DB session
helpers, health checks, Celery enqueueing, startup hooks, provider factories,
password rehashing and error-path rollbacks."""

import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.database import connection, redis_client
from app.models import Conversation, Message, User
from app.services import llm_service, task_queue
from app.services.auth_service import AuthService
from app.services.chat_service import ChatService
from app.tests.conftest import TEST_DATABASE_URL


# ------------------------------------------------------------------ database helpers
@pytest.fixture
def test_sessionmaker(db_engine, monkeypatch):
    """Point connection.SessionLocal at the TEST database."""
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(connection, "SessionLocal", factory)
    return factory


def _cleanup(factory, email: str) -> None:
    with factory() as s:
        for u in s.scalars(select(User).where(User.email == email)):
            s.delete(u)
        s.commit()


def test_session_scope_commits_on_success(test_sessionmaker) -> None:
    email = f"scope-{uuid.uuid4().hex[:8]}@example.com"
    try:
        with connection.session_scope() as db:
            db.add(User(name="S", email=email, hashed_password="x"))
        with test_sessionmaker() as s:
            assert s.scalar(select(User).where(User.email == email)) is not None
    finally:
        _cleanup(test_sessionmaker, email)


def test_session_scope_rolls_back_on_error(test_sessionmaker) -> None:
    email = f"scope-{uuid.uuid4().hex[:8]}@example.com"
    with pytest.raises(RuntimeError), connection.session_scope() as db:
        db.add(User(name="S", email=email, hashed_password="x"))
        db.flush()
        raise RuntimeError("boom")
    with test_sessionmaker() as s:
        assert s.scalar(select(User).where(User.email == email)) is None  # nothing persisted


def test_get_db_closes_session_and_rolls_back_on_error(test_sessionmaker) -> None:
    gen = connection.get_db()
    db = next(gen)
    assert isinstance(db, Session)
    email = f"getdb-{uuid.uuid4().hex[:8]}@example.com"
    db.add(User(name="S", email=email, hashed_password="x"))
    db.flush()
    with pytest.raises(ValueError):
        gen.throw(ValueError("request failed"))
    with test_sessionmaker() as s:
        assert s.scalar(select(User).where(User.email == email)) is None


def test_database_health_check_ok_and_error(monkeypatch) -> None:
    good_engine = create_engine(TEST_DATABASE_URL)
    bad_engine = create_engine("postgresql+psycopg://nobody:wrong@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
    try:
        monkeypatch.setattr(connection, "engine", good_engine)
        ok = connection.check_database_connection()
        assert ok["status"] == "ok" and ok["pgvector"]
        monkeypatch.setattr(connection, "engine", bad_engine)
        bad = connection.check_database_connection()
        assert bad == {"status": "error", "detail": "OperationalError"}  # type only, no connection string leaked
    finally:
        good_engine.dispose()  # close pooled connections (no leaked sockets)
        bad_engine.dispose()


def test_redis_health_check_error(monkeypatch) -> None:
    class Down:
        def ping(self):
            raise RedisConnectionError("down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: Down())
    assert redis_client.check_redis_connection() == {"status": "error", "detail": "ConnectionError"}


# ------------------------------------------------------------------ Celery enqueue
def test_celery_queue_sends_task(monkeypatch) -> None:
    from app.tasks.document_tasks import process_document_task

    sent = {}
    monkeypatch.setattr(process_document_task, "apply_async", lambda **kw: sent.update(kw))
    doc_id = uuid.uuid4()
    assert task_queue.CeleryTaskQueue().enqueue_document(doc_id, force=True) is True
    assert sent["args"] == [str(doc_id)] and sent["kwargs"] == {"force": True}
    assert sent["retry"] is True and sent["retry_policy"]["max_retries"] == 2  # fail fast if Redis is down


def test_celery_queue_returns_false_when_broker_down(monkeypatch) -> None:
    from kombu.exceptions import OperationalError

    from app.tasks.document_tasks import process_document_task

    def fail(**_):
        raise OperationalError("Error 10061 connecting to localhost:6379")

    monkeypatch.setattr(process_document_task, "apply_async", fail)
    assert task_queue.CeleryTaskQueue().enqueue_document(uuid.uuid4()) is False  # upload still succeeds


# ------------------------------------------------------------------ startup hooks
def test_api_preload_hook_loads_model_and_survives_errors(monkeypatch, caplog) -> None:
    from app import main
    from app.services import embedding_service

    calls = []

    class Fake:
        def embed_query(self, text):
            calls.append(text)

    monkeypatch.setattr(embedding_service, "get_embedder", lambda: Fake())
    main._preload_embedding_model()
    assert calls == ["warm-up"]

    class Broken:
        def embed_query(self, text):
            raise OSError("model download failed")

    monkeypatch.setattr(embedding_service, "get_embedder", lambda: Broken())
    main._preload_embedding_model()  # must not raise: the API still starts
    assert "pre-load failed" in caplog.text


def test_worker_warm_up_hook(monkeypatch) -> None:
    from app.services import embedding_service
    from app.tasks import celery_app

    calls = []
    monkeypatch.setattr(embedding_service, "get_embedder", lambda: type("E", (), {"embed_query": lambda s, t: calls.append(t)})())
    celery_app._warm_up_embedding_model()
    assert calls == ["warm-up"]
    celery_app._configure_logging()  # Celery logging hook uses the app format; must not raise


# ------------------------------------------------------------------ LLM provider factory
def test_llm_client_factory_follows_settings(monkeypatch) -> None:
    llm_service.get_llm_client.cache_clear()
    try:
        monkeypatch.setattr(get_settings(), "llm_provider", "extractive")
        assert llm_service.get_llm_client() is None
        llm_service.get_llm_client.cache_clear()
        monkeypatch.setattr(get_settings(), "llm_provider", "openai")
        monkeypatch.setattr(get_settings(), "openai_api_key", None)
        monkeypatch.setattr(get_settings(), "openai_base_url", "http://127.0.0.1:11434/v1")
        client = llm_service.get_llm_client()
        assert isinstance(client, llm_service.OpenAIChatClient) and client.model_name == get_settings().openai_model
    finally:
        llm_service.get_llm_client.cache_clear()


def test_llm_empty_response_is_an_error() -> None:
    import httpx

    def handler(request):
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": []})

    client = llm_service.OpenAIChatClient(api_key="k", model="m", base_url="https://x/v1", max_retries=0,
                                          http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(llm_service.LLMError, match="empty"):
        client.complete([llm_service.ChatMessage("user", "q")], max_tokens=5, temperature=0)


# ------------------------------------------------------------------ auth: transparent rehash
def test_login_upgrades_outdated_password_hash(db_session) -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("Str0ngPassw0rd")
    user = User(name="Old", email="old@example.com", hashed_password=weak)
    db_session.add(user)
    db_session.flush()

    AuthService(db_session).authenticate("old@example.com", "Str0ngPassw0rd")
    db_session.refresh(user)
    assert user.hashed_password != weak  # re-hashed with current, stronger parameters
    assert user.hashed_password.startswith("$argon2id$") and "m=65536" in user.hashed_password
    assert hash_password("x") != hash_password("x")  # salted


# ------------------------------------------------------------------ chat: rollback on DB failure
def test_chat_rolls_back_when_saving_fails(db_session, create_user, fake_embedder, monkeypatch) -> None:
    user = create_user()
    service = ChatService(db_session, fake_embedder, None)
    real_add = service.conversations.add_message
    calls = {"n": 0}

    def flaky_add(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # question saved, answer insert fails
            raise RuntimeError("disk full")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(service.conversations, "add_message", flaky_add)
    with pytest.raises(RuntimeError):
        service.ask(user, question="What is the leave policy?")
    # Neither a half conversation nor the orphan question survives.
    assert db_session.scalar(select(Message).limit(1)) is None
    assert db_session.scalar(select(Conversation).limit(1)) is None


def test_chat_ask_requires_embedder(db_session, create_user) -> None:
    with pytest.raises(RuntimeError, match="embedder"):
        ChatService(db_session).ask(create_user(), question="hello")


# ------------------------------------------------------------------ cache fail-open (direct)
def test_every_cache_operation_fails_open(monkeypatch) -> None:
    from app.services import cache_service

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RedisConnectionError("down")
            return fail

    monkeypatch.setattr(redis_client, "get_redis", lambda: Down())
    uid = uuid.uuid4()
    assert cache_service.get_json("k") is None
    cache_service.set_json("k", {"a": 1}, 60)           # no exception
    assert cache_service.document_version(uid) is None  # -> answer cache skipped
    cache_service.bump_document_version(uid)            # no exception
    assert cache_service.cache_stats() == {}


def test_production_dependency_providers(monkeypatch) -> None:
    from app.core import dependencies
    from app.services.cache_service import CachedEmbedder

    monkeypatch.setattr(dependencies, "_cached_embedder", None)
    first = dependencies.get_embedder_dependency()
    assert isinstance(first, CachedEmbedder) and dependencies.get_embedder_dependency() is first  # singleton
    llm_service.get_llm_client.cache_clear()
    monkeypatch.setattr(get_settings(), "llm_provider", "extractive")
    assert dependencies.get_llm_dependency() is None
    llm_service.get_llm_client.cache_clear()
    assert dependencies.get_task_queue() is dependencies.get_task_queue()
