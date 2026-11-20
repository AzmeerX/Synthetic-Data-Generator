"""The model gateway: the only path from a module to a model (CLAUDE.md invariant 5).

Purpose: count tokens, enforce stage caps, request structured output, validate
and repair it once, separate reasoning text, enforce the sovereign guard, and
write one L2 telemetry row per HTTP call.

Contract::

    result = gateway.call(endpoint="t1_micro", prompt=load_prompt("u5_micro.v1"),
                          user=content, output_model=U5Output, cap_in=650, cap_out=600,
                          run_id=..., stage="MICRO", module="U5", unit_id=chunk_id)
    result.data        # validated dict
    result.model       # the Pydantic instance (when output_model was given)
    result.call_ids    # every L2 row this call wrote (first + repair/retry)

Failure modes (all subclasses of GatewayError): ``BudgetExceeded`` before any
call (module shrinks or records SKIPPED_BUDGET); ``SchemaInvalid`` after the one
repair; ``OutputTruncated`` after the hard-cap retry (module splits its unit);
``EndpointUnavailable``; ``SovereignViolation``.
Stores written: ``model_calls`` (L2). Spec: P1 §9, §10.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from pydantic import BaseModel, ValidationError

from synth.config import SynthConfig
from synth.gateway.clients import OpenAICompatClient, ScriptedModelClient
from synth.gateway.schema import strict_schema
from synth.gateway.tokens import TokenCounter
from synth.gateway.types import (
    BudgetExceeded, ChatRequest, ChatResponse, GatewayError, ModelClient,
    OutputTruncated, SchemaInvalid, SovereignViolation,
)
from synth.ids import new_id
from synth.prompts import Prompt

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

REPAIR_INSTRUCTION = (
    "Your previous reply was not valid against the required JSON schema.\n"
    "Problem: {error}\n"
    "Return ONLY the corrected JSON object. No commentary, no markdown fences."
)


@dataclass
class GatewayResult:
    data: Any
    model: BaseModel | None
    text: str
    reasoning: str
    call_ids: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_in_counted: int = 0
    repairs: int = 0
    model_id: str = ""


def parse_json(text: str) -> Any:
    """Strict JSON first; then the first balanced ``{…}`` block (reasoning models chatter)."""
    cleaned = _FENCE.sub("", (text or "").strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for pos in range(start, len(cleaned)):
            ch = cleaned[pos]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start:pos + 1])
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    raise ValueError("no JSON object found in the reply")


class Gateway:
    def __init__(self, config: SynthConfig, clients: dict[str, ModelClient] | None = None,
                 store=None, counter: TokenCounter | None = None,
                 default_client: ModelClient | None = None) -> None:
        self.config = config
        self.store = store
        self.clients: dict[str, ModelClient] = dict(clients or {})
        self.default_client = default_client
        if counter is None:
            paths = {}
            for name, ep in config.model_settings.endpoints.items():
                paths[name] = config.resolve(ep.tokenizer) if ep.tokenizer else None
            counter = TokenCounter(paths)
        self.counter = counter

    # -- construction -------------------------------------------------------------
    @classmethod
    def scripted(cls, config: SynthConfig, store=None,
                 client: ScriptedModelClient | None = None) -> "Gateway":
        """Every endpoint answered by one :class:`ScriptedModelClient` (tests)."""
        return cls(config, store=store, default_client=client or ScriptedModelClient(),
                   counter=TokenCounter())

    def client_for(self, endpoint: str) -> ModelClient:
        if endpoint in self.clients:
            return self.clients[endpoint]
        if self.default_client is not None:
            return self.default_client
        ep = self.config.endpoint(endpoint)
        if "<" in ep.base_url or "<" in ep.model:      # a template placeholder, never a real endpoint
            raise GatewayError(f"endpoint {endpoint} is not configured (configs/synth/models.yaml): "
                               f"base_url={ep.base_url!r} model={ep.model!r}")
        client = OpenAICompatClient(ep)
        self.clients[endpoint] = client
        return client

    def count(self, text: str, endpoint: str | None = None) -> int:
        return self.counter.n(text, endpoint)

    # -- the call -------------------------------------------------------------------
    def call(self, *, endpoint: str, prompt: Prompt, user: str, cap_in: int, cap_out: int,
             stage: str, module: str, unit_id: str, run_id: str | None = None,
             output_model: type[BaseModel] | None = None, schema: dict | None = None,
             sovereign: bool = False, hard_cap_out: int | None = None,
             system_suffix: str = "") -> GatewayResult:
        ep = self.config.endpoint(endpoint)
        if sovereign and not ep.is_local():
            raise SovereignViolation(f"sovereign unit {unit_id} may not use non-local endpoint {endpoint}")
        counted, exact = self.counter.count(user, endpoint)
        if counted > cap_in:
            raise BudgetExceeded(counted, cap_in)
        if schema is None and output_model is not None:
            schema = strict_schema(output_model)
        system = prompt.text + (("\n\n" + system_suffix) if system_suffix else "")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        request = ChatRequest(endpoint=endpoint, model=ep.model, system=system, user=user,
                              messages=messages, max_tokens=cap_out + ep.reasoning_budget,
                              temperature=ep.temperature,
                              schema=schema, schema_name=prompt.name.replace(".", "_"),
                              structured_mode=ep.structured_mode, prompt_name=prompt.name)
        meta = dict(run_id=run_id, stage=stage, module=module, unit_id=unit_id,
                    endpoint=endpoint, prompt=prompt, counted=counted, exact=exact,
                    cap_in=cap_in, cap_out=cap_out)
        result = GatewayResult(data=None, model=None, text="", reasoning="", tokens_in_counted=counted)

        response = self._send(request, meta, result, repairs=0)
        if response.finish_reason == "length":
            hard = (hard_cap_out or min(cap_out * 2, max(ep.context_length // 2, cap_out))) + ep.reasoning_budget
            retry = ChatRequest(**{**request.__dict__, "max_tokens": hard, "attempt": "length_retry"})
            response = self._send(retry, meta, result, repairs=0, retries=1)
            if response.finish_reason == "length":
                raise OutputTruncated(f"{module} {unit_id}: reply hit max_tokens {hard} twice")

        data, error = self._validate(response.text, schema, output_model)
        if error is not None:
            repair_messages = messages + [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": REPAIR_INSTRUCTION.format(error=error[:600])},
            ]
            repair = ChatRequest(**{**request.__dict__, "messages": repair_messages,
                                    "user": request.user, "attempt": "repair"})
            response = self._send(repair, meta, result, repairs=1)
            result.repairs = 1
            data, error = self._validate(response.text, schema, output_model)
            if error is not None:
                self._mark_invalid(result)
                raise SchemaInvalid(f"{module} {unit_id}: reply invalid after one repair: {error[:300]}")
        result.data = data
        result.model = output_model.model_validate(data) if output_model is not None else None
        result.text = response.text
        result.reasoning = response.reasoning
        return result

    # -- internals --------------------------------------------------------------------
    def _send(self, request: ChatRequest, meta: dict, result: GatewayResult, repairs: int,
              retries: int = 0) -> ChatResponse:
        client = self.client_for(request.endpoint)
        error = None
        started = time.monotonic()
        try:
            response = client.chat(request)
        except GatewayError as exc:
            error = str(exc)
            self._log(request, None, meta, result, repairs, retries, error,
                      int((time.monotonic() - started) * 1000))
            raise
        valid, _ = self._validate(response.text, request.schema, None) if request.schema else (True, None)
        call_id = self._log(request, response, meta, result, repairs, retries + response.retries,
                            None, response.latency_ms, schema_valid=valid is not None)
        result.tokens_in += response.tokens_in
        result.tokens_out += response.tokens_out
        result.model_id = response.model or request.model
        _ = call_id
        return response

    def _validate(self, text: str, schema: dict | None, output_model: type[BaseModel] | None):
        try:
            data = parse_json(text)
        except ValueError as exc:
            return None, str(exc)
        if schema is not None:
            try:
                jsonschema.validate(data, schema)
            except jsonschema.ValidationError as exc:
                path = "/".join(str(p) for p in exc.absolute_path)
                return None, f"at '{path}': {exc.message}"
        if output_model is not None:
            try:
                output_model.model_validate(data)
            except ValidationError as exc:
                return None, str(exc).replace("\n", " ")
        return data, None

    def _log(self, request: ChatRequest, response: ChatResponse | None, meta: dict,
             result: GatewayResult, repairs: int, retries: int, error: str | None,
             latency_ms: int, schema_valid: bool = False) -> str:
        call_id = new_id("mcl")
        result.call_ids.append(call_id)
        if self.store is None:
            return call_id
        ep = self.config.endpoint(request.endpoint)
        tokens_in = response.tokens_in if response else 0
        tokens_out = response.tokens_out if response else 0
        cost = (tokens_in * ep.price_per_mtok_in + tokens_out * ep.price_per_mtok_out) / 1_000_000
        row = dict(
            call_id=call_id, run_id=meta["run_id"], stage=meta["stage"], module=meta["module"],
            unit_id=meta["unit_id"], endpoint=request.endpoint,
            model_id=(response.model if response and response.model else request.model),
            prompt_sha256=meta["prompt"].sha256, prompt_name=meta["prompt"].name,
            tokens_in=tokens_in, tokens_out=tokens_out,
            reasoning_tokens=response.reasoning_tokens if response else 0,
            tokens_in_counted=meta["counted"], tokens_counted_exact=meta["exact"],
            cap_in=meta["cap_in"], cap_out=request.max_tokens,
            latency_ms=latency_ms, finish_reason=response.finish_reason if response else None,
            schema_valid=schema_valid, repairs=repairs, retries=retries, cost_usd=cost,
            gpu_mem_mb_peak=_gpu_mem_mb(), error=error,
            request={"messages": request.messages, "max_tokens": request.max_tokens,
                     "attempt": request.attempt, "structured_mode": request.structured_mode},
            response={"text": response.text if response else None,
                      "reasoning": response.reasoning if response else None},
        )
        with self.store.begin(run_id=meta["run_id"], stage=meta["stage"], module=meta["module"]) as tx:
            tx.insert("model_calls", row)
        return call_id

    def _mark_invalid(self, result: GatewayResult) -> None:  # telemetry already says schema_valid=False
        return None


def _gpu_mem_mb() -> int | None:
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        return int(pynvml.nvmlDeviceGetMemoryInfo(handle).used / (1024 * 1024))
    except Exception:
        return None
