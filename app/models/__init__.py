"""Import every model here so `Base.metadata` knows all tables (needed by Alembic)."""

from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus, MessageRole, UserRole
from app.models.message import Message
from app.models.user import User

__all__ = [
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "Message",
    "MessageRole",
    "User",
    "UserRole",
]
