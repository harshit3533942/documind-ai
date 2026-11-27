"""Background processing: Celery task behaviour (success, failure, retries,
duplicates, crash recovery), upload->queue integration and admin monitoring.

Tasks are executed with `.apply()` (eager, in-process): no broker/worker needed,
and Celery runs retries synchronously, so retry logic is tested deterministically.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentChunk, DocumentStatus, User, UserRole
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import EmbeddingConfigError, HashingEmbedder
from app.services.storage_service import LocalStorage
from app.tasks import document_tasks
from app.tasks.document_tasks import process_document_task, requeue_stale_documents
from app.tests import pdf_factory
from app.tests.conftest import FakeTaskQueue

DOCS = "/api/v1/documents"
TEXT = ["Annual leave is 24 days per year. Requests go through the HR portal.",
        "Parental leave is 26 weeks. It can be split into three blocks."]


class NoCloseSession:
    """The task closes its session when done; the test session must survive."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def close(self) -> None:
        pass

    def __getattr__(self, name: str):
        return getattr(self._s, name)


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture(autouse=True)
def task_env(monkeypatch, db_session: Session, storage: LocalStorage, embedder: HashingEmbedder) -> None:
    """Point the Celery task at the test database, temp storage and fake embedder."""
    monkeypatch.setattr(document_tasks, "_session_factory", lambda: NoCloseSession(db_session))
    monkeypatch.setattr(document_tasks, "_storage_factory", lambda: storage)
    monkeypatch.setattr(document_tasks, "_embedder_factory", lambda: embedder)
    monkeypatch.setattr(document_tasks, "_llm_factory", lambda: None)
    # Never hand a follow-up job to a real broker from a test.
    monkeypatch.setattr(document_tasks, "enqueue_summary", lambda document_id: summaries_enqueued.append(document_id))
    summaries_enqueued.clear()


summaries_enqueued: list[uuid.UUID] = []


@pytest.fixture
def alice(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("alice@example.com")
    return auth_headers("alice@example.com")


def upload(client: TestClient, headers, data: bytes | None = None, name: str = "doc.pdf") -> uuid.UUID:
    resp = client.post(DOCS, headers=headers, files={"file": (name, data or pdf_factory.text_pdf(TEXT), "application/pdf")})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def doc(db: Session, doc_id: uuid.UUID) -> Document:
    return db.get(Document, doc_id, populate_existing=True)


def chunk_count(db: Session, doc_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == doc_id))


def flaky_process(fail_times: int, exc: Exception):
    """Wrap DocumentProcessor.process so it raises `exc` the first N calls."""
    real = DocumentProcessor.process
    calls = {"n": 0}

    def wrapper(self, document_id, *, force=False):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            # Behave like a real mid-processing crash: claim, then blow up.
            self._claim(document_id, force=force)
            self.db.rollback()
            self._mark_failed(document_id, "Processing failed due to an internal error. Please try again later.")
            raise exc
        return real(self, document_id, force=force)

    return wrapper, calls


# ------------------------------------------------------------------ upload -> queue
def test_upload_enqueues_job_and_sets_queued(client, alice, task_queue: FakeTaskQueue, db_session) -> None:
    resp = client.post(DOCS, headers=alice, files={"file": ("a.pdf", pdf_factory.text_pdf(TEXT), "application/pdf")})
    assert resp.status_code == 201
    assert resp.json()["status"] == "queued"
    doc_id = uuid.UUID(resp.json()["id"])
    assert task_queue.enqueued == [(doc_id, False)]


def test_upload_still_succeeds_when_broker_is_down(client, alice, task_queue: FakeTaskQueue) -> None:
    task_queue.available = False
    resp = client.post(DOCS, headers=alice, files={"file": ("a.pdf", pdf_factory.text_pdf(TEXT), "application/pdf")})
    assert resp.status_code == 201
    assert resp.json()["status"] == "uploaded"  # the sweeper will enqueue it later


def test_fast_worker_status_is_not_overwritten(client, alice, task_queue: FakeTaskQueue, db_session) -> None:
    """Race: the worker claims the document before the API marks it queued."""

    class RacingQueue(FakeTaskQueue):
        def enqueue_document(self, document_id, *, force=False):
            db_session.execute(update(Document).where(Document.id == document_id).values(status=DocumentStatus.PROCESSING))
            db_session.commit()
            return True

    from app.core.dependencies import get_task_queue
    from app.main import app

    app.dependency_overrides[get_task_queue] = lambda: RacingQueue()
    resp = client.post(DOCS, headers=alice, files={"file": ("a.pdf", pdf_factory.text_pdf(TEXT), "application/pdf")})
    assert resp.json()["status"] == "processing"  # NOT overwritten with "queued"


# ------------------------------------------------------------------ task: success / permanent failure
def test_task_processes_document_end_to_end(client, alice, db_session) -> None:
    doc_id = upload(client, alice)
    out = process_document_task.apply(args=[str(doc_id)])
    assert out.successful()
    assert out.result["status"] == "completed" and out.result["chunks"] >= 1
    d = doc(db_session, doc_id)
    assert d.status == DocumentStatus.COMPLETED and d.embedding_model == HashingEmbedder.model_name
    assert chunk_count(db_session, doc_id) == out.result["chunks"]


def test_permanent_failure_is_not_retried(client, alice, db_session, monkeypatch) -> None:
    doc_id = upload(client, alice, pdf_factory.image_only_pdf())
    wrapper, calls = flaky_process(0, RuntimeError())
    monkeypatch.setattr(DocumentProcessor, "process", wrapper)
    out = process_document_task.apply(args=[str(doc_id)])
    assert out.successful() and out.result["status"] == "failed"
    assert calls["n"] == 1  # exactly one attempt
    d = doc(db_session, doc_id)
    assert d.status == DocumentStatus.FAILED and "No extractable text" in d.error_message


# ------------------------------------------------------------------ task: retries
def test_transient_error_is_retried_then_succeeds(client, alice, db_session, monkeypatch) -> None:
    doc_id = upload(client, alice)
    wrapper, calls = flaky_process(2, OperationalError("SELECT 1", {}, Exception("connection reset")))
    monkeypatch.setattr(DocumentProcessor, "process", wrapper)

    out = process_document_task.apply(args=[str(doc_id)])
    assert out.successful() and out.result["status"] == "completed"
    assert calls["n"] == 3  # 2 failures + 1 success
    assert doc(db_session, doc_id).status == DocumentStatus.COMPLETED
    assert chunk_count(db_session, doc_id) == out.result["chunks"]  # no duplicates from earlier attempts


def test_gives_up_after_max_retries(client, alice, db_session, monkeypatch) -> None:
    doc_id = upload(client, alice)
    wrapper, calls = flaky_process(99, OperationalError("SELECT 1", {}, Exception("db down")))
    monkeypatch.setattr(DocumentProcessor, "process", wrapper)

    out = process_document_task.apply(args=[str(doc_id)])
    assert out.failed()  # recorded as FAILURE for monitoring
    assert calls["n"] == get_settings().processing_max_retries + 1
    d = doc(db_session, doc_id)
    assert d.status == DocumentStatus.FAILED
    assert d.error_message == "Processing failed due to an internal error. Please try again later."


def test_retry_waits_with_exponential_backoff(client, alice, monkeypatch) -> None:
    doc_id = upload(client, alice)
    wrapper, _ = flaky_process(3, OperationalError("SELECT 1", {}, Exception("x")))
    monkeypatch.setattr(DocumentProcessor, "process", wrapper)
    countdowns: list[int] = []
    real_retry = process_document_task.retry

    def spy_retry(*args, **kwargs):
        countdowns.append(kwargs["countdown"])
        return real_retry(*args, **kwargs)

    monkeypatch.setattr(process_document_task, "retry", spy_retry)
    process_document_task.apply(args=[str(doc_id)])
    base = get_settings().processing_retry_backoff
    assert countdowns == [base, base * 2, base * 4]


def test_configuration_error_is_not_retried(client, alice, db_session, monkeypatch) -> None:
    doc_id = upload(client, alice)
    wrapper, calls = flaky_process(99, EmbeddingConfigError("wrong dimension"))
    monkeypatch.setattr(DocumentProcessor, "process", wrapper)
    out = process_document_task.apply(args=[str(doc_id)])
    assert calls["n"] == 1 and out.result["status"] == "failed"
    assert doc(db_session, doc_id).error_message == document_tasks.CONFIG_ERROR_MESSAGE


def test_timeout_is_not_retried(client, alice, db_session, monkeypatch) -> None:
    doc_id = upload(client, alice)
    wrapper, calls = flaky_process(99, SoftTimeLimitExceeded())
    monkeypatch.setattr(DocumentProcessor, "process", wrapper)
    out = process_document_task.apply(args=[str(doc_id)])
    assert calls["n"] == 1 and out.result["status"] == "failed"
    assert doc(db_session, doc_id).error_message == document_tasks.TIMEOUT_MESSAGE


# ------------------------------------------------------------------ duplicates / crash recovery
def test_duplicate_delivery_does_not_duplicate_chunks(client, alice, db_session, embedder) -> None:
    doc_id = upload(client, alice)
    first = process_document_task.apply(args=[str(doc_id)]).result
    calls_after_first = embedder.calls
    second = process_document_task.apply(args=[str(doc_id)]).result
    assert second["status"] == "skipped"
    assert embedder.calls == calls_after_first  # no embedding work repeated
    assert chunk_count(db_session, doc_id) == first["chunks"]


def test_fresh_processing_document_is_left_alone(client, alice, db_session) -> None:
    doc_id = upload(client, alice)
    db_session.execute(update(Document).where(Document.id == doc_id).values(status=DocumentStatus.PROCESSING))
    db_session.commit()
    assert process_document_task.apply(args=[str(doc_id)]).result["status"] == "skipped"


def test_stale_processing_document_is_taken_over(client, alice, db_session) -> None:
    """Worker crashed mid-task: Celery redelivers, and the stale lease lets the new attempt claim it."""
    doc_id = upload(client, alice)
    long_ago = datetime.now(UTC) - timedelta(seconds=get_settings().processing_stale_after + 60)
    db_session.execute(
        update(Document).where(Document.id == doc_id).values(status=DocumentStatus.PROCESSING, updated_at=long_ago)
    )
    db_session.commit()
    assert process_document_task.apply(args=[str(doc_id)]).result["status"] == "completed"


def test_sweeper_requeues_only_stuck_documents(client, alice, db_session, monkeypatch) -> None:
    stuck_upload, fresh, abandoned, done = (upload(client, alice, pdf_factory.text_pdf([f"Doc {i} text here."])) for i in range(4))
    old = datetime.now(UTC) - timedelta(seconds=get_settings().processing_stale_after + 60)
    db_session.execute(update(Document).where(Document.id == stuck_upload).values(status=DocumentStatus.UPLOADED, updated_at=old))
    db_session.execute(update(Document).where(Document.id == abandoned).values(status=DocumentStatus.PROCESSING, updated_at=old))
    db_session.execute(update(Document).where(Document.id == done).values(status=DocumentStatus.COMPLETED, updated_at=old))
    db_session.commit()

    sent: list[str] = []
    monkeypatch.setattr(process_document_task, "apply_async", lambda args, **kw: sent.append(args[0]))
    assert requeue_stale_documents.apply().result == 2
    assert set(sent) == {str(stuck_upload), str(abandoned)}
    assert str(fresh) not in sent and str(done) not in sent


def test_celery_can_really_talk_to_redis() -> None:
    """Regression test: kombu 5.5+ refuses redis-py < 6.1, but only when a
    channel is opened at runtime - pip check and the fake queue both missed it.
    This opens a real broker channel through Celery's own transport."""
    from app.tasks.celery_app import celery_app

    with celery_app.connection_for_write() as conn:
        try:
            conn.ensure_connection(max_retries=1, timeout=3)
        except Exception as exc:  # noqa: BLE001
            if "VersionMismatch" in type(exc).__name__:
                raise
            pytest.skip(f"Redis not reachable: {exc}")
        channel = conn.default_channel  # this is where VersionMismatch is raised
        assert channel is not None


def test_invalid_document_id_fails_fast() -> None:
    assert process_document_task.apply(args=["not-a-uuid"]).failed()


# ------------------------------------------------------------------ reprocess endpoint
def test_owner_can_reprocess_failed_document(client, alice, task_queue: FakeTaskQueue, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.blank_pdf())
    process_document_task.apply(args=[str(doc_id)])
    assert doc(db_session, doc_id).status == DocumentStatus.FAILED

    resp = client.post(f"{DOCS}/{doc_id}/reprocess", headers=alice)
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued" and resp.json()["error_message"] is None
    assert task_queue.enqueued[-1] == (doc_id, False)


def test_cannot_reprocess_while_queued_or_processing(client, alice) -> None:
    doc_id = upload(client, alice)  # status: queued
    assert client.post(f"{DOCS}/{doc_id}/reprocess", headers=alice).status_code == 409


def test_other_user_cannot_reprocess(client, alice, create_user, auth_headers) -> None:
    doc_id = upload(client, alice)
    create_user("bob@example.com")
    assert client.post(f"{DOCS}/{doc_id}/reprocess", headers=auth_headers("bob@example.com")).status_code == 404


# ------------------------------------------------------------------ admin monitoring
@pytest.fixture
def admin(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("admin@example.com", role=UserRole.ADMIN)
    return auth_headers("admin@example.com")


def test_admin_stats_and_failed_list(client, alice, admin, db_session) -> None:
    ok = upload(client, alice, pdf_factory.text_pdf(TEXT), "good.pdf")
    bad = upload(client, alice, pdf_factory.image_only_pdf(), "scan.pdf")
    upload(client, alice, pdf_factory.text_pdf(["Still waiting in queue."]), "waiting.pdf")
    process_document_task.apply(args=[str(ok)])
    process_document_task.apply(args=[str(bad)])

    stats = client.get("/api/v1/admin/documents/stats", headers=admin).json()
    assert stats["documents"] == 3
    assert stats["by_status"] == {"uploaded": 0, "queued": 1, "processing": 0, "completed": 1, "failed": 1}
    assert stats["chunks"] == stats["chunks_with_embeddings"] > 0
    assert stats["storage_bytes"] > 0

    failed = client.get("/api/v1/admin/documents/failed", headers=admin).json()
    assert failed["total"] == 1
    item = failed["items"][0]
    assert item["id"] == str(bad) and item["user_email"] == "alice@example.com"
    assert "No extractable text" in item["error_message"]
    assert "content" not in item  # metadata only


def test_admin_retry(client, alice, admin, task_queue: FakeTaskQueue, db_session) -> None:
    bad = upload(client, alice, pdf_factory.image_only_pdf())
    process_document_task.apply(args=[str(bad)])
    resp = client.post(f"/api/v1/admin/documents/{bad}/retry", headers=admin)
    assert resp.status_code == 202 and resp.json()["status"] == "queued"
    assert client.post(f"/api/v1/admin/documents/{bad}/retry", headers=admin).status_code == 409  # not failed anymore
    assert client.post(f"/api/v1/admin/documents/{uuid.uuid4()}/retry", headers=admin).status_code == 404


def test_admin_monitoring_forbidden_for_users(client, alice) -> None:
    for path in ("/api/v1/admin/documents/stats", "/api/v1/admin/documents/failed"):
        assert client.get(path, headers=alice).status_code == 403
    assert client.post(f"/api/v1/admin/documents/{uuid.uuid4()}/retry", headers=alice).status_code == 403
