"""Ask a question about a user's documents from the command line.

Usage:
    python -m scripts.ask --email user@example.com "How many vacation days do I get?"
    python -m scripts.ask --email user@example.com --show-sources "..."
"""

import argparse
import sys

from app.database.connection import SessionLocal
from app.repositories.user_repository import UserRepository
from app.services.embedding_service import get_embedder
from app.services.llm_service import get_llm_client
from app.services.rag_service import RagService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--show-sources", action="store_true")
    parser.add_argument("question")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = UserRepository(db).get_by_email(args.email)
        if user is None:
            print("No such user", file=sys.stderr)
            return 1
        embedder = get_embedder()
        embedder.embed_query("warm-up")
        result = RagService(db, embedder, get_llm_client()).answer(user_id=user.id, question=args.question)

        print(f"Q: {args.question}")
        print(f"[mode={result.mode} found={result.found} grounded={result.grounded} "
              f"{' '.join(f'{k}={v:.0f}ms' for k, v in result.timings_ms.items())}]")
        print(f"\n{result.answer}\n")
        for c in result.citations:
            pages = f"p.{c.page_start}" if c.page_start == c.page_end else f"pp.{c.page_start}-{c.page_end}"
            pages += f" (chunk spans pp.{c.chunk_page_start}-{c.chunk_page_end})" if c.chunk_page_start != c.chunk_page_end else ""
            print(f"  [{c.number}] {c.filename}, {pages}  (score {c.score:.2f})\n      \"{c.excerpt}\"")
        if args.show_sources:
            print("\nAll retrieved sources:")
            for s in result.sources:
                print(f"  ({s.number}) {s.score:.3f} {s.filename} p{s.page_start}-{s.page_end}: {s.excerpt[:90]}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
