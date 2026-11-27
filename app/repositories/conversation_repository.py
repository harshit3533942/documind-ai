"""Data access for conversations and messages.

OWNERSHIP RULE: every read takes `user_id` and puts it in the WHERE clause.
Messages are only reachable through a conversation the user owns.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import Conversation, Message, MessageRole

PREVIEW_CHARS = 120


@dataclass(frozen=True)
class ConversationSummary:
    conversation: Conversation
    message_count: int
    last_message_preview: str | None
    last_message_at: datetime | None
    match_snippet: str | None = None  # search: the first message containing the query


LIKE_ESCAPE = "\\"


def like_pattern(query: str) -> str:
    r"""'50%_off' -> '%50\%\_off%': the user's % and _ are literal characters,
    not wildcards (otherwise a search for "%" would match everything)."""
    escaped = query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def snippet(text: str, query: str, width: int = 60) -> str:
    """~2*width characters of `text` around the first occurrence of `query`."""
    at = text.lower().find(query.lower())
    if at < 0:
        return text[: 2 * width]
    start, end = max(0, at - width), min(len(text), at + len(query) + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


class ConversationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------ conversations
    def create(self, user_id: uuid.UUID, title: str) -> Conversation:
        conversation = Conversation(user_id=user_id, title=title)
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def get_for_user(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation | None:
        return self.db.scalar(
            select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        )

    def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, offset: int, query: str | None = None
    ) -> tuple[list[ConversationSummary], int]:
        """ONE query for the page, however many conversations: message counts and
        the last message come from correlated subqueries, not a query per row
        (that would be the N+1 problem).

        `query`: case-insensitive substring search in the title OR any message.
        ILIKE '%...%' cannot use a normal B-tree index; the pg_trgm GIN indexes
        on conversations.title and messages.content make it fast anyway."""
        count_sq = (
            select(func.count(Message.id)).where(Message.conversation_id == Conversation.id).scalar_subquery()
        )
        last_content_sq = (
            select(func.left(Message.content, PREVIEW_CHARS))
            .where(Message.conversation_id == Conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        last_at_sq = select(func.max(Message.created_at)).where(Message.conversation_id == Conversation.id).scalar_subquery()

        conditions = [Conversation.user_id == user_id]
        match_sq = None
        if query:
            pattern = like_pattern(query)
            content_matches = Message.content.ilike(pattern, escape=LIKE_ESCAPE)
            conditions.append(or_(
                Conversation.title.ilike(pattern, escape=LIKE_ESCAPE),
                exists().where(Message.conversation_id == Conversation.id, content_matches),
            ))
            match_sq = (
                select(Message.content)
                .where(Message.conversation_id == Conversation.id, content_matches)
                .order_by(Message.created_at, Message.id)
                .limit(1)
                .scalar_subquery()
            )

        total = self.db.scalar(select(func.count()).select_from(Conversation).where(*conditions)) or 0
        columns = [Conversation, count_sq, last_content_sq, last_at_sq]
        if match_sq is not None:
            columns.append(match_sq)
        rows = self.db.execute(
            select(*columns)
            .where(*conditions)
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .limit(limit)
            .offset(offset)
        ).all()
        summaries = []
        for c, n, preview, at, *match in rows:
            found = match[0] if match else None
            summaries.append(ConversationSummary(c, n or 0, preview, at, snippet(found, query) if found and query else None))
        return summaries, total

    def message_count(self, conversation_id: uuid.UUID) -> int:
        return self.db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)) or 0

    def touch(self, conversation_id: uuid.UUID) -> None:
        """Bump updated_at so the conversation moves to the top of the sidebar.
        clock_timestamp() (real time) rather than now() (transaction start):
        activity order must not depend on transaction boundaries."""
        self.db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )

    def rename(self, conversation: Conversation, title: str) -> None:
        # Plain UPDATE (no touch()): renaming is not "activity", the
        # conversation keeps its place in the most-recent-first list.
        self.db.execute(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(title=title, updated_at=Conversation.updated_at)
            .execution_options(synchronize_session=False)
        )
        conversation.title = title

    def all_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        return list(self.db.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
        ))

    def delete(self, conversation: Conversation) -> None:
        self.db.delete(conversation)  # messages removed by ON DELETE CASCADE
        self.db.flush()

    # ------------------------------------------------------------ messages
    def add_message(
        self, conversation_id: uuid.UUID, role: MessageRole, content: str, sources: dict[str, Any] | None = None
    ) -> Message:
        # clock_timestamp(), not the column default now(): now() is the
        # TRANSACTION start time, so a question and its answer saved in one
        # transaction would get identical timestamps and an unstable order.
        message = Message(
            conversation_id=conversation_id, role=role, content=content, sources=sources,
            created_at=func.clock_timestamp(),
        )
        self.db.add(message)
        self.db.flush()
        return message

    def list_messages(self, conversation_id: uuid.UUID, *, limit: int, offset: int) -> tuple[list[Message], int]:
        total = self.message_count(conversation_id)
        rows = self.db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), total

    def get_message(self, conversation_id: uuid.UUID, message_id: uuid.UUID) -> Message | None:
        """Callers must have checked that the user owns `conversation_id`."""
        return self.db.scalar(
            select(Message).where(Message.id == message_id, Message.conversation_id == conversation_id)
        )

    def delete_messages(self, messages: list[Message]) -> None:
        for m in messages:
            self.db.delete(m)
        self.db.flush()

    def recent_messages(self, conversation_id: uuid.UUID, limit: int) -> list[Message]:
        """The last `limit` messages, oldest first (conversation context for follow-ups)."""
        rows = self.db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))
