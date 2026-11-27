import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DocumentStatus, str_enum_column

if TYPE_CHECKING:
    from app.models.document_chunk import DocumentChunk
    from app.models.user import User


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Original name shown to the user (untrusted input - never used as a path).
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Server-generated storage key (e.g. "<uuid>.pdf"). Unique so two docs can
    # never point at the same file on disk.
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)  # bytes
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # SHA-256 of file content: lets us detect a user uploading the same PDF twice.
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Format the user uploaded. DOCX/TXT/MD are converted to PDF at upload, so
    # the stored file (and mime_type) is always a PDF; this keeps the original type.
    source_type: Mapped[str] = mapped_column(String(10), server_default="pdf", default="pdf", nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum_column(DocumentStatus, "document_status"),
        default=DocumentStatus.UPLOADED,
        nullable=False,
    )
    total_pages: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)  # why processing failed
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which model produced this document's chunk embeddings. Vectors from
    # different models are NOT comparable, so retrieval and embedding reuse
    # must only combine vectors from the same model.
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    # Generated after processing (summary_service). NULL = not generated yet.
    summary: Mapped[str | None] = mapped_column(Text)
    suggested_questions: Mapped[list[str] | None] = mapped_column(JSONB)
    summarized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped["User"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("file_size > 0", name="file_size_positive"),
        CheckConstraint("source_type IN ('pdf', 'docx', 'txt', 'md')", name="source_type_valid"),
        CheckConstraint("total_pages IS NULL OR total_pages >= 0", name="total_pages_non_negative"),
        # "My documents, newest first" -> composite index serves filter + sort.
        Index("ix_documents_user_id_created_at", "user_id", "created_at"),
        # Admin dashboard: count / list failed documents quickly.
        Index("ix_documents_status", "status"),
        # Duplicate-upload lookup per user.
        Index("ix_documents_user_id_file_hash", "user_id", "file_hash"),
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename!r} status={self.status}>"
