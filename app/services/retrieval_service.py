"""Semantic search over a user's document chunks (pgvector cosine distance).

Phase 6 provides the core similarity search; Phase 8 builds RAG on top
(score thresholds, context building, citations).

SECURITY: `user_id` is a mandatory argument and is always in the WHERE clause.
There is no code path that searches across users.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentChunk, DocumentStatus
from app.services.embedding_service import Embedder


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    page_start: int
    page_end: int
    score: float  # cosine similarity in [-1, 1]; higher = more similar
    page_offsets: tuple[tuple[int, int], ...] = ()  # exact page per character range


def _tune_hnsw(db: Session) -> None:
    """Per-transaction HNSW settings (SET LOCAL resets at commit/rollback).

    * ef_search: candidate list size - higher = better recall, slower.
    * iterative_scan (pgvector >= 0.8): the index returns the nearest vectors
      globally and the `user_id` filter is applied afterwards. With many users,
      the first ef_search candidates may contain none of *this* user's chunks
      and the search would return too few rows. Iterative scan keeps walking
      the graph until enough rows pass the filter.
    SET cannot take bind parameters; the value is a validated int from settings.
    """
    ef_search = int(get_settings().rag_hnsw_ef_search)
    db.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))


def search_chunks(
    db: Session,
    embedder: Embedder,
    *,
    user_id: uuid.UUID,
    query: str,
    top_k: int = 5,
    document_ids: Sequence[uuid.UUID] | None = None,
    min_score: float | None = None,
) -> list[RetrievedChunk]:
    _tune_hnsw(db)
    query_vector = embedder.embed_query(query)
    # <=> is pgvector's cosine DISTANCE (0 = identical direction, 2 = opposite).
    distance = DocumentChunk.embedding.cosine_distance(query_vector)

    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            Document.filename,
            DocumentChunk.chunk_index,
            DocumentChunk.content,
            DocumentChunk.page_start,
            DocumentChunk.page_end,
            DocumentChunk.page_offsets,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.user_id == user_id,            # data isolation
            Document.status == DocumentStatus.COMPLETED,  # never half-processed docs
            # Vectors from different models live in different "spaces";
            # comparing them would return garbage.
            Document.embedding_model == embedder.model_name,
            DocumentChunk.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(top_k)
    )
    if document_ids:
        stmt = stmt.where(DocumentChunk.document_id.in_(list(document_ids)))

    results = [
        RetrievedChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            filename=row.filename,
            chunk_index=row.chunk_index,
            content=row.content,
            page_start=row.page_start,
            page_end=row.page_end,
            score=1.0 - float(row.distance),
            page_offsets=tuple(tuple(o) for o in (row.page_offsets or ())),
        )
        for row in db.execute(stmt)
    ]
    # relaxed_order may return rows slightly out of order -> sort exactly here.
    results.sort(key=lambda r: r.score, reverse=True)
    if min_score is not None:
        results = [r for r in results if r.score >= min_score]
    return results
