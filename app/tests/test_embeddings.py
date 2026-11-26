"""Embedding pipeline + vector retrieval tests (fast: HashingEmbedder, real PostgreSQL)."""

import math
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentStatus, User
from app.services import document_processing_service as dps
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.document_processing_service import DocumentProcessor, documents_missing_embeddings
from app.services.embedding_service import EmbeddingConfigError, HashingEmbedder
from app.services.retrieval_service import search_chunks
from app.services.storage_service import LocalStorage
from app.tests import pdf_factory

DOCS = "/api/v1/documents"

HANDBOOK = [
    "Annual leave: every employee receives 24 days of paid annual leave each year. "
    "Leave requests go through the HR portal two weeks in advance.",
    "Travel policy: economy class flights are mandatory for trips under six hours. "
    "Hotel bookings must use the corporate travel agency.",
    "Security policy: laptops must use the VPN outside the office. "
    "Passwords rotate every ninety days and must be twelve characters long.",
]


class RenamedEmbedder(HashingEmbedder):
    model_name = "test/other-model"


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def make_processor(db_session: Session, storage: LocalStorage) -> Callable[..., DocumentProcessor]:
    def _make(embedder: HashingEmbedder, size: int = 40, overlap: int = 8) -> DocumentProcessor:
        return DocumentProcessor(db_session, storage, TextChunker(ChunkingConfig(size, overlap)), embedder=embedder)

    return _make


def register(create_user, auth_headers, email: str) -> tuple[User, dict[str, str]]:
    user = create_user(email)
    return user, auth_headers(email)


def upload(client: TestClient, headers, data: bytes, name: str = "doc.pdf") -> uuid.UUID:
    resp = client.post(DOCS, headers=headers, files={"file": (name, data, "application/pdf")})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def chunks_of(db: Session, doc_id: uuid.UUID) -> list[DocumentChunk]:
    return list(db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)))


# ------------------------------------------------------------------ storage of vectors
def test_every_chunk_gets_a_normalised_vector(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    result = make_processor(embedder).process(doc_id)

    assert result.status == "completed"
    assert result.embedded_count == result.chunk_count > 0 and result.reused_count == 0
    for c in chunks_of(db_session, doc_id):
        assert c.embedding is not None and len(c.embedding) == 384
        assert math.isclose(math.sqrt(sum(float(x) ** 2 for x in c.embedding)), 1.0, rel_tol=1e-5)
    assert db_session.get(Document, doc_id).embedding_model == embedder.model_name


def test_embeddings_computed_in_batches(client, create_user, auth_headers, make_processor, embedder, monkeypatch) -> None:
    monkeypatch.setattr(dps, "INSERT_BATCH_SIZE", 2)
    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    result = make_processor(embedder, size=20, overlap=0).process(doc_id)
    assert result.chunk_count >= 5
    assert embedder.calls == math.ceil(result.chunk_count / 2)  # one model call per batch, not per chunk


# ------------------------------------------------------------------ duplicate processing
def test_reprocessing_reuses_existing_embeddings(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    processor = make_processor(embedder)
    first = processor.process(doc_id)
    before = {c.content: list(c.embedding) for c in chunks_of(db_session, doc_id)}
    texts_embedded = embedder.embedded_texts

    second = processor.process(doc_id, force=True)
    assert second.embedded_count == 0
    assert second.reused_count == second.chunk_count == first.chunk_count
    assert embedder.embedded_texts == texts_embedded  # the model was not called again
    after = {c.content: list(c.embedding) for c in chunks_of(db_session, doc_id)}
    assert after == before


def test_changed_chunk_settings_only_embed_new_chunks(client, create_user, auth_headers, make_processor, embedder) -> None:
    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    make_processor(embedder, size=40, overlap=8).process(doc_id)
    second = make_processor(embedder, size=25, overlap=0).process(doc_id, force=True)
    assert second.embedded_count + second.reused_count == second.chunk_count
    assert second.embedded_count > 0  # different chunk boundaries -> new text -> new vectors


def test_no_reuse_across_different_models(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    make_processor(embedder).process(doc_id)
    second = make_processor(RenamedEmbedder()).process(doc_id, force=True)
    assert second.reused_count == 0 and second.embedded_count == second.chunk_count
    assert db_session.get(Document, doc_id).embedding_model == "test/other-model"


def test_documents_missing_embeddings(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    _, h = register(create_user, auth_headers, "a@example.com")
    good = upload(client, h, pdf_factory.text_pdf(HANDBOOK[:1]), "good.pdf")
    stale = upload(client, h, pdf_factory.text_pdf(HANDBOOK[1:2]), "stale.pdf")
    holes = upload(client, h, pdf_factory.text_pdf(HANDBOOK[2:]), "holes.pdf")
    for d in (good, stale, holes):
        make_processor(embedder).process(d)

    db_session.execute(update(Document).where(Document.id == stale).values(embedding_model="old/model"))
    db_session.execute(update(DocumentChunk).where(DocumentChunk.document_id == holes).values(embedding=None))
    db_session.commit()

    assert set(documents_missing_embeddings(db_session, embedder.model_name)) == {stale, holes}


# ------------------------------------------------------------------ config safety
def test_chunk_size_larger_than_model_limit_is_rejected(client, create_user, auth_headers, make_processor, db_session) -> None:
    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    tiny_model = HashingEmbedder(max_tokens=32)  # can only embed 30 tokens + 2 special
    with pytest.raises(EmbeddingConfigError, match="exceeds"):
        make_processor(tiny_model, size=40).process(doc_id)
    assert db_session.get(Document, doc_id, populate_existing=True).status == DocumentStatus.FAILED


def test_wrong_vector_size_from_model_is_caught(client, create_user, auth_headers, make_processor) -> None:
    class BrokenEmbedder(HashingEmbedder):
        def embed_documents(self, texts):
            return [[0.1] * 10 for _ in texts]

    _, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    with pytest.raises(EmbeddingConfigError):
        make_processor(BrokenEmbedder()).process(doc_id)


# ------------------------------------------------------------------ retrieval
def test_search_ranks_relevant_chunk_first(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    user, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK), "handbook.pdf")
    make_processor(embedder).process(doc_id)

    results = search_chunks(db_session, embedder, user_id=user.id, query="economy class flights travel", top_k=3)
    assert results[0].page_start == 2
    assert "economy class" in results[0].content
    assert results[0].filename == "handbook.pdf"
    assert results == sorted(results, key=lambda r: r.score, reverse=True)
    assert all(-1.0 <= r.score <= 1.0 for r in results)


def test_search_never_returns_other_users_chunks(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    alice, ha = register(create_user, auth_headers, "alice@example.com")
    bob, hb = register(create_user, auth_headers, "bob@example.com")
    make_processor(embedder).process(upload(client, ha, pdf_factory.text_pdf(HANDBOOK[:1])))
    bob_doc = upload(client, hb, pdf_factory.text_pdf(["Bob secret: the vault code is 4242. " + HANDBOOK[1]]))
    make_processor(embedder).process(bob_doc)

    results = search_chunks(db_session, embedder, user_id=alice.id, query="Bob secret vault code 4242", top_k=10)
    assert results, "Alice should still get her own (less relevant) chunks"
    assert all("vault" not in r.content for r in results)
    assert all(r.document_id != bob_doc for r in results)


def test_search_ignores_unfinished_documents(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    user, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    make_processor(embedder).process(doc_id)
    db_session.execute(update(Document).where(Document.id == doc_id).values(status=DocumentStatus.PROCESSING))
    db_session.commit()
    assert search_chunks(db_session, embedder, user_id=user.id, query="annual leave") == []


def test_search_ignores_vectors_from_another_model(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    user, h = register(create_user, auth_headers, "a@example.com")
    make_processor(embedder).process(upload(client, h, pdf_factory.text_pdf(HANDBOOK)))
    assert search_chunks(db_session, RenamedEmbedder(), user_id=user.id, query="annual leave") == []


def test_search_filters_by_document_ids(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    user, h = register(create_user, auth_headers, "a@example.com")
    d1 = upload(client, h, pdf_factory.text_pdf(HANDBOOK[:1]), "leave.pdf")
    d2 = upload(client, h, pdf_factory.text_pdf(HANDBOOK[1:2]), "travel.pdf")
    for d in (d1, d2):
        make_processor(embedder).process(d)
    results = search_chunks(db_session, embedder, user_id=user.id, query="annual leave days", document_ids=[d2])
    assert results and all(r.document_id == d2 for r in results)


def test_deleting_document_removes_its_vectors_from_search(client, create_user, auth_headers, make_processor, embedder, db_session) -> None:
    user, h = register(create_user, auth_headers, "a@example.com")
    doc_id = upload(client, h, pdf_factory.text_pdf(HANDBOOK))
    make_processor(embedder).process(doc_id)
    assert search_chunks(db_session, embedder, user_id=user.id, query="annual leave")
    assert client.delete(f"{DOCS}/{doc_id}", headers=h).status_code == 204
    assert search_chunks(db_session, embedder, user_id=user.id, query="annual leave") == []
