"""Streaming answers (POST /chat/stream, Server-Sent Events)."""

import json

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Conversation, Message
from app.services import rag_service
from app.services.llm_service import LLMError, OpenAIChatClient, stream_completion
from app.services.rag_service import INSUFFICIENT_TOKEN, NO_ANSWER_MESSAGE, RagService
from app.tests.fakes import FakeLLM, FakeStreamingLLM
from app.tests.test_chat import setup_user, use_llm  # noqa: F401 - fixture import

STREAM = "/api/v1/chat/stream"


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def stream(client, headers, question: str, **extra) -> list[tuple[str, dict]]:
    resp = client.post(STREAM, headers=headers, json={"question": question, **extra})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"
    return parse_sse(resp.text)


def tokens(events) -> str:
    return "".join(d["text"] for e, d in events if e == "token")


def done(events) -> dict:
    [(kind, data)] = [(e, d) for e, d in events if e in ("done", "error")]
    assert kind == "done", data
    return data


# ------------------------------------------------------------------ API
def test_stream_extractive_answer_and_save(client, setup_user, db_session) -> None:  # noqa: F811
    _, h, _ = setup_user()
    events = stream(client, h, "What is the daily meal allowance for domestic travel?")

    assert events[0][0] == "meta"
    body = done(events)
    assert body["conversation_id"] == events[0][1]["conversation_id"]
    answer = body["assistant_message"]
    assert answer["mode"] == "extractive" and "1500 rupees" in answer["content"]
    assert tokens(events) == answer["content"]  # the pieces add up to the saved answer
    assert len([e for e, _ in events if e == "token"]) > 1  # really streamed, not one blob
    assert db_session.scalar(select(func.count()).select_from(Message)) == 2


def test_stream_llm_tokens_and_cleaned_final_answer(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    use_llm(llm := FakeStreamingLLM(["Employees get ", "24 days ", "of leave [1]", " and more [7]."]))
    events = stream(client, h, "How many days of annual leave do employees get?")

    assert tokens(events) == "Employees get 24 days of leave [1] and more [7]."
    answer = done(events)["assistant_message"]
    assert answer["mode"] == "llm" and answer["grounded"] is True
    assert "[7]" not in answer["content"]  # invalid citation removed in the final answer
    assert [c["number"] for c in answer["citations"]] == [1]
    assert len(llm.calls) == 1


def test_stream_hides_insufficient_context_keyword(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    use_llm(FakeStreamingLLM(["INSUFF", "ICIENT_", "CONTEXT", " - sorry"]))
    events = stream(client, h, "How many days of annual leave do employees get?")
    assert "INSUFF" not in tokens(events)
    answer = done(events)["assistant_message"]
    assert answer["found"] is False and answer["content"] == NO_ANSWER_MESSAGE


def test_stream_falls_back_to_extractive_when_llm_fails_midway(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    use_llm(FakeStreamingLLM(["Employees get ", LLMError("connection reset")]))
    events = stream(client, h, "How many days of annual leave do employees get?")
    answer = done(events)["assistant_message"]
    assert answer["mode"] == "extractive" and "24 days" in answer["content"]


def test_stream_with_non_streaming_llm(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    use_llm(FakeLLM("Employees get 24 days of paid annual leave [1]."))
    events = stream(client, h, "How many days of annual leave do employees get?")
    assert tokens(events) == "Employees get 24 days of paid annual leave [1]."
    assert done(events)["assistant_message"]["mode"] == "llm"


def test_stream_no_relevant_chunks(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user(docs={})
    events = stream(client, h, "What is the meaning of life?")
    answer = done(events)["assistant_message"]
    assert answer["found"] is False and tokens(events) == NO_ANSWER_MESSAGE


def test_stream_follow_up_and_cache(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    question = "What is the daily meal allowance for domestic travel?"
    first = done(stream(client, h, question))
    assert first["cached"] is False
    again = stream(client, h, question)  # new conversation, same first question -> cache
    assert done(again)["cached"] is True and tokens(again) == first["assistant_message"]["content"]

    follow = done(stream(client, h, "and flights?", conversation_id=first["conversation_id"]))
    assert follow["conversation_id"] == first["conversation_id"]
    assert follow["search_query"] != "and flights?"  # rewritten with the previous question's topic


def test_stream_validation_errors_are_normal_http_errors(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    missing = "00000000-0000-0000-0000-000000000000"
    resp = client.post(STREAM, headers=h, json={"question": "hi", "document_ids": [missing]})
    assert resp.status_code == 404 and resp.json()["error"]["code"]
    resp = client.post(STREAM, headers=h, json={"question": "hi", "conversation_id": missing})
    assert resp.status_code == 404
    assert client.post(STREAM, json={"question": "hi"}).status_code == 401


def test_stream_generation_error_is_reported_and_nothing_saved(client, setup_user, db_session, monkeypatch) -> None:  # noqa: F811
    _, h, _ = setup_user()

    def broken(self, **kwargs):
        yield "token", "partial "
        raise RuntimeError("boom")

    monkeypatch.setattr(RagService, "answer_stream", broken)
    events = stream(client, h, "What is the daily meal allowance for domestic travel?")
    assert events[-1][0] == "error" and "boom" not in events[-1][1]["message"]
    assert db_session.scalar(select(func.count()).select_from(Message)) == 0
    assert db_session.scalar(select(func.count()).select_from(Conversation)) == 0


# ------------------------------------------------------------------ service level
def test_client_disconnect_saves_nothing(client, setup_user, db_session, fake_embedder) -> None:  # noqa: F811
    from app.services.chat_service import ChatService

    user, _, _ = setup_user()
    service = ChatService(db_session, fake_embedder, None)
    ctx = service.start_ask(user, question="What is the daily meal allowance for domestic travel?")
    gen = service.ask_stream(ctx)
    assert next(gen)[0] == "token"
    gen.close()  # what happens when the browser tab is closed mid-answer
    assert db_session.scalar(select(func.count()).select_from(Conversation)) == 0


@pytest.mark.parametrize(("pieces", "shown"), [
    (["Hello ", "world"], "Hello world"),
    (["INS"], "INS"),                                   # short answer that only looked like the keyword
    ([" insufficient_context"], ""),                    # case-insensitive, leading space
    (["IN", "SUFFICIENT_CONTEXT", " anything"], ""),
    (["INSURANCE covers it [1]"], "INSURANCE covers it [1]"),
    ([], ""),
])
def test_hold_insufficient(pieces, shown) -> None:
    collected: list[str] = []
    assert "".join(rag_service._hold_insufficient(iter(pieces), collected)) == shown
    assert collected == pieces


def test_text_pieces_round_trip() -> None:
    text = "Here is what your documents say:\n- one two three four [1]"
    pieces = [p for _, p in rag_service.text_pieces(text)]
    assert "".join(pieces) == text and len(pieces) > 1


def test_stream_completion_falls_back_to_complete() -> None:
    assert list(stream_completion(FakeLLM("whole answer"), [], max_tokens=5, temperature=0)) == ["whole answer"]


# ------------------------------------------------------------------ OpenAI client streaming
def _sse_chunks(*contents: str | None) -> bytes:
    out = []
    for c in contents:
        delta = {} if c is None else {"content": c}
        out.append("data: " + json.dumps({
            "id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _client(handler) -> OpenAIChatClient:
    return OpenAIChatClient(api_key="k", model="m", base_url="https://llm.example/v1", max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_openai_stream_yields_deltas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=_sse_chunks("Hel", None, "lo"),
                              headers={"content-type": "text/event-stream"})

    assert list(_client(handler).stream([], max_tokens=5, temperature=0)) == ["Hel", "lo"]


def test_openai_stream_errors_become_llm_errors() -> None:
    empty = _client(lambda r: httpx.Response(200, content=_sse_chunks(None),
                                             headers={"content-type": "text/event-stream"}))
    with pytest.raises(LLMError, match="empty"):
        list(empty.stream([], max_tokens=5, temperature=0))

    failing = _client(lambda r: httpx.Response(500, json={"error": {"message": "x", "type": "y"}}))
    with pytest.raises(LLMError, match="request failed"):
        list(failing.stream([], max_tokens=5, temperature=0))


def test_insufficient_token_constant_is_upper_case() -> None:
    assert INSUFFICIENT_TOKEN == INSUFFICIENT_TOKEN.upper()  # _hold_insufficient compares upper-case
