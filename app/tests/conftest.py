"""Shared pytest fixtures.

Integration tests run against a SEPARATE database (documind_test) so they can
never damage development data. Each test runs inside a transaction that is
rolled back afterwards -> every test starts with clean tables and tests are
fast (no TRUNCATE / re-create between tests).
"""

import os

# Must be set BEFORE any app module is imported (settings are cached).
# A test-only secret so tests run on CI machines that have no .env file.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-at-least-32-characters-long")
# Tests inject fake/real embedders explicitly; don't load the model on every TestClient start.
os.environ["PRELOAD_EMBEDDING_MODEL"] = "false"
# Tests must never call a real LLM, even if the developer's .env has a key.
os.environ["LLM_PROVIDER"] = "extractive"
# Rate limits are tested explicitly (test_performance.py); elsewhere they would
# make tests depend on how many requests a test happens to send.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import uuid  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.models  # noqa: F401, E402 - register models
from app.core.dependencies import (  # noqa: E402
    get_embedder_dependency,
    get_llm_dependency,
    get_storage,
    get_task_queue,
)
from app.services.embedding_service import HashingEmbedder  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.services.storage_service import LocalStorage  # noqa: E402
from app.database.base import Base  # noqa: E402
from app.database.connection import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import User, UserRole  # noqa: E402

TEST_PASSWORD = "Str0ngPassw0rd"

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://documind:documind_dev_password@localhost:5432/documind_test",
)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except OperationalError:
        pytest.skip("Test PostgreSQL not reachable - run `docker compose up -d`")
    # Fresh schema for the whole test session.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    connection = db_engine.connect()
    transaction = connection.begin()
    # create_savepoint: code under test may call session.commit(); that only
    # releases a SAVEPOINT, and the outer transaction is still rolled back.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    """Isolated storage directory per test - tests never touch storage/uploads."""
    return LocalStorage(tmp_path / "uploads")


class FakeTaskQueue:
    """In-memory stand-in for Celery: records what would have been enqueued."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[uuid.UUID, bool]] = []
        self.available = True  # set False to simulate "Redis is down"

    def enqueue_document(self, document_id: uuid.UUID, *, force: bool = False) -> bool:
        if not self.available:
            return False
        self.enqueued.append((document_id, force))
        return True


@pytest.fixture
def task_queue() -> FakeTaskQueue:
    return FakeTaskQueue()


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own empty in-memory Redis (cache, rate limits,
    document versions). The real Redis is never touched by unit tests."""
    import fakeredis

    from app.database import redis_client

    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: server)
    return server


@pytest.fixture
def fake_embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def client(
    db_session: Session, storage: LocalStorage, task_queue: FakeTaskQueue, fake_embedder: HashingEmbedder
) -> Iterator[TestClient]:
    """API client whose requests use the rolled-back test session, temp storage,
    an in-memory task queue (no Celery/Redis), the fast fake embedder and NO
    LLM (extractive mode). Tests can override get_llm_dependency for a FakeLLM."""
    fastapi_app.dependency_overrides[get_db] = lambda: db_session
    fastapi_app.dependency_overrides[get_storage] = lambda: storage
    fastapi_app.dependency_overrides[get_task_queue] = lambda: task_queue
    fastapi_app.dependency_overrides[get_embedder_dependency] = lambda: fake_embedder
    fastapi_app.dependency_overrides[get_llm_dependency] = lambda: None
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def create_user(db_session: Session) -> Callable[..., User]:
    def _create(email: str = "user@example.com", role: UserRole = UserRole.USER, is_active: bool = True) -> User:
        user = User(
            name="Test User", email=email, hashed_password=hash_password(TEST_PASSWORD), role=role, is_active=is_active
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _create


@pytest.fixture
def auth_headers(client: TestClient) -> Callable[[str], dict[str, str]]:
    def _login(email: str, password: str = TEST_PASSWORD) -> dict[str, str]:
        resp = client.post("/api/v1/auth/login", data={"username": email, "password": password})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _login


@pytest.fixture(scope="session")
def real_embedder():
    """The real Sentence Transformers model, loaded ONCE per test session
    (loading takes ~15 s). Tests using it are marked `model`."""
    from app.core.config import get_settings
    from app.services.embedding_service import SentenceTransformerEmbedder

    s = get_settings()
    embedder = SentenceTransformerEmbedder(s.embedding_model_name, expected_dimension=s.embedding_dimension)
    try:
        embedder.embed_query("warm up")
    except Exception as exc:  # noqa: BLE001 - e.g. offline machine without cached model
        pytest.skip(f"Embedding model unavailable: {exc}")
    return embedder
