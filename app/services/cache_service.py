"""Redis caching: query embeddings and answers.

What is cached, and why it is safe
----------------------------------
* Query embeddings: embedding(text) is deterministic for a given model ->
  key includes the model name; TTL 24 h. Saves the model forward pass for
  repeated / popular questions.
* Answers: only for the FIRST question of a conversation (no history, so the
  answer depends only on question + documents). The key contains the user id
  and that user's *document version*.

Invalidation by versioning: every time a user's searchable documents change
(processing completed, document deleted or re-processed) their version counter
is incremented. New lookups use the new version, so stale answers are simply
never read again and expire via TTL. No need to find and delete keys.

Fail-open: any Redis error = cache miss. The cache can only make things faster,
never break a request.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from typing import Any

from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database import redis_client

logger = get_logger(__name__)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(metric: str) -> None:
    try:
        redis_client.get_redis().hincrby("metrics:cache", metric, 1)
    except RedisError:
        pass


def get_json(key: str) -> Any | None:
    if not get_settings().cache_enabled:
        return None
    try:
        raw = redis_client.get_redis().get(key)
    except RedisError:
        logger.warning("Cache read failed for %s", key.split(":")[0])
        return None
    return json.loads(raw) if raw is not None else None


def set_json(key: str, value: Any, ttl: int) -> None:
    if not get_settings().cache_enabled:
        return
    try:
        redis_client.get_redis().set(key, json.dumps(value, default=str), ex=ttl)
    except RedisError:
        logger.warning("Cache write failed for %s", key.split(":")[0])


# ------------------------------------------------------------------ document version
def document_version(user_id: uuid.UUID) -> int | None:
    """None if Redis is unavailable -> callers skip the answer cache entirely."""
    try:
        return int(redis_client.get_redis().get(f"docver:{user_id}") or 0)
    except RedisError:
        return None


def bump_document_version(user_id: uuid.UUID) -> None:
    """Call whenever the set of searchable chunks of `user_id` changes."""
    try:
        redis_client.get_redis().incr(f"docver:{user_id}")
    except RedisError:
        # Worst case: cached answers survive until their TTL (10 min).
        logger.warning("Could not bump document version for user %s", user_id)


# ------------------------------------------------------------------ answer cache
def answer_key(user_id: uuid.UUID, version: int, question: str, document_ids: Sequence[uuid.UUID] | None, mode_tag: str) -> str:
    normalised = " ".join(question.lower().split())
    docs = ",".join(sorted(str(d) for d in document_ids)) if document_ids else "*"
    return f"ans:{user_id}:v{version}:{_sha(f'{mode_tag}|{docs}|{normalised}')}"


def get_answer(key: str) -> dict[str, Any] | None:
    value = get_json(key)
    _record("answer_hit" if value is not None else "answer_miss")
    return value


def set_answer(key: str, value: dict[str, Any]) -> None:
    set_json(key, value, get_settings().cache_answer_ttl)


# ------------------------------------------------------------------ embedding cache
class CachedEmbedder:
    """Wraps an Embedder; caches embed_query() results in Redis. Everything
    else is delegated unchanged (document embedding is never cached: it runs
    once per chunk in the worker anyway)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    @property
    def max_tokens(self) -> int:
        return self._inner.max_tokens

    def embed_query(self, text: str) -> list[float]:
        key = f"qemb:{self._inner.model_name}:{_sha(text)}"
        cached = get_json(key)
        if cached is not None:
            _record("embedding_hit")
            return cached
        _record("embedding_miss")
        vector = self._inner.embed_query(text)
        set_json(key, vector, get_settings().cache_query_embedding_ttl)
        return vector


def cache_stats() -> dict[str, int]:
    try:
        return {k: int(v) for k, v in redis_client.get_redis().hgetall("metrics:cache").items()}
    except RedisError:
        return {}
