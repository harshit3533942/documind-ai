"""Import every model here so `Base.metadata` knows all tables (needed by Alembic)."""

from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus, MessageRole, TokenPurpose, UserRole
from app.models.message import Message
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.user_token import UserToken

__all__ = [
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "Message",
    "MessageRole",
    "RefreshToken",
    "TokenPurpose",
    "User",
    "UserRole",
    "UserToken",
]
