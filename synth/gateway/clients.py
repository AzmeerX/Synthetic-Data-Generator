"""Model clients: the OpenAI-compatible HTTP client and the scripted fake.

``OpenAICompatClient`` talks to LM Studio, vLLM, llama.cpp, Ollama's /v1, or a
hosted provider. ``ScriptedModelClient`` answers from canned responses keyed by
prompt name + input hash, or from a handler function, and otherwise returns a
schema-valid empty reply — so every test runs without a model server (P1 §9).
Clients never validate, retry on bad JSON, or log: the gateway does that.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable

import httpx

from synth import __version__
from synth.config import EndpointConfig, resolve_secret
from synth.gateway.schema import empty_instance
from synth.gateway.tokens import heuristic_tokens
from synth.gateway.types import ChatRequest, ChatResponse, EndpointUnavailable, GatewayError

#: ``<think>…</think>`` (Qwen, DeepSeek, Granite) and ``[THINK]…[/THINK]`` (Mistral reasoning models)
_THINK = re.compile(r"<think>(.*?)</think>|\[THINK\](.*?)\[/THINK\]", re.DOTALL | re.IGNORECASE)
_VERSION_SUFFIX = re.compile(r"/v\d+$")
USER_AGENT = f"synth/{__version__}"
#: Providers that ask for a stable per-conversation session header (routing and prompt caching).
#: One id per client, i.e. per endpoint per run.
SESSION_HEADERS = {"opencode.ai": "x-opencode-session"}


def _host(base_url: str) -> str:
    return base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()


def chat_url(base_url: str) -> str:
    """Same normalisation as Datanaut's backend (fixes the double ``/v1/v1`` trap)."""
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if _VERSION_SUFFIX.search(base_url):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def split_reasoning(content: str, reasoning_field: str | None = None) -> tuple[str, str]:
    """Separate ``<think>…</think>`` / ``[THINK]…[/THINK]`` (or a provider reasoning field) from the answer."""
    thoughts = [(m.group(1) or m.group(2) or "").strip() for m in _THINK.finditer(content or "")]
    answer = _THINK.sub("", content or "").strip()
    if reasoning_field:
        thoughts.insert(0, reasoning_field.strip())
    return answer, "\n\n".join(t for t in thoughts if t)


class OpenAICompatClient:
    """``POST {base}/chat/completions`` with timeouts and up to 3 retries with backoff."""

    RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

    def __init__(self, endpoint: EndpointConfig, retries: int = 3, backoff_s: float = 1.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.endpoint = endpoint
        self.retries = retries
        self.backoff_s = backoff_s
        self._client = httpx.Client(timeout=endpoint.timeout_s, transport=transport)
        self._session = f"synth-{uuid.uuid4().hex}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        host = _host(self.endpoint.base_url)
        for domain, name in SESSION_HEADERS.items():
            if host == domain or host.endswith("." + domain):
                headers[name] = self._session
        if self.endpoint.api_key_env:
            key = resolve_secret(self.endpoint.api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def body(self, request: ChatRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.schema is not None and request.structured_mode == "strict_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "strict": True, "schema": request.schema},
            }
        return body

    def chat(self, request: ChatRequest) -> ChatResponse:
        url = chat_url(self.endpoint.base_url)
        body = self.body(request)
        last_error = ""
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                resp = self._client.post(url, json=body, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code in self.RETRY_STATUS:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                elif resp.status_code >= 400:
                    raise GatewayError(f"HTTP {resp.status_code} from {url}: {resp.text[:300]}")
                else:
                    return self._parse(resp, started, attempt, url)
            if attempt < self.retries:
                time.sleep(self.backoff_s * (2 ** attempt))
        raise EndpointUnavailable(f"{url} unreachable after {self.retries + 1} attempts: {last_error}")

    def _parse(self, resp: httpx.Response, started: float, attempt: int, url: str) -> ChatResponse:
        try:
            data = resp.json()
        except ValueError as exc:
            raise GatewayError(f"{url} returned non-JSON (HTTP {resp.status_code}): "
                               f"{resp.text[:200]!r}") from exc
        if not isinstance(data, dict) or not data.get("choices"):
            raise GatewayError(f"{url} answered without 'choices' — wrong path or server? "
                               f"body: {str(data)[:200]}")
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text, reasoning = split_reasoning(message.get("content") or "",
                                          message.get("reasoning_content") or message.get("reasoning"))
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return ChatResponse(
            text=text, reasoning=reasoning,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0)
            or (heuristic_tokens(reasoning) if reasoning else 0),
            finish_reason=choice.get("finish_reason") or "stop",
            latency_ms=int((time.monotonic() - started) * 1000),
            model=data.get("model") or "", retries=attempt, raw=data,
        )


Handler = Callable[[ChatRequest], "str | dict | list | ChatResponse"]


class ScriptedModelClient:
    """A fake model for tests.

    Lookup order for each request: ``(prompt_name, input_hash)`` in ``responses``,
    then ``prompt_name`` in ``responses``, then ``handlers[prompt_name]``, then a
    schema-valid empty reply. A queued list under a key is consumed in order
    (useful for "first pass, then recheck"); the last one repeats. Synth
    replies are always JSON objects, so a list under a key is always a queue.
    """

    def __init__(self, responses: dict[Any, Any] | None = None,
                 handlers: dict[str, Handler] | None = None,
                 default: Handler | None = None) -> None:
        self.responses: dict[Any, Any] = dict(responses or {})
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.default = default
        self.calls: list[ChatRequest] = []

    def add(self, prompt_name: str, reply: Any, input_hash: str | None = None) -> None:
        key = (prompt_name, input_hash) if input_hash else prompt_name
        self.responses[key] = reply

    def queue(self, prompt_name: str, *replies: Any) -> None:
        self.responses[prompt_name] = list(replies)

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        reply: Any = None
        for key in ((request.prompt_name, request.input_hash), request.prompt_name):
            if key in self.responses:
                value = self.responses[key]
                if isinstance(value, list) and value:
                    reply = value.pop(0) if len(value) > 1 else value[0]
                else:
                    reply = value
                break
        if reply is None and request.prompt_name in self.handlers:
            reply = self.handlers[request.prompt_name](request)
        if reply is None and self.default is not None:
            reply = self.default(request)
        if reply is None:
            reply = empty_instance(request.schema) if request.schema else {}
        if isinstance(reply, ChatResponse):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        finish = "stop"
        if request.max_tokens and heuristic_tokens(text) > request.max_tokens:
            # behave like a real server: stop at max_tokens, so the length paths run in tests
            text, finish = text[:int(request.max_tokens * 3.5)], "length"
        return ChatResponse(text=text, tokens_in=heuristic_tokens(request.system + request.user),
                            tokens_out=heuristic_tokens(text), finish_reason=finish,
                            latency_ms=1, model=f"scripted:{request.model}")

