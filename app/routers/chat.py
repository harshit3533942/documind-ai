"""Ask questions about your documents.

`def` (not `async def`): embedding the question is CPU work and the DB driver
is synchronous; FastAPI runs `def` endpoints in a thread pool, so the event
loop keeps serving other requests meanwhile.
"""

from fastapi import APIRouter, Depends

from app.core.dependencies import CurrentUser, DbSession, EmbedderDep, LLMDep
from app.core.rate_limit import limit_by_user
from app.schemas.chat import AskRequest, AskResponse, MessageRead
from app.services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/ask", response_model=AskResponse,
    dependencies=[Depends(limit_by_user("chat", "rate_limit_chat_per_user", 60))],
)
def ask(body: AskRequest, user: CurrentUser, db: DbSession, embedder: EmbedderDep, llm: LLMDep) -> AskResponse:
    """Answer a question from your documents, with page citations.

    * no `conversation_id` -> a new conversation is created
    * with `conversation_id` -> the question is added to it, and earlier
      messages are used to understand follow-ups ("what about sick leave?")
    * `document_ids` -> search only these documents
    """
    conversation, user_msg, assistant_msg, result = ChatService(db, embedder, llm).ask(
        user, question=body.question, conversation_id=body.conversation_id, document_ids=body.document_ids
    )
    return AskResponse(
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        user_message=MessageRead.from_message(user_msg),
        assistant_message=MessageRead.from_message(assistant_msg),
        search_query=result.search_query,
        timings_ms=result.timings_ms,
        cached=result.cached,
    )
