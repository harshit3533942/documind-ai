"""Celery application (broker + result backend = Redis).

Run a worker:
    Windows (dev):  celery -A app.tasks.celery_app worker --pool=solo -l info
    Linux/Docker:   celery -A app.tasks.celery_app worker -l info --concurrency=2
Run the periodic sweeper (re-enqueues stuck documents):
    celery -A app.tasks.celery_app beat -l info

Windows note: Celery's default "prefork" pool is not supported on Windows,
hence --pool=solo (one task at a time, in the main process) for local dev.
"""

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

settings = get_settings()
logger = get_logger(__name__)

celery_app = Celery(
    "documind",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=["app.tasks.document_tasks", "app.tasks.maintenance_tasks"],
)

celery_app.conf.update(
    # JSON only: never unpickle data from the broker (pickle = remote code execution risk).
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_default_queue="documents",
    # Acknowledge a message only AFTER the task finishes. If the worker crashes
    # mid-task, Redis re-delivers the message and another worker retries it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Long CPU-heavy tasks: each worker reserves only one message at a time, so
    # a busy worker does not hoard jobs that an idle worker could run.
    worker_prefetch_multiplier=1,
    task_soft_time_limit=settings.processing_soft_time_limit,
    task_time_limit=settings.processing_time_limit,
    # Redis re-delivers unacknowledged messages after visibility_timeout. It MUST
    # be longer than the longest task, or a slow task gets executed twice.
    broker_transport_options={
        "visibility_timeout": settings.processing_stale_after + 300,
        "socket_connect_timeout": 3,
        "socket_timeout": 10,
    },
    broker_connection_retry_on_startup=True,
    result_expires=24 * 3600,
    task_track_started=True,
    # Recycle worker processes periodically (guards against slow memory growth
    # in ML libraries). Applies to the prefork pool on Linux.
    worker_max_tasks_per_child=200,
    worker_hijack_root_logger=False,
    beat_schedule={
        "requeue-stale-documents": {
            "task": "documents.requeue_stale",
            "schedule": 300.0,  # every 5 minutes
        },
        "purge-trash": {
            "task": "maintenance.purge_trash",
            "schedule": 6 * 3600.0,  # every 6 hours
            "options": {"queue": "documents"},
        },
        "purge-refresh-tokens": {
            "task": "maintenance.purge_refresh_tokens",
            "schedule": 24 * 3600.0,  # daily
            "options": {"queue": "documents"},
        },
    },
)


@celery_setup_logging.connect
def _configure_logging(**_: object) -> None:
    """Use the app's log format in workers instead of Celery's default."""
    setup_logging(settings.log_level)


@worker_process_init.connect
def _warm_up_embedding_model(**_: object) -> None:
    """Load the embedding model ONCE when a worker process starts, so the
    first task does not pay the multi-second model load."""
    try:
        from app.services.embedding_service import get_embedder

        get_embedder().embed_query("warm-up")
        logger.info("Embedding model loaded in worker process")
    except Exception:  # noqa: BLE001 - a task will surface the real error later
        logger.exception("Could not pre-load the embedding model")
