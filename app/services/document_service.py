"""Document upload / listing / deletion business logic.

Upload pipeline (synchronous part - must be fast):
    1. validate filename extension + declared content type
    2. stream to disk (size limit, SHA-256) under a random server-side name
    3. magic bytes + open with PyMuPDF (corrupt / encrypted / empty / too many pages)
    4. reject duplicate (same user, same content hash)
    5. insert row with status=uploaded
Text extraction, chunking and embeddings happen later in a background worker.
"""

import uuid
from pathlib import PurePosixPath
from typing import BinaryIO

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnprocessableError,
    UnsupportedMediaTypeError,
)
from app.core.logging import get_logger
from app.models import Document, DocumentChunk, DocumentStatus, User
from app.repositories.document_repository import DocumentRepository
from app.services import cache_service, pdf_service
from app.services.document_processing_service import mark_queued
from app.services.storage_service import EmptyFileError, FileTooLargeError, LocalStorage, StoredFile
from app.services.task_queue import TaskQueue

logger = get_logger(__name__)

PDF_MIME = "application/pdf"
# Some browsers/OSes send generic types for PDFs; the magic-byte check is the real gate.
ACCEPTED_CONTENT_TYPES = {PDF_MIME, "application/x-pdf", "application/octet-stream", ""}
REPROCESSABLE = (DocumentStatus.FAILED, DocumentStatus.COMPLETED)


def sanitize_filename(raw: str | None) -> str:
    """Display name only (never a path): drop directories and control characters."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if len(name) > 255:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 255 - len(ext) - 1] + dot + ext) if dot else name[:255]
    return name or "document.pdf"


class DocumentService:
    def __init__(self, db: Session, storage: LocalStorage, queue: TaskQueue | None = None) -> None:
        self.db = db
        self.storage = storage
        self.queue = queue
        self.documents = DocumentRepository(db)

    # ------------------------------------------------------------ upload
    def upload(self, user: User, *, stream: BinaryIO, filename: str | None, content_type: str | None) -> Document:
        settings = get_settings()
        display_name = sanitize_filename(filename)

        if not display_name.lower().endswith(".pdf"):
            raise UnsupportedMediaTypeError("Only PDF files are allowed")
        if (content_type or "").split(";")[0].strip().lower() not in ACCEPTED_CONTENT_TYPES:
            raise UnsupportedMediaTypeError("Only PDF files are allowed")

        try:
            stored = self.storage.save_stream(stream, user.id, settings.max_upload_size_bytes)
        except FileTooLargeError:
            raise PayloadTooLargeError(f"File is larger than {settings.max_upload_size_mb} MB") from None
        except EmptyFileError:
            raise UnprocessableError("The uploaded file is empty") from None

        # From here on, any failure must remove the file we just wrote.
        try:
            info = self._validate_pdf(stored)
            if existing := self.documents.find_by_hash(user.id, stored.sha256):
                raise ConflictError(f"You already uploaded this file as '{existing.filename}' (id {existing.id})")

            document = self.documents.add(
                Document(
                    user_id=user.id,
                    filename=display_name,
                    storage_key=stored.key,
                    file_size=stored.size,
                    mime_type=PDF_MIME,
                    file_hash=stored.sha256,
                    status=DocumentStatus.UPLOADED,
                    total_pages=info.page_count,
                )
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.storage.delete(stored.key)
            raise

        logger.info("Document uploaded id=%s user=%s pages=%d size=%d", document.id, user.id, info.page_count, stored.size)
        self._enqueue_new(document)
        return document

    def _enqueue_new(self, document: Document) -> None:
        """Hand the document to the background worker.

        The row is committed BEFORE enqueueing, so the worker can always find it.
        The uploaded -> queued change is conditional: a fast worker may already
        have moved it to `processing`, which must not be overwritten. If the
        broker is down the document stays `uploaded` and the periodic sweeper
        enqueues it later - the upload itself still succeeds.
        """
        if self.queue is None:
            return
        if self.queue.enqueue_document(document.id):
            mark_queued(self.db, document.id, from_statuses=(DocumentStatus.UPLOADED,))
        self.db.refresh(document)

    def reprocess(self, user: User, document_id: uuid.UUID) -> Document:
        """Owner-triggered retry of a failed (or re-run of a completed) document."""
        document = self.get(user, document_id)
        if not mark_queued(self.db, document.id, from_statuses=REPROCESSABLE):
            raise ConflictError(f"Document is currently '{document.status.value}' and cannot be reprocessed now")
        cache_service.bump_document_version(user.id)  # a completed doc leaves search while re-processing
        if self.queue is not None:
            self.queue.enqueue_document(document.id)  # if this fails the sweeper retries
        self.db.refresh(document)
        logger.info("Document %s queued for reprocessing by user %s", document.id, user.id)
        return document

    @staticmethod
    def _validate_pdf(stored: StoredFile) -> pdf_service.PdfInfo:
        try:
            return pdf_service.inspect_pdf(stored.path)
        except pdf_service.NotAPdfError as exc:
            raise UnsupportedMediaTypeError(exc.message) from None
        except pdf_service.PdfError as exc:
            raise UnprocessableError(exc.message) from None

    # ------------------------------------------------------------ read
    def get(self, user: User, document_id: uuid.UUID) -> Document:
        document = self.documents.get_for_user(document_id, user.id)
        if document is None:
            # 404 (not 403) for someone else's document: do not even reveal it exists.
            raise NotFoundError("Document not found")
        return document

    def list_documents(self, user: User, *, limit: int, offset: int, status: DocumentStatus | None) -> tuple[list[Document], int]:
        return self.documents.list_for_user(user.id, limit=limit, offset=offset, status=status)

    def list_chunks(self, user: User, document_id: uuid.UUID, *, limit: int, offset: int) -> tuple[list[DocumentChunk], int]:
        self.get(user, document_id)  # 404 if not the owner
        return self.documents.list_chunks(document_id, user.id, limit=limit, offset=offset)

    # ------------------------------------------------------------ delete
    def delete(self, user: User, document_id: uuid.UUID) -> None:
        document = self.get(user, document_id)
        key = document.storage_key
        self.documents.delete(document)
        # Commit the DB delete FIRST. If the file delete then fails we only leave
        # an orphan file (harmless, can be swept later). The reverse order could
        # leave a DB row pointing at a missing file.
        self.db.commit()
        cache_service.bump_document_version(user.id)  # its chunks are gone from search
        try:
            self.storage.delete(key)
        except OSError:
            logger.exception("Document %s deleted but file %s could not be removed", document_id, key)
        logger.info("Document deleted id=%s user=%s", document_id, user.id)
