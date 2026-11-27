"""Document endpoints. Every route requires a logged-in user, and every
lookup is scoped to that user inside the service/repository layer."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import get_settings
from app.core.sse import sse_response
from app.services import document_events

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


@router.get(
    "/events", response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}},
                     "description": "Server-Sent Events: `snapshot`, then `document` events"}},
)
def document_status_events(request: Request, user: CurrentUser, db: DbSession, storage: Storage) -> StreamingResponse:
    """Live processing status of YOUR documents (replaces polling).

    * `snapshot` - `{documents: [{document_id, status, pages_done?, pages_total?}], live}`:
      your unfinished documents right now (sent first, also after every reconnect)
    * `document` - one status change or progress update, same fields + `error_message`

    The stream ends after DOCUMENT_EVENTS_MAX_SECONDS; reconnect to continue.
    `live: false` = Redis is unavailable, poll instead.
    """
    # The snapshot is read here, before streaming starts: the DB session is not
    # used (or kept open) for the lifetime of the stream.
    active = DocumentService(db, storage).active_documents(user)
    snapshot = []
    for d in active:
        item = {"document_id": str(d.id), "status": d.status.value}
        if d.status == DocumentStatus.PROCESSING and (p := document_events.progress(d.id)):
            item |= {"pages_done": p.get("pages_done"), "pages_total": p.get("pages_total")}
        snapshot.append(item)
    return sse_response(document_events.stream_events(
        user.id, snapshot, request.is_disconnected, max_seconds=get_settings().document_events_max_seconds,
    ))


@router.post("/{document_id}/reprocess", response_model=DocumentStatusRead, status_code=status.HTTP_202_ACCEPTED)
def reprocess_document(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage, queue: Queue
) -> DocumentStatusRead:
    """Retry a failed document (or re-run a completed one). 202 = accepted, work happens in the background."""
    return DocumentStatusRead.model_validate(DocumentService(db, storage, queue).reprocess(user, document_id))


@router.post(
    "/{document_id}/summary", response_model=DocumentRead, status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_user("summary", "rate_limit_upload_per_user", 3600))],
)
def request_document_summary(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage, queue: Queue
) -> DocumentRead:
    """(Re-)generate the summary and suggested questions of a processed document.
    They are created automatically after processing; this is for older documents
    or a fresh version. 202: it happens in the background (watch /documents/events)."""
    return DocumentRead.model_validate(DocumentService(db, storage, queue).request_summary(user, document_id))


@router.get("/trash", response_model=Page[DocumentRead])
def list_trash(
    user: CurrentUser, db: DbSession, storage: Storage,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DocumentRead]:
    """Your deleted documents, most recently deleted first. They are purged
    automatically TRASH_RETENTION_DAYS after deletion."""
    items, total = DocumentService(db, storage).list_trash(user, limit=limit, offset=offset)
    return Page[DocumentRead](items=[DocumentRead.model_validate(d) for d in items], total=total, limit=limit, offset=offset)


@router.post("/{document_id}/restore", response_model=DocumentRead)
def restore_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage) -> DocumentRead:
    """Move a document back from the trash; it is searchable again immediately."""
    return DocumentRead.model_validate(DocumentService(db, storage).restore(user, document_id))


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


@router.get(
    "/{document_id}/file", response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}, "description": "The original uploaded file"}},
)
def download_document_file(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> FileResponse:
    """The original file, for the in-app viewer (opened at a cited page).
    `inline` so the browser displays it instead of downloading; `private`
    caching because it is user data."""
    document, path = DocumentService(db, storage).file_for(user, document_id)
    return FileResponse(
        path, media_type=document.mime_type, filename=document.filename,
        content_disposition_type="inline", headers={"Cache-Control": "private, max-age=300"},
    )


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
def delete_document(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage,
    permanent: Annotated[bool, Query(description="true = delete for good (also for a document in the trash)")] = False,
) -> Response:
    """Move to the trash (default; restorable) or, with `permanent=true`,
    delete the file, its text and embeddings for good."""
    service = DocumentService(db, storage)
    if permanent:
        service.delete(user, document_id)
    else:
        service.move_to_trash(user, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
