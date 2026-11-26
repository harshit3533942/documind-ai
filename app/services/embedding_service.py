"""Text -> vector embeddings.

`Embedder` is a small interface with two implementations:
  * SentenceTransformerEmbedder - the real local model (default all-MiniLM-L6-v2)
  * HashingEmbedder             - deterministic, dependency-free; used in tests
                                  so they run in milliseconds without the model.

All vectors are L2-normalised (length 1). For unit vectors, cosine similarity
is just the dot product, and pgvector's cosine distance `<=>` = 1 - similarity.

The real model is loaded lazily, once per process (thread-safe), because
loading takes seconds and ~100 MB RAM. The API process only needs it for query
embeddings; the heavy document embedding runs in the background worker.
"""

import hashlib
import math
import re
import threading
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class EmbeddingConfigError(RuntimeError):
    pass


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def max_tokens(self) -> int:
        """Max input tokens including special tokens; longer input is truncated."""
        ...

    def count_tokens(self, text: str) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# ------------------------------------------------------------------ real model
class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_name: str,
        *,
        expected_dimension: int,
        device: str = "cpu",
        batch_size: int = 32,
        query_prefix: str = "",
        document_prefix: str = "",
    ) -> None:
        self._model_name = model_name
        self._expected_dimension = expected_dimension
        self._device = device
        self._batch_size = batch_size
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._model = None
        self._lock = threading.Lock()        # guards one-time model loading
        self._infer_lock = threading.Lock()  # serialises tokenizer/model calls

    def _load(self):  # noqa: ANN202 - SentenceTransformer type is heavy to import
        if self._model is None:
            with self._lock:  # double-checked locking: load exactly once
                if self._model is None:
                    from sentence_transformers import SentenceTransformer  # heavy import, deferred

                    logger.info("Loading embedding model %s on %s", self._model_name, self._device)
                    model = SentenceTransformer(self._model_name, device=self._device)
                    # sentence-transformers >= 6 renamed the method; keep the old name as fallback.
                    get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
                    actual = get_dim()
                    if actual != self._expected_dimension:
                        # Fail loudly: storing 768-dim vectors in a VECTOR(384) column
                        # would error on every insert, or worse, silently mix models.
                        raise EmbeddingConfigError(
                            f"Model {self._model_name} produces {actual}-dim vectors but "
                            f"EMBEDDING_DIMENSION={self._expected_dimension}. Update the setting "
                            "and add a migration for the vector column."
                        )
                    self._model = model
        return self._model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._expected_dimension

    @property
    def max_tokens(self) -> int:
        return int(self._load().max_seq_length)

    # THREAD SAFETY: the HuggingFace fast tokenizer (Rust) raises
    # "RuntimeError: Already borrowed" when two threads use it at once - and
    # FastAPI runs `def` endpoints in a thread pool. All model/tokenizer use is
    # therefore serialised by `_infer_lock`. Cost is small: inference is
    # CPU-bound and PyTorch already parallelises one call across all cores.
    # (Found by the concurrent benchmark, see scripts/benchmark.py.)

    def count_tokens(self, text: str) -> int:
        """Word-piece tokens WITHOUT [CLS]/[SEP] (callers budget for those)."""
        tokenizer = self._load().tokenizer
        with self._infer_lock:
            return len(tokenizer(text, add_special_tokens=False, truncation=False)["input_ids"])

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        with self._infer_lock:
            vectors = model.encode(
                [self._document_prefix + t for t in texts],
                batch_size=self._batch_size,  # process N texts per forward pass
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        with self._infer_lock:
            vector = model.encode(
                self._query_prefix + text, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
            )
        return vector.tolist()


# ------------------------------------------------------------------ test double
_WORD = re.compile(r"\w+")


class HashingEmbedder:
    """Deterministic bag-of-words embedding via the hashing trick.

    Texts that share words get similar vectors, so retrieval *ordering* can be
    tested realistically - but it has no understanding of synonyms. Tests only.
    """

    def __init__(self, dimension: int = 384, max_tokens: int = 256) -> None:
        self._dimension = dimension
        self._max_tokens = max_tokens
        self.calls = 0          # how many embed_documents calls
        self.embedded_texts = 0  # how many texts were embedded in total

    model_name = "test/hashing-embedder"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def count_tokens(self, text: str) -> int:
        return len(_WORD.findall(text))

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for word in _WORD.findall(text.lower()):
            # Bucketing words, not security: declared explicitly so scanners agree.
            h = int.from_bytes(hashlib.md5(word.encode(), usedforsecurity=False).digest()[:4], "little")
            vec[h % self._dimension] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        self.embedded_texts += len(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


# ------------------------------------------------------------------ factory
@lru_cache
def get_embedder() -> SentenceTransformerEmbedder:
    """Process-wide singleton (the model is loaded on first use, not here)."""
    s = get_settings()
    return SentenceTransformerEmbedder(
        s.embedding_model_name,
        expected_dimension=s.embedding_dimension,
        device=s.embedding_device,
        batch_size=s.embedding_batch_size,
        query_prefix=s.embedding_query_prefix,
        document_prefix=s.embedding_document_prefix,
    )
