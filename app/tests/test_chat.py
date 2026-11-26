"""Chat & conversation API: ask, history, follow-ups, document selection,
ownership isolation and query efficiency."""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.dependencies import get_llm_dependency
from app.main import app as fastapi_app
from app.models import Document, DocumentStatus, Message, User
from app.services.chat_service import make_title
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import HashingEmbedder
from app.services.llm_service import LLMError
from app.services.rag_service import NO_ANSWER_MESSAGE
from app.tests import pdf_factory
from app.tests.fakes import FakeLLM

ASK = "/api/v1/chat/ask"
CONV = "/api/v1/conversations"

HANDBOOK = [
    "Leave policy. Every employee receives 24 days of paid annual leave each year. "
    "Sick leave is ten days per year.",
    "Travel policy. Economy class flights are mandatory for trips under six hours. "
    "The daily meal allowance for domestic travel is 1500 rupees.",
]


@pytest.fixture
def setup_user(client: TestClient, create_user, auth_headers, db_session: Session, storage, fake_embedder: HashingEmbedder):
    """Create a user with processed documents. Returns (user, headers, {filename: doc_id})."""

    def _make(email: str = "alice@example.com", docs: dict[str, list[str]] | None = None, process: bool = True):
        user = create_user(email)
        headers = auth_headers(email)
        processor = DocumentProcessor(db_session, storage, TextChunker(ChunkingConfig(60, 10)), embedder=fake_embedder)
        ids = {}
        for name, pages in (docs if docs is not None else {"handbook.pdf": HANDBOOK}).items():
            resp = client.post("/api/v1/documents", headers=headers,
                               files={"file": (name, pdf_factory.text_pdf(pages), "application/pdf")})
            ids[name] = uuid.UUID(resp.json()["id"])
            if process:
                assert processor.process(ids[name]).status == "completed"
        return user, headers, ids

    return _make


def use_llm(llm: FakeLLM) -> None:
    fastapi_app.dependency_overrides[get_llm_dependency] = lambda: llm


def ask(client: TestClient, headers, question: str, **extra):
    return client.post(ASK, headers=headers, json={"question": question, **extra})


# ------------------------------------------------------------------ ask: new conversation
def test_ask_creates_conversation_and_saves_both_messages(client, setup_user, db_session) -> None:
    _, h, ids = setup_user()
    resp = ask(client, h, "What is the daily meal allowance for domestic travel?")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["conversation_title"] == "What is the daily meal allowance for domestic travel?"
    assert body["user_message"]["role"] == "user"
    a = body["assistant_message"]
    assert a["role"] == "assistant" and a["found"] is True and a["mode"] == "extractive" and a["grounded"] is True
    assert "1500 rupees" in a["content"]
    [cite] = a["citations"]
    assert cite["filename"] == "handbook.pdf" and cite["page_start"] == 2
    assert cite["document_id"] == str(ids["handbook.pdf"]) and "1500" in cite["excerpt"]
    assert set(body["timings_ms"]) >= {"retrieval", "total"}

    conv_id = uuid.UUID(body["conversation_id"])
    count = db_session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == conv_id))
    assert count == 2


def test_answer_with_llm_is_persisted_with_citations(client, setup_user) -> None:
    _, h, _ = setup_user()
    use_llm(FakeLLM("The meal allowance is 1500 rupees per day [1]."))
    body = ask(client, h, "What is the daily meal allowance for domestic travel?").json()
    msgs = client.get(f"{CONV}/{body['conversation_id']}/messages", headers=h).json()["items"]
    assistant = msgs[1]
    assert assistant["mode"] == "llm" and assistant["content"] == "The meal allowance is 1500 rupees per day [1]."
    assert assistant["citations"][0]["page_start"] == 2


def test_no_documents_gives_clear_no_answer(client, setup_user) -> None:
    _, h, _ = setup_user(docs={})
    a = ask(client, h, "What is the leave policy?").json()["assistant_message"]
    assert a["found"] is False and a["content"] == NO_ANSWER_MESSAGE and a["citations"] == []


def test_llm_outage_still_answers_and_saves(client, setup_user) -> None:
    _, h, _ = setup_user()
    use_llm(FakeLLM(LLMError("LLM request failed: APITimeoutError")))
    a = ask(client, h, "What is the daily meal allowance for domestic travel?").json()["assistant_message"]
    assert a["mode"] == "extractive" and a["found"] is True


# ------------------------------------------------------------------ follow-ups / continuing
def test_continue_conversation_and_follow_up_uses_history(client, setup_user) -> None:
    _, h, _ = setup_user()
    first = ask(client, h, "What is the travel policy for economy class flights?").json()
    conv_id = first["conversation_id"]

    second = ask(client, h, "what about meals?", conversation_id=conv_id).json()
    assert second["conversation_id"] == conv_id
    assert second["conversation_title"] == first["conversation_title"]  # title stays the first question
    # Without an LLM the heuristic carries the previous question's topic into the search.
    assert second["search_query"] == "What is the travel policy for economy class flights? what about meals?"

    msgs = client.get(f"{CONV}/{conv_id}/messages", headers=h).json()
    assert msgs["total"] == 4
    assert [m["role"] for m in msgs["items"]] == ["user", "assistant", "user", "assistant"]
    assert msgs["items"][2]["content"] == "what about meals?"


def test_follow_up_rewrite_receives_conversation_history(client, setup_user) -> None:
    _, h, _ = setup_user()
    conv_id = ask(client, h, "What is the travel policy for economy class flights?").json()["conversation_id"]
    llm = FakeLLM("What is the daily meal allowance for domestic travel?", "1500 rupees [1].")
    use_llm(llm)
    body = ask(client, h, "And meals?", conversation_id=conv_id).json()
    rewrite_prompt = llm.prompt(0)
    assert "USER: What is the travel policy for economy class flights?" in rewrite_prompt
    assert "ASSISTANT:" in rewrite_prompt  # previous answer is part of the context
    assert body["search_query"] == "What is the daily meal allowance for domestic travel?"


def test_history_window_is_limited(client, setup_user, monkeypatch) -> None:
    from app.services import chat_service

    monkeypatch.setattr(chat_service, "HISTORY_MESSAGES", 2)
    _, h, _ = setup_user()
    conv_id = ask(client, h, "First question about economy class flights travel?").json()["conversation_id"]
    ask(client, h, "Second question about annual leave days policy?", conversation_id=conv_id)
    llm = FakeLLM("rewritten standalone", "x [1]")
    use_llm(llm)
    ask(client, h, "and that?", conversation_id=conv_id)
    assert "First question" not in llm.prompt(0) and "Second question" in llm.prompt(0)


# ------------------------------------------------------------------ document selection
def test_document_selection_restricts_sources(client, setup_user) -> None:
    _, h, ids = setup_user(docs={"leave.pdf": HANDBOOK[:1], "travel.pdf": HANDBOOK[1:]})
    body = ask(client, h, "policy days allowance leave travel", document_ids=[str(ids["travel.pdf"])]).json()
    cites = body["assistant_message"]["citations"]
    assert cites and all(c["filename"] == "travel.pdf" for c in cites)
    assert body["user_message"]["document_ids"] == [str(ids["travel.pdf"])]


def test_selecting_another_users_document_is_404(client, setup_user) -> None:
    _, _, bob_ids = setup_user("bob@example.com")
    _, alice, _ = setup_user("alice@example.com")
    resp = ask(client, alice, "leave policy", document_ids=[str(bob_ids["handbook.pdf"])])
    assert resp.status_code == 404  # identical to a non-existent id: nothing leaks
    assert ask(client, alice, "leave policy", document_ids=[str(uuid.uuid4())]).status_code == 404


def test_selecting_unprocessed_document_is_409(client, setup_user) -> None:
    _, h, ids = setup_user(process=False)
    resp = ask(client, h, "leave policy", document_ids=[str(ids["handbook.pdf"])])
    assert resp.status_code == 409 and "not ready" in resp.json()["error"]["message"]


# ------------------------------------------------------------------ conversation list / detail
def test_list_conversations_most_recent_first_with_preview(client, setup_user) -> None:
    _, h, _ = setup_user()
    c1 = ask(client, h, "What is the annual leave policy?").json()["conversation_id"]
    c2 = ask(client, h, "What is the travel policy?").json()["conversation_id"]
    ask(client, h, "And the meal allowance for domestic travel?", conversation_id=c1)  # c1 active again

    body = client.get(CONV, headers=h).json()
    assert body["total"] == 2
    assert [c["id"] for c in body["items"]] == [c1, c2]
    first = body["items"][0]
    assert first["message_count"] == 4 and first["title"] == "What is the annual leave policy?"
    assert first["last_message_preview"] and first["last_message_at"]

    page = client.get(CONV, headers=h, params={"limit": 1, "offset": 1}).json()
    assert [c["id"] for c in page["items"]] == [c2]


def test_get_conversation_detail(client, setup_user) -> None:
    _, h, _ = setup_user()
    conv_id = ask(client, h, "What is the annual leave policy?").json()["conversation_id"]
    detail = client.get(f"{CONV}/{conv_id}", headers=h).json()
    assert detail["id"] == conv_id and detail["message_count"] == 2


def test_message_pagination(client, setup_user) -> None:
    _, h, _ = setup_user()
    conv_id = ask(client, h, "annual leave policy").json()["conversation_id"]
    for q in ("travel policy", "meal allowance"):
        ask(client, h, q, conversation_id=conv_id)
    page = client.get(f"{CONV}/{conv_id}/messages", headers=h, params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 6 and [m["content"] for m in page["items"]][0] == "travel policy"


def test_conversation_list_has_no_n_plus_1_queries(client, setup_user, db_engine: Engine) -> None:
    _, h, _ = setup_user()

    def queries_for_list() -> int:
        statements: list[str] = []
        listener = lambda conn, cursor, stmt, *a: statements.append(stmt)  # noqa: E731
        event.listen(db_engine, "before_cursor_execute", listener)
        try:
            assert client.get(CONV, headers=h).status_code == 200
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
        return len(statements)

    ask(client, h, "annual leave policy")
    with_one = queries_for_list()
    for i in range(6):
        ask(client, h, f"travel policy question {i}")
    assert queries_for_list() == with_one  # constant, not "1 + one per conversation"


# ------------------------------------------------------------------ ownership isolation
def test_other_user_cannot_touch_conversation(client, setup_user, db_session) -> None:
    _, alice, _ = setup_user("alice@example.com")
    conv_id = ask(client, alice, "What is the annual leave policy?").json()["conversation_id"]
    _, bob, _ = setup_user("bob@example.com")

    assert client.get(f"{CONV}/{conv_id}", headers=bob).status_code == 404
    assert client.get(f"{CONV}/{conv_id}/messages", headers=bob).status_code == 404
    assert client.delete(f"{CONV}/{conv_id}", headers=bob).status_code == 404
    # Bob cannot append to (or read history through) Alice's conversation either.
    assert ask(client, bob, "and what about it?", conversation_id=conv_id).status_code == 404
    assert client.get(CONV, headers=bob).json()["total"] == 0
    # Alice's data is untouched.
    assert client.get(f"{CONV}/{conv_id}/messages", headers=alice).json()["total"] == 2


def test_endpoints_require_authentication(client) -> None:
    assert client.post(ASK, json={"question": "hi"}).status_code == 401
    assert client.get(CONV).status_code == 401
    assert client.get(f"{CONV}/{uuid.uuid4()}/messages").status_code == 401


# ------------------------------------------------------------------ delete
def test_delete_conversation_removes_messages(client, setup_user, db_session) -> None:
    _, h, _ = setup_user()
    conv_id = ask(client, h, "What is the annual leave policy?").json()["conversation_id"]
    assert client.delete(f"{CONV}/{conv_id}", headers=h).status_code == 204
    assert client.get(f"{CONV}/{conv_id}", headers=h).status_code == 404
    assert db_session.scalar(select(func.count()).select_from(Message)) == 0
    assert client.delete(f"{CONV}/{conv_id}", headers=h).status_code == 404


def test_history_keeps_citations_after_document_deleted(client, setup_user) -> None:
    _, h, ids = setup_user()
    conv_id = ask(client, h, "What is the daily meal allowance for domestic travel?").json()["conversation_id"]
    assert client.delete(f"/api/v1/documents/{ids['handbook.pdf']}", headers=h).status_code == 204
    a = client.get(f"{CONV}/{conv_id}/messages", headers=h).json()["items"][1]
    assert a["citations"][0]["filename"] == "handbook.pdf"  # snapshot of what was cited at the time


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("payload", [
    {"question": ""},
    {"question": "   "},
    {"question": "x" * 2001},
    {"question": "ok", "conversation_id": "not-a-uuid"},
    {"question": "ok", "document_ids": ["nope"]},
    {"question": "ok", "document_ids": [str(uuid.uuid4()) for _ in range(21)]},
    {},
])
def test_ask_validation(client, setup_user, payload) -> None:
    _, h, _ = setup_user(docs={})
    assert client.post(ASK, headers=h, json=payload).status_code == 422


def test_unknown_conversation_id_is_404(client, setup_user) -> None:
    _, h, _ = setup_user(docs={})
    assert ask(client, h, "hello there", conversation_id=str(uuid.uuid4())).status_code == 404


def test_duplicate_document_ids_are_deduplicated(client, setup_user) -> None:
    _, h, ids = setup_user()
    d = str(ids["handbook.pdf"])
    body = ask(client, h, "annual leave policy", document_ids=[d, d, d]).json()
    assert body["user_message"]["document_ids"] == [d]


def test_messages_have_stable_chronological_order(client, setup_user) -> None:
    """Question and answer are saved in one transaction; they must still be ordered."""
    _, h, _ = setup_user()
    conv_id = ask(client, h, "annual leave policy").json()["conversation_id"]
    for _ in range(3):
        items = client.get(f"{CONV}/{conv_id}/messages", headers=h).json()["items"]
        assert [m["role"] for m in items] == ["user", "assistant"]
        assert items[0]["created_at"] < items[1]["created_at"]


def test_make_title() -> None:
    assert make_title("Short question?") == "Short question?"
    long_q = "How many days of paid annual leave do full time employees receive per calendar year in the Bangalore office?"
    title = make_title(long_q)
    assert len(title) <= 81 and title.endswith("…") and not title[:-1].endswith(" ")
