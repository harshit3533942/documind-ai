"""Live document status for the web app (Server-Sent Events over Redis pub/sub).

    worker --PUBLISH docs:<user_id>--> Redis --SUBSCRIBE--> API --SSE--> browser

* The worker publishes every status change (queued / processing / completed /
  failed) and, while processing, progress ("page 12 of 40"). The latest progress
  is also stored in a short-lived key, so a browser that connects mid-way sees it.
* The API endpoint subscribes to the user's channel only (per-user isolation),
  first sends a snapshot of the user's unfinished documents, then forwards
  events as they arrive.

Pub/sub is fire-and-forget: a message published while nobody listens, or during
a reconnect, is lost. That is fine here because (1) every new connection starts
with a snapshot from the database and (2) the web app keeps a slow fallback poll.
Everything is best effort: if Redis is down, processing still works and the
web app simply falls back to polling.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.core.sse import sse_event
from app.database import redis_client

logger = get_logger(__name__)

PROGRESS_TTL_SECONDS = 3600
HEARTBEAT_SECONDS = 15
POLL_INTERVAL_SECONDS = 0.5


def channel(user_id: uuid.UUID) -> str:
    return f"docs:{user_id}"


def _progress_key(document_id: uuid.UUID) -> str:
    return f"docprogress:{document_id}"


def publish(
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    status: str,
    *,
    pages_done: int | None = None,
    pages_total: int | None = None,
    error_message: str | None = None,
) -> None:
    event: dict[str, Any] = {"document_id": str(document_id), "status": status}
    if pages_total:
        event |= {"pages_done": pages_done or 0, "pages_total": pages_total}
    if error_message:
        event["error_message"] = error_message
    try:
        r = redis_client.get_redis()
        if status == "processing" and pages_total:
            r.set(_progress_key(document_id), json.dumps(event), ex=PROGRESS_TTL_SECONDS)
        elif status != "processing":
            r.delete(_progress_key(document_id))
        r.publish(channel(user_id), json.dumps(event))
    except RedisError:
        logger.debug("Could not publish document event for %s", document_id)


def progress(document_id: uuid.UUID) -> dict[str, Any] | None:
    try:
        raw = redis_client.get_redis().get(_progress_key(document_id))
    except RedisError:
        return None
    return json.loads(raw) if raw else None


class ProgressReporter:
    """Publishes "page x of y" while a document is processed, at most every
    `min_interval` seconds (a 1,000-page PDF must not send 1,000 messages)."""

    def __init__(self, user_id: uuid.UUID, document_id: uuid.UUID, pages_total: int | None,
                 *, min_interval: float = 0.5) -> None:
        self.user_id, self.document_id, self.pages_total = user_id, document_id, pages_total
        self.min_interval = min_interval
        self._last = 0.0

    def page_done(self, page_number: int) -> None:
        now = time.monotonic()
        if not self.pages_total or now - self._last < self.min_interval:
            return
        self._last = now
        publish(self.user_id, self.document_id, "processing",
                pages_done=min(page_number, self.pages_total), pages_total=self.pages_total)


async def stream_events(
    user_id: uuid.UUID,
    snapshot: list[dict[str, Any]],
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    max_seconds: float,
) -> AsyncIterator[str]:
    """SSE body: `snapshot` events first, then live `document` events.

    Runs in the event loop. Redis calls are short, but they go through the thread
    pool so a slow Redis can never block other requests. After `max_seconds` the
    stream ends and the browser reconnects (bounded lifetime: no connection lives
    forever, and auth is re-checked on every reconnect)."""
    pubsub = None
    try:
        pubsub = await run_in_threadpool(_subscribe, user_id)
    except RedisError:
        logger.warning("Document events: Redis unavailable, sending snapshot only")

    try:
        yield sse_event("snapshot", {"documents": snapshot, "live": pubsub is not None})
        started = last_beat = time.monotonic()
        while time.monotonic() - started < max_seconds:
            if await is_disconnected():
                break
            message = None
            if pubsub is not None:
                try:
                    message = await run_in_threadpool(pubsub.get_message, ignore_subscribe_messages=True, timeout=0)
                except RedisError:
                    break  # the client reconnects and gets a fresh snapshot
            if message and message.get("type") == "message":
                yield sse_event("document", json.loads(message["data"]))
                continue  # drain queued messages without sleeping
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                last_beat = time.monotonic()
                yield ": ping\n\n"  # SSE comment: keeps proxies from closing an idle connection
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        if pubsub is not None:
            try:
                await run_in_threadpool(pubsub.close)
            except RedisError:
                pass


def _subscribe(user_id: uuid.UUID):  # noqa: ANN202 - redis PubSub
    pubsub = redis_client.get_redis().pubsub()
    pubsub.subscribe(channel(user_id))
    return pubsub
