"""RAG with the REAL embedding model (extractive mode): regression test for the
score thresholds calibrated in docs/rag-pipeline.md. Marked `model`."""

import uuid

import pytest

from app.core.config import get_settings
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import SentenceTransformerEmbedder
from app.services.rag_service import NO_ANSWER_MESSAGE, RagService
from app.tests import pdf_factory

pytestmark = pytest.mark.model

PAGES = [
    "Leave Policy\n\nAll full-time employees receive 24 days of paid annual leave per year. "
    "Employees are entitled to 10 days of paid sick leave.",
    "Remote Work\n\nEmployees may work remotely up to three days per week with manager approval.",
    "Expenses\n\nThe daily meal allowance for domestic travel is INR 1,500. "
    "Lost laptops must be reported to IT within one hour.",
]


@pytest.fixture
def model(real_embedder) -> SentenceTransformerEmbedder:
    return real_embedder  # shared, loaded once per session (conftest)


@pytest.fixture
def user_id(client, create_user, auth_headers, db_session, storage, model) -> uuid.UUID:
    user = create_user("real@example.com")
    resp = client.post("/api/v1/documents", headers=auth_headers("real@example.com"),
                       files={"file": ("handbook.pdf", pdf_factory.text_pdf(PAGES), "application/pdf")})
    assert DocumentProcessor(db_session, storage, embedder=model).process(uuid.UUID(resp.json()["id"])).status == "completed"
    return user.id


@pytest.mark.parametrize(
    ("question", "expected_text", "page"),
    [
        ("How many vacation days do I get?", "24 days of paid annual leave", 1),   # synonym: vacation ~ annual leave
        ("Can I work from home?", "work remotely", 2),                            # paraphrase
        ("What is the food budget on business trips?", "meal allowance", 3),
        ("I lost my laptop, what now?", "reported to IT", 3),
    ],
)
def test_answerable_questions(db_session, model, user_id, question, expected_text, page) -> None:
    result = RagService(db_session, model, None).answer(user_id=user_id, question=question)
    assert result.found, result
    assert expected_text in result.answer
    assert any(c.page_start == page for c in result.citations)


@pytest.mark.parametrize("question", [
    "What is the capital of France?",
    "How do I bake sourdough bread?",
    "Who won the 2022 football world cup?",
])
def test_unanswerable_questions_are_refused(db_session, model, user_id, question) -> None:
    result = RagService(db_session, model, None).answer(user_id=user_id, question=question)
    assert result.found is False and result.answer == NO_ANSWER_MESSAGE
