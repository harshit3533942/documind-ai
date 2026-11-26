import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import DocumentStatus


class DocumentStats(BaseModel):
    by_status: dict[str, int]
    documents: int
    chunks: int
    chunks_with_embeddings: int
    storage_bytes: int


class FailedDocument(BaseModel):
    """Monitoring view: metadata + error only, never document content."""

    id: uuid.UUID
    filename: str
    user_id: uuid.UUID
    user_email: str
    status: DocumentStatus
    error_message: str | None
    total_pages: int | None
    file_size: int
    created_at: datetime
    updated_at: datetime
