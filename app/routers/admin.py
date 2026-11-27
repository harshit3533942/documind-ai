"""Admin-only endpoints. Every route depends on the admin role check.

Admins see metadata for monitoring (counts, failures); they never get access
to the content of users' documents or chunks.
"""

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.dependencies import DbSession, Queue, require_roles
from app.database import redis_client
from app.models import Conversation, Message, User
from app.services import cache_service
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import DocumentStatus, UserRole
from app.repositories.document_repository import DocumentRepository
from app.repositories.user_repository import UserRepository
from app.schemas.account import QuotaSummary, QuotaUpdate
from app.schemas.admin import DocumentStats, FailedDocument
from app.schemas.common import Page
from app.schemas.document import DocumentStatusRead
from app.schemas.user import UserRead
from app.services import quota_service
from app.services.document_processing_service import mark_queued

logger = get_logger(__name__)
STARTED_AT = time.time()

# Applying the dependency at router level protects every route in this file,
# so a new admin endpoint can never accidentally be left unprotected.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_roles(UserRole.ADMIN))])

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


@router.get("/users", response_model=Page[UserRead])
def list_users(db: DbSession, limit: PageLimit = 20, offset: PageOffset = 0) -> Page[UserRead]:
    users, total = UserRepository(db).list_users(limit=limit, offset=offset)
    return Page[UserRead](items=[UserRead.model_validate(u) for u in users], total=total, limit=limit, offset=offset)


@router.get("/documents/stats", response_model=DocumentStats)
def document_stats(db: DbSession) -> DocumentStats:
    repo = DocumentRepository(db)
    return DocumentStats(by_status=repo.admin_status_counts(), **repo.admin_totals())


@router.get("/documents/failed", response_model=Page[FailedDocument])
def failed_documents(db: DbSession, limit: PageLimit = 20, offset: PageOffset = 0) -> Page[FailedDocument]:
    rows, total = DocumentRepository(db).admin_list_failed(limit=limit, offset=offset)
    items = [
        FailedDocument(
            id=d.id, filename=d.filename, user_id=d.user_id, user_email=email, status=d.status,
            error_message=d.error_message, total_pages=d.total_pages, file_size=d.file_size,
            created_at=d.created_at, updated_at=d.updated_at,
        )
        for d, email in rows
    ]
    return Page[FailedDocument](items=items, total=total, limit=limit, offset=offset)


@router.get("/metrics")
def system_metrics(db: DbSession) -> dict[str, Any]:
    """Health + capacity snapshot for the admin dashboard."""
    from app.database.connection import engine
    from app.services.embedding_service import get_embedder

    settings = get_settings()
    pool = engine.pool
    counts = db.execute(
        select(
            select(func.count()).select_from(User).scalar_subquery(),
            select(func.count()).select_from(Conversation).scalar_subquery(),
            select(func.count()).select_from(Message).scalar_subquery(),
        )
    ).one()

    redis_info: dict[str, Any] = {"status": "error"}
    queue_length = None
    rate_limited: dict[str, int] = {}
    try:
        r = redis_client.get_redis()
        r.ping()  # health = PING; INFO details are optional (some managed Redis restrict INFO)
        redis_info = {"status": "ok"}
        try:
            info = r.info()
            redis_info.update(used_memory=info.get("used_memory_human"),
                              connected_clients=info.get("connected_clients"), version=info.get("redis_version"))
        except RedisError:
            pass
        queue_length = r.llen("documents")  # Celery's default queue = a Redis list
        rate_limited = {k: int(v) for k, v in r.hgetall("metrics:rate_limited").items()}
    except RedisError:
        pass

    repo = DocumentRepository(db)
    return {
        "app": {
            "version": settings.app_version,
            "environment": settings.environment,
            "uptime_seconds": round(time.time() - STARTED_AT),
            "answer_mode": settings.resolved_llm_provider,
            "embedding_model": settings.embedding_model_name,
            "embedding_model_loaded": getattr(get_embedder(), "_model", None) is not None,
        },
        "database": {
            "pool_size": pool.size(), "connections_in_use": pool.checkedout(),
            "overflow": pool.overflow(), "status": pool.status(),
            "users": counts[0], "conversations": counts[1], "messages": counts[2],
        },
        "documents": {"by_status": repo.admin_status_counts(), **repo.admin_totals()},
        "redis": redis_info,
        "processing_queue_length": queue_length,
        "cache": cache_service.cache_stats(),
        "rate_limited_requests": rate_limited,
    }


@router.post("/documents/{document_id}/retry", response_model=DocumentStatusRead, status_code=status.HTTP_202_ACCEPTED)
def retry_failed_document(document_id: uuid.UUID, db: DbSession, queue: Queue) -> DocumentStatusRead:
    repo = DocumentRepository(db)
    document = repo.admin_get(document_id)
    if document is None:
        raise NotFoundError("Document not found")
    if not mark_queued(db, document_id, from_statuses=(DocumentStatus.FAILED,)):
        raise ConflictError("Only failed documents can be retried")
    queue.enqueue_document(document_id)
    db.refresh(document)
    logger.info("Admin retry queued for document %s", document_id)
    return DocumentStatusRead.model_validate(document)


@router.patch("/users/{user_id}/quota", response_model=QuotaSummary)
def set_user_quota(user_id: uuid.UUID, body: QuotaUpdate, db: DbSession) -> QuotaSummary:
    """Override one user's limits (null = default from settings, 0 = unlimited).
    Only the fields sent are changed."""
    user = UserRepository(db).get_by_id(user_id)
    if user is None:
        raise NotFoundError("User not found")
    fields = {"max_documents": "quota_max_documents", "max_storage_mb": "quota_max_storage_mb",
              "max_questions_per_day": "quota_max_questions_per_day"}
    for field in body.model_fields_set:
        setattr(user, fields[field], getattr(body, field))
    db.commit()
    logger.info("Admin changed quota of user %s: %s", user.id, body.model_dump(exclude_unset=True))
    return QuotaSummary(**quota_service.summary(db, user))


@router.get("/users/{user_id}/quota", response_model=QuotaSummary)
def get_user_quota(user_id: uuid.UUID, db: DbSession) -> QuotaSummary:
    user = UserRepository(db).get_by_id(user_id)
    if user is None:
        raise NotFoundError("User not found")
    return QuotaSummary(**quota_service.summary(db, user))
