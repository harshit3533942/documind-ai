"""Integration tests: processing pipeline (extract -> chunk -> store) and the
chunks API. Real PDFs, real PostgreSQL."""

import re
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentStatus, User
from app.services import document_processing_service as dps
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.document_processing_service import DocumentProcessor
from app.services.storage_service import LocalStorage
from app.services.embedding_service import HashingEmbedder
from app.tests import pdf_factory

DOCS = "/api/v1/documents"


def page_text(page: int, sentences: int = 12) -> str:
    return " ".join(f"Page {page} fact {s} explains an important idea." for s in range(sentences))


@pytest.fixture
def alice(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def processor(db_session: Session, storage: LocalStorage) -> DocumentProcessor:
    return DocumentProcessor(
        db_session, storage, TextChunker(ChunkingConfig(chunk_size=60, chunk_overlap=15)), embedder=HashingEmbedder()
    )


def upload(client: TestClient, headers, data: bytes) -> uuid.UUID:
    resp = client.post(DOCS, headers=headers, files={"file": ("doc.pdf", data, "application/pdf")})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def chunks_of(db: Session, doc_id: uuid.UUID) -> list[DocumentChunk]:
    return list(db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id).order_by(DocumentChunk.chunk_index)))


# ------------------------------------------------------------------ success
def test_process_text_pdf_creates_page_accurate_chunks(client, alice, processor, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1), page_text(2), page_text(3)]))
    result = processor.process(doc_id)

    assert result.status == "completed"
    assert result.total_pages == 3
    chunks = chunks_of(db_session, doc_id)
    assert len(chunks) == result.chunk_count > 3
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    doc = db_session.get(Document, doc_id)
    assert doc.status == DocumentStatus.COMPLETED
    assert doc.processed_at is not None and doc.error_message is None
    for c in chunks:
        assert c.user_id == doc.user_id  # denormalised owner for retrieval filtering
        assert c.embedding is not None and len(c.embedding) == 384
        pages_in_text = {int(p) for p in re.findall(r"Page (\d) fact", c.content)}
        assert (c.page_start, c.page_end) == (min(pages_in_text), max(pages_in_text))
    assert any(c.page_start != c.page_end for c in chunks), "expected at least one cross-page chunk"


def test_empty_pages_reported(client, alice, processor) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1), "", page_text(3)]))
    result = processor.process(doc_id)
    assert result.status == "completed"
    assert result.empty_pages == [2]


def test_batched_inserts(client, alice, processor, db_session, monkeypatch) -> None:
    monkeypatch.setattr(dps, "INSERT_BATCH_SIZE", 3)
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(p, 20) for p in range(1, 4)], fontsize=9))
    result = processor.process(doc_id)
    assert result.chunk_count == len(chunks_of(db_session, doc_id)) > 3


# ------------------------------------------------------------------ idempotency / duplicates
def test_completed_document_is_not_reprocessed(client, alice, processor, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1)]))
    first = processor.process(doc_id)
    assert processor.process(doc_id).status == "skipped"
    assert len(chunks_of(db_session, doc_id)) == first.chunk_count


def test_forced_reprocessing_replaces_chunks_without_duplicates(client, alice, processor, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1), page_text(2)]))
    first = processor.process(doc_id)
    before = {c.id for c in chunks_of(db_session, doc_id)}

    second = processor.process(doc_id, force=True)
    after = chunks_of(db_session, doc_id)
    assert second.status == "completed"
    assert len(after) == first.chunk_count == second.chunk_count
    assert before.isdisjoint({c.id for c in after})  # old rows replaced, not appended


def test_document_already_processing_is_skipped(client, alice, processor, db_session) -> None:
    """Simulates a second worker receiving the same job."""
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1)]))
    db_session.get(Document, doc_id).status = DocumentStatus.PROCESSING
    db_session.commit()
    assert processor.process(doc_id).status == "skipped"
    assert chunks_of(db_session, doc_id) == []


def test_unknown_document_is_skipped(processor) -> None:
    assert processor.process(uuid.uuid4()).status == "skipped"


# ------------------------------------------------------------------ failures
def test_scanned_pdf_marked_failed_with_message(client, alice, processor, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.image_only_pdf())
    result = processor.process(doc_id)
    assert result.status == "failed"
    assert "No extractable text" in result.error
    assert chunks_of(db_session, doc_id) == []

    status = client.get(f"{DOCS}/{doc_id}/status", headers=alice).json()
    assert status["status"] == "failed"
    assert "scanned" in status["error_message"]


def test_failed_document_can_be_retried(client, alice, processor) -> None:
    doc_id = upload(client, alice, pdf_factory.blank_pdf(2))
    assert processor.process(doc_id).status == "failed"
    assert processor.process(doc_id).status == "failed"  # claimable again, not "skipped"


def test_missing_file_marked_failed(client, alice, processor, storage, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1)]))
    storage.delete(db_session.get(Document, doc_id).storage_key)
    result = processor.process(doc_id)
    assert result.status == "failed"
    assert "missing" in result.error


def test_unexpected_error_rolls_back_marks_failed_and_reraises(client, alice, processor, db_session) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1), page_text(2)]))
    good = processor.process(doc_id)

    def exploding_chunks(pages):
        for i, c in enumerate(TextChunker(ChunkingConfig(60, 15)).chunk(pages)):
            if i == 2:
                raise RuntimeError("simulated crash mid-way")
            yield c

    processor.chunker.chunk = exploding_chunks  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        processor.process(doc_id, force=True)

    doc = db_session.get(Document, doc_id, populate_existing=True)
    assert doc.status == DocumentStatus.FAILED
    assert doc.error_message == dps.INTERNAL_ERROR_MESSAGE  # no internal details leaked
    # The half-finished run was rolled back: previous chunks intact, no partial set, no duplicates.
    assert len(chunks_of(db_session, doc_id)) == good.chunk_count


# ------------------------------------------------------------------ chunks API
def test_chunks_endpoint_returns_ordered_chunks(client, alice, processor) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1), page_text(2)]))
    total = processor.process(doc_id).chunk_count

    body = client.get(f"{DOCS}/{doc_id}/chunks", headers=alice, params={"limit": 2}).json()
    assert body["total"] == total
    assert [c["chunk_index"] for c in body["items"]] == [0, 1]
    assert {"content", "page_start", "page_end", "token_count"} <= body["items"][0].keys()
    assert "embedding" not in body["items"][0]


def test_chunks_endpoint_enforces_ownership(client, alice, processor, create_user, auth_headers) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf([page_text(1)]))
    processor.process(doc_id)
    create_user("bob@example.com")
    bob = auth_headers("bob@example.com")
    assert client.get(f"{DOCS}/{doc_id}/chunks", headers=bob).status_code == 404
    assert client.get(f"{DOCS}/{doc_id}/chunks").status_code == 401


def test_pending_document_ids(client, alice, processor, db_session) -> None:
    a = upload(client, alice, pdf_factory.text_pdf([page_text(1)]))
    b = upload(client, alice, pdf_factory.text_pdf([page_text(2)]))
    processor.process(a)
    assert dps.pending_document_ids(db_session) == [b]
    assert db_session.scalar(select(func.count()).select_from(Document)) == 2
