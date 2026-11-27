"""Answer feedback (thumbs up / down) and regenerating the last answer."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.models import Message
from app.services import cache_service
from app.tests.fakes import FakeLLM, FakeStreamingLLM
from app.tests.test_chat import ask, setup_user, use_llm  # noqa: F401 - fixture import
from app.tests.test_streaming import done, stream

CONV = "/api/v1/conversations"
Q = "How many days of annual leave do employees get?"


def feedback_url(conv_id: str, msg_id: str) -> str:
    return f"{CONV}/{conv_id}/messages/{msg_id}/feedback"


# ------------------------------------------------------------------ feedback
def test_rate_answer_up_down_and_clear(client, setup_user) -> None:
    _, h, _ = setup_user()
    body = ask(client, h, Q).json()
    conv, answer = body["conversation_id"], body["assistant_message"]["id"]
    assert body["assistant_message"]["feedback"] is None

    for rating in (1, 1, -1, None):  # PUT is idempotent; null clears
        resp = client.put(feedback_url(conv, answer), headers=h, json={"rating": rating})
        assert resp.status_code == 200 and resp.json()["feedback"] == rating

    client.put(feedback_url(conv, answer), headers=h, json={"rating": -1})
    history = client.get(f"{CONV}/{conv}/messages", headers=h).json()["items"]
    assert [m["feedback"] for m in history] == [None, -1]  # persisted, shown in history


def test_feedback_validation_and_rules(client, setup_user) -> None:
    _, h, _ = setup_user()
    body = ask(client, h, Q).json()
    conv, question = body["conversation_id"], body["user_message"]["id"]
    answer = body["assistant_message"]["id"]

    assert client.put(feedback_url(conv, answer), headers=h, json={"rating": 5}).status_code == 422
    assert client.put(feedback_url(conv, question), headers=h, json={"rating": 1}).status_code == 409
    assert client.put(feedback_url(conv, str(uuid.uuid4())), headers=h, json={"rating": 1}).status_code == 404


def test_feedback_is_owner_only(client, setup_user) -> None:
    _, alice, _ = setup_user()
    _, bob, _ = setup_user("bob@example.com")
    body = ask(client, alice, Q).json()
    url = feedback_url(body["conversation_id"], body["assistant_message"]["id"])
    assert client.put(url, headers=bob, json={"rating": -1}).status_code == 404
    # A message id is only reachable through its own conversation.
    bob_conv = ask(client, bob, Q).json()["conversation_id"]
    assert client.put(feedback_url(bob_conv, body["assistant_message"]["id"]), headers=bob,
                      json={"rating": -1}).status_code == 404


def test_export_feedback_builds_eval_candidates(client, setup_user, db_session) -> None:
    from scripts.export_feedback import collect_feedback

    _, h, _ = setup_user()
    good = ask(client, h, Q).json()
    bad = ask(client, h, "What is the meal allowance?", conversation_id=good["conversation_id"]).json()
    ask(client, h, "Unrated question?")
    client.put(feedback_url(good["conversation_id"], good["assistant_message"]["id"]), headers=h, json={"rating": 1})
    client.put(feedback_url(bad["conversation_id"], bad["assistant_message"]["id"]), headers=h, json={"rating": -1})

    data = collect_feedback(db_session)
    [cand] = data["candidates"]
    assert cand["q"] == Q and cand["filename"] == "handbook.pdf" and cand["page"] == 1
    [review] = data["to_review"]
    assert review["q"] == "What is the meal allowance?"  # paired with ITS question, not the first one
    assert "email" not in json.dumps(data)


# ------------------------------------------------------------------ regenerate
def test_regenerate_replaces_last_answer_and_bypasses_cache(client, setup_user, db_session) -> None:
    _, h, _ = setup_user()
    llm = FakeLLM("First answer: 24 days [1].", "Second answer: 24 days of paid leave [1].")
    use_llm(llm)
    first = ask(client, h, Q).json()
    conv = first["conversation_id"]

    again = ask(client, h, Q, conversation_id=conv, regenerate=True)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["cached"] is False  # a normal repeat would have been a cache hit
    assert body["assistant_message"]["content"].startswith("Second answer")
    assert len(llm.calls) == 2
    # The new answer does not see the replaced one as conversation history.
    assert "First answer" not in "".join(m.content for m in llm.calls[1])

    history = client.get(f"{CONV}/{conv}/messages", headers=h).json()["items"]
    assert [m["content"] for m in history] == [Q, body["assistant_message"]["content"]]
    old_ids = {first["user_message"]["id"], first["assistant_message"]["id"]}
    assert not db_session.scalars(select(Message).where(Message.id.in_(old_ids))).all()


def test_regenerate_stream_replaces_last_answer(client, setup_user) -> None:
    _, h, _ = setup_user()
    use_llm(FakeStreamingLLM("Employees get 24 days [1].", "They get 24 paid days [1]."))
    conv = done(stream(client, h, Q))["conversation_id"]
    body = done(stream(client, h, Q, conversation_id=conv, regenerate=True))
    assert body["assistant_message"]["content"] == "They get 24 paid days [1]."
    assert client.get(f"{CONV}/{conv}", headers=h).json()["message_count"] == 2


def test_regenerate_rules(client, setup_user, db_session) -> None:
    _, h, _ = setup_user()
    conv = ask(client, h, Q).json()["conversation_id"]
    ask(client, h, "And sick leave?", conversation_id=conv)

    # Needs a conversation.
    assert ask(client, h, Q, regenerate=True).status_code == 422
    # Only the LAST exchange, with its own question.
    resp = ask(client, h, Q, conversation_id=conv, regenerate=True)
    assert resp.status_code == 409
    count = db_session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == uuid.UUID(conv)))
    assert count == 4  # nothing deleted


def test_failed_regenerate_keeps_the_old_answer(client, setup_user, monkeypatch) -> None:
    _, h, _ = setup_user()
    conv = ask(client, h, Q).json()["conversation_id"]

    def boom(*args, **kwargs):
        raise RuntimeError("search exploded")

    monkeypatch.setattr("app.services.rag_service.RagService.answer", boom)
    with pytest.raises(RuntimeError):  # the TestClient re-raises unhandled server errors
        ask(client, h, Q, conversation_id=conv, regenerate=True)
    monkeypatch.undo()
    history = client.get(f"{CONV}/{conv}/messages", headers=h).json()["items"]
    assert len(history) == 2  # rolled back: the original pair is still there


def test_regenerate_result_refreshes_the_cache(client, setup_user) -> None:
    user, h, _ = setup_user()
    use_llm(FakeLLM("Old [1].", "New [1]."))
    conv = ask(client, h, Q).json()["conversation_id"]
    ask(client, h, Q, conversation_id=conv, regenerate=True)
    # A brand-new conversation with the same question now gets the regenerated answer.
    fresh = ask(client, h, Q).json()
    assert fresh["cached"] is True and fresh["assistant_message"]["content"] == "New [1]."
    assert cache_service.document_version(user.id) is not None
