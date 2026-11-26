"""Tests against the REAL Sentence Transformers model (all-MiniLM-L6-v2).

Marked `model`; skipped automatically if the model cannot be loaded (e.g. an
offline CI machine without the cached model). Skip explicitly with:
    pytest -m "not model"
"""

import math

import pytest

from app.core.config import get_settings
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.embedding_service import EmbeddingConfigError, SentenceTransformerEmbedder
from app.services.pdf_service import PageText

pytestmark = pytest.mark.model


@pytest.fixture
def model(real_embedder) -> SentenceTransformerEmbedder:
    return real_embedder  # shared, loaded once per session (conftest)


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors are unit length


def test_model_properties(model: SentenceTransformerEmbedder) -> None:
    assert model.dimension == 384
    assert model.max_tokens == 256
    vec = model.embed_query("hello world")
    assert len(vec) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-5)


def test_semantic_similarity_understands_meaning(model: SentenceTransformerEmbedder) -> None:
    """No shared keywords needed: 'vacation days' should match 'paid annual leave'."""
    q = model.embed_query("How many vacation days do I get?")
    leave, flights, vpn = model.embed_documents([
        "Every employee receives 24 days of paid annual leave per year.",
        "Economy class is mandatory for flights under six hours.",
        "Laptops must connect through the VPN outside the office.",
    ])
    assert cos(q, leave) > cos(q, flights)
    assert cos(q, leave) > cos(q, vpn)
    assert cos(q, leave) > 0.4


def test_identical_text_has_similarity_one(model: SentenceTransformerEmbedder) -> None:
    a, b = model.embed_documents(["Same sentence.", "Same sentence."])
    assert math.isclose(cos(a, b), 1.0, rel_tol=1e-4)


def test_batch_and_single_embeddings_agree(model: SentenceTransformerEmbedder) -> None:
    texts = [f"Sentence number {i} about topic {i % 3}." for i in range(40)]
    batch = model.embed_documents(texts)
    single = model.embed_documents([texts[7]])[0]
    assert math.isclose(cos(batch[7], single), 1.0, rel_tol=1e-4)


def test_count_tokens_uses_wordpiece_tokenizer(model: SentenceTransformerEmbedder) -> None:
    assert model.count_tokens("Parents receive 26 weeks of paid parental leave.") == 9
    assert model.count_tokens("internationalization") > 1  # split into sub-words


def test_chunks_never_exceed_model_limit_with_real_tokenizer(model: SentenceTransformerEmbedder) -> None:
    """Chunks measured with the real tokenizer must fit in 256 tokens INCLUDING
    [CLS]/[SEP] - otherwise the model would silently drop their tail."""
    text = " ".join(
        f"Clause {i}: the contractor shall indemnify the client against third-party claims "
        f"arising from negligence, per subsection {i}.{i % 7}(b)." for i in range(120)
    )
    pages = [PageText(1, text[: len(text) // 2]), PageText(2, text[len(text) // 2 :])]
    chunker = TextChunker(ChunkingConfig(chunk_size=254, chunk_overlap=40), count_tokens=model.count_tokens)
    chunks = list(chunker.chunk(pages))
    assert len(chunks) > 5
    tokenizer = model._load().tokenizer
    for c in chunks:
        with_special = len(tokenizer(c.content, add_special_tokens=True)["input_ids"])
        assert with_special <= model.max_tokens, (c.chunk_index, with_special)


def test_dimension_mismatch_fails_loudly() -> None:
    wrong = SentenceTransformerEmbedder(get_settings().embedding_model_name, expected_dimension=768)
    with pytest.raises(EmbeddingConfigError, match="384-dim"):
        wrong.embed_query("x")


def test_concurrent_use_from_many_threads(model: SentenceTransformerEmbedder) -> None:
    """Regression: the Rust fast tokenizer raised 'RuntimeError: Already borrowed'
    under concurrent API requests. All model use is serialised by a lock."""
    from concurrent.futures import ThreadPoolExecutor

    texts = [f"Question number {i} about annual leave, travel and security policy details." for i in range(64)]

    def work(text: str) -> int:
        model.count_tokens(text)
        docs = model.embed_documents([text, text[::-1]])
        return len(model.embed_query(text)) + sum(len(v) for v in docs)

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(n == 384 * 3 for n in pool.map(work, texts))
