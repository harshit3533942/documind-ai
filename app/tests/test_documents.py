"""API tests for document upload / list / get / delete + ownership isolation."""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentChunk, User
from app.services.storage_service import LocalStorage
from app.tests import pdf_factory

DOCS = "/api/v1/documents"


@pytest.fixture
def alice(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("bob@example.com")
    return auth_headers("bob@example.com")


def upload(client: TestClient, headers: dict[str, str], data: bytes, name: str = "report.pdf",
           content_type: str = "application/pdf"):
    return client.post(DOCS, headers=headers, files={"file": (name, data, content_type)})


def stored_files(storage: LocalStorage) -> list:
    return [p for p in storage.root.rglob("*") if p.is_file()] if storage.root.exists() else []


# ------------------------------------------------------------------ upload: success
def test_upload_pdf_success(client: TestClient, alice, storage: LocalStorage, db_session: Session) -> None:
    pdf = pdf_factory.text_pdf(["Page one text", "Page two text"])
    resp = upload(client, alice, pdf)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "report.pdf"
    assert body["status"] == "queued"
    assert body["total_pages"] == 2
    assert body["file_size"] == len(pdf)
    assert body["mime_type"] == "application/pdf"
    assert "storage_key" not in body and "file_hash" not in body  # internals not exposed

    doc = db_session.get(Document, uuid.UUID(body["id"]))
    assert storage.path_for(doc.storage_key).read_bytes() == pdf
    assert len(stored_files(storage)) == 1


def test_upload_accepts_octet_stream_if_content_is_pdf(client: TestClient, alice) -> None:
    resp = upload(client, alice, pdf_factory.text_pdf(["x" * 50]), content_type="application/octet-stream")
    assert resp.status_code == 201


def test_upload_filename_path_traversal_is_neutralised(client: TestClient, alice, storage, db_session) -> None:
    resp = upload(client, alice, pdf_factory.text_pdf(["hello"]), name="../../../etc/evil.pdf")
    assert resp.status_code == 201
    assert resp.json()["filename"] == "evil.pdf"
    doc = db_session.get(Document, uuid.UUID(resp.json()["id"]))
    assert ".." not in doc.storage_key
    assert storage.path_for(doc.storage_key).is_relative_to(storage.root)


# ------------------------------------------------------------------ upload: rejections
@pytest.mark.parametrize(
    ("name", "content_type", "data", "expected"),
    [
        ("photo.png", "image/png", b"\x89PNG\r\n", 415),                            # unsupported extension
        ("fake.pdf", "application/pdf", b"MZ\x90 this is an exe", 415),            # wrong magic bytes
        ("report.pdf", "image/png", pdf_factory.text_pdf(["x"]), 415),             # wrong content type
        ("empty.pdf", "application/pdf", b"", 422),                                # zero bytes
        ("broken.pdf", "application/pdf", pdf_factory.corrupt_pdf(), 422),         # corrupt
        ("locked.pdf", "application/pdf", pdf_factory.encrypted_pdf(), 422),       # password protected
    ],
)
def test_upload_rejections_leave_no_files(client, alice, storage, db_session, name, content_type, data, expected) -> None:
    resp = upload(client, alice, data, name=name, content_type=content_type)
    assert resp.status_code == expected, resp.text
    assert "error" in resp.json()
    assert stored_files(storage) == []
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0


def test_upload_password_protected_has_helpful_message(client, alice) -> None:
    resp = upload(client, alice, pdf_factory.encrypted_pdf())
    assert "Password-protected" in resp.json()["error"]["message"]


def test_upload_too_large_streaming_limit(client, alice, storage, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    big = b"%PDF-1.7\n" + b"0" * (1024 * 1024 + 10)
    resp = upload(client, alice, big)
    assert resp.status_code == 413
    assert stored_files(storage) == []


def test_upload_too_large_rejected_by_content_length_middleware(client, alice) -> None:
    too_big = b"0" * (get_settings().max_upload_size_bytes + 2 * 1024 * 1024)
    resp = upload(client, alice, too_big)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_scanned_pdf_upload_is_accepted(client, alice) -> None:
    """Upload only does cheap validation. A scanned PDF is a valid PDF; the
    'no extractable text' outcome is decided by the processing pipeline."""
    resp = upload(client, alice, pdf_factory.image_only_pdf())
    assert resp.status_code == 201
    assert resp.json()["status"] == "queued"


def test_duplicate_upload_rejected_and_file_cleaned(client, alice, storage) -> None:
    pdf = pdf_factory.text_pdf(["same content"])
    assert upload(client, alice, pdf, name="first.pdf").status_code == 201
    resp = upload(client, alice, pdf, name="second.pdf")
    assert resp.status_code == 409
    assert "first.pdf" in resp.json()["error"]["message"]
    assert len(stored_files(storage)) == 1


def test_same_file_by_different_users_is_allowed(client, alice, bob) -> None:
    pdf = pdf_factory.text_pdf(["shared handout"])
    assert upload(client, alice, pdf).status_code == 201
    assert upload(client, bob, pdf).status_code == 201


def test_upload_requires_auth(client: TestClient) -> None:
    assert upload(client, {}, pdf_factory.text_pdf(["x"])).status_code == 401


# ------------------------------------------------------------------ list / get / status
def test_list_only_returns_own_documents(client, alice, bob) -> None:
    upload(client, alice, pdf_factory.text_pdf(["a1"]), name="a1.pdf")
    upload(client, alice, pdf_factory.text_pdf(["a2"]), name="a2.pdf")
    upload(client, bob, pdf_factory.text_pdf(["b1"]), name="b1.pdf")

    body = client.get(DOCS, headers=alice).json()
    assert body["total"] == 2
    assert {d["filename"] for d in body["items"]} == {"a1.pdf", "a2.pdf"}
    assert client.get(DOCS, headers=bob).json()["total"] == 1


def test_list_pagination_and_status_filter(client, alice) -> None:
    for i in range(3):
        upload(client, alice, pdf_factory.text_pdf([f"doc {i}"]), name=f"d{i}.pdf")
    page = client.get(DOCS, headers=alice, params={"limit": 2, "offset": 0}).json()
    assert len(page["items"]) == 2 and page["total"] == 3
    assert client.get(DOCS, headers=alice, params={"status": "completed"}).json()["total"] == 0
    assert client.get(DOCS, headers=alice, params={"status": "queued"}).json()["total"] == 3
    assert client.get(DOCS, headers=alice, params={"status": "bogus"}).status_code == 422


def test_get_document_and_status(client, alice) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf(["x"])).json()["id"]
    assert client.get(f"{DOCS}/{doc_id}", headers=alice).json()["id"] == doc_id
    status = client.get(f"{DOCS}/{doc_id}/status", headers=alice).json()
    assert status == {**status, "id": doc_id, "status": "queued", "total_pages": 1}


def test_get_unknown_and_malformed_ids(client, alice) -> None:
    assert client.get(f"{DOCS}/{uuid.uuid4()}", headers=alice).status_code == 404
    assert client.get(f"{DOCS}/not-a-uuid", headers=alice).status_code == 422


# ------------------------------------------------------------------ ownership isolation
def test_other_user_cannot_access_document(client, alice, bob, storage) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf(["alice private"])).json()["id"]

    # 404 (not 403) so Bob cannot even learn that the document exists.
    assert client.get(f"{DOCS}/{doc_id}", headers=bob).status_code == 404
    assert client.get(f"{DOCS}/{doc_id}/status", headers=bob).status_code == 404
    assert client.delete(f"{DOCS}/{doc_id}", headers=bob).status_code == 404

    # Still intact for Alice.
    assert client.get(f"{DOCS}/{doc_id}", headers=alice).status_code == 200
    assert len(stored_files(storage)) == 1


# ------------------------------------------------------------------ file download (viewer)
def test_owner_can_open_original_file_inline(client, alice, bob) -> None:
    data = pdf_factory.text_pdf(["page one", "page two"])
    doc_id = upload(client, alice, data, name="hand book.pdf").json()["id"]

    resp = client.get(f"{DOCS}/{doc_id}/file", headers=alice)
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline")
    assert "private" in resp.headers["cache-control"]

    assert client.get(f"{DOCS}/{doc_id}/file", headers=bob).status_code == 404  # isolation
    assert client.get(f"{DOCS}/{doc_id}/file").status_code == 401


def test_file_missing_on_disk_is_404(client, alice, storage) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf(["x"])).json()["id"]
    for path in stored_files(storage):
        path.unlink()
    resp = client.get(f"{DOCS}/{doc_id}/file", headers=alice)
    assert resp.status_code == 404 and "no longer available" in resp.json()["error"]["message"]


# ------------------------------------------------------------------ delete
def test_delete_removes_row_chunks_and_file(client, alice, storage, db_session: Session) -> None:
    doc_id = uuid.UUID(upload(client, alice, pdf_factory.text_pdf(["x"])).json()["id"])
    doc = db_session.get(Document, doc_id)
    db_session.add(DocumentChunk(document_id=doc.id, user_id=doc.user_id, chunk_index=0,
                                 content="x", page_start=1, page_end=1))
    db_session.flush()

    resp = client.delete(f"{DOCS}/{doc_id}", headers=alice)
    assert resp.status_code == 204
    assert client.get(f"{DOCS}/{doc_id}", headers=alice).status_code == 404
    assert db_session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert stored_files(storage) == []
    assert client.delete(f"{DOCS}/{doc_id}", headers=alice).status_code == 404
