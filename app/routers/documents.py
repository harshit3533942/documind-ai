"""Document endpoints. Every route requires a logged-in user, and every
lookup is scoped to that user inside the service/repository layer."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.core.dependencies import CurrentUser, DbSession, Queue, Storage
from app.core.rate_limit import limit_by_user
from app.models import DocumentStatus
from app.schemas.common import Page
from app.schemas.document import DocumentChunkRead, DocumentRead, DocumentStatusRead
from app.services.document_service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "", response_model=DocumentRead, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_user("upload", "rate_limit_upload_per_user", 3600))],
)
def upload_document(
    user: CurrentUser,
    db: DbSession,
    storage: Storage,
    queue: Queue,
    file: Annotated[UploadFile, File(description="PDF file")],
) -> DocumentRead:
    """Stores the PDF and queues it for background processing. Poll
    GET /documents/{id}/status until `completed` or `failed`."""
    # file.file is a SpooledTemporaryFile: Starlette keeps small uploads in
    # memory and spills large ones to disk, so big PDFs never sit in RAM.
    document = DocumentService(db, storage, queue).upload(
        user, stream=file.file, filename=file.filename, content_type=file.content_type
    )
    return DocumentRead.model_validate(document)


@router.post("/{document_id}/reprocess", response_model=DocumentStatusRead, status_code=status.HTTP_202_ACCEPTED)
def reprocess_document(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage, queue: Queue
) -> DocumentStatusRead:
    """Retry a failed document (or re-run a completed one). 202 = accepted, work happens in the background."""
    return DocumentStatusRead.model_validate(DocumentService(db, storage, queue).reprocess(user, document_id))


@router.get("", response_model=Page[DocumentRead])
def list_documents(
    user: CurrentUser,
    db: DbSession,
    storage: Storage,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> Page[DocumentRead]:
    items, total = DocumentService(db, storage).list_documents(user, limit=limit, offset=offset, status=status_filter)
    return Page[DocumentRead](
        items=[DocumentRead.model_validate(d) for d in items], total=total, limit=limit, offset=offset
    )


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage) -> DocumentRead:
    return DocumentRead.model_validate(DocumentService(db, storage).get(user, document_id))


@router.get("/{document_id}/status", response_model=DocumentStatusRead)
def get_document_status(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> DocumentStatusRead:
    return DocumentStatusRead.model_validate(DocumentService(db, storage).get(user, document_id))


@router.get("/{document_id}/chunks", response_model=Page[DocumentChunkRead])
def list_document_chunks(
    document_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    storage: Storage,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DocumentChunkRead]:
    items, total = DocumentService(db, storage).list_chunks(user, document_id, limit=limit, offset=offset)
    return Page[DocumentChunkRead](
        items=[DocumentChunkRead.model_validate(c) for c in items], total=total, limit=limit, offset=offset
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage) -> Response:
    DocumentService(db, storage).delete(user, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
