"""Prometheus metrics (GET /metrics, text exposition format).

What is measured
----------------
* HTTP (this API process): requests by method / route TEMPLATE / status,
  latency histogram, requests in progress. The label is the route template
  ("/api/v1/documents/{document_id}"), never the raw path - a label per UUID
  would create millions of time series ("cardinality explosion").
* RAG: duration of each answer stage (rewrite, retrieval, generation, total)
  and answers by mode / found / cached.
* State, read at scrape time (custom collector): documents by status,
  processing-queue length, users - from PostgreSQL and Redis, cached for a
  few seconds so frequent scrapes cannot overload the database.
* Cross-process counters kept in Redis: cache hits/misses, rate-limit
  rejections and the Celery worker's processing results. The worker is a
  different process (often another container); writing its counters to
  Redis lets this one endpoint expose them, without a second exporter.

Counters/histograms live in process memory: with several API replicas, each
replica is scraped separately and Prometheus sums them (standard practice).
"""

import threading
import time
from collections.abc import Iterator
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector
from redis.exceptions import RedisError

from app.core.logging import get_logger

logger = get_logger(__name__)

REGISTRY = CollectorRegistry(auto_describe=True)
NAMESPACE = "documind"
STATE_CACHE_SECONDS = 10

# Latency buckets (seconds) chosen for this API: most calls are < 100 ms,
# answers with an LLM take seconds.
HTTP_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
RAG_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"], namespace=NAMESPACE, registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "Time to the response's first byte (streams: until headers are sent)",
    ["method", "route"], buckets=HTTP_BUCKETS, namespace=NAMESPACE, registry=REGISTRY,
)
HTTP_IN_PROGRESS = Gauge(
    "http_requests_in_progress", "Requests currently being handled", namespace=NAMESPACE, registry=REGISTRY,
)
RAG_STAGE = Histogram(
    "rag_stage_duration_seconds", "Duration of each RAG answer stage", ["stage"],
    buckets=RAG_BUCKETS, namespace=NAMESPACE, registry=REGISTRY,
)
RAG_ANSWERS = Counter(
    "rag_answers_total", "Answers produced", ["mode", "found", "cached"], namespace=NAMESPACE, registry=REGISTRY,
)

RAG_STAGES = ("rewrite", "retrieval", "generation", "cache", "total")
UNMATCHED_ROUTE = "unmatched"


def route_label(scope: dict[str, Any]) -> str:
    """Route template of the matched endpoint; 'unmatched' for 404s on unknown
    paths (scanners hitting random URLs must not create new series)."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(template, str):
        return UNMATCHED_ROUTE
    # Since FastAPI 0.13x the template of a route from include_router(prefix=...)
    # excludes that prefix ("/documents/{document_id}"). Path parameters are
    # single segments, so the segments of the real path BEFORE the template's
    # are exactly the include prefix ("/api/v1").
    actual = scope.get("path", "").rstrip("/").split("/")
    parts = template.rstrip("/").split("/")
    extra = len(actual) - len(parts)
    prefix = "/".join(actual[: extra + 1]) if extra > 0 else ""
    return f"{prefix}{template}" or "/"


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


def observe_answer(timings_ms: dict[str, float], *, mode: str, found: bool, cached: bool) -> None:
    for stage in RAG_STAGES:
        if stage in timings_ms:
            RAG_STAGE.labels(stage).observe(timings_ms[stage] / 1000)
    RAG_ANSWERS.labels(mode or "none", str(bool(found)).lower(), str(bool(cached)).lower()).inc()


# ------------------------------------------------------------------ worker counters (via Redis)
PROCESSING_KEY = "metrics:processing"


def record_processing(status: str, seconds: float, pages: int) -> None:
    """Called by the Celery worker after each document (best effort)."""
    from app.database import redis_client

    try:
        pipe = redis_client.get_redis().pipeline(transaction=False)
        pipe.hincrby(PROCESSING_KEY, f"documents:{status}", 1)
        pipe.hincrbyfloat(PROCESSING_KEY, "seconds", seconds)
        pipe.hincrby(PROCESSING_KEY, "pages", max(pages, 0))
        pipe.execute()
    except RedisError:
        logger.debug("Could not record processing metrics")


# ------------------------------------------------------------------ scrape-time state
class StateCollector(Collector):
    """Reads current state from PostgreSQL + Redis when Prometheus scrapes."""

    def describe(self) -> list:
        # Empty on purpose: otherwise registering the collector would run
        # collect() - i.e. database queries - at import time.
        return []

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: list | None = None
        self._cached_at = 0.0

    def collect(self) -> Iterator:
        with self._lock:
            if self._cached is None or time.monotonic() - self._cached_at > STATE_CACHE_SECONDS:
                self._cached = list(self._build())
                self._cached_at = time.monotonic()
            yield from self._cached

    def _build(self) -> Iterator:
        yield from _database_state()
        yield from _redis_state()


def _database_state() -> Iterator:
    from sqlalchemy import func, select

    from app.database.connection import SessionLocal, engine
    from app.models import Document, DocumentStatus, User

    up = GaugeMetricFamily(f"{NAMESPACE}_database_up", "1 if PostgreSQL answered the metrics query")
    try:
        with SessionLocal() as db:
            by_status = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
            users = db.scalar(select(func.count()).select_from(User)) or 0
    except Exception:  # noqa: BLE001 - metrics must never fail the scrape
        logger.warning("Metrics: database query failed", exc_info=True)
        up.add_metric([], 0)
        yield up
        return
    up.add_metric([], 1)
    yield up
    documents = GaugeMetricFamily(f"{NAMESPACE}_documents", "Documents by processing status", labels=["status"])
    for status in DocumentStatus:
        documents.add_metric([status.value], by_status.get(status, 0))
    yield documents
    yield GaugeMetricFamily(f"{NAMESPACE}_users", "Registered users", value=users)
    pool = GaugeMetricFamily(f"{NAMESPACE}_db_pool_connections_in_use", "Checked-out DB connections (this process)")
    pool.add_metric([], engine.pool.checkedout())
    yield pool


def _redis_state() -> Iterator:
    from app.database import redis_client

    up = GaugeMetricFamily(f"{NAMESPACE}_redis_up", "1 if Redis answered the metrics query")
    try:
        r = redis_client.get_redis()
        queue = r.llen("documents")  # Celery's queue is a Redis list
        cache = r.hgetall("metrics:cache")
        limited = r.hgetall("metrics:rate_limited")
        processing = r.hgetall(PROCESSING_KEY)
    except RedisError:
        up.add_metric([], 0)
        yield up
        return
    up.add_metric([], 1)
    yield up
    yield GaugeMetricFamily(f"{NAMESPACE}_processing_queue_length", "Documents waiting for a worker", value=queue)

    cache_events = CounterMetricFamily(f"{NAMESPACE}_cache_events", "Cache lookups by kind and result", labels=["kind", "result"])
    for field, count in sorted(cache.items()):
        kind, _, result = field.rpartition("_")  # "answer_hit" -> ("answer", "hit")
        cache_events.add_metric([kind or field, result], float(count))
    yield cache_events

    rejected = CounterMetricFamily(f"{NAMESPACE}_rate_limited_requests", "Requests rejected by rate limits", labels=["scope"])
    for scope, count in sorted(limited.items()):
        rejected.add_metric([scope], float(count))
    yield rejected

    processed = CounterMetricFamily(f"{NAMESPACE}_documents_processed", "Documents processed by the workers", labels=["status"])
    for field, count in sorted(processing.items()):
        if field.startswith("documents:"):
            processed.add_metric([field.split(":", 1)[1]], float(count))
    yield processed
    yield CounterMetricFamily(f"{NAMESPACE}_document_processing_seconds", "Total worker time spent processing",
                              value=float(processing.get("seconds", 0)))
    yield CounterMetricFamily(f"{NAMESPACE}_document_pages_processed", "Total pages processed",
                              value=float(processing.get("pages", 0)))


REGISTRY.register(StateCollector())


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
