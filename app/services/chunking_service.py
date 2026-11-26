"""Split extracted PDF text into overlapping, page-aware chunks.

Algorithm
---------
1. Each page is split into paragraphs, then sentences ("units"). Every unit
   belongs to exactly ONE page - this is what makes citations exact.
2. A sentence longer than the chunk size is split on word boundaries
   (and a single absurdly long "word" on characters).
3. Units are packed greedily into a chunk until the next one would exceed
   `chunk_size` tokens.
4. The next chunk starts with the last few whole sentences of the previous
   chunk (up to `chunk_overlap` tokens) so an idea that straddles a boundary
   is fully present in at least one chunk.
5. page_start / page_end = min / max page of the units inside the chunk.
   A chunk crossing from page 3 to page 4 is cited as "pages 3-4", never as a
   guess.

Token counting is pluggable. The default is a fast regex approximation
(words + punctuation). Phase 6 can pass the embedding model's real tokenizer.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

from app.core.config import get_settings
from app.services.pdf_service import PageText

TokenCounter = Callable[[str], int]

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def approximate_token_count(text: str) -> int:
    """Approximate subword-token count: each punctuation mark = 1 token, each
    word = 1 token per started 8 characters. Ordinary English words cost 1;
    long identifiers/URLs/base64 blobs cost proportionally more, like they do
    with a real word-piece tokenizer."""
    return sum(1 + (len(match) - 1) // 8 for match in _TOKEN_RE.findall(text))


# ------------------------------------------------------------------ config / output
@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size: int = 180
    chunk_overlap: int = 30

    def __post_init__(self) -> None:
        if self.chunk_size < 10:
            raise ValueError("chunk_size must be at least 10 tokens")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")

    @classmethod
    def from_settings(cls) -> "ChunkingConfig":
        s = get_settings()
        return cls(chunk_size=s.chunk_size_tokens, chunk_overlap=s.chunk_overlap_tokens)


@dataclass(frozen=True)
class TextChunk:
    chunk_index: int
    content: str
    page_start: int
    page_end: int
    token_count: int
    # [[char_offset, page], ...]: content[offset:] is on `page` until the next
    # entry. Lets a citation point to the exact page of a single sentence even
    # when the chunk spans several pages.
    page_offsets: tuple[tuple[int, int], ...] = ()


def page_at(page_offsets: Sequence[Sequence[int]], char_index: int, default: int) -> int:
    """Page of the character at `char_index` according to page_offsets."""
    page = default
    for offset, p in page_offsets:
        if offset > char_index:
            break
        page = p
    return page


@dataclass(frozen=True)
class _Unit:
    text: str
    page: int
    tokens: int
    new_paragraph: bool  # first unit of a paragraph -> joined with a blank line
    glue: str = " "      # separator before this unit on the same paragraph;
                         # "" for the continuation pieces of a split giant word


# ------------------------------------------------------------------ sentence splitting
# Split after . ! ? (optionally followed by a closing quote/bracket) when the
# next sentence starts with an uppercase letter, digit, quote or bracket.
# Two fixed-width look-behinds because Python's `re` forbids variable-width ones.
_SENTENCE_BOUNDARY = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"')\]]))\s+(?=[A-Z0-9\"'(\[])")
_ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "dr.", "mr.", "mrs.", "ms.", "prof.", "sr.", "jr.",
    "fig.", "no.", "vol.", "pp.", "approx.", "dept.", "inc.", "ltd.", "co.", "st.",
}
_INITIAL = re.compile(r"(?:^|\s)[A-Z]\.$")  # "J. R. R. Tolkien"


def split_sentences(paragraph: str) -> list[str]:
    pieces = [p.strip() for p in _SENTENCE_BOUNDARY.split(paragraph) if p.strip()]
    merged: list[str] = []
    for piece in pieces:
        if merged:
            last_word = merged[-1].rsplit(maxsplit=1)[-1].lower()
            if last_word in _ABBREVIATIONS or _INITIAL.search(merged[-1]):
                merged[-1] = f"{merged[-1]} {piece}"
                continue
        merged.append(piece)
    return merged


_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"\s+")


# ------------------------------------------------------------------ chunker
class TextChunker:
    def __init__(self, config: ChunkingConfig | None = None, count_tokens: TokenCounter = approximate_token_count) -> None:
        self.config = config or ChunkingConfig.from_settings()
        self.count_tokens = count_tokens

    # -------------------------------------------------- units
    def _units(self, pages: Iterable[PageText]) -> Iterator[_Unit]:
        for page in pages:
            for paragraph in _PARAGRAPH_BREAK.split(page.text):
                # Single newlines inside a paragraph are just PDF line wraps.
                paragraph = _WHITESPACE.sub(" ", paragraph).strip()
                if not paragraph:
                    continue
                first = True
                for sentence in split_sentences(paragraph):
                    for piece, glue in self._fit(sentence):
                        yield _Unit(piece, page.page_number, self.count_tokens(piece), first, glue)
                        first = False

    def _fit(self, sentence: str) -> Iterator[tuple[str, str]]:
        """Yield (piece, glue) pairs, each piece within chunk_size tokens.
        Joining the pieces with their glue reproduces the sentence exactly."""
        limit = self.config.chunk_size
        if self.count_tokens(sentence) <= limit:
            yield sentence, " "
            return

        words: list[str] = []
        used = 0
        for word in sentence.split():
            cost = self.count_tokens(word)
            if cost > limit:  # e.g. a 5,000-character URL or base64 blob
                if words:
                    yield " ".join(words), " "
                    words, used = [], 0
                for i, piece in enumerate(self._split_long_word(word)):
                    yield piece, (" " if i == 0 else "")
                continue
            if used + cost > limit and words:
                yield " ".join(words), " "
                words, used = [], 0
            words.append(word)
            used += cost
        if words:
            yield " ".join(words), " "

    def _split_long_word(self, word: str) -> Iterator[str]:
        """Character-level split. Every character is emitted exactly once."""
        limit = self.config.chunk_size
        start = 0
        while start < len(word):
            length = min(len(word) - start, limit * 8)  # optimistic first guess
            while length > 1 and self.count_tokens(word[start : start + length]) > limit:
                length //= 2
            yield word[start : start + length]
            start += length

    # -------------------------------------------------- packing
    def chunk(self, pages: Iterable[PageText]) -> Iterator[TextChunk]:
        size, overlap = self.config.chunk_size, self.config.chunk_overlap
        window: list[_Unit] = []
        window_tokens = 0
        has_new_content = False
        index = 0

        for unit in self._units(pages):
            if window and window_tokens + unit.tokens > size:
                yield self._build(index, window)
                index += 1
                window = self._overlap_tail(window, overlap)
                window_tokens = sum(u.tokens for u in window)
                # Overlap must never stop the new unit from fitting.
                while window and window_tokens + unit.tokens > size:
                    window_tokens -= window.pop(0).tokens
                has_new_content = False

            window.append(unit)
            window_tokens += unit.tokens
            has_new_content = True

        if window and has_new_content:  # never emit a chunk that is pure overlap
            yield self._build(index, window)

    @staticmethod
    def _overlap_tail(window: list[_Unit], overlap: int) -> list[_Unit]:
        if overlap == 0:
            return []
        tail: list[_Unit] = []
        used = 0
        # len(tail) < len(window) guarantees forward progress.
        for unit in reversed(window[1:]):
            if used + unit.tokens > overlap:
                break
            tail.insert(0, unit)
            used += unit.tokens
        return tail

    @staticmethod
    def _build(index: int, units: list[_Unit]) -> TextChunk:
        parts: list[str] = []
        offsets: list[tuple[int, int]] = []
        length = 0
        previous: _Unit | None = None
        for unit in units:
            if previous is None:
                sep = ""
            elif unit.new_paragraph or unit.page != previous.page:
                sep = "\n\n"
            else:
                sep = unit.glue
            if previous is None or unit.page != previous.page:
                offsets.append((length + len(sep), unit.page))
            parts.append(sep + unit.text)
            length += len(sep) + len(unit.text)
            previous = unit
        return TextChunk(
            chunk_index=index,
            content="".join(parts),
            page_start=min(u.page for u in units),
            page_end=max(u.page for u in units),
            token_count=sum(u.tokens for u in units),
            page_offsets=tuple(offsets),
        )
