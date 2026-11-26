"""Retrieval over a user's document chunks: semantic, keyword and hybrid.

Two retrievers
--------------
* Vector (semantic): pgvector cosine distance on the chunk embeddings. Finds
  chunks with the same MEANING ("time off" ~ "vacation days").
* Keyword (lexical): PostgreSQL full-text search on `content_tsv`. Finds
  chunks with the same WORDS - strong where embeddings are weak: codes
  ("ERR-4012"), names, numbers, section titles.

Hybrid = both, merged with Reciprocal Rank Fusion (RRF):
    rrf(chunk) = sum over retrievers of 1 / (k + rank_in_that_retriever)
RRF only uses RANKS, so the two very different score scales (cosine
similarity vs ts_rank) never have to be normalised against each other. A
chunk that is near the top of both lists wins.

Safety threshold: every candidate - also a keyword-only hit - must still
reach RAG_MIN_SCORE cosine similarity. Hybrid search changes the ORDER of
relevant chunks; it never lets an unrelated chunk in just because it shares a
word with the question.

Optional second stage: a cross-encoder re-ranks the candidates (see
rerank_service). `retrieve()` runs the full pipeline.

SECURITY: `user_id` is a mandatory argument and is always in the WHERE clause.
There is no code path that searches across users.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import Text, cast, func, literal_column, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentChunk, DocumentStatus
from app.models.document_chunk import FTS_CONFIG
from app.services.embedding_service import Embedder

_REGCONFIG = literal_column(f"'{FTS_CONFIG}'::regconfig")


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
    # Diagnostics (shown by scripts/evaluate_rag.py, never needed for correctness)
    vector_rank: int | None = None    # 1-based position in the semantic list
    keyword_rank: int | None = None   # 1-based position in the full-text list
    fused_score: float | None = None  # RRF score (hybrid mode)
    rerank_score: float | None = None  # cross-encoder score (if re-ranking ran)


class Reranker(Protocol):
    """Relevance score for each (query, passage) pair; higher = better."""

    @property
    def model_name(self) -> str: ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


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


def keyword_query(question: str):  # noqa: ANN201 - SQL expression
    """OR-query of the question's stemmed words.

    plainto_tsquery() safely parses ANY user text (no syntax errors, no
    injection) and removes stop words, but joins words with AND - a natural
    question ("how many vacation days do I get") would then only match chunks
    containing every word. Replacing & with | makes it an OR query; ts_rank_cd
    then ranks chunks that match more (and rarer, closer) words higher.
    """
    and_query = cast(func.plainto_tsquery(_REGCONFIG, question), Text)
    return func.to_tsquery(_REGCONFIG, func.replace(and_query, "&", "|"))


def _to_chunk(row, **extra) -> RetrievedChunk:  # noqa: ANN001
    return RetrievedChunk(
        chunk_id=row.id,
        document_id=row.document_id,
        filename=row.filename,
        chunk_index=row.chunk_index,
        content=row.content,
        page_start=row.page_start,
        page_end=row.page_end,
        score=1.0 - float(row.distance),
        page_offsets=tuple(tuple(o) for o in (row.page_offsets or ())),
        **extra,
    )


def search_chunks(
    db: Session,
    embedder: Embedder,
    *,
    user_id: uuid.UUID,
    query: str,
    top_k: int = 5,
    document_ids: Sequence[uuid.UUID] | None = None,
    min_score: float | None = None,
    mode: str | None = None,
    pool: int | None = None,
) -> list[RetrievedChunk]:
    """Top `top_k` chunks for `query`. mode: "vector" | "hybrid" (default: settings)."""
    settings = get_settings()
    mode = mode or settings.rag_retrieval_mode
    pool = max(pool or settings.rag_candidate_pool, top_k)

    _tune_hnsw(db)
    query_vector = embedder.embed_query(query)
    # <=> is pgvector's cosine DISTANCE (0 = identical direction, 2 = opposite).
    distance = DocumentChunk.embedding.cosine_distance(query_vector)

    conditions = [
        DocumentChunk.user_id == user_id,            # data isolation
        Document.status == DocumentStatus.COMPLETED,  # never half-processed docs
        # Vectors from different models live in different "spaces";
        # comparing them would return garbage.
        Document.embedding_model == embedder.model_name,
        DocumentChunk.embedding.is_not(None),
    ]
    if document_ids:
        conditions.append(DocumentChunk.document_id.in_(list(document_ids)))

    base = (
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
        .where(*conditions)
    )

    # --- semantic candidates
    limit = top_k if mode == "vector" else pool
    vector_hits = [_to_chunk(row) for row in db.execute(base.order_by(distance).limit(limit))]
    # relaxed_order may return rows slightly out of order -> sort exactly here.
    vector_hits.sort(key=lambda r: r.score, reverse=True)

    if mode == "vector":
        results = [replace(c, vector_rank=i) for i, c in enumerate(vector_hits, start=1)]
        return [r for r in results if min_score is None or r.score >= min_score]

    # --- keyword candidates
    tsquery = keyword_query(query)
    rank = func.ts_rank_cd(DocumentChunk.content_tsv, tsquery)
    keyword_stmt = (
        base.where(DocumentChunk.content_tsv.op("@@")(tsquery))
        .order_by(rank.desc(), DocumentChunk.id)
        .limit(pool)
    )
    keyword_hits = [_to_chunk(row) for row in db.execute(keyword_stmt)]

    # --- Reciprocal Rank Fusion
    k = settings.rag_rrf_k
    by_id: dict[uuid.UUID, RetrievedChunk] = {}
    fused: dict[uuid.UUID, float] = {}
    ranks: dict[uuid.UUID, dict[str, int]] = {}
    for field_name, hits in (("vector_rank", vector_hits), ("keyword_rank", keyword_hits)):
        for position, chunk in enumerate(hits, start=1):
            by_id.setdefault(chunk.chunk_id, chunk)
            fused[chunk.chunk_id] = fused.get(chunk.chunk_id, 0.0) + 1.0 / (k + position)
            ranks.setdefault(chunk.chunk_id, {})[field_name] = position

    results = [
        replace(by_id[cid], fused_score=round(fused[cid], 6), **ranks[cid])
        for cid in by_id
        if min_score is None or by_id[cid].score >= min_score
    ]
    # Tie-break by cosine similarity, then id (deterministic order).
    results.sort(key=lambda r: (r.fused_score, r.score, str(r.chunk_id)), reverse=True)
    return results[:top_k]


def rerank(
    query: str, chunks: list[RetrievedChunk], reranker: Reranker, top_k: int, min_score: float | None = None
) -> list[RetrievedChunk]:
    """Second stage: re-order candidates by cross-encoder relevance and drop
    those below `min_score`."""
    if not chunks:
        return []
    scores = reranker.score(query, [c.content for c in chunks])
    rescored = [replace(c, rerank_score=round(float(s), 4)) for c, s in zip(chunks, scores, strict=True)]
    if min_score is not None:
        rescored = [c for c in rescored if c.rerank_score >= min_score]
    rescored.sort(key=lambda c: c.rerank_score, reverse=True)
    return rescored[:top_k]


def retrieve(
    db: Session,
    embedder: Embedder,
    *,
    user_id: uuid.UUID,
    query: str,
    top_k: int,
    document_ids: Sequence[uuid.UUID] | None = None,
    min_score: float | None = None,
    reranker: Reranker | None = None,
    mode: str | None = None,
) -> list[RetrievedChunk]:
    """Full retrieval pipeline used by RAG: search (+ optional re-rank).

    Without a re-ranker: top_k chunks, cosine `min_score` is the relevance gate.
    With a re-ranker: a larger candidate set (RAG_CANDIDATE_POOL) is fetched
    WITHOUT the cosine gate, the cross-encoder orders it, and its own
    threshold (RAG_RERANK_MIN_SCORE) decides what is relevant. That lets it
    promote a chunk the first stage ranked e.g. 12th, or one that only the
    keyword search found."""
    settings = get_settings()
    if reranker is None:
        return search_chunks(
            db, embedder, user_id=user_id, query=query, top_k=top_k,
            document_ids=document_ids, min_score=min_score, mode=mode,
        )
    candidates = search_chunks(
        db, embedder, user_id=user_id, query=query, top_k=max(settings.rag_candidate_pool, top_k),
        document_ids=document_ids, min_score=None, mode=mode,
    )
    return rerank(query, candidates, reranker, top_k, settings.rag_rerank_min_score)
