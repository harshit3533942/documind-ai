"""Live document status: worker events over Redis pub/sub and the SSE endpoint."""

import asyncio
import json
import uuid

from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import get_settings
from app.database import redis_client
from app.services import document_events
from app.services.document_processing_service import mark_queued
from app.models import DocumentStatus
from app.tests import pdf_factory
from app.tests.test_document_processing import alice, processor, upload  # noqa: F401 - fixtures
from app.tests.test_streaming import parse_sse

EVENTS = "/api/v1/documents/events"


def received(pubsub) -> list[dict]:
    out = []
    while (m := pubsub.get_message(ignore_subscribe_messages=True, timeout=0)) is not None:
        if m["type"] == "message":
            out.append(json.loads(m["data"]))
    return out


def subscribe(fake_redis, user_id):
    pubsub = fake_redis.pubsub()
    pubsub.subscribe(document_events.channel(user_id))
    pubsub.get_message(timeout=0)  # the subscribe confirmation
    return pubsub


def owner_of(db_session, doc_id):
    from app.models import Document
    return db_session.get(Document, doc_id).user_id


# ------------------------------------------------------------------ publishing
def test_processing_publishes_status_and_progress(client, alice, processor, db_session, fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(document_events.ProgressReporter, "__init__",
                        _no_throttle(document_events.ProgressReporter.__init__))
    doc_id = upload(client, alice, pdf_factory.text_pdf([f"Page {i} text about policies." for i in range(1, 4)]))
    pubsub = subscribe(fake_redis, owner_of(db_session, doc_id))

    assert processor.process(doc_id).status == "completed"
    events = received(pubsub)
    assert [e["status"] for e in events] == ["processing", "processing", "processing", "processing", "completed"]
    assert events[0] | {} == {"document_id": str(doc_id), "status": "processing", "pages_done": 0, "pages_total": 3}
    assert [e["pages_done"] for e in events[1:4]] == [1, 2, 3]
    assert events[-1]["pages_done"] == events[-1]["pages_total"] == 3
    assert document_events.progress(doc_id) is None  # progress key removed when finished


def _no_throttle(original):
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.min_interval = 0
    return init


def test_progress_is_throttled(fake_redis) -> None:
    user, doc = uuid.uuid4(), uuid.uuid4()
    pubsub = subscribe(fake_redis, user)
    reporter = document_events.ProgressReporter(user, doc, 1000, min_interval=60)
    for page in range(1, 1001):
        reporter.page_done(page)
    assert len(received(pubsub)) == 1  # not 1,000 messages
    assert document_events.progress(doc)["pages_done"] == 1


def test_failure_is_published_with_message(client, alice, processor, db_session, fake_redis) -> None:
    doc_id = upload(client, alice, pdf_factory.image_only_pdf())
    pubsub = subscribe(fake_redis, owner_of(db_session, doc_id))
    assert processor.process(doc_id).status == "failed"
    last = received(pubsub)[-1]
    assert last["status"] == "failed" and "No extractable text" in last["error_message"]


def test_mark_queued_publishes_only_when_changed(client, alice, db_session, fake_redis) -> None:
    doc_id = upload(client, alice, pdf_factory.text_pdf(["hello world text"]))
    pubsub = subscribe(fake_redis, owner_of(db_session, doc_id))
    received(pubsub)
    assert mark_queued(db_session, doc_id, from_statuses=(DocumentStatus.QUEUED, DocumentStatus.UPLOADED))
    assert [e["status"] for e in received(pubsub)] == ["queued"]
    assert not mark_queued(db_session, doc_id, from_statuses=(DocumentStatus.FAILED,))
    assert received(pubsub) == []


def test_publish_survives_redis_outage(monkeypatch) -> None:
    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RedisConnectionError("down")
            return fail

    monkeypatch.setattr(redis_client, "get_redis", lambda: Down())
    document_events.publish(uuid.uuid4(), uuid.uuid4(), "queued")  # no exception
    assert document_events.progress(uuid.uuid4()) is None


# ------------------------------------------------------------------ the stream
def test_stream_sends_snapshot_then_live_events(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(document_events, "POLL_INTERVAL_SECONDS", 0.01)
    user, other = uuid.uuid4(), uuid.uuid4()

    async def run() -> list[str]:
        async def connected() -> bool:
            return False

        gen = document_events.stream_events(user, [{"document_id": "d1", "status": "queued"}], connected, max_seconds=2)
        out = [await anext(gen)]  # snapshot (the subscription exists now)
        document_events.publish(other, uuid.uuid4(), "queued")  # another user's channel: never forwarded
        document_events.publish(user, uuid.uuid4(), "completed")
        out.append(await anext(gen))
        await gen.aclose()
        return out

    snapshot, live = asyncio.run(run())
    assert snapshot.startswith("event: snapshot") and '"live": true' in snapshot
    assert live.startswith("event: document") and '"completed"' in live


def test_stream_ends_when_client_disconnects(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(document_events, "POLL_INTERVAL_SECONDS", 0.01)

    async def run() -> list[str]:
        async def gone() -> bool:
            return True

        return [e async for e in document_events.stream_events(uuid.uuid4(), [], gone, max_seconds=30)]

    assert len(asyncio.run(run())) == 1  # snapshot only, then it stops


def test_heartbeat_comment_when_idle(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(document_events, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(document_events, "HEARTBEAT_SECONDS", 0)

    async def run() -> str:
        async def connected() -> bool:
            return False

        gen = document_events.stream_events(uuid.uuid4(), [], connected, max_seconds=5)
        await anext(gen)
        beat = await anext(gen)
        await gen.aclose()
        return beat

    assert asyncio.run(run()) == ": ping\n\n"


def test_endpoint_snapshot_lists_only_own_unfinished_documents(
    client, alice, create_user, auth_headers, processor, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "document_events_max_seconds", 0.05)
    waiting = upload(client, alice, pdf_factory.text_pdf(["first document text"]))
    done = upload(client, alice, pdf_factory.text_pdf(["second document text"]))
    processor.process(done)
    create_user("bob@example.com")
    upload(client, auth_headers("bob@example.com"), pdf_factory.text_pdf(["bob document text"]))

    resp = client.get(EVENTS, headers=alice)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    [(kind, data)] = parse_sse(resp.text)
    assert kind == "snapshot" and data["live"] is True
    assert [d["document_id"] for d in data["documents"]] == [str(waiting)]
    assert client.get(EVENTS).status_code == 401


def test_endpoint_without_redis_says_not_live(client, alice, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "document_events_max_seconds", 0.05)

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RedisConnectionError("down")
            return fail

    monkeypatch.setattr(redis_client, "get_redis", lambda: Down())
    [(kind, data)] = parse_sse(client.get(EVENTS, headers=alice).text)
    assert kind == "snapshot" and data["live"] is False
