"""Per-user quotas: number of documents, storage, questions per day.

Limits = the user's own override (set by an admin) or the global default
from settings. 0 means "unlimited".

* Documents + storage are checked at upload, INSIDE the upload transaction,
  after locking the user's row (SELECT ... FOR UPDATE). Two uploads of the
  same user arriving at the same moment are therefore checked one after the
  other - without the lock both could see "99 of 100" and both succeed.
  Documents in the trash still count: their files still use storage until
  they are deleted for good (automatically after TRASH_RETENTION_DAYS).
* Questions per day: a Redis counter per user and UTC day (INCR + EXPIRE in
  one transaction). Fail-open like the rate limiter: if Redis is down the
  question is allowed.

Quota vs rate limit: a rate limit protects the SYSTEM from bursts (30 / min);
a quota is a product rule about how much one account may use in total.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import QuotaExceededError
from app.core.logging import get_logger
from app.database import redis_client
from app.models import Document, User

logger = get_logger(__name__)

MB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    max_documents: int       # 0 = unlimited
    max_storage_bytes: int   # 0 = unlimited
    max_questions_per_day: int  # 0 = unlimited


@dataclass(frozen=True)
class Usage:
    documents: int
    storage_bytes: int
    trashed_documents: int
    questions_today: int


def limits_for(user: User) -> Limits:
    s = get_settings()
    docs = user.quota_max_documents if user.quota_max_documents is not None else s.quota_max_documents
    storage_mb = user.quota_max_storage_mb if user.quota_max_storage_mb is not None else s.quota_max_storage_mb
    questions = (user.quota_max_questions_per_day if user.quota_max_questions_per_day is not None
                 else s.quota_max_questions_per_day)
    return Limits(docs, storage_mb * MB, questions)


def usage_for(db: Session, user: User) -> Usage:
    count, size, trashed = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Document.file_size), 0),
            func.count().filter(Document.deleted_at.is_not(None)),
        ).where(Document.user_id == user.id)
    ).one()
    return Usage(count, int(size), trashed, questions_today(user.id))


def summary(db: Session, user: User) -> dict:
    return {"limits": asdict(limits_for(user)), "usage": asdict(usage_for(db, user))}


# ------------------------------------------------------------------ uploads
def check_upload(db: Session, user: User, new_file_bytes: int) -> None:
    """Raise QuotaExceededError if one more file of this size does not fit.
    Must run inside the transaction that inserts the document."""
    limits = limits_for(user)
    if not limits.max_documents and not limits.max_storage_bytes:
        return
    # Serialise this user's concurrent uploads (row lock until commit/rollback).
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    count, size = db.execute(
        select(func.count(), func.coalesce(func.sum(Document.file_size), 0)).where(Document.user_id == user.id)
    ).one()
    if limits.max_documents and count + 1 > limits.max_documents:
        raise QuotaExceededError(
            f"Document limit reached ({limits.max_documents}). Delete documents (and empty the trash) to upload more."
        )
    if limits.max_storage_bytes and int(size) + new_file_bytes > limits.max_storage_bytes:
        raise QuotaExceededError(
            f"Storage limit reached ({limits.max_storage_bytes // MB} MB). Delete documents (and empty the trash) to free space."
        )


# ------------------------------------------------------------------ questions
def _day_key(user_id: uuid.UUID, now: datetime) -> str:
    return f"quota:questions:{user_id}:{now:%Y%m%d}"


def questions_today(user_id: uuid.UUID) -> int:
    try:
        return int(redis_client.get_redis().get(_day_key(user_id, datetime.now(UTC))) or 0)
    except RedisError:
        return 0


def consume_question(user: User) -> None:
    """Count one question; raise QuotaExceededError (429) over the daily limit."""
    limit = limits_for(user).max_questions_per_day
    if not limit:
        return
    now = datetime.now(UTC)
    key = _day_key(user.id, now)
    try:
        pipe = redis_client.get_redis().pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, 2 * 24 * 3600, nx=True)
        count = int(pipe.execute()[0])
    except RedisError:
        logger.warning("Question quota unavailable (Redis error); allowing the question")
        return
    if count > limit:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise QuotaExceededError(
            f"Daily question limit reached ({limit}). It resets at midnight UTC.",
            headers={"Retry-After": str(int((tomorrow - now).total_seconds()))},
            status_code=429,
        )
