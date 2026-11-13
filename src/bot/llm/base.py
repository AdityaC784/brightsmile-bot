"""Provider-neutral types for chat and embedding calls."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass
class ChatRequest:
    model: str
    system: str
    messages: list[Message]
    max_tokens: int
    temperature: float | None = None
    json_mode: bool = False
    # Provider-specific knobs from configs/models.yaml (e.g. Groq `reasoning_effort`).
    options: dict = field(default_factory=dict)

    def cache_payload(self, provider: str) -> dict:
        d = asdict(self)
        d["provider"] = provider
        return d


@dataclass
class ChatResponse:
    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cached: bool = False


class ChatProvider(Protocol):
    name: str

    def complete(self, req: ChatRequest) -> ChatResponse: ...

    def list_models(self) -> list[str]: ...


class LLMError(RuntimeError):
    """Raised when a provider call fails after retries or returns unusable output."""
