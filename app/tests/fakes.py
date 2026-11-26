"""Shared test doubles."""

from app.services.llm_service import ChatMessage, LLMResult

class FakeLLM:
    """Scripted LLM: returns queued replies (or raises) and records every prompt."""

    model_name = "fake-llm"

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages, *, max_tokens, temperature) -> LLMResult:
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else "ok [1]"
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(text=reply, model="fake-llm")

    def prompt(self, call: int = -1) -> str:
        return self.calls[call][-1].content


class FakeStreamingLLM(FakeLLM):
    """FakeLLM that also streams. A reply may be a list of pieces; an Exception
    among the pieces is raised at that point of the stream (mid-stream failure)."""

    def stream(self, messages, *, max_tokens, temperature):
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else ["ok ", "[1]"]
        if isinstance(reply, Exception):
            raise reply
        pieces = reply if isinstance(reply, list) else [w + " " for w in reply.split(" ")]
        for piece in pieces:
            if isinstance(piece, Exception):
                raise piece
            yield piece
