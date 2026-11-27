import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import MessageRole, str_enum_column

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Message(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(str_enum_column(MessageRole, "message_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Retrieved source metadata.
    #   assistant: {"citations": [{"number": 1, "document_id": "...", "filename": "x.pdf",
    #               "page_start": 3, "page_end": 3, "excerpt": "...", "score": 0.82, ...}],
    #               "found": true, "mode": "llm", "grounded": true,
    #               "search_query": "...", "model": "...", "timings_ms": {...}}
    #   user:      {"document_ids": [...]}  (document selection for that question)
    # A snapshot: history still shows what was cited even if a document is later
    # deleted. JSONB: read-mostly and always loaded with the message, so a
    # separate citations table would add JOINs without real benefit.
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # The owner's rating of an assistant answer: +1 helpful, -1 not helpful,
    # NULL not rated. A real column (not inside `sources`) so it can be counted
    # and filtered cheaply for quality dashboards and the evaluation set.
    feedback: Mapped[int | None] = mapped_column(SmallInteger)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    __table_args__ = (
        # Load a conversation's messages in chronological order.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
        CheckConstraint("feedback IS NULL OR feedback IN (-1, 1)", name="feedback_valid"),
    )
