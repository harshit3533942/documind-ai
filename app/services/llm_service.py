"""LLM client abstraction for answer generation.

`LLMClient` is a one-method interface. `OpenAIChatClient` talks to any
OpenAI-compatible Chat Completions endpoint (OpenAI, Groq, Together, Ollama...)
selected by OPENAI_BASE_URL. Tests use a fake client - no network, no cost.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """Any failure talking to the LLM (timeout, auth, rate limit, bad response)."""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMClient(Protocol):
    @property
    def model_name(self) -> str: ...

    def complete(self, messages: list[ChatMessage], *, max_tokens: int, temperature: float) -> LLMResult: ...


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIChatClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str | None = None,
        timeout: float = 30,
        max_retries: int = 2,  # SDK retries 429/5xx/connection errors with backoff
        http_client=None,  # noqa: ANN001 - injectable httpx.Client for tests
    ) -> None:
        from openai import OpenAI  # imported lazily: optional dependency path

        self._model = model
        self._client = OpenAI(
            api_key=api_key or "not-needed-for-local-servers",
            # Never pass None: the SDK would then read the OPENAI_BASE_URL env var itself,
            # and Docker Compose sets it to "" when unset -> "missing protocol" on every call.
            base_url=base_url or DEFAULT_OPENAI_BASE_URL,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, messages: list[ChatMessage], *, max_tokens: int, temperature: float) -> LLMResult:
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APIError as exc:  # base class: timeouts, auth, rate limits, 5xx
            # Never log the request body (it contains user documents) or the key.
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from None
        if not response.choices or response.choices[0].message.content is None:
            raise LLMError("LLM returned an empty response")
        usage = response.usage
        return LLMResult(
            text=response.choices[0].message.content.strip(),
            model=response.model or self._model,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )


@lru_cache
def get_llm_client() -> LLMClient | None:
    """None when running in extractive (no-LLM) mode."""
    s = get_settings()
    if s.resolved_llm_provider != "openai":
        return None
    return OpenAIChatClient(
        api_key=s.openai_api_key.get_secret_value() if s.openai_api_key else None,
        model=s.openai_model,
        base_url=s.openai_base_url,
        timeout=s.llm_timeout_seconds,
    )
