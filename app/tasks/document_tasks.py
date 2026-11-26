"""Celery tasks for document processing.

Retry policy
------------
* Permanent problems (scanned PDF, missing file) are handled inside the
  processor: status=failed with a clear message, NO retry.
* Configuration errors and timeouts: status=failed, NO retry (retrying cannot
  fix a wrong setting, and a document that timed out will time out again).
* Anything else (DB hiccup, out-of-memory, network): retried with exponential
  backoff (10s, 20s, 40s...). While waiting the document shows `queued`.
  After the last attempt it stays `failed` and the error is logged.

Every attempt is safe to repeat: the processor's claim step drops duplicate
deliveries and its single-transaction write never duplicates chunks.
"""

import uuid
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from app.core.config import get_settings
from app.core.dependencies import get_storage
from app.core.logging import get_logger
from app.database.connection import SessionLocal
from app.models import Document, DocumentStatus
from app.services.document_processing_service import DocumentProcessor, mark_queued, stale_document_ids
from app.services.embedding_service import EmbeddingConfigError, get_embedder
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)
settings = get_settings()

TIMEOUT_MESSAGE = "Processing took too long and was stopped. Try a smaller document."
CONFIG_ERROR_MESSAGE = "Processing failed due to a server configuration problem."
UNQUEUED_GRACE_SECONDS = 120

# Indirection so tests can swap in the test database / fake embedder.
_session_factory = SessionLocal
_storage_factory = get_storage
_embedder_factory = get_embedder


def _set_failed(document_id: uuid.UUID, message: str) -> None:
    db = _session_factory()
    try:
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.FAILED, error_message=message)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    finally:
        db.close()


@celery_app.task(bind=True, name="documents.process", max_retries=settings.processing_max_retries)
def process_document_task(self: Any, document_id: str, force: bool = False) -> dict[str, Any]:
    doc_id = uuid.UUID(document_id)
    attempt = self.request.retries + 1
    logger.info("Task %s: processing document %s (attempt %d)", self.request.id, doc_id, attempt)

    db = _session_factory()
    try:
        processor = DocumentProcessor(db, _storage_factory(), embedder=_embedder_factory())
        result = processor.process(doc_id, force=force)
    except SoftTimeLimitExceeded:
        db.rollback()
        _set_failed(doc_id, TIMEOUT_MESSAGE)
        logger.error("Task %s: document %s exceeded the time limit", self.request.id, doc_id)
        return {"document_id": document_id, "status": "failed", "error": TIMEOUT_MESSAGE}
    except EmbeddingConfigError:
        db.rollback()
        _set_failed(doc_id, CONFIG_ERROR_MESSAGE)
        logger.exception("Task %s: configuration error for document %s", self.request.id, doc_id)
        return {"document_id": document_id, "status": "failed", "error": CONFIG_ERROR_MESSAGE}
    except Exception as exc:
        db.rollback()
        if self.request.retries >= self.max_retries:
            logger.error("Task %s: giving up on document %s after %d attempts", self.request.id, doc_id, attempt)
            raise  # recorded as FAILURE in the result backend; document already `failed`
        countdown = settings.processing_retry_backoff * (2 ** self.request.retries)
        mark_queued(db, doc_id, from_statuses=(DocumentStatus.FAILED,))
        logger.warning(
            "Task %s: document %s attempt %d failed (%s); retrying in %ds",
            self.request.id, doc_id, attempt, type(exc).__name__, countdown,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()

    return {
        "document_id": document_id,
        "status": result.status,
        "chunks": result.chunk_count,
        "embedded": result.embedded_count,
        "reused": result.reused_count,
        "pages": result.total_pages,
        "error": result.error,
    }


@celery_app.task(name="documents.requeue_stale")
def requeue_stale_documents() -> int:
    """Periodic sweeper (Celery beat): re-enqueue documents that never got
    queued or whose worker died. Duplicate enqueues are harmless."""
    db = _session_factory()
    try:
        ids = stale_document_ids(db, unqueued_after=UNQUEUED_GRACE_SECONDS)
    finally:
        db.close()
    for doc_id in ids:
        process_document_task.apply_async(args=[str(doc_id)])
    if ids:
        logger.info("Re-enqueued %d stale document(s)", len(ids))
    return len(ids)
