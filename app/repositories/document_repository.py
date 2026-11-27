"""Data access for documents.

OWNERSHIP RULE: every method that reads a user's documents takes `user_id`
and puts it in the WHERE clause. There is deliberately no public
"get document by id" without an owner - so forgetting an ownership check is
impossible at the call site.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentStatus, User


class DocumentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, document: Document) -> Document:
        self.db.add(document)
        self.db.flush()
        return document

    def get_for_user(self, document_id: uuid.UUID, user_id: uuid.UUID) -> Document | None:
        return self.db.scalar(
            select(Document).where(Document.id == document_id, Document.user_id == user_id)
        )

    def find_by_hash(self, user_id: uuid.UUID, file_hash: str) -> Document | None:
        return self.db.scalar(
            select(Document).where(Document.user_id == user_id, Document.file_hash == file_hash).limit(1)
        )

    def list_active(self, user_id: uuid.UUID, limit: int = 100) -> list[Document]:
        active = (DocumentStatus.UPLOADED, DocumentStatus.QUEUED, DocumentStatus.PROCESSING)
        return list(self.db.scalars(
            select(Document)
            .where(Document.user_id == user_id, Document.status.in_(active))
            .order_by(Document.created_at.desc())
            .limit(limit)
        ))

    def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: DocumentStatus | None = None,
    ) -> tuple[list[Document], int]:
        conditions = [Document.user_id == user_id]
        if status is not None:
            conditions.append(Document.status == status)

        total = self.db.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
        rows = self.db.scalars(
            select(Document)
            .where(*conditions)
            .order_by(Document.created_at.desc(), Document.id)  # id = stable tie-break
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), total

    def list_chunks(
        self, document_id: uuid.UUID, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[DocumentChunk], int]:
        # user_id filter on the chunk itself: isolation holds even if a caller
        # forgets to check the parent document first.
        conditions = (DocumentChunk.document_id == document_id, DocumentChunk.user_id == user_id)
        total = self.db.scalar(select(func.count()).select_from(DocumentChunk).where(*conditions)) or 0
        rows = self.db.scalars(
            select(DocumentChunk).where(*conditions).order_by(DocumentChunk.chunk_index).limit(limit).offset(offset)
        ).all()
        return list(rows), total

    # ------------------------------------------------------------ admin-only queries
    # Metadata only (never chunk content): admins monitor the system, they do
    # not read users' documents.
    def admin_status_counts(self) -> dict[str, int]:
        rows = self.db.execute(select(Document.status, func.count()).group_by(Document.status))
        counts = {s.value: 0 for s in DocumentStatus}
        counts.update({status.value: n for status, n in rows})
        return counts

    def admin_totals(self) -> dict[str, int]:
        docs, size = self.db.execute(select(func.count(), func.coalesce(func.sum(Document.file_size), 0))).one()
        chunks, embedded = self.db.execute(
            select(func.count(), func.count(DocumentChunk.embedding))  # count(col) skips NULLs
        ).one()
        return {"documents": docs, "storage_bytes": int(size), "chunks": chunks, "chunks_with_embeddings": embedded}

    def admin_list_failed(self, *, limit: int, offset: int) -> tuple[list[tuple[Document, str]], int]:
        cond = Document.status == DocumentStatus.FAILED
        total = self.db.scalar(select(func.count()).select_from(Document).where(cond)) or 0
        rows = self.db.execute(
            select(Document, User.email)
            .join(User, User.id == Document.user_id)  # one query, no N+1
            .where(cond)
            .order_by(Document.updated_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [(d, email) for d, email in rows], total

    def admin_get(self, document_id: uuid.UUID) -> Document | None:
        return self.db.get(Document, document_id)

    def delete(self, document: Document) -> None:
        self.db.delete(document)  # chunks removed by ON DELETE CASCADE
        self.db.flush()
