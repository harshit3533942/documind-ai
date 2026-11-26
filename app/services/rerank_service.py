"""Cross-encoder re-ranking (second retrieval stage).

Bi-encoder vs cross-encoder
---------------------------
* Bi-encoder (our embedding model): question and chunk are embedded
  SEPARATELY, then compared with cosine similarity. Chunk vectors are
  pre-computed, so searching millions of chunks is fast - but the model never
  sees question and chunk together.
* Cross-encoder: reads "[question] [SEP] [chunk]" as ONE input and outputs a
  relevance score. Much more accurate (attention across both texts), but it
  must run once per pair at query time - far too slow for the whole corpus.

So: the fast bi-encoder (+ keyword search) picks ~20 candidates, the slow,
accurate cross-encoder orders those 20. Enabled with RAG_RERANK_ENABLED=true.
"""

import threading
from collections.abc import Sequence
from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.retrieval_service import Reranker

logger = get_logger(__name__)


class CrossEncoderReranker:
    def __init__(self, model_name: str, device: str = "cpu", max_length: int = 512) -> None:
        self._model_name = model_name
        self._device = device
        self._max_length = max_length
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self):  # noqa: ANN202 - heavy type, imported lazily
        if self._model is None:
            with self._lock:  # two concurrent first requests must not load it twice
                if self._model is None:
                    from sentence_transformers import CrossEncoder

                    logger.info("Loading re-ranker %s", self._model_name)
                    self._model = CrossEncoder(self._model_name, device=self._device, max_length=self._max_length)
        return self._model

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        scores = self._load().predict([(query, p) for p in passages], show_progress_bar=False)
        return [float(s) for s in scores]


@lru_cache
def get_reranker() -> Reranker | None:
    """Process-wide re-ranker, or None when RAG_RERANK_ENABLED is false."""
    s = get_settings()
    if not s.rag_rerank_enabled:
        return None
    return CrossEncoderReranker(s.rag_rerank_model, device=s.embedding_device)
