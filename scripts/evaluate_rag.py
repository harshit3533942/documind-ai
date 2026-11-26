"""Measure retrieval and answer quality on a fixed, labelled dataset.

Usage:
    python -m scripts.evaluate_rag                 # all configurations
    python -m scripts.evaluate_rag --no-rerank     # skip the cross-encoder runs
    python -m scripts.evaluate_rag --misses        # also list every question that missed

What it does
------------
1. Builds a PDF from scripts/eval/dataset.json (12-page fictional handbook).
2. Processes it with the REAL pipeline (chunking + real embedding model) for a
   temporary user - inside ONE database transaction that is rolled back at
   the end, so nothing is left behind in the database.
3. For every configuration (vector / hybrid, with / without re-ranking) asks
   every question and measures:

   Hit@1      the best chunk contains the answer page
   Recall@5   any of the top 5 chunks contains the answer page
   MRR        mean of 1/rank of the first correct chunk (1.0 = always first)
   Cite acc.  the final answer (extractive mode) cites the correct page
   Refusal    unanswerable questions correctly answered with "not enough
              information" (higher = fewer hallucination risks)

Why labelled data: without numbers, "hybrid search is better" is an opinion.
With a fixed dataset every change to chunking, thresholds or models can be
compared objectively - and regressions are caught.
"""

import argparse
import io
import json
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentStatus, User, UserRole
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import get_embedder
from app.services.rag_service import RagService
from app.services.rerank_service import CrossEncoderReranker
from app.services.retrieval_service import RetrievedChunk, retrieve
from app.services.storage_service import LocalStorage

DATASET = Path(__file__).parent / "eval" / "dataset.json"
TOP_K = 5


def build_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=10.5) < 0:
            raise ValueError("Dataset page text does not fit on one PDF page")
    data = doc.tobytes()
    doc.close()
    return data


def covers(chunk: RetrievedChunk, page: int) -> bool:
    return chunk.page_start <= page <= chunk.page_end


@dataclass
class Scores:
    hits1: list[float] = field(default_factory=list)
    recall: list[float] = field(default_factory=list)
    rr: list[float] = field(default_factory=list)
    cite: list[float] = field(default_factory=list)
    refusals: list[float] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def add(self, ranked: list[RetrievedChunk], page: int) -> int | None:
        rank = next((i for i, c in enumerate(ranked, start=1) if covers(c, page)), None)
        self.hits1.append(1.0 if rank == 1 else 0.0)
        self.recall.append(1.0 if rank is not None else 0.0)
        self.rr.append(1.0 / rank if rank else 0.0)
        return rank


def pct(values: list[float]) -> str:
    return f"{100 * statistics.mean(values):5.1f}%" if values else "    -"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-rerank", action="store_true", help="skip configurations with the cross-encoder")
    parser.add_argument("--misses", action="store_true", help="list questions whose answer page was not ranked first")
    parser.add_argument("--database-url", default=None, help="default: DATABASE_URL from settings")
    args = parser.parse_args()

    settings = get_settings()
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    pages, questions, unanswerable = data["pages"], data["questions"], data["unanswerable"]

    engine = create_engine(args.database_url or settings.database_url)
    connection = engine.connect()
    outer = connection.begin()
    # create_savepoint: the pipeline's own commit() calls only release savepoints;
    # the outer transaction is rolled back below -> nothing persists.
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    original_mode = settings.rag_retrieval_mode
    try:
        user = User(name="RAG eval", email=f"eval-{uuid.uuid4().hex}@example.invalid",
                    hashed_password="!", role=UserRole.USER)
        db.add(user)
        db.flush()

        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            stored = storage.save_stream(io.BytesIO(build_pdf(pages)), user.id, 50 * 1024 * 1024)
            document = Document(
                user_id=user.id, filename="northwind-handbook.pdf", storage_key=stored.key, file_size=stored.size,
                mime_type="application/pdf", file_hash=stored.sha256, status=DocumentStatus.UPLOADED,
                total_pages=len(pages),
            )
            db.add(document)
            db.commit()

            print(f"Processing {len(pages)}-page dataset with {settings.embedding_model_name} ...", flush=True)
            embedder = get_embedder()
            result = DocumentProcessor(db, storage, embedder=embedder).process(document.id)
            if result.status != "completed":
                print(f"Processing failed: {result.error}", file=sys.stderr)
                return 1
            print(f"{result.chunk_count} chunks, {len(questions)} questions, {len(unanswerable)} unanswerable\n")

            reranker = None if args.no_rerank else CrossEncoderReranker(settings.rag_rerank_model)
            configs = [("vector", None), ("hybrid", None)]
            if reranker:
                configs += [("vector", reranker), ("hybrid", reranker)]

            rows = []
            for mode, rr in configs:
                settings.rag_retrieval_mode = mode  # RagService reads the mode from settings
                name = mode + (" + rerank" if rr else "")
                total, by_kind = Scores(), {"semantic": Scores(), "keyword": Scores()}
                rag = RagService(db, embedder, None, rr)
                for item in questions:
                    started = time.perf_counter()
                    ranked = retrieve(db, embedder, user_id=user.id, query=item["q"], top_k=TOP_K,
                                      min_score=settings.rag_min_score, reranker=rr, mode=mode)
                    total.latency_ms.append((time.perf_counter() - started) * 1000)
                    rank = total.add(ranked, item["page"])
                    by_kind[item["kind"]].add(ranked, item["page"])
                    if rank != 1:
                        total.misses.append(f"  rank={rank or '-':<2} p{item['page']:<3} {item['q']}")

                    answer = rag.answer(user_id=user.id, question=item["q"])
                    total.cite.append(1.0 if any(
                        c.page_start <= item["page"] <= c.page_end for c in answer.citations) else 0.0)
                for q in unanswerable:
                    total.refusals.append(0.0 if rag.answer(user_id=user.id, question=q).found else 1.0)
                rows.append((name, total, by_kind))

            print(f"| Configuration     | Hit@1  | Recall@{TOP_K} | MRR   | Hit@1 semantic | Hit@1 keyword "
                  f"| Cite acc. | Refusal | Retrieval p50 |")
            print("|-------------------|--------|----------|-------|----------------|---------------"
                  "|-----------|---------|---------------|")
            for name, s, k in rows:
                print(f"| {name:<17} | {pct(s.hits1)} | {pct(s.recall)}   | {statistics.mean(s.rr):.3f} "
                      f"| {pct(k['semantic'].hits1)}         | {pct(k['keyword'].hits1)}        "
                      f"| {pct(s.cite)}    | {pct(s.refusals)}  | {statistics.median(s.latency_ms):9.1f} ms  |")
            if args.misses:
                for name, s, _ in rows:
                    print(f"\nNot ranked first - {name}:")
                    print("\n".join(s.misses) or "  (none)")
    finally:
        settings.rag_retrieval_mode = original_mode
        db.close()
        outer.rollback()
        connection.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
