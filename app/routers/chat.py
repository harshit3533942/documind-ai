"""Ask questions about your documents.

`def` (not `async def`): embedding the question is CPU work and the DB driver
is synchronous; FastAPI runs `def` endpoints in a thread pool, so the event
loop keeps serving other requests meanwhile. The streaming endpoint's sync
generator is likewise iterated in the thread pool by Starlette.
"""

from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.dependencies import CurrentUser, DbSession, EmbedderDep, LLMDep, RerankerDep
from app.core.logging import get_logger
from app.core.rate_limit import limit_by_user
from app.core.sse import sse_event, sse_response
from app.models import Conversation, Message
from app.schemas.chat import AskRequest, AskResponse, MessageRead
from app.services.chat_service import ChatService
from app.services.rag_service import RagAnswer

router = APIRouter(prefix="/chat", tags=["chat"])
logger = get_logger(__name__)

# Both endpoints share ONE budget ("chat"): streaming is not a way around the limit.
_chat_limit = Depends(limit_by_user("chat", "rate_limit_chat_per_user", 60))


def _response(conversation: Conversation, user_msg: Message, assistant_msg: Message, result: RagAnswer) -> AskResponse:
    return AskResponse(
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        user_message=MessageRead.from_message(user_msg),
        assistant_message=MessageRead.from_message(assistant_msg),
        search_query=result.search_query,
        timings_ms=result.timings_ms,
        cached=result.cached,
    )


@router.post("/ask", response_model=AskResponse, dependencies=[_chat_limit])
def ask(
    body: AskRequest, user: CurrentUser, db: DbSession, embedder: EmbedderDep, llm: LLMDep, reranker: RerankerDep
) -> AskResponse:
    """Answer a question from your documents, with page citations.

    * no `conversation_id` -> a new conversation is created
    * with `conversation_id` -> the question is added to it, and earlier
      messages are used to understand follow-ups ("what about sick leave?")
    * `document_ids` -> search only these documents
    """
    return _response(*ChatService(db, embedder, llm, reranker).ask(
        user, question=body.question, conversation_id=body.conversation_id, document_ids=body.document_ids
    ))


@router.post(
    "/stream", dependencies=[_chat_limit], response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}},
                     "description": "Server-Sent Events: `meta`, then `token`*, then `done` (or `error`)"}},
)
def ask_stream(
    body: AskRequest, user: CurrentUser, db: DbSession, embedder: EmbedderDep, llm: LLMDep, reranker: RerankerDep
) -> StreamingResponse:
    """Like /chat/ask, but the answer is streamed as it is generated.

    Events:
    * `meta`  - `{conversation_id, conversation_title}` (sent immediately)
    * `token` - `{text}`: the next piece of the answer
    * `done`  - the full AskResponse (same body as /chat/ask). Its answer text
      is authoritative - replace the streamed text with it.
    * `error` - `{message}`: generation failed; nothing was saved

    Invalid document / conversation ids fail with a normal 404/409 before the
    stream starts.
    """
    service = ChatService(db, embedder, llm, reranker)
    ctx = service.start_ask(
        user, question=body.question, conversation_id=body.conversation_id, document_ids=body.document_ids
    )

    def events() -> Iterator[str]:
        yield sse_event("meta", {"conversation_id": str(ctx.conversation.id),
                                 "conversation_title": ctx.conversation.title})
        try:
            for kind, payload in service.ask_stream(ctx):
                if kind == "token":
                    yield sse_event("token", {"text": payload})
                else:  # "done"
                    yield sse_event("done", _response(*payload).model_dump(mode="json"))  # type: ignore[misc]
        except Exception:
            # The 200 status line is already sent: report the failure in-band.
            logger.exception("Streaming answer failed for user %s", user.id)
            yield sse_event("error", {"message": "Could not generate an answer. Please try again."})

    return sse_response(events())
