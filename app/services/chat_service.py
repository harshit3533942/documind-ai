"""Chat: ask a question inside a conversation, keep history, enable follow-ups.

ask() flow
----------
1. Validate the document selection: every id must belong to the user (404
   otherwise - never reveal other users' ids) and be processed (409).
2. Load the conversation (owner only, 404) or create a new one titled after
   the first question.
3. Take the last HISTORY_MESSAGES messages as context for follow-up questions.
4. Run RAG (retrieval is always scoped to the user).
5. Save the question and the answer (with citation snapshot) in ONE
   transaction and bump the conversation's updated_at.
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import Conversation, Document, DocumentStatus, Message, MessageRole, User
from app.repositories.conversation_repository import ConversationRepository, ConversationSummary
from app.services import cache_service
from app.services.embedding_service import Embedder
from app.services.llm_service import ChatMessage, LLMClient
from app.services.rag_service import RagAnswer, RagService

logger = get_logger(__name__)

HISTORY_MESSAGES = 6  # last 3 question/answer pairs
TITLE_MAX_CHARS = 80


def make_title(question: str) -> str:
    if len(question) <= TITLE_MAX_CHARS:
        return question
    cut = question[:TITLE_MAX_CHARS].rsplit(" ", 1)[0]
    return (cut or question[:TITLE_MAX_CHARS]).rstrip(" ,.;:") + "…"


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class ChatService:
    """`embedder`/`llm` are only needed by ask(); history endpoints omit them."""

    def __init__(self, db: Session, embedder: Embedder | None = None, llm: LLMClient | None = None) -> None:
        self.db = db
        self.conversations = ConversationRepository(db)
        self.embedder = embedder
        self.llm = llm

    # ------------------------------------------------------------ ask
    def ask(
        self,
        user: User,
        *,
        question: str,
        conversation_id: uuid.UUID | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
    ) -> tuple[Conversation, Message, Message, RagAnswer]:
        if document_ids:
            self._check_documents(user, document_ids)

        if conversation_id is not None:
            conversation = self.get_conversation(user, conversation_id)
            history = [
                ChatMessage(m.role.value, m.content)
                for m in self.conversations.recent_messages(conversation.id, HISTORY_MESSAGES)
            ]
        else:
            conversation = self.conversations.create(user.id, make_title(question))
            history = []

        if self.embedder is None:
            raise RuntimeError("ChatService.ask requires an embedder")
        result = self._answer(user, question, document_ids, history)

        try:
            user_msg = self.conversations.add_message(
                conversation.id, MessageRole.USER, question,
                {"document_ids": [str(d) for d in document_ids]} if document_ids else None,
            )
            assistant_msg = self.conversations.add_message(
                conversation.id, MessageRole.ASSISTANT, result.answer, self._answer_metadata(result)
            )
            self.conversations.touch(conversation.id)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(conversation)
        logger.info(
            "Chat answer user=%s conversation=%s mode=%s found=%s citations=%d total=%.0fms",
            user.id, conversation.id, result.mode, result.found, len(result.citations), result.timings_ms.get("total", 0),
        )
        return conversation, user_msg, assistant_msg, result

    def _answer(
        self, user: User, question: str, document_ids: Sequence[uuid.UUID] | None, history: list[ChatMessage]
    ) -> RagAnswer:
        """RAG, with the Redis answer cache for first questions of a conversation.
        Follow-ups depend on history, so they are never cached."""
        if self.embedder is None:  # explicit check: `assert` is stripped under python -O
            raise RuntimeError("ChatService.ask requires an embedder")
        rag = RagService(self.db, self.embedder, self.llm)
        if history:
            return rag.answer(user_id=user.id, question=question, document_ids=document_ids, history=history)

        started = time.perf_counter()
        version = cache_service.document_version(user.id)
        key = None
        if version is not None:
            mode_tag = f"{self.llm.model_name if self.llm else 'extractive'}:{self.embedder.model_name}"
            key = cache_service.answer_key(user.id, version, question, document_ids, mode_tag)
            if (hit := cache_service.get_answer(key)) is not None:
                result = RagAnswer.from_dict(hit)
                result.cached = True
                result.timings_ms = {"cache": round((time.perf_counter() - started) * 1000, 1)}
                result.timings_ms["total"] = result.timings_ms["cache"]
                return result

        result = rag.answer(user_id=user.id, question=question, document_ids=document_ids)
        if key is not None and result.llm_error is None:  # never cache a degraded (fallback) answer
            cache_service.set_answer(key, result.to_dict())
        return result

    def _check_documents(self, user: User, document_ids: Sequence[uuid.UUID]) -> None:
        rows = self.db.execute(
            select(Document.id, Document.filename, Document.status).where(
                Document.id.in_(list(document_ids)), Document.user_id == user.id
            )
        ).all()
        found = {r.id: r for r in rows}
        if missing := [d for d in document_ids if d not in found]:
            # Same 404 for "does not exist" and "belongs to someone else".
            raise NotFoundError(f"Document not found: {missing[0]}")
        if not_ready := [r for r in rows if r.status != DocumentStatus.COMPLETED]:
            r = not_ready[0]
            raise ConflictError(f"Document '{r.filename}' is not ready yet (status: {r.status.value})")

    @staticmethod
    def _answer_metadata(result: RagAnswer) -> dict[str, Any]:
        return _jsonable({
            "citations": [asdict(c) for c in result.citations],
            "found": result.found,
            "mode": result.mode,
            "grounded": result.grounded,
            "search_query": result.search_query,
            "model": result.model,
            "llm_error": result.llm_error,
            "timings_ms": result.timings_ms,
            "cached": result.cached,
        })

    # ------------------------------------------------------------ conversations
    def get_conversation(self, user: User, conversation_id: uuid.UUID) -> Conversation:
        conversation = self.conversations.get_for_user(conversation_id, user.id)
        if conversation is None:
            raise NotFoundError("Conversation not found")  # also for other users' conversations
        return conversation

    def list_conversations(self, user: User, *, limit: int, offset: int) -> tuple[list[ConversationSummary], int]:
        return self.conversations.list_for_user(user.id, limit=limit, offset=offset)

    def conversation_summary(self, user: User, conversation_id: uuid.UUID) -> ConversationSummary:
        conversation = self.get_conversation(user, conversation_id)
        last = self.conversations.recent_messages(conversation.id, 1)
        return ConversationSummary(
            conversation,
            self.conversations.message_count(conversation.id),
            last[0].content[:120] if last else None,
            last[0].created_at if last else None,
        )

    def list_messages(self, user: User, conversation_id: uuid.UUID, *, limit: int, offset: int) -> tuple[list[Message], int]:
        conversation = self.get_conversation(user, conversation_id)
        return self.conversations.list_messages(conversation.id, limit=limit, offset=offset)

    def delete_conversation(self, user: User, conversation_id: uuid.UUID) -> None:
        conversation = self.get_conversation(user, conversation_id)
        self.conversations.delete(conversation)
        self.db.commit()
        logger.info("Conversation %s deleted by user %s", conversation_id, user.id)
