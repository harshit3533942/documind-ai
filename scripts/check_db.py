"""Verify the database is reachable, pgvector is installed and migrations ran.

Usage:  python -m scripts.check_db
"""

import sys

from sqlalchemy import inspect, text

from app.database.connection import engine

EXPECTED_TABLES = {"users", "documents", "document_chunks", "conversations", "messages"}


def main() -> int:
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()
        print(f"[ok] Connected to PostgreSQL {version}")

        vector = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar()
        if not vector:
            print("[fail] pgvector extension not installed - run: alembic upgrade head")
            return 1
        print(f"[ok] pgvector {vector}")

        # Cosine distance sanity check: identical vectors -> 0, orthogonal -> 1.
        same = conn.execute(text("SELECT '[1,0]'::vector <=> '[1,0]'::vector")).scalar()
        orth = conn.execute(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")).scalar()
        print(f"[ok] cosine distance same={same} orthogonal={orth}")

    tables = set(inspect(engine).get_table_names())
    missing = EXPECTED_TABLES - tables
    if missing:
        print(f"[fail] Missing tables: {sorted(missing)}")
        return 1
    print(f"[ok] Tables present: {sorted(EXPECTED_TABLES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
