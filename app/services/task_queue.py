"""Thin abstraction over "send a background job".

The API depends on this interface, not on Celery directly, so tests can use an
in-memory fake and the broker could be swapped without touching services.
"""

import uuid
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)


class TaskQueue(Protocol):
    def enqueue_document(self, document_id: uuid.UUID, *, force: bool = False) -> bool:
        """Return True if the job was handed to the broker."""
        ...

    def enqueue_summary(self, document_id: uuid.UUID) -> bool:
        """Summary + suggested questions of a completed document."""
        ...


class CeleryTaskQueue:
    def enqueue_document(self, document_id: uuid.UUID, *, force: bool = False) -> bool:
        from app.tasks.document_tasks import process_document_task  # avoid import cycle

        try:
            process_document_task.apply_async(
                args=[str(document_id)],
                kwargs={"force": force},
                # Fail fast if Redis is down: the upload request must not hang.
                retry=True,
                retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.5, "interval_max": 1},
            )
            return True
        except Exception:  # noqa: BLE001 - broker down: the sweeper will pick it up later
            logger.warning("Could not enqueue document %s; it will be retried by the sweeper", document_id, exc_info=True)
            return False

    def enqueue_summary(self, document_id: uuid.UUID) -> bool:
        from app.tasks.document_tasks import enqueue_summary

        return enqueue_summary(document_id)
