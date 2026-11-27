"""Document processing pipeline: extract text -> chunk -> embed -> store.

(Phase 7 runs this inside a Celery worker.)

Safety properties
-----------------
* Claim step: an atomic `UPDATE ... WHERE status IN (...) RETURNING` moves the
  document to `processing`. If two workers receive the same job, only one
  UPDATE matches - the other one skips. No long-held row locks.
* Idempotent write: old chunks are deleted and new chunks inserted in ONE
  transaction together with the final status. A crash half-way rolls back
  everything - we never end up with half the chunks, or duplicates.
* No duplicate embedding work: when a document is re-processed with the same
  model, chunks whose text is unchanged reuse their stored vector
  (matched by SHA-256 of the content) instead of being re-embedded.
* Streaming + batching: pages are read one at a time; chunks are embedded and
  inserted in batches (one model forward pass and one multi-row INSERT per
  batch), so a 1,000-page PDF never needs all its text or vectors in memory.
* Failures are recorded on the document (status=failed + user-safe message).
  Permanent errors (scanned PDF, missing file) are not raised; unexpected
  errors are re-raised so a task queue can retry them.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import Document, DocumentChunk, DocumentStatus
from app.services import cache_service, conversion_service, document_events, pdf_service
from app.services.chunking_service import ChunkingConfig, TextChunk, TextChunker
from app.services.embedding_service import Embedder, EmbeddingConfigError, get_embedder
from app.services.storage_service import LocalStorage, StorageError

logger = get_logger(__name__)

INSERT_BATCH_SIZE = 128  # chunks per embed + INSERT round
SPECIAL_TOKENS = 2       # [CLS] and [SEP] added by BERT-style models
CLAIMABLE = (DocumentStatus.UPLOADED, DocumentStatus.QUEUED, DocumentStatus.FAILED)
INTERNAL_ERROR_MESSAGE = "Processing failed due to an internal error. Please try again later."


class PermanentProcessingError(Exception):
    """Retrying will not help (bad input). `message` is safe to show users."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class ProcessingResult:
    document_id: uuid.UUID
    status: str  # completed | failed | skipped
    chunk_count: int = 0
    embedded_count: int = 0  # vectors computed by the model in this run
    reused_count: int = 0    # vectors reused from the previous run
    total_pages: int = 0
    empty_pages: list[int] = field(default_factory=list)
    error: str | None = None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DocumentProcessor:
    def __init__(
        self,
        db: Session,
        storage: LocalStorage,
        chunker: TextChunker | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.embedder: Embedder = embedder or get_embedder()
        # Default chunker measures chunks with the embedding model's OWN
        # tokenizer, so "180 tokens" means exactly what the model will see.
        self.chunker = chunker or TextChunker(ChunkingConfig.from_settings(), count_tokens=self.embedder.count_tokens)

    # ------------------------------------------------------------ public
    def process(self, document_id: uuid.UUID, *, force: bool = False) -> ProcessingResult:
        document = self._claim(document_id, force=force)
        if document is None:
            return ProcessingResult(document_id, status="skipped")
        owner_id = document.user_id
        pages_total = document.total_pages
        # The document is now "processing" -> excluded from search: cached answers
        # that may have cited it must not be served any more.
        cache_service.bump_document_version(owner_id)
        document_events.publish(owner_id, document_id, "processing", pages_done=0, pages_total=pages_total)

        try:
            result = self._run(document, document_events.ProgressReporter(owner_id, document_id, pages_total))
        except PermanentProcessingError as exc:
            self.db.rollback()
            self._mark_failed(document_id, exc.message)
            document_events.publish(owner_id, document_id, "failed", error_message=exc.message)
            logger.info("Document %s failed permanently: %s", document_id, exc.message)
            return ProcessingResult(document_id, status="failed", error=exc.message)
        except Exception:
            self.db.rollback()
            self._mark_failed(document_id, INTERNAL_ERROR_MESSAGE)
            document_events.publish(owner_id, document_id, "failed", error_message=INTERNAL_ERROR_MESSAGE)
            logger.exception("Document %s failed with an unexpected error", document_id)
            raise

        cache_service.bump_document_version(owner_id)  # new chunks are searchable now
        document_events.publish(owner_id, document_id, "completed",
                                pages_done=result.total_pages, pages_total=result.total_pages)
        logger.info(
            "Document %s processed: %d pages, %d chunks (%d embedded, %d reused), %d empty pages",
            document_id, result.total_pages, result.chunk_count, result.embedded_count,
            result.reused_count, len(result.empty_pages),
        )
        return result

    # ------------------------------------------------------------ steps
    def _claim(self, document_id: uuid.UUID, *, force: bool) -> Document | None:
        if force:
            claimable = Document.status.in_(tuple(DocumentStatus))
        else:
            # A document stuck in "processing" for longer than the stale window
            # belonged to a worker that crashed or was killed - take it over.
            stale_before = datetime.now(UTC) - timedelta(seconds=get_settings().processing_stale_after)
            claimable = Document.status.in_(CLAIMABLE) | (
                (Document.status == DocumentStatus.PROCESSING) & (Document.updated_at < stale_before)
            )
        claimed = self.db.execute(
            update(Document)
            .where(Document.id == document_id, claimable)
            # updated_at is refreshed here: it acts as the lease timestamp.
            .values(status=DocumentStatus.PROCESSING, error_message=None, updated_at=func.now())
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        ).scalar()
        self.db.commit()  # make "processing" visible to the API immediately
        if claimed is None:
            logger.info("Document %s not claimable (missing, completed or already processing)", document_id)
            return None
        return self.db.get(Document, document_id, populate_existing=True)

    def _check_chunk_size_fits_model(self) -> None:
        limit = self.embedder.max_tokens - SPECIAL_TOKENS
        if self.chunker.config.chunk_size > limit:
            # Otherwise the model silently truncates every long chunk and the
            # end of the chunk becomes unsearchable.
            raise EmbeddingConfigError(
                f"CHUNK_SIZE_TOKENS={self.chunker.config.chunk_size} exceeds what "
                f"{self.embedder.model_name} can embed ({limit} tokens)"
            )

    def _reusable_embeddings(self, document: Document) -> dict[str, Any]:
        """content-hash -> vector from the previous run, if made by the same model."""
        if document.embedding_model != self.embedder.model_name:
            return {}
        rows = self.db.execute(
            select(DocumentChunk.content, DocumentChunk.embedding).where(
                DocumentChunk.document_id == document.id, DocumentChunk.embedding.is_not(None)
            )
        )
        return {content_hash(content): embedding for content, embedding in rows}

    def _run(self, document: Document, progress: "document_events.ProgressReporter | None" = None) -> ProcessingResult:
        self._check_chunk_size_fits_model()
        try:
            path = self.storage.path_for(document.storage_key)
        except StorageError:
            raise PermanentProcessingError("Stored file reference is invalid") from None
        if not path.is_file():
            raise PermanentProcessingError("The uploaded file is missing from storage. Please upload it again.")

        result = ProcessingResult(document.id, status="completed")
        total_chars = 0

        def source_pages():
            if document.source_type == "pdf":
                yield from pdf_service.iter_pages(path)
                return
            # Converted DOCX/TXT/MD: the exact original text per page (sidecar),
            # not the lossy text layer of the generated PDF.
            try:
                for number, text in conversion_service.iter_sidecar_pages(path):
                    yield pdf_service.PageText(page_number=number, text=pdf_service.clean_text(text))
            except (OSError, ValueError, KeyError):
                raise PermanentProcessingError("The converted text of this document is missing. Please upload it again.") from None

        def pages():  # tracks page stats while streaming pages into the chunker
            nonlocal total_chars
            for page in source_pages():
                result.total_pages = page.page_number
                if progress is not None:
                    progress.page_done(page.page_number)
                if page.text:
                    total_chars += len(page.text)
                    yield page
                else:
                    result.empty_pages.append(page.page_number)

        reusable = self._reusable_embeddings(document)  # read BEFORE deleting old chunks

        # Same transaction from here to commit: delete old -> insert new -> status.
        self.db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))

        batch: list[TextChunk] = []
        try:
            for chunk in self.chunker.chunk(pages()):
                batch.append(chunk)
                if len(batch) >= INSERT_BATCH_SIZE:
                    self._embed_and_insert(document, batch, reusable, result)
                    batch = []
        except pdf_service.PdfError as exc:
            raise PermanentProcessingError(exc.message) from None

        if total_chars < get_settings().min_extractable_chars:
            raise PermanentProcessingError(
                "No extractable text found. The PDF may be scanned or contain only images; "
                "enable OCR to process scanned documents."
            )

        self._embed_and_insert(document, batch, reusable, result)

        document.status = DocumentStatus.COMPLETED
        document.total_pages = result.total_pages
        document.processed_at = datetime.now(UTC)
        document.error_message = None
        document.embedding_model = self.embedder.model_name
        self.db.commit()
        return result

    def _embed_and_insert(
        self, document: Document, chunks: list[TextChunk], reusable: dict[str, Any], result: ProcessingResult
    ) -> None:
        if not chunks:
            return
        hashes = [content_hash(c.content) for c in chunks]
        missing = [i for i, h in enumerate(hashes) if h not in reusable]

        vectors: dict[int, Any] = {i: reusable[h] for i, h in enumerate(hashes) if h in reusable}
        if missing:
            computed = self.embedder.embed_documents([chunks[i].content for i in missing])
            if len(computed) != len(missing) or any(len(v) != self.embedder.dimension for v in computed):
                raise EmbeddingConfigError("Embedding model returned an unexpected number or size of vectors")
            vectors.update(zip(missing, computed, strict=True))

        rows = [
            {
                "document_id": document.id,
                "user_id": document.user_id,
                "chunk_index": c.chunk_index,
                "content": c.content,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "token_count": c.token_count,
                "page_offsets": [list(o) for o in c.page_offsets],
                "embedding": vectors[i],
            }
            for i, c in enumerate(chunks)
        ]
        self.db.execute(insert(DocumentChunk), rows)  # one multi-row INSERT per batch
        result.chunk_count += len(rows)
        result.embedded_count += len(missing)
        result.reused_count += len(rows) - len(missing)

    def _mark_failed(self, document_id: uuid.UUID, message: str) -> None:
        self.db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.FAILED, error_message=message)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()


def mark_queued(db: Session, document_id: uuid.UUID, *, from_statuses: tuple[DocumentStatus, ...]) -> bool:
    """Conditional status change. Returns False if the document was in another
    state (e.g. a fast worker already moved it to processing - never overwrite that)."""
    owner_id = db.execute(
        update(Document)
        .where(Document.id == document_id, Document.status.in_(from_statuses))
        # A fresh attempt starts clean: a stale error next to "queued" would mislead users.
        .values(status=DocumentStatus.QUEUED, error_message=None, updated_at=func.now())
        .returning(Document.user_id)
        .execution_options(synchronize_session=False)
    ).scalar()
    db.commit()
    if owner_id is not None:
        document_events.publish(owner_id, document_id, "queued")
    return owner_id is not None


def stale_document_ids(db: Session, *, unqueued_after: int, limit: int = 100) -> list[uuid.UUID]:
    """Documents that should be (re-)enqueued by the periodic sweeper:
      * uploaded/queued for longer than `unqueued_after` seconds (enqueue failed,
        e.g. Redis was down during upload, or the message was lost)
      * processing for longer than PROCESSING_STALE_AFTER (worker died)
    Re-enqueueing is always safe: the claim step drops duplicates."""
    now = datetime.now(UTC)
    waiting = Document.status.in_((DocumentStatus.UPLOADED, DocumentStatus.QUEUED)) & (
        Document.updated_at < now - timedelta(seconds=unqueued_after)
    )
    abandoned = (Document.status == DocumentStatus.PROCESSING) & (
        Document.updated_at < now - timedelta(seconds=get_settings().processing_stale_after)
    )
    rows = db.scalars(select(Document.id).where(waiting | abandoned).order_by(Document.updated_at).limit(limit))
    return list(rows)


def pending_document_ids(db: Session, limit: int = 100) -> list[uuid.UUID]:
    rows = db.scalars(
        select(Document.id)
        .where(Document.status.in_((DocumentStatus.UPLOADED, DocumentStatus.QUEUED)))
        .order_by(Document.created_at)
        .limit(limit)
    )
    return list(rows)


def documents_missing_embeddings(db: Session, model_name: str, limit: int = 100) -> list[uuid.UUID]:
    """Completed documents that have chunks without vectors, or vectors from a
    different model (e.g. processed before embeddings existed, or after the
    configured model changed). These need re-processing."""
    has_null_vectors = (
        select(DocumentChunk.id)
        .where(DocumentChunk.document_id == Document.id, DocumentChunk.embedding.is_(None))
        .exists()
    )
    rows = db.scalars(
        select(Document.id)
        .where(
            Document.status == DocumentStatus.COMPLETED,
            (Document.embedding_model.is_distinct_from(model_name)) | has_null_vectors,
        )
        .order_by(Document.created_at)
        .limit(limit)
    )
    return list(rows)
