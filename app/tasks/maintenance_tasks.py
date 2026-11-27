"""Periodic housekeeping tasks (Celery beat)."""

from app.core.dependencies import get_storage
from app.core.logging import get_logger
from app.database.connection import SessionLocal
from app.services.auth_service import purge_expired_refresh_tokens, purge_expired_user_tokens
from app.services.document_service import purge_trash
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)

_session_factory = SessionLocal  # tests swap in the test database


@celery_app.task(name="maintenance.purge_refresh_tokens")
def purge_refresh_tokens_task() -> int:
    """Delete refresh tokens and email (verification / reset) tokens that
    expired more than a day ago."""
    db = _session_factory()
    try:
        deleted = purge_expired_refresh_tokens(db) + purge_expired_user_tokens(db)
    finally:
        db.close()
    if deleted:
        logger.info("Purged %d expired refresh token(s)", deleted)
    return deleted


@celery_app.task(name="maintenance.purge_trash")
def purge_trash_task() -> int:
    """Delete documents that have been in the trash longer than
    TRASH_RETENTION_DAYS: rows, chunks and files."""
    db = _session_factory()
    try:
        purged = purge_trash(db, get_storage())
    finally:
        db.close()
    if purged:
        logger.info("Purged %d document(s) from the trash", purged)
    return purged
