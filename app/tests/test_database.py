"""Integration tests: schema, constraints, cascades and vector search."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    Message,
    MessageRole,
    User,
    UserRole,
)

DIM = 384


def make_user(db: Session, email: str = "alice@example.com") -> User:
    user = User(name="Alice", email=email, hashed_password="not-a-real-hash")
    db.add(user)
    db.flush()  # sends INSERT, assigns defaults, but does not commit
    return user


def make_document(db: Session, user: User, key: str = "a.pdf") -> Document:
    doc = Document(
        user_id=user.id,
        filename="report.pdf",
        storage_key=key,
        file_size=1024,
        mime_type="application/pdf",
        file_hash="0" * 64,
    )
    db.add(doc)
    db.flush()
    return doc


def one_hot(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def test_pgvector_extension_installed(db_session: Session) -> None:
    assert db_session.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar() == 1


def test_user_defaults(db_session: Session) -> None:
    user = make_user(db_session)
    db_session.refresh(user)
    assert user.role == UserRole.USER
    assert user.is_active is True
    assert user.created_at is not None


def test_email_must_be_unique(db_session: Session) -> None:
    make_user(db_session, "dup@example.com")
    with pytest.raises(IntegrityError):
        make_user(db_session, "dup@example.com")


def test_invalid_status_rejected_by_database(db_session: Session) -> None:
    user = make_user(db_session)
    doc = make_document(db_session, user)
    with pytest.raises(IntegrityError):
        # Bypass the ORM to prove the CHECK constraint protects the DB itself.
        db_session.execute(text("UPDATE documents SET status='hacked' WHERE id=:id"), {"id": doc.id})


def test_document_defaults_to_uploaded(db_session: Session) -> None:
    doc = make_document(db_session, make_user(db_session))
    assert doc.status == DocumentStatus.UPLOADED


def test_duplicate_chunk_index_rejected(db_session: Session) -> None:
    """Idempotency guarantee for retried processing tasks."""
    user = make_user(db_session)
    doc = make_document(db_session, user)
    db_session.add(DocumentChunk(document_id=doc.id, user_id=user.id, chunk_index=0, content="a", page_start=1, page_end=1))
    db_session.flush()
    db_session.add(DocumentChunk(document_id=doc.id, user_id=user.id, chunk_index=0, content="b", page_start=1, page_end=1))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_invalid_page_range_rejected(db_session: Session) -> None:
    user = make_user(db_session)
    doc = make_document(db_session, user)
    db_session.add(DocumentChunk(document_id=doc.id, user_id=user.id, chunk_index=0, content="a", page_start=5, page_end=2))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_deleting_document_cascades_to_chunks(db_session: Session) -> None:
    user = make_user(db_session)
    doc = make_document(db_session, user)
    db_session.add(DocumentChunk(document_id=doc.id, user_id=user.id, chunk_index=0, content="x", page_start=1, page_end=1))
    db_session.flush()

    db_session.execute(text("DELETE FROM documents WHERE id=:id"), {"id": doc.id})
    remaining = db_session.execute(text("SELECT count(*) FROM document_chunks")).scalar()
    assert remaining == 0


def test_deleting_user_cascades_everywhere(db_session: Session) -> None:
    user = make_user(db_session)
    doc = make_document(db_session, user)
    db_session.add(DocumentChunk(document_id=doc.id, user_id=user.id, chunk_index=0, content="x", page_start=1, page_end=1))
    conv = Conversation(user_id=user.id, title="Q&A")
    db_session.add(conv)
    db_session.flush()
    db_session.add(Message(conversation_id=conv.id, role=MessageRole.USER, content="hi"))
    db_session.flush()

    db_session.execute(text("DELETE FROM users WHERE id=:id"), {"id": user.id})
    for table in ("documents", "document_chunks", "conversations", "messages"):
        assert db_session.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table


def test_message_sources_stored_as_jsonb(db_session: Session) -> None:
    user = make_user(db_session)
    conv = Conversation(user_id=user.id)
    db_session.add(conv)
    db_session.flush()
    sources = [{"filename": "report.pdf", "page_start": 3, "page_end": 4, "score": 0.91}]
    msg = Message(conversation_id=conv.id, role=MessageRole.ASSISTANT, content="answer", sources=sources)
    db_session.add(msg)
    db_session.flush()
    db_session.expire_all()
    assert db_session.get(Message, msg.id).sources == sources


def test_vector_cosine_search_ordered_and_user_filtered(db_session: Session) -> None:
    """Nearest-neighbour search returns closest chunk first and never another user's chunks."""
    alice, bob = make_user(db_session, "a@x.com"), make_user(db_session, "b@x.com")
    doc_a, doc_b = make_document(db_session, alice, "a.pdf"), make_document(db_session, bob, "b.pdf")

    near = one_hot(0)
    far = one_hot(1)
    db_session.add_all([
        DocumentChunk(document_id=doc_a.id, user_id=alice.id, chunk_index=0, content="far", page_start=1, page_end=1, embedding=far),
        DocumentChunk(document_id=doc_a.id, user_id=alice.id, chunk_index=1, content="near", page_start=2, page_end=2, embedding=near),
        # Bob's chunk is a PERFECT match - it must still never be returned to Alice.
        DocumentChunk(document_id=doc_b.id, user_id=bob.id, chunk_index=0, content="bob-secret", page_start=1, page_end=1, embedding=near),
    ])
    db_session.flush()

    query = near
    distance = DocumentChunk.embedding.cosine_distance(query)
    rows = db_session.execute(
        select(DocumentChunk.content, distance.label("distance"))
        .where(DocumentChunk.user_id == alice.id)
        .order_by(distance)
        .limit(5)
    ).all()

    assert [r.content for r in rows] == ["near", "far"]
    assert rows[0].distance == pytest.approx(0.0)
    assert rows[1].distance == pytest.approx(1.0)
