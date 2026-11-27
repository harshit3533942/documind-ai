"""Document summary + suggested questions (summary_service, task, API)."""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from app.models import Document, DocumentStatus, User
from app.services import summary_service
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import HashingEmbedder
from app.services.llm_service import LLMError
from app.services.storage_service import LocalStorage
from app.tasks import document_tasks
from app.tests import pdf_factory
from app.tests.fakes import FakeLLM
from app.tests.test_background_tasks import NoCloseSession, summaries_enqueued

DOCS = "/api/v1/documents"
HANDBOOK = [
    "Employee Handbook\n\nThis handbook explains the leave and travel rules of the company. "
    "All full-time employees receive 24 days of paid annual leave per year. "
    "Employees are entitled to 10 days of paid sick leave.",
    "Remote Work\n\nEmployees may work remotely up to three days per week with manager approval. "
    "Travel Allowance\n\nThe daily meal allowance for domestic travel is INR 1,500.",
]


@pytest.fixture
def alice(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def processed(client, alice, db_session: Session, storage: LocalStorage, embedder) -> Callable[..., Document]:
    def _make(pages: list[str] | None = None, name: str = "handbook.pdf") -> Document:
        resp = client.post(DOCS, headers=alice, files={"file": (name, pdf_factory.text_pdf(pages or HANDBOOK), "application/pdf")})
        assert resp.status_code == 201, resp.text
        doc_id = uuid.UUID(resp.json()["id"])
        processor = DocumentProcessor(db_session, storage, TextChunker(ChunkingConfig(60, 10)), embedder=embedder)
        assert processor.process(doc_id).status == "completed"
        return db_session.get(Document, doc_id)

    return _make


# ------------------------------------------------------------------ extractive
def test_extractive_summary_and_questions(processed, db_session, embedder) -> None:
    doc = processed()
    assert summary_service.summarize_document(db_session, doc.id, embedder=embedder, llm=None) == "completed"
    db_session.refresh(doc)
    assert doc.summarized_at is not None
    sentences = [s for s in doc.summary.split(". ") if s]
    assert 1 <= len(sentences) <= summary_service.SUMMARY_SENTENCES + 1
    assert all(part.strip(" .") in " ".join(" ".join(HANDBOOK).split()) for part in sentences)  # only real sentences
    assert 1 <= len(doc.suggested_questions) <= summary_service.QUESTION_COUNT
    assert all(q.endswith("?") for q in doc.suggested_questions)
    assert any("Remote Work" in q or "Travel Allowance" in q for q in doc.suggested_questions)  # from headings


def test_extractive_questions_fall_back_to_frequent_terms(processed, db_session, embedder) -> None:
    doc = processed([" ".join(["The quarterly revenue grew because the marketing campaign worked well."] * 4)])
    summary_service.summarize_document(db_session, doc.id, embedder=embedder, llm=None)
    db_session.refresh(doc)
    assert doc.summary.startswith("The quarterly revenue grew")
    assert any("revenue" in q or "quarterly" in q or "marketing" in q for q in doc.suggested_questions)


# ------------------------------------------------------------------ llm
def test_llm_summary(processed, db_session, embedder) -> None:
    doc = processed()
    llm = FakeLLM('Sure! {"summary": "A handbook about leave and travel.", '
                  '"questions": ["1. How many leave days?", "How many leave days?", "- What is the meal allowance"]}')
    summary_service.summarize_document(db_session, doc.id, embedder=embedder, llm=llm)
    db_session.refresh(doc)
    assert doc.summary == "A handbook about leave and travel."
    assert doc.suggested_questions == ["How many leave days?", "What is the meal allowance?"]
    prompt = llm.prompt()
    assert "<excerpt" in prompt and "24 days of paid annual leave" in prompt
    assert "untrusted" in llm.calls[0][0].content  # prompt-injection rule in the system prompt


@pytest.mark.parametrize("reply", [LLMError("timeout"), "no json here", '{"summary": ""}', '{"summary": 5}', "{broken"])
def test_bad_llm_output_falls_back_to_extractive(processed, db_session, embedder, reply) -> None:
    doc = processed()
    summary_service.summarize_document(db_session, doc.id, embedder=embedder, llm=FakeLLM(reply))
    db_session.refresh(doc)
    assert doc.summary and "24 days" in doc.summary or "leave" in doc.summary.lower()


def test_parse_and_clean_helpers() -> None:
    summary, questions = summary_service.parse_llm_output('{"summary": "  Two\\n words ", "questions": "not a list"}')
    assert summary == "Two words" and questions == []
    long = summary_service.parse_llm_output('{"summary": "' + "word " * 400 + '"}')[0]
    assert len(long) <= summary_service.MAX_SUMMARY_CHARS + 1 and long.endswith("…")
    cleaned = summary_service.clean_questions(["What is clause 4", "short", "Q5?", "A?" * 150] + [f"Question {i}?" for i in range(9)])
    assert cleaned[0] == "What is clause 4?"  # trailing digits are kept
    assert len(cleaned) == summary_service.QUESTION_COUNT
    assert all(len(q) <= summary_service.MAX_QUESTION_CHARS + 2 for q in cleaned)


def test_sample_chunks_spreads_over_the_document(processed, db_session) -> None:
    pages = [f"Section {p}. " + " ".join(f"Sentence {p}-{i} about topic {p}." for i in range(20)) for p in range(1, 7)]
    doc = processed(pages)
    picked = summary_service.sample_chunks(db_session, doc, budget=300)
    indexes = [c.chunk_index for c in picked]
    assert indexes == sorted(indexes) and indexes[0] == 0
    assert picked[-1].page_end == 6  # the end of the document is represented too
    assert len(summary_service.sample_chunks(db_session, doc, budget=10)) == 1


def test_not_completed_or_missing_is_skipped(client, alice, db_session, embedder) -> None:
    resp = client.post(DOCS, headers=alice, files={"file": ("x.pdf", pdf_factory.text_pdf(HANDBOOK), "application/pdf")})
    doc_id = uuid.UUID(resp.json()["id"])
    assert summary_service.summarize_document(db_session, doc_id, embedder=embedder, llm=None) == "skipped"
    assert summary_service.summarize_document(db_session, uuid.uuid4(), embedder=embedder, llm=None) == "skipped"


# ------------------------------------------------------------------ Celery task
def test_summary_task_and_follow_up_after_processing(client, alice, db_session, storage, embedder, monkeypatch) -> None:
    monkeypatch.setattr(document_tasks, "_session_factory", lambda: NoCloseSession(db_session))
    monkeypatch.setattr(document_tasks, "_storage_factory", lambda: storage)
    monkeypatch.setattr(document_tasks, "_embedder_factory", lambda: embedder)
    monkeypatch.setattr(document_tasks, "_llm_factory", lambda: None)
    monkeypatch.setattr(document_tasks, "enqueue_summary", lambda d: summaries_enqueued.append(d))
    summaries_enqueued.clear()

    resp = client.post(DOCS, headers=alice, files={"file": ("x.pdf", pdf_factory.text_pdf(HANDBOOK), "application/pdf")})
    doc_id = uuid.UUID(resp.json()["id"])
    assert document_tasks.process_document_task.apply(args=[str(doc_id)]).get()["status"] == "completed"
    assert summaries_enqueued == [doc_id]  # processing hands off to the summary job

    assert document_tasks.summarize_document_task.apply(args=[str(doc_id)]).get()["status"] == "completed"
    body = client.get(f"{DOCS}/{doc_id}", headers=alice).json()
    assert body["summary"] and body["suggested_questions"] and body["summarized_at"]

    monkeypatch.setattr(summary_service, "generate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert document_tasks.summarize_document_task.apply(args=[str(doc_id)]).get()["status"] == "failed"


def test_enqueue_summary_survives_broker_errors(monkeypatch) -> None:
    def down(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(document_tasks.summarize_document_task, "apply_async", down)
    assert document_tasks.enqueue_summary(uuid.uuid4()) is False


# ------------------------------------------------------------------ API
def test_request_summary_endpoint(client, alice, processed, task_queue, create_user, auth_headers) -> None:
    doc = processed()
    resp = client.post(f"{DOCS}/{doc.id}/summary", headers=alice)
    assert resp.status_code == 202 and task_queue.summaries == [doc.id]

    create_user("bob@example.com")
    assert client.post(f"{DOCS}/{doc.id}/summary", headers=auth_headers("bob@example.com")).status_code == 404

    task_queue.available = False
    unavailable = client.post(f"{DOCS}/{doc.id}/summary", headers=alice)
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "service_unavailable"


def test_request_summary_needs_a_processed_document(client, alice, db_session) -> None:
    resp = client.post(DOCS, headers=alice, files={"file": ("x.pdf", pdf_factory.text_pdf(HANDBOOK), "application/pdf")})
    doc_id = resp.json()["id"]
    assert db_session.get(Document, uuid.UUID(doc_id)).status != DocumentStatus.COMPLETED
    assert client.post(f"{DOCS}/{doc_id}/summary", headers=alice).status_code == 409
