"""P1-M2 live smoke test: is each local endpoint up, and which structured mode works?

Purpose: before an overnight run, prove that ``t1_micro`` and ``t2`` answer a tiny
request through the real gateway (caps, schema validation, reasoning
separation, telemetry), and find out whether the model honours
``response_format: json_schema, strict`` or needs ``json_then_validate``.
The result for ``t2`` is what DECISIONS.md records as the 14B structured mode.

Contract: :func:`smoke(config, store, endpoints)` -> per-endpoint dict with
``reachable``, ``models`` (ids the server lists), ``strict_schema`` and
``json_then_validate`` outcomes, ``recommended`` mode, and an error if any.
Stores written: ``model_calls`` (L2) when a store is given. Spec: P1 §9, §15 P1-M2.
"""

from __future__ import annotations

from typing import Any, Sequence

import httpx
from pydantic import BaseModel

from synth.config import SynthConfig
from synth.gateway.clients import chat_url
from synth.gateway.gateway import Gateway
from synth.gateway.types import GatewayError
from synth.prompts import load_prompt


class SmokeOut(BaseModel):
    answer: str
    number: int


QUESTION = "What is the capital of France, and how many sides does a triangle have?"


def models_url(base_url: str) -> str:
    return chat_url(base_url).rsplit("/chat/completions", 1)[0] + "/models"


def reachable(base_url: str, timeout: float = 5.0) -> tuple[bool, list[str], str | None]:
    try:
        resp = httpx.get(models_url(base_url), timeout=timeout)
    except httpx.HTTPError as exc:
        return False, [], f"{type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return False, [], f"HTTP {resp.status_code}"
    try:
        data = resp.json()
        ids = [m.get("id", "") for m in data.get("data", [])] if isinstance(data, dict) else []
    except ValueError:
        return False, [], "non-JSON /models reply"
    return True, ids, None


def smoke(config: SynthConfig, store=None, endpoints: Sequence[str] = ("t1_micro", "t2"),
          run_id: str | None = None) -> dict[str, Any]:
    prompt = load_prompt("smoke.v1")
    results: dict[str, Any] = {}
    for name in endpoints:
        ep = config.endpoint(name)
        ok, ids, error = reachable(ep.base_url)
        entry: dict[str, Any] = {"base_url": ep.base_url, "model": ep.model, "reachable": ok,
                                 "models": ids, "error": error}
        if not ok:
            results[name] = entry
            continue
        entry["model_listed"] = ep.model in ids
        for mode in ("strict_schema", "json_then_validate"):
            trial = config.model_copy(deep=True)
            trial.model_settings.endpoints[name].structured_mode = mode
            gateway = Gateway(trial, store=store)
            try:
                result = gateway.call(endpoint=name, prompt=prompt, user=QUESTION, output_model=SmokeOut,
                                      cap_in=200, cap_out=400, stage="SMOKE", module="M2",
                                      unit_id=f"{name}:{mode}", run_id=run_id)
                entry[mode] = {"ok": True, "repairs": result.repairs, "reply": result.data,
                               "reasoning_chars": len(result.reasoning),
                               "tokens_in": result.tokens_in, "tokens_out": result.tokens_out}
            except GatewayError as exc:
                entry[mode] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
        if entry["strict_schema"]["ok"] and not entry["strict_schema"]["repairs"]:
            entry["recommended"] = "strict_schema"
        elif entry["json_then_validate"]["ok"]:
            entry["recommended"] = "json_then_validate"
        else:
            entry["recommended"] = None
        if not entry["model_listed"]:
            # LM Studio answers an unknown id with whatever model is loaded: the trial proves nothing
            entry["recommended"] = None
            entry["error"] = (f"model id {ep.model!r} is not served at {ep.base_url}; "
                              f"use one of {ids} in configs/synth/models.yaml")
        results[name] = entry
    return results
