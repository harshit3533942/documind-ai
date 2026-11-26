"""Database engine, connection pool and session management.

We use the *synchronous* SQLAlchemy engine (psycopg 3 driver) because:
  * Celery workers are synchronous - one engine/session style for API + workers.
  * FastAPI runs plain `def` endpoints in a thread pool, so sync DB calls do
    not block the event loop.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

# The engine owns a *connection pool*: opening a new TCP connection + auth for
# every query is slow, so connections are reused.
engine: Engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,        # connections kept open
    max_overflow=settings.db_max_overflow,  # extra connections under burst load
    pool_timeout=settings.db_pool_timeout,  # wait this long for a free connection
    pool_pre_ping=True,                     # detect dead connections (DB restart)
    pool_recycle=1800,                      # recycle connections every 30 minutes
    echo=settings.db_echo,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed.

    Services commit explicitly; anything not committed is rolled back on close.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction for scripts / Celery tasks: commit on success, rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database_connection() -> dict[str, object]:
    """Used by the readiness probe. Returns status + whether pgvector is installed."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            pgvector = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
        return {"status": "ok", "pgvector": pgvector or "not installed"}
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        return {"status": "error", "detail": type(exc).__name__}
