"""Hybrid retrieval (vector + full-text, RRF) and cross-encoder re-ranking.

Fake embedders make the semantic side predictable, so each test can show
exactly what the keyword side and the fusion add.
"""

import pytest

from app.services.embedding_service import HashingEmbedder
from app.services.rerank_service import CrossEncoderReranker, get_reranker
from app.services.retrieval_service import retrieve, search_chunks
from app.tests import pdf_factory
from app.tests.test_embeddings import make_processor, register, upload  # noqa: F401 - fixture import

PAGES = [
    "Troubleshooting overview: restart the device and check the network cable before calling support.",
    "Error reference: code ERR-4012 means the licence server could not be reached. Renew the licence token.",
    "Warranty: devices are covered for two years. Water damage is not covered by the warranty.",
]


class ConstantEmbedder(HashingEmbedder):
    """Every text gets the SAME vector: semantic search cannot tell chunks
    apart (all cosine 1.0), so any ordering signal must come from keywords."""

    def _vector(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (self.dimension - 1)


class TopicEmbedder(HashingEmbedder):
    """Vector A for texts mentioning 'warranty', vector B (orthogonal) otherwise."""

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        vec[0 if "warranty" in text.lower() else 1] = 1.0
        return vec


class WordReranker:
    """Fake cross-encoder: relevance = does the passage contain `word`."""

    model_name = "test/word-reranker"

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls: list[int] = []

    def score(self, query: str, passages):
        self.calls.append(len(passages))
        return [1.0 if self.word in p else 0.0 for p in passages]


def _setup(client, create_user, auth_headers, make_processor, embedder, email="a@example.com", pages=PAGES):  # noqa: F811
    """One single-page document per text -> exactly one chunk each, named p1.pdf, p2.pdf..."""
    user, h = register(create_user, auth_headers, email)
    for i, text in enumerate(pages, start=1):
        make_processor(embedder).process(upload(client, h, pdf_factory.text_pdf([text]), f"p{i}.pdf"))
    return user, None


def test_hybrid_promotes_exact_keyword_match(client, create_user, auth_headers, make_processor, db_session) -> None:  # noqa: F811
    embedder = ConstantEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)

    results = search_chunks(db_session, embedder, user_id=user.id, query="what does ERR-4012 mean", top_k=3, mode="hybrid")
    assert "ERR-4012" in results[0].content
    assert results[0].keyword_rank == 1 and results[0].vector_rank is not None
    assert results[0].fused_score > results[1].fused_score
    assert results[0].filename == "p2.pdf"


def test_vector_mode_has_no_keyword_signal(client, create_user, auth_headers, make_processor, db_session) -> None:  # noqa: F811
    embedder = HashingEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)
    results = search_chunks(db_session, embedder, user_id=user.id, query="warranty water damage", top_k=3, mode="vector")
    assert results and all(r.keyword_rank is None and r.fused_score is None for r in results)
    assert [r.vector_rank for r in results] == list(range(1, len(results) + 1))
    assert results == sorted(results, key=lambda r: r.score, reverse=True)


def test_keyword_hit_still_needs_min_score(client, create_user, auth_headers, make_processor, db_session) -> None:  # noqa: F811
    """'device' appears on pages 1 and 3, but only page 3 is semantically about
    the warranty question: the threshold keeps unrelated keyword hits out."""
    embedder = TopicEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)
    results = search_chunks(
        db_session, embedder, user_id=user.id, query="warranty device", top_k=5, min_score=0.2, mode="hybrid"
    )
    assert [r.filename for r in results] == ["p3.pdf"]


def test_hybrid_keyword_side_respects_user_isolation(client, create_user, auth_headers, make_processor, db_session) -> None:  # noqa: F811
    embedder = ConstantEmbedder()
    alice, _ = _setup(client, create_user, auth_headers, make_processor, embedder, "alice@example.com", PAGES[:1])
    _setup(client, create_user, auth_headers, make_processor, embedder, "bob@example.com",
           ["Bob only: the vault code is ZX-9931."])

    results = search_chunks(db_session, embedder, user_id=alice.id, query="vault code ZX-9931", top_k=10, mode="hybrid")
    assert results and all("ZX-9931" not in r.content for r in results)


@pytest.mark.parametrize("query", ["the and of", "!!! ' ; DROP TABLE users; --", "ünïcödé & | ! <->", "a"])
def test_keyword_query_accepts_any_text(client, create_user, auth_headers, make_processor, db_session, query) -> None:  # noqa: F811
    """User text goes through plainto_tsquery (a bound parameter): stop-word-only
    queries, tsquery operators and SQL fragments neither error nor inject."""
    embedder = HashingEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)
    results = search_chunks(db_session, embedder, user_id=user.id, query=query, top_k=3, mode="hybrid")
    assert len(results) <= 3  # the vector side still returns results


def test_reranker_reorders_a_larger_candidate_pool(client, create_user, auth_headers, make_processor, db_session) -> None:  # noqa: F811
    embedder = ConstantEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)
    reranker = WordReranker("Water damage")

    results = retrieve(db_session, embedder, user_id=user.id, query="troubleshooting restart", top_k=1, reranker=reranker)
    assert len(results) == 1
    assert "Water damage" in results[0].content and results[0].rerank_score == 1.0
    assert reranker.calls == [3]  # all candidates were scored, not just top_k


def test_rerank_gate_replaces_cosine_threshold(client, create_user, auth_headers, make_processor, db_session, monkeypatch) -> None:  # noqa: F811
    """With a re-ranker the cosine threshold is not applied (TopicEmbedder gives
    the non-warranty pages cosine 0), the cross-encoder threshold decides."""
    from app.core.config import get_settings

    embedder = TopicEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)
    monkeypatch.setattr(get_settings(), "rag_rerank_min_score", 0.5)

    results = retrieve(db_session, embedder, user_id=user.id, query="warranty", top_k=5, min_score=0.2,
                       reranker=WordReranker("ERR-4012"))
    assert [r.filename for r in results] == ["p2.pdf"]  # cosine 0.0, but the re-ranker says relevant
    assert retrieve(db_session, embedder, user_id=user.id, query="warranty", top_k=5, min_score=0.2,
                    reranker=WordReranker("nothing-matches")) == []


def test_retrieve_without_reranker_returns_top_k(client, create_user, auth_headers, make_processor, db_session) -> None:  # noqa: F811
    embedder = HashingEmbedder()
    user, _ = _setup(client, create_user, auth_headers, make_processor, embedder)
    results = retrieve(db_session, embedder, user_id=user.id, query="licence error", top_k=2)
    assert len(results) == 2 and all(r.rerank_score is None for r in results)


def test_rerank_of_nothing_is_nothing(db_session, create_user) -> None:
    user = create_user()
    reranker = WordReranker("x")
    assert retrieve(db_session, HashingEmbedder(), user_id=user.id, query="q", top_k=3, reranker=reranker) == []
    assert reranker.calls == []


def test_reranker_none_when_disabled(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_rerank_enabled", False)
    get_reranker.cache_clear()
    try:
        assert get_reranker() is None
    finally:
        get_reranker.cache_clear()


def test_reranker_enabled_by_setting(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_rerank_enabled", True)
    get_reranker.cache_clear()
    try:
        reranker = get_reranker()
        assert isinstance(reranker, CrossEncoderReranker)
        assert reranker.model_name == get_settings().rag_rerank_model
        assert reranker.score("q", []) == []  # no model load needed for an empty list
    finally:
        get_reranker.cache_clear()


@pytest.mark.model
def test_cross_encoder_prefers_the_answering_passage() -> None:
    from app.core.config import get_settings

    reranker = CrossEncoderReranker(get_settings().rag_rerank_model)
    try:
        scores = reranker.score("How long is the warranty?", PAGES)
    except Exception as exc:  # noqa: BLE001 - e.g. offline machine without the cached model
        pytest.skip(f"Re-ranker model unavailable: {exc}")
    assert max(range(3), key=scores.__getitem__) == 2
