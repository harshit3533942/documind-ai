"""Conversation history. Every route is scoped to the logged-in user; another
user's conversation id always returns 404."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.dependencies import CurrentUser, DbSession
from app.repositories.conversation_repository import ConversationSummary
from app.schemas.chat import ConversationRead, MessageRead
from app.schemas.common import Page
from app.services.chat_service import ChatService

router = APIRouter(prefix="/conversations", tags=["conversations"])

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


def _read(s: ConversationSummary) -> ConversationRead:
    c = s.conversation
    return ConversationRead(
        id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at,
        message_count=s.message_count, last_message_preview=s.last_message_preview, last_message_at=s.last_message_at,
    )


@router.get("", response_model=Page[ConversationRead])
def list_conversations(user: CurrentUser, db: DbSession, limit: PageLimit = 20, offset: PageOffset = 0) -> Page[ConversationRead]:
    """Your conversations, most recently active first."""
    items, total = ChatService(db).list_conversations(user, limit=limit, offset=offset)
    return Page[ConversationRead](items=[_read(s) for s in items], total=total, limit=limit, offset=offset)


@router.get("/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ConversationRead:
    return _read(ChatService(db).conversation_summary(user, conversation_id))


@router.get("/{conversation_id}/messages", response_model=Page[MessageRead])
def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: PageOffset = 0,
) -> Page[MessageRead]:
    """Messages oldest first; each assistant message carries its citations."""
    messages, total = ChatService(db).list_messages(user, conversation_id, limit=limit, offset=offset)
    return Page[MessageRead](items=[MessageRead.from_message(m) for m in messages], total=total, limit=limit, offset=offset)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    ChatService(db).delete_conversation(user, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
