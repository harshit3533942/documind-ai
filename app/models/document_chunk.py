import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.document import Document

EMBEDDING_DIM = get_settings().embedding_dimension


class DocumentChunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised owner: technically derivable via documents.user_id, but
    # storing it here lets vector search filter `WHERE user_id = :me` without a
    # JOIN - the most important query in the system and the security boundary.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)  # order within document
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # A chunk can span a page boundary, so we store a page *range*.
    # For single-page chunks page_start == page_end. Pages are 1-based.
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    # [[char_offset, page], ...] - exact page of every character in `content`
    # (see chunking_service.TextChunk). NULL for chunks created before it existed.
    page_offsets: Mapped[list[list[int]] | None] = mapped_column(JSONB)
    # NULL until the embedding step runs.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        # Idempotency: re-running a processing task cannot insert chunk #3 twice.
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_id_chunk_index"),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="valid_page_range"),
        Index("ix_document_chunks_user_id", "user_id"),
        # HNSW = approximate nearest-neighbour graph index. Makes similarity
        # search fast on millions of vectors. vector_cosine_ops -> cosine distance (<=>).
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<DocumentChunk doc={self.document_id} idx={self.chunk_index} pages={self.page_start}-{self.page_end}>"
