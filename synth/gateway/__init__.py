"""Model gateway (P1 §9): every model call goes through here."""

from synth.gateway.clients import OpenAICompatClient, ScriptedModelClient, chat_url, split_reasoning
from synth.gateway.gateway import Gateway, GatewayResult, parse_json
from synth.gateway.schema import empty_instance, strict_schema
from synth.gateway.tokens import TokenCounter, count_tokens, heuristic_tokens
from synth.gateway.types import (
    BudgetExceeded, ChatRequest, ChatResponse, EndpointUnavailable, GatewayError,
    ModelClient, OutputTruncated, SchemaInvalid, SovereignViolation,
)

__all__ = [
    "Gateway", "GatewayResult", "parse_json", "OpenAICompatClient", "ScriptedModelClient",
    "chat_url", "split_reasoning", "empty_instance", "strict_schema", "TokenCounter",
    "count_tokens", "heuristic_tokens", "BudgetExceeded", "ChatRequest", "ChatResponse",
    "EndpointUnavailable", "GatewayError", "ModelClient", "OutputTruncated", "SchemaInvalid",
    "SovereignViolation",
]
