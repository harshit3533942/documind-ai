import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import DocumentStatus


class DocumentRead(BaseModel):
    """Public view of a document. storage_key / file_hash are internal and not exposed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    file_size: int
    mime_type: str
    source_type: str  # pdf | docx | txt | md (the stored file is always a PDF)
    status: DocumentStatus
    total_pages: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None
    # Filled in by the background worker shortly after processing completes.
    summary: str | None = None
    suggested_questions: list[str] | None = None
    summarized_at: datetime | None = None


class DocumentChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chunk_index: int
    content: str
    page_start: int
    page_end: int
    token_count: int | None


class DocumentStatusRead(BaseModel):
    """Lightweight payload for frontend polling."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DocumentStatus
    total_pages: int | None
    error_message: str | None
    updated_at: datetime
