import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import Message
from app.models.enums import MessageRole

MAX_DOCUMENT_SELECTION = 20


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = Field(default=None, description="Omit to start a new conversation")
    document_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=MAX_DOCUMENT_SELECTION,
        description="Restrict the search to these documents (default: all your completed documents)",
    )
    regenerate: bool = Field(
        default=False,
        description="Replace the conversation's LAST answer: its question/answer pair is removed and "
        "the question answered again, bypassing the answer cache. Needs `conversation_id`, and "
        "`question` must be that last question.",
    )

    @model_validator(mode="after")
    def regenerate_needs_conversation(self) -> "AskRequest":
        if self.regenerate and self.conversation_id is None:
            raise ValueError("regenerate requires conversation_id")
        return self

    @field_validator("question")
    @classmethod
    def strip_question(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Question must not be blank")
        return v

    @field_validator("document_ids")
    @classmethod
    def dedupe(cls, v: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return list(dict.fromkeys(v)) if v else None  # keep order, drop duplicates; [] -> None


class CitationRead(BaseModel):
    number: int
    document_id: uuid.UUID
    filename: str
    page_start: int
    page_end: int
    chunk_page_start: int
    chunk_page_end: int
    excerpt: str
    score: float
    chunk_id: uuid.UUID


class MessageRead(BaseModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime
    # assistant messages
    citations: list[CitationRead] = []
    found: bool | None = None
    mode: str | None = None  # "llm" | "extractive" | "none"
    grounded: bool | None = None
    feedback: Literal[-1, 1] | None = None  # the owner's rating of an answer
    # user messages
    document_ids: list[uuid.UUID] | None = None

    @classmethod
    def from_message(cls, m: Message) -> "MessageRead":
        meta: dict[str, Any] = m.sources if isinstance(m.sources, dict) else {}
        return cls(
            id=m.id, role=m.role, content=m.content, created_at=m.created_at,
            citations=[CitationRead(**c) for c in meta.get("citations", [])],
            found=meta.get("found"), mode=meta.get("mode"), grounded=meta.get("grounded"),
            document_ids=meta.get("document_ids"), feedback=m.feedback,
        )


class FeedbackRequest(BaseModel):
    rating: Literal[-1, 1] | None = Field(description="1 = helpful, -1 = not helpful, null = remove the rating")


class AskResponse(BaseModel):
    conversation_id: uuid.UUID
    conversation_title: str
    user_message: MessageRead
    assistant_message: MessageRead
    search_query: str
    timings_ms: dict[str, float]
    cached: bool = False


class ConversationRead(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message_preview: str | None = None
    last_message_at: datetime | None = None
