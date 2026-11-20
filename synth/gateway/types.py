"""Gateway data types and errors (P1 §9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class GatewayError(Exception):
    """Base class for every failure the gateway reports to a module."""


class BudgetExceeded(GatewayError):
    """Content tokens exceed the stage cap. The module must shrink or record SKIPPED_BUDGET."""

    def __init__(self, counted: int, cap: int, what: str = "content") -> None:
        super().__init__(f"{what} is {counted} tokens, cap is {cap}")
        self.counted = counted
        self.cap = cap


class SchemaInvalid(GatewayError):
    """The reply failed validation even after the one repair call."""


class OutputTruncated(GatewayError):
    """The reply hit max_tokens twice (cap, then hard cap). The module splits its unit."""


class EndpointUnavailable(GatewayError):
    """The model server could not be reached (after retries)."""


class SovereignViolation(GatewayError):
    """A sovereign document tried to reach a non-local endpoint."""


@dataclass
class ChatRequest:
    endpoint: str
    model: str
    system: str
    user: str
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float = 0.0
    schema: dict[str, Any] | None = None
    schema_name: str = "Output"
    structured_mode: str = "strict_schema"
    prompt_name: str = ""
    attempt: str = "first"            # first | repair | length_retry

    @property
    def input_hash(self) -> str:
        import hashlib

        return hashlib.sha256(self.user.encode("utf-8")).hexdigest()


@dataclass
class ChatResponse:
    text: str
    reasoning: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    finish_reason: str = "stop"
    latency_ms: int = 0
    model: str = ""
    retries: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    """Turns one request into one reply. Clients do not validate or log."""

    def chat(self, request: ChatRequest) -> ChatResponse: ...
