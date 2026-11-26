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
