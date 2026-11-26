"""Run the processing pipeline manually (until Celery takes over in Phase 7).

Usage:
    python -m scripts.process_documents --pending             # all uploaded/queued docs
    python -m scripts.process_documents --missing-embeddings  # completed docs lacking vectors
    python -m scripts.process_documents --id <uuid> [--force]
"""

import argparse
import sys
import time
import uuid

from app.core.config import get_settings
from app.database.connection import SessionLocal
from app.services.document_processing_service import (
    DocumentProcessor,
    documents_missing_embeddings,
    pending_document_ids,
)
from app.services.storage_service import LocalStorage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", type=uuid.UUID, help="process one document")
    group.add_argument("--pending", action="store_true", help="process all uploaded/queued documents")
    group.add_argument("--missing-embeddings", action="store_true", help="re-process completed docs without vectors")
    parser.add_argument("--force", action="store_true", help="re-process even if completed")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        processor = DocumentProcessor(db, LocalStorage(get_settings().storage_dir))
        force = args.force or args.missing_embeddings
        if args.id:
            ids = [args.id]
        elif args.pending:
            ids = pending_document_ids(db)
        else:
            ids = documents_missing_embeddings(db, processor.embedder.model_name)
        if not ids:
            print("Nothing to process.")
        for doc_id in ids:
            started = time.perf_counter()
            r = processor.process(doc_id, force=force)
            took = time.perf_counter() - started
            if r.status == "completed":
                detail = (f"{r.chunk_count} chunks ({r.embedded_count} embedded, {r.reused_count} reused), "
                          f"{r.total_pages} pages in {took:.2f}s")
            else:
                detail = r.error or ""
            print(f"{doc_id}: {r.status} {detail}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
