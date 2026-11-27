"""Soft delete (trash, restore, purge) and per-user quotas."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentChunk, User, UserRole
from app.services import quota_service
from app.services.document_service import purge_trash
from app.services.retrieval_service import search_chunks
from app.tasks import maintenance_tasks
from app.tests import pdf_factory
from app.tests.test_background_tasks import NoCloseSession
from app.tests.test_chat import ask, setup_user  # noqa: F401 - fixture re-used

DOCS = "/api/v1/documents"


def upload(client, headers, text: str = "Some document text for the tests.", name: str = "doc.pdf"):
    return client.post(DOCS, headers=headers, files={"file": (name, pdf_factory.text_pdf([text]), "application/pdf")})


def stored_files(storage) -> list:
    return [p for p in storage.root.rglob("*") if p.is_file()] if storage.root.exists() else []


# ------------------------------------------------------------------ trash
def test_trash_hides_everywhere_and_restore_brings_back(client, setup_user, db_session, fake_embedder) -> None:  # noqa: F811
    user, h, ids = setup_user()
    doc_id = ids["handbook.pdf"]
    assert ask(client, h, "How many days of paid annual leave?").json()["assistant_message"]["citations"]

    assert client.delete(f"{DOCS}/{doc_id}", headers=h).status_code == 204
    # Hidden: library, detail, file, chunks, search, chat document selection.
    assert client.get(DOCS, headers=h).json()["total"] == 0
    for path in ("", "/file", "/chunks", "/status"):
        assert client.get(f"{DOCS}/{doc_id}{path}", headers=h).status_code == 404
    assert search_chunks(db_session, fake_embedder, user_id=user.id, query="annual leave") == []
    assert ask(client, h, "How many days of paid annual leave?", document_ids=[str(doc_id)]).status_code == 404
    reply = ask(client, h, "How many days of paid annual leave?").json()  # not served from the answer cache
    assert reply["cached"] is False and reply["assistant_message"]["citations"] == []
    # ...but still on disk and in the trash.
    trash = client.get(f"{DOCS}/trash", headers=h).json()
    assert trash["total"] == 1 and trash["items"][0]["id"] == str(doc_id) and trash["items"][0]["deleted_at"]

    restored = client.post(f"{DOCS}/{doc_id}/restore", headers=h)
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None
    assert client.get(f"{DOCS}/trash", headers=h).json()["total"] == 0
    assert search_chunks(db_session, fake_embedder, user_id=user.id, query="annual leave")
    assert client.post(f"{DOCS}/{doc_id}/restore", headers=h).status_code == 404  # not in the trash any more


def test_trash_is_per_user(client, setup_user) -> None:  # noqa: F811
    _, alice, ids = setup_user("alice@example.com")
    client.delete(f"{DOCS}/{ids['handbook.pdf']}", headers=alice)
    _, bob, _ = setup_user("bob@example.com", docs={})
    assert client.get(f"{DOCS}/trash", headers=bob).json()["total"] == 0
    assert client.post(f"{DOCS}/{ids['handbook.pdf']}/restore", headers=bob).status_code == 404
    assert client.delete(f"{DOCS}/{ids['handbook.pdf']}", headers=bob, params={"permanent": "true"}).status_code == 404


def test_permanent_delete_from_trash(client, setup_user, db_session, storage) -> None:  # noqa: F811
    _, h, ids = setup_user()
    doc_id = ids["handbook.pdf"]
    client.delete(f"{DOCS}/{doc_id}", headers=h)
    assert client.delete(f"{DOCS}/{doc_id}", headers=h).status_code == 404  # already trashed: not "live"
    assert client.delete(f"{DOCS}/{doc_id}", headers=h, params={"permanent": "true"}).status_code == 204
    assert db_session.get(Document, doc_id) is None
    assert db_session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert stored_files(storage) == []


def test_duplicate_of_a_trashed_file_points_to_the_trash(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user(docs={})
    pdf = pdf_factory.text_pdf(["Exactly the same bytes twice."])  # (PDFs embed a timestamp: build once)
    first = client.post(DOCS, headers=h, files={"file": ("first.pdf", pdf, "application/pdf")}).json()["id"]
    client.delete(f"{DOCS}/{first}", headers=h)
    again = client.post(DOCS, headers=h, files={"file": ("second.pdf", pdf, "application/pdf")})
    assert again.status_code == 409 and "in the trash" in again.json()["error"]["message"]


def test_purge_removes_only_old_trash(client, setup_user, db_session, storage, monkeypatch) -> None:  # noqa: F811
    _, h, _ = setup_user(docs={})
    old = uuid.UUID(upload(client, h, "old document text here").json()["id"])
    recent = uuid.UUID(upload(client, h, "recent document text here").json()["id"])
    live = uuid.UUID(upload(client, h, "live document text here").json()["id"])
    client.delete(f"{DOCS}/{old}", headers=h)
    client.delete(f"{DOCS}/{recent}", headers=h)
    days = get_settings().trash_retention_days
    db_session.execute(update(Document).where(Document.id == old)
                       .values(deleted_at=datetime.now(UTC) - timedelta(days=days, minutes=1)))

    monkeypatch.setattr(maintenance_tasks, "_session_factory", lambda: NoCloseSession(db_session))
    monkeypatch.setattr(maintenance_tasks, "get_storage", lambda: storage)
    assert maintenance_tasks.purge_trash_task.apply().get() == 1
    assert db_session.get(Document, old) is None
    assert db_session.get(Document, recent) is not None and db_session.get(Document, live) is not None
    assert len(stored_files(storage)) == 2
    assert purge_trash(db_session, storage) == 0  # idempotent


# ------------------------------------------------------------------ quotas: uploads
def test_document_count_quota(client, setup_user, monkeypatch) -> None:  # noqa: F811
    _, h, _ = setup_user(docs={})
    monkeypatch.setattr(get_settings(), "quota_max_documents", 2)
    assert upload(client, h, "first document text").status_code == 201
    second = upload(client, h, "second document text").json()["id"]
    blocked = upload(client, h, "third document text")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "quota_exceeded"

    client.delete(f"{DOCS}/{second}", headers=h)              # trash still counts
    assert upload(client, h, "third document text").status_code == 403
    client.delete(f"{DOCS}/{second}", headers=h, params={"permanent": "true"})
    assert upload(client, h, "third document text").status_code == 201


def test_storage_quota_and_rejected_file_is_removed(client, setup_user, storage, monkeypatch) -> None:  # noqa: F811
    _, h, _ = setup_user(docs={})
    first = upload(client, h, "small file")
    assert first.status_code == 201
    monkeypatch.setattr(get_settings(), "quota_max_storage_mb", 1)
    monkeypatch.setattr(quota_service, "MB", first.json()["file_size"] + 10)  # "1 MB" = just over the first file
    files_before = len(stored_files(storage))
    blocked = upload(client, h, "another small file that does not fit")
    assert blocked.status_code == 403 and "Storage limit" in blocked.json()["error"]["message"]
    assert len(stored_files(storage)) == files_before  # the rejected upload left nothing behind


def test_user_override_beats_default_and_zero_is_unlimited(client, setup_user, db_session, monkeypatch) -> None:  # noqa: F811
    user, h, _ = setup_user(docs={})
    monkeypatch.setattr(get_settings(), "quota_max_documents", 1)
    user.quota_max_documents = 0  # unlimited for this user
    db_session.flush()
    for i in range(3):
        assert upload(client, h, f"document number {i} text").status_code == 201


# ------------------------------------------------------------------ quotas: questions
def test_daily_question_quota(client, setup_user, monkeypatch) -> None:  # noqa: F811
    _, h, _ = setup_user()
    monkeypatch.setattr(get_settings(), "quota_max_questions_per_day", 2)
    assert ask(client, h, "annual leave").status_code == 200
    assert client.post("/api/v1/chat/stream", headers=h, json={"question": "sick leave"}).status_code == 200
    blocked = ask(client, h, "travel policy")
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "quota_exceeded"
    assert 0 < int(blocked.headers["Retry-After"]) <= 24 * 3600
    usage = client.get("/api/v1/account/usage", headers=h).json()
    assert usage["usage"]["questions_today"] == 3 and usage["limits"]["max_questions_per_day"] == 2


def test_question_quota_fails_open_without_redis(client, setup_user, monkeypatch) -> None:  # noqa: F811
    from redis.exceptions import RedisError

    from app.database import redis_client

    _, h, _ = setup_user()
    monkeypatch.setattr(get_settings(), "quota_max_questions_per_day", 1)

    class Down:
        def pipeline(self, *a, **k):
            raise RedisError("down")

        def get(self, *a, **k):
            raise RedisError("down")

    real = redis_client.get_redis
    user = User(id=uuid.uuid4(), name="x", email="x@example.com", hashed_password="x")
    monkeypatch.setattr(redis_client, "get_redis", lambda: Down())
    quota_service.consume_question(user)  # no exception
    assert quota_service.questions_today(user.id) == 0
    monkeypatch.setattr(redis_client, "get_redis", real)


# ------------------------------------------------------------------ usage + admin
def test_usage_endpoint(client, setup_user, monkeypatch) -> None:  # noqa: F811
    _, h, ids = setup_user()
    client.delete(f"{DOCS}/{ids['handbook.pdf']}", headers=h)
    upload(client, h, "a second document")
    body = client.get("/api/v1/account/usage", headers=h).json()
    s = get_settings()
    assert body["limits"] == {"max_documents": s.quota_max_documents,
                              "max_storage_bytes": s.quota_max_storage_mb * 1024 * 1024,
                              "max_questions_per_day": s.quota_max_questions_per_day}
    assert body["usage"]["documents"] == 2 and body["usage"]["trashed_documents"] == 1
    assert body["usage"]["storage_bytes"] > 0
    assert client.get("/api/v1/account/usage").status_code == 401


def test_admin_sets_quota(client, setup_user, create_user, auth_headers, db_session: Session) -> None:  # noqa: F811
    user, h, _ = setup_user(docs={})
    create_user("admin@example.com", role=UserRole.ADMIN)
    admin = auth_headers("admin@example.com")
    url = f"/api/v1/admin/users/{user.id}/quota"

    resp = client.patch(url, headers=admin, json={"max_documents": 5, "max_questions_per_day": 0})
    assert resp.status_code == 200
    assert resp.json()["limits"]["max_documents"] == 5 and resp.json()["limits"]["max_questions_per_day"] == 0
    client.patch(url, headers=admin, json={"max_storage_mb": 10})  # other fields unchanged
    db_session.refresh(user)
    assert (user.quota_max_documents, user.quota_max_storage_mb, user.quota_max_questions_per_day) == (5, 10, 0)
    client.patch(url, headers=admin, json={"max_documents": None})  # back to the default
    db_session.refresh(user)
    assert user.quota_max_documents is None
    assert client.get(url, headers=admin).json()["limits"]["max_storage_bytes"] == 10 * 1024 * 1024

    assert client.patch(url, headers=h, json={"max_documents": 1000}).status_code == 403  # users cannot raise their own
    assert client.patch(url, headers=admin, json={"max_documents": -1}).status_code == 422
    assert client.patch(f"/api/v1/admin/users/{uuid.uuid4()}/quota", headers=admin, json={}).status_code == 404
    assert client.get(f"/api/v1/admin/users/{uuid.uuid4()}/quota", headers=admin).status_code == 404


@pytest.mark.parametrize("status", [403, None])
def test_quota_error_status(status) -> None:
    from app.core.exceptions import QuotaExceededError

    err = QuotaExceededError("x", status_code=status)
    assert err.status_code == (status or 403) and err.code == "quota_exceeded"
