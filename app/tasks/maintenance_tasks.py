"""Periodic housekeeping tasks (Celery beat)."""

from app.core.logging import get_logger
from app.database.connection import SessionLocal
from app.services.auth_service import purge_expired_refresh_tokens
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)

_session_factory = SessionLocal  # tests swap in the test database


@celery_app.task(name="maintenance.purge_refresh_tokens")
def purge_refresh_tokens_task() -> int:
    """Delete refresh tokens that expired more than a day ago."""
    db = _session_factory()
    try:
        deleted = purge_expired_refresh_tokens(db)
    finally:
        db.close()
    if deleted:
        logger.info("Purged %d expired refresh token(s)", deleted)
    return deleted
