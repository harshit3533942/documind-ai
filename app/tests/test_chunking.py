"""Unit tests for the chunker. Property-style checks: size limits, no lost
text, overlap, and exact page attribution."""

import re

import pytest
from pydantic import ValidationError

from app.services.chunking_service import (
    ChunkingConfig,
    TextChunk,
    TextChunker,
    approximate_token_count,
    split_sentences,
)
from app.services.pdf_service import PageText


def pages_of(*texts: str) -> list[PageText]:
    return [PageText(page_number=i + 1, text=t) for i, t in enumerate(texts)]


def labelled_pages(page_count: int, sentences_per_page: int) -> list[PageText]:
    """Every sentence carries its true page: 'Fact p3 s7 is about topic seven.'"""
    return pages_of(*[
        " ".join(f"Fact p{p} s{s} is about topic number {s} here." for s in range(sentences_per_page))
        for p in range(1, page_count + 1)
    ])


def chunk(pages: list[PageText], size: int = 50, overlap: int = 10) -> list[TextChunk]:
    return list(TextChunker(ChunkingConfig(size, overlap)).chunk(pages))


# ------------------------------------------------------------------ basics
def test_short_text_is_one_chunk() -> None:
    result = chunk(pages_of("Hello world. This is short."))
    assert len(result) == 1
    assert result[0].content == "Hello world. This is short."
    assert (result[0].chunk_index, result[0].page_start, result[0].page_end) == (0, 1, 1)


def test_empty_and_whitespace_pages_produce_no_chunks() -> None:
    assert chunk([]) == []
    assert chunk(pages_of("", "   \n\n  ", "\t")) == []


def test_chunk_indices_are_sequential() -> None:
    result = chunk(labelled_pages(5, 20))
    assert [c.chunk_index for c in result] == list(range(len(result)))
    assert len(result) > 5


def test_every_chunk_respects_size_limit() -> None:
    for size, overlap in [(30, 0), (50, 10), (120, 40)]:
        for c in chunk(labelled_pages(4, 25), size, overlap):
            assert c.token_count <= size
            assert approximate_token_count(c.content) <= size


def test_chunking_is_deterministic() -> None:
    pages = labelled_pages(3, 15)
    assert chunk(pages) == chunk(pages)


# ------------------------------------------------------------------ no lost text
def test_no_sentence_is_lost() -> None:
    pages = labelled_pages(6, 18)
    joined = "\n".join(c.content for c in chunk(pages))
    for p in range(1, 7):
        for s in range(18):
            assert f"Fact p{p} s{s} is about" in joined, (p, s)


def test_without_overlap_text_is_reproduced_exactly_in_order() -> None:
    pages = labelled_pages(3, 12)
    original_words = " ".join(p.text for p in pages).split()
    chunk_words = " ".join(c.content for c in chunk(pages, 40, 0)).split()
    assert chunk_words == original_words


# ------------------------------------------------------------------ overlap
def test_consecutive_chunks_overlap() -> None:
    result = chunk(labelled_pages(2, 30), size=50, overlap=15)
    assert len(result) > 2
    for prev, nxt in zip(result, result[1:], strict=False):
        first_sentence = split_sentences(nxt.content.replace("\n\n", " "))[0]
        assert first_sentence in prev.content, "next chunk should start with the tail of the previous one"


def test_zero_overlap_means_no_repeated_sentences() -> None:
    result = chunk(labelled_pages(2, 30), size=50, overlap=0)
    sentences = [s for c in result for s in split_sentences(c.content.replace("\n\n", " "))]
    assert len(sentences) == len(set(sentences)) == 60


def test_overlap_never_creates_a_pure_overlap_final_chunk() -> None:
    result = chunk(labelled_pages(1, 9), size=50, overlap=40)
    last_sentences = split_sentences(result[-1].content)
    earlier = " ".join(c.content for c in result[:-1])
    assert any(s not in earlier for s in last_sentences)


# ------------------------------------------------------------------ page attribution
def test_page_ranges_are_exact() -> None:
    """For every chunk, page_start/page_end equal the min/max true page of its sentences."""
    for size, overlap in [(30, 0), (50, 10), (90, 30)]:
        for c in chunk(labelled_pages(5, 7), size, overlap):
            true_pages = {int(p) for p in re.findall(r"Fact p(\d+) s", c.content)}
            assert true_pages, c.content
            assert (c.page_start, c.page_end) == (min(true_pages), max(true_pages))


def test_chunk_crossing_page_boundary_cites_both_pages() -> None:
    result = chunk(pages_of("End of page one.", "Start of page two."), size=100, overlap=0)
    assert len(result) == 1
    assert (result[0].page_start, result[0].page_end) == (1, 2)
    assert result[0].content == "End of page one.\n\nStart of page two."


def test_pages_with_no_text_are_skipped_but_numbers_preserved() -> None:
    pages = [PageText(1, "First page text."), PageText(2, ""), PageText(5, "Fifth page text.")]
    result = chunk(pages, size=100, overlap=0)
    assert (result[0].page_start, result[0].page_end) == (1, 5)


# ------------------------------------------------------------------ long inputs
def test_very_long_sentence_is_split_on_words() -> None:
    sentence = " ".join(f"word{i}" for i in range(500))  # no punctuation at all
    result = chunk(pages_of(sentence), size=50, overlap=0)
    assert len(result) >= 10
    assert all(c.token_count <= 50 for c in result)
    assert " ".join(c.content for c in result).split() == sentence.split()


def test_giant_single_word_keeps_every_character() -> None:
    blob = "".join(f"ab{i % 10}" for i in range(3000))  # 9,000-char "word"
    counter = lambda text: max(1, len(text) // 10)  # noqa: E731 - pretend 10 chars = 1 token
    chunker = TextChunker(ChunkingConfig(50, 0), count_tokens=counter)
    result = list(chunker.chunk(pages_of(blob)))
    assert "".join(c.content for c in result) == blob
    assert all(counter(c.content) <= 50 for c in result)


def test_default_counter_charges_long_words_and_splits_them_losslessly() -> None:
    assert approximate_token_count("hello") == 1
    assert approximate_token_count("internationalization") == 3  # 20 chars
    assert approximate_token_count("a" * 8000) == 1000
    url = "https://example.com/" + "x" * 3000
    text = f"See {url} for details."
    result = chunk(pages_of(text), size=50, overlap=0)
    assert len(result) > 1
    assert all(c.token_count <= 50 for c in result)
    # Pieces of the split URL are glued back without spaces.
    assert "".join(c.content for c in result).replace("See ", "See").replace(" for", "for") == (
        text.replace("See ", "See").replace(" for", "for")
    )
    assert url in "".join(c.content for c in result)


# ------------------------------------------------------------------ text handling
def test_paragraph_breaks_kept_and_line_wraps_joined() -> None:
    text = "First paragraph line one\nline two continues.\n\nSecond paragraph."
    assert chunk(pages_of(text), size=100)[0].content == (
        "First paragraph line one line two continues.\n\nSecond paragraph."
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One. Two! Three? Four.", ["One.", "Two!", "Three?", "Four."]),
        ("Dr. Smith arrived. He sat down.", ["Dr. Smith arrived.", "He sat down."]),
        ("Use tools e.g. Python. Then test.", ["Use tools e.g. Python.", "Then test."]),
        ("J. R. R. Tolkien wrote it. Yes.", ["J. R. R. Tolkien wrote it.", "Yes."]),
        ('He said "Stop." Then left.', ['He said "Stop."', "Then left."]),
        ("Version 2.5 is out. Upgrade now.", ["Version 2.5 is out.", "Upgrade now."]),
    ],
)
def test_split_sentences(text: str, expected: list[str]) -> None:
    assert split_sentences(text) == expected


def test_custom_token_counter_is_used() -> None:
    calls: list[str] = []

    def counter(text: str) -> int:
        calls.append(text)
        return len(text.split())

    list(TextChunker(ChunkingConfig(20, 5), count_tokens=counter).chunk(pages_of("A b c. D e f.")))
    assert calls


# ------------------------------------------------------------------ configuration
@pytest.mark.parametrize(("size", "overlap"), [(5, 0), (50, 50), (50, 60), (50, -1)])
def test_invalid_config_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(size, overlap)


def test_settings_reject_overlap_not_smaller_than_size(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError, match="CHUNK_OVERLAP_TOKENS"):
        Settings(chunk_size_tokens=100, chunk_overlap_tokens=100)


def test_default_config_comes_from_settings() -> None:
    from app.core.config import get_settings

    cfg = ChunkingConfig.from_settings()
    assert (cfg.chunk_size, cfg.chunk_overlap) == (get_settings().chunk_size_tokens, get_settings().chunk_overlap_tokens)
