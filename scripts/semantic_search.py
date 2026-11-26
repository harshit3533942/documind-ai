"""Semantic search from the command line (debugging / demo).

Usage:
    python -m scripts.semantic_search --email user@example.com "how many leave days?"
"""

import argparse
import sys
import time

from app.database.connection import SessionLocal
from app.repositories.user_repository import UserRepository
from app.services.embedding_service import get_embedder
from app.services.retrieval_service import search_chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="search this user's documents")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("query")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = UserRepository(db).get_by_email(args.email)
        if user is None:
            print("No such user", file=sys.stderr)
            return 1
        embedder = get_embedder()
        embedder.embed_query("warm-up")  # load the model before timing
        started = time.perf_counter()
        results = search_chunks(db, embedder, user_id=user.id, query=args.query, top_k=args.top_k)
        took_ms = (time.perf_counter() - started) * 1000
        print(f'Query: "{args.query}"  ({took_ms:.0f} ms incl. query embedding)\n')
        for r in results:
            pages = f"p{r.page_start}" if r.page_start == r.page_end else f"p{r.page_start}-{r.page_end}"
            snippet = " ".join(r.content.split())[:160]
            print(f"  score={r.score:.3f}  {r.filename} [{pages}] #{r.chunk_index}\n    {snippet}...\n")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
