"""Redis caching: query embeddings and answers.

What is cached, and why it is safe
----------------------------------
* Query embeddings: embedding(text) is deterministic for a given model ->
  key includes the model name; TTL 24 h. Saves the model forward pass for
  repeated / popular questions.
* Answers: only for the FIRST question of a conversation (no history, so the
  answer depends only on question + documents). The key contains the user id
  and that user's *document version*.
* Semantic answer cache: when the exact question is not cached, compare the
  question's embedding with the embeddings of earlier questions (same user,
  same documents, same version). Similar enough (cosine >= threshold) AND the
  same numbers and negations -> reuse that answer. See semantic_lookup().

Invalidation by versioning: every time a user's searchable documents change
(processing completed, document deleted or re-processed) their version counter
is incremented. New lookups use the new version, so stale answers are simply
never read again and expire via TTL. No need to find and delete keys.

Fail-open: any Redis error = cache miss. The cache can only make things faster,
never break a request.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

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


def _scope(document_ids: Sequence[uuid.UUID] | None, mode_tag: str) -> str:
    docs = ",".join(sorted(str(d) for d in document_ids)) if document_ids else "*"
    return _sha(f"{mode_tag}|{docs}")


def get_answer(key: str) -> dict[str, Any] | None:
    value = get_json(key)
    _record("answer_hit" if value is not None else "answer_miss")
    return value


def set_answer(key: str, value: dict[str, Any]) -> None:
    set_json(key, value, get_settings().cache_answer_ttl)


# ------------------------------------------------------------------ semantic answer cache
# Calibration with all-MiniLM-L6-v2 (cosine similarity of question embeddings):
#   same meaning:  "How many vacation days do I get?" / "How many days of vacation do I get?"  0.98
#                  "How many days of paid annual leave?" / "How many paid annual leave days?"   0.96
#   DIFFERENT:     "What was the revenue in 2023?" / "... in 2024?"                             0.93 (!)
#                  "meal allowance for domestic travel" / "... international travel"             0.92 (!)
#                  "sick leave days" / "annual leave days"                                       0.78
# Embeddings barely notice a changed number or a single swapped word, so:
#   * a high threshold (0.95): a miss only costs one normal answer, a false hit
#     gives a confident WRONG answer - be conservative;
#   * numbers and negations must match exactly (embeddings are weak at both).
_NUMBERS = re.compile(r"\d+(?:[.,]\d+)*")
_NEGATION = re.compile(r"\b(?:not|no|never|without|except|neither|nor)\b|n't", re.I)


@dataclass(frozen=True)
class SemanticMatch:
    answer_key: str
    similarity: float
    question: str


def semantic_key(user_id: uuid.UUID, version: int, document_ids: Sequence[uuid.UUID] | None, mode_tag: str) -> str:
    """One list per user + document version + document selection + mode."""
    return f"sem:{user_id}:v{version}:{_scope(document_ids, mode_tag)}"


def same_facts(a: str, b: str) -> bool:
    """Guards the embeddings cannot be trusted with: numbers and negation."""
    return (set(_NUMBERS.findall(a)) == set(_NUMBERS.findall(b))
            and bool(_NEGATION.search(a)) == bool(_NEGATION.search(b)))


def _unit(vector: Sequence[float]) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def semantic_lookup(key: str, question: str, vector: Sequence[float]) -> SemanticMatch | None:
    """Most similar earlier question in this scope, if similar enough."""
    s = get_settings()
    if not (s.cache_enabled and s.semantic_cache_enabled):
        return None
    try:
        raw = redis_client.get_redis().lrange(key, 0, -1)
    except RedisError:
        return None
    if not raw:
        return None
    entries = [json.loads(r) for r in raw]
    matrix = np.asarray([e["v"] for e in entries], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    similarities = matrix @ _unit(vector)  # cosine similarity with every cached question at once
    for i in np.argsort(-similarities):
        similarity = float(similarities[i])
        if similarity < s.semantic_cache_threshold:
            break
        if same_facts(question, entries[i]["q"]):
            return SemanticMatch(entries[i]["k"], round(similarity, 4), entries[i]["q"])
    return None


def semantic_store(key: str, question: str, vector: Sequence[float], answer_key: str) -> None:
    s = get_settings()
    if not (s.cache_enabled and s.semantic_cache_enabled):
        return
    entry = json.dumps({"q": question, "k": answer_key, "v": [round(float(x), 5) for x in vector]})
    try:
        pipe = redis_client.get_redis().pipeline(transaction=True)
        pipe.lpush(key, entry)                                  # newest first
        pipe.ltrim(key, 0, s.semantic_cache_max_entries - 1)     # bounded: the scan stays cheap
        pipe.expire(key, s.cache_answer_ttl)
        pipe.execute()
    except RedisError:
        logger.warning("Semantic cache write failed")


def record(metric: str) -> None:
    _record(metric)


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
