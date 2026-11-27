"""Conversation history. Every route is scoped to the logged-in user; another
user's conversation id always returns 404."""

import json
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.core.dependencies import CurrentUser, DbSession
from app.repositories.conversation_repository import ConversationSummary
from app.schemas.chat import ConversationRead, ConversationRename, FeedbackRequest, MessageRead
from app.schemas.common import Page
from app.services import export_service
from app.services.chat_service import ChatService

router = APIRouter(prefix="/conversations", tags=["conversations"])

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


def _read(s: ConversationSummary) -> ConversationRead:
    c = s.conversation
    return ConversationRead(
        id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at,
        message_count=s.message_count, last_message_preview=s.last_message_preview, last_message_at=s.last_message_at,
        match_snippet=s.match_snippet,
    )


@router.get("", response_model=Page[ConversationRead])
def list_conversations(
    user: CurrentUser, db: DbSession, limit: PageLimit = 20, offset: PageOffset = 0,
    q: Annotated[str | None, Query(max_length=200, description="Search titles and messages (case-insensitive)")] = None,
) -> Page[ConversationRead]:
    """Your conversations, most recently active first. With `q`: only those
    whose title or any message contains it; `match_snippet` shows where."""
    items, total = ChatService(db).list_conversations(user, limit=limit, offset=offset, query=q)
    return Page[ConversationRead](items=[_read(s) for s in items], total=total, limit=limit, offset=offset)


@router.patch("/{conversation_id}", response_model=ConversationRead)
def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationRename, user: CurrentUser, db: DbSession
) -> ConversationRead:
    """Rename a conversation (its position in the list does not change)."""
    return _read(ChatService(db).rename_conversation(user, conversation_id, body.title))


@router.get(
    "/{conversation_id}/export",
    responses={200: {"content": {"text/markdown": {}, "application/json": {}},
                     "description": "The conversation as a file download"}},
)
def export_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, db: DbSession,
    format: Annotated[Literal["markdown", "json"], Query()] = "markdown",  # noqa: A002 - public API name
) -> Response:
    """Download the whole conversation with its citations (Markdown or JSON)."""
    conversation, messages = ChatService(db).export_conversation(user, conversation_id)
    if format == "json":
        body = json.dumps(export_service.to_json(conversation, messages), ensure_ascii=False, indent=2)
        media_type, filename = "application/json", export_service.export_filename(conversation.title, "json")
    else:
        body = export_service.to_markdown(conversation, messages)
        media_type, filename = "text/markdown; charset=utf-8", export_service.export_filename(conversation.title, "md")
    return Response(body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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


@router.put("/{conversation_id}/messages/{message_id}/feedback", response_model=MessageRead)
def set_message_feedback(
    conversation_id: uuid.UUID, message_id: uuid.UUID, body: FeedbackRequest, user: CurrentUser, db: DbSession
) -> MessageRead:
    """Rate an answer (1 = helpful, -1 = not helpful, null = clear). PUT: sending
    the same rating twice is harmless (idempotent)."""
    return MessageRead.from_message(ChatService(db).set_feedback(user, conversation_id, message_id, body.rating))


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    ChatService(db).delete_conversation(user, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
