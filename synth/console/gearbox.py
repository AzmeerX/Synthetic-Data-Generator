"""The gearbox: look inside a run, stage by stage and call by call — and archive runs.

Purpose: show what every model in a run was asked, what it thought, what it
answered, and what became of the answer downstream (rows created, dropped with
the rule that dropped them, removed or confirmed by a reviewer), so an operator
can see how much of each stage's work survives. Runs cannot be deleted (stores
and ledgers are append-only, CLAUDE.md invariant 4); they can be archived:
hidden from the console's lists, reversibly, with no change to the database.
Contract: :func:`add_routes` registers ``GET /api/gearbox``,
``GET /api/gearbox/calls``, ``GET /api/gearbox/calls/{call_id}`` and
``POST /api/runs/{run_id}/archive``; :func:`archived_runs`. Stores written:
none (archive list: ``<profiles.local>/_archived_runs.yaml``). Token caps: none.
Spec: P1 §3 (J1 viewer), §9 (L2 telemetry), §10 (L4 change log).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from sqlalchemy import func, select

from synth import engines
from synth.api import query
from synth.config import local_profiles_dir
from synth.store import Store
from synth.store.tables import TABLES

CELL = 4000                      # characters of before/after JSON shown per change
PROBLEMS = ("error", "invalid", "repaired", "retried", "truncated", "empty", "dropped")
_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


# ------------------------------------------------------------------ archive (a console view setting, not a store)
def _archive_file() -> Path:
    return local_profiles_dir() / "_archived_runs.yaml"


def archived_runs() -> set[str]:
    path = _archive_file()
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return set(data.get("archived") or [])


def _set_archived(run_id: str, archived: bool) -> set[str]:
    ids = archived_runs()
    ids = ids | {run_id} if archived else ids - {run_id}
    path = _archive_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"archived": sorted(ids)}), encoding="utf-8")
    return ids


# ------------------------------------------------------------------ call health
def call_flags(call: dict[str, Any], dropped: int = 0) -> list[str]:
    """What went wrong with one model call (empty list = clean)."""
    text = ((call.get("response") or {}).get("text") or "").strip()
    out = []
    if call.get("error"):
        out.append("error")
    elif not call.get("schema_valid"):
        out.append("invalid")
    if call.get("repairs"):
        out.append("repaired")
    if call.get("retries"):
        out.append("retried")
    if (call.get("finish_reason") or "") == "length":
        out.append("truncated")
    if not text and not call.get("error"):
        out.append("empty")
    if dropped:
        out.append("dropped")
    return out


def _parsed(text: str) -> Any:
    text = (text or "").strip()
    for candidate in (text, (_BLOCK.search(text) or [None])[0]):
        if candidate:
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    return None


def _messages(request: dict[str, Any]) -> tuple[str, str]:
    msgs = (request or {}).get("messages") or []
    system = "\n\n".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
    user = "\n\n".join(m.get("content") or "" for m in msgs if m.get("role") != "system")
    return system, user


def _trim(value: Any) -> Any:
    value = query.clean(value)
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return value if len(text) <= CELL else text[:CELL] + "…"


def _stage_order(arm: str) -> list[tuple[str, str]]:
    eng = engines.ENGINES.get(arm)
    seen, out = set(), []
    for n in (eng.nodes if eng else ()):
        if n.stage and n.stage not in seen:
            seen.add(n.stage)
            out.append((n.stage, n.label))
    return out


def _calls(r, run_id: str, stage: str | None = None) -> list[dict[str, Any]]:
    t = TABLES["model_calls"]
    cols = [c for c in t.c if c.name != "request"]          # prompts are heavy; only the detail route reads them
    stmt = select(*cols).where(t.c.run_id == run_id)
    if stage:
        stmt = stmt.where(t.c.stage == stage)
    return r.rows(stmt.order_by(t.c.call_id))


def _dropped_by_call(r, run_id: str) -> Counter:
    t = TABLES["change_log"]
    stmt = (select(t.c.call_id, func.count()).where(t.c.run_id == run_id, t.c.op == "DROPPED",
                                                    t.c.call_id.is_not(None)).group_by(t.c.call_id))
    return Counter({cid: n for cid, n in r.execute(stmt)})


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, round(0.95 * (len(s) - 1)))]


def gearbox(r, run_row: dict[str, Any]) -> dict[str, Any]:
    run_id, arm = run_row["run_id"], run_row["arm"]
    calls = _calls(r, run_id)
    dropped = _dropped_by_call(r, run_id)
    cl = TABLES["change_log"]
    changes = r.rows(select(cl.c.stage, cl.c.object_type, cl.c.op, cl.c.rule_id, func.count().label("n"))
                     .where(cl.c.run_id == run_id)
                     .group_by(cl.c.stage, cl.c.object_type, cl.c.op, cl.c.rule_id))
    us = TABLES["unit_status"]
    units = r.rows(select(us.c.stage, us.c.status, func.count().label("n")).where(us.c.run_id == run_id)
                   .group_by(us.c.stage, us.c.status))

    stages: dict[str, dict[str, Any]] = {}

    def stage(name: str) -> dict[str, Any]:
        if name not in stages:
            stages[name] = {"stage": name, "label": name, "modules": set(), "endpoints": set(), "models": set(),
                            "calls": 0, "problem_calls": 0, "problems": Counter(), "tokens_in": 0, "tokens_out": 0,
                            "reasoning_tokens": 0, "cost_usd": 0.0, "latencies": [], "changes": Counter(),
                            "dropped": Counter(), "units": Counter()}
        return stages[name]

    for c in calls:
        s = stage(c["stage"] or "?")
        s["modules"].add(c.get("module") or "")
        s["endpoints"].add(c.get("endpoint") or "")
        s["models"].add(c.get("model_id") or "")
        s["calls"] += 1
        flags = call_flags(c, dropped.get(c["call_id"], 0))
        s["problems"].update(flags)
        s["problem_calls"] += bool(flags)
        s["tokens_in"] += c.get("tokens_in") or 0
        s["tokens_out"] += c.get("tokens_out") or 0
        s["reasoning_tokens"] += c.get("reasoning_tokens") or 0
        s["cost_usd"] += c.get("cost_usd") or 0.0
        s["latencies"].append(c.get("latency_ms") or 0)
    for ch in changes:
        s = stage(ch["stage"] or "?")
        s["changes"][f"{ch['op']} {ch['object_type']}"] += ch["n"]
        if ch["op"] == "DROPPED":
            s["dropped"][ch["rule_id"] or "unspecified"] += ch["n"]
    for u in units:
        stage(u["stage"] or "?")["units"][u["status"]] += u["n"]

    order = _stage_order(arm)
    labels = dict(order)
    ranked = [n for n, _ in order if n in stages] + sorted(n for n in stages if n not in labels)
    out = []
    for name in ranked:
        s = stages[name]
        lat = s.pop("latencies")
        out.append({**s, "label": labels.get(name, name),
                    "modules": sorted(m for m in s["modules"] if m), "endpoints": sorted(e for e in s["endpoints"] if e),
                    "models": sorted(m for m in s["models"] if m), "problems": dict(s["problems"]),
                    "changes": dict(s["changes"].most_common()), "dropped": dict(s["dropped"].most_common()),
                    "units": dict(s["units"]), "latency_ms_avg": int(sum(lat) / len(lat)) if lat else 0,
                    "latency_ms_p95": _p95(lat), "cost_usd": round(s["cost_usd"], 6)})
    total_calls = sum(s["calls"] for s in out)
    total_problem = sum(s["problem_calls"] for s in out)
    return {"run_id": run_id, "engine": arm,
            "engine_name": engines.ENGINES[arm].name if arm in engines.ENGINES else arm,
            "status": run_row["status"], "calls": total_calls, "problem_calls": total_problem,
            "dropped": sum(sum(s["dropped"].values()) for s in out), "stages": out}


# ------------------------------------------------------------------ routes
def add_routes(app: FastAPI, store: Store, run_row: Callable[..., Any], read_only: bool) -> None:
    @app.get("/api/gearbox")
    def get_gearbox(run_id: str) -> dict[str, Any]:
        with store.reader() as r:
            return gearbox(r, run_row(r, run_id))

    @app.get("/api/gearbox/calls")
    def list_calls(run_id: str, stage: str | None = None, problems: bool = False,
                   limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        with store.reader() as r:
            run_row(r, run_id)
            calls = _calls(r, run_id, stage)
            dropped = _dropped_by_call(r, run_id)
        rows = []
        for c in calls:
            flags = call_flags(c, dropped.get(c["call_id"], 0))
            if problems and not flags:
                continue
            text = (c.get("response") or {}).get("text") or ""
            rows.append({"call_id": c["call_id"], "stage": c["stage"], "module": c.get("module"),
                         "unit_id": c.get("unit_id"), "endpoint": c.get("endpoint"), "model": c.get("model_id"),
                         "prompt": c.get("prompt_name"), "tokens_in": c.get("tokens_in") or 0,
                         "tokens_out": c.get("tokens_out") or 0, "reasoning_tokens": c.get("reasoning_tokens") or 0,
                         "latency_ms": c.get("latency_ms") or 0, "finish_reason": c.get("finish_reason"),
                         "flags": flags, "dropped": dropped.get(c["call_id"], 0),
                         "error": (c.get("error") or "")[:300] or None, "preview": text[:160]})
        return {"total": len(rows), "calls": rows[offset:offset + limit]}

    @app.get("/api/gearbox/calls/{call_id}")
    def get_call(call_id: str) -> dict[str, Any]:
        with store.reader() as r:
            call = r.get("model_calls", call_id=call_id)
            if not call:
                raise HTTPException(status_code=404, detail=f"unknown call {call_id}")
            changes = r.select("change_log", call_id=call_id, order_by="change_id")
            unit = r.get("unit_status", run_id=call["run_id"], stage=call["stage"], unit_id=call["unit_id"]) \
                if call.get("unit_id") else None
            siblings = [c["call_id"] for c in r.select("model_calls", run_id=call["run_id"], stage=call["stage"],
                                                       unit_id=call["unit_id"], order_by="call_id")]
        dropped = sum(1 for c in changes if c["op"] == "DROPPED")
        system, user = _messages(call.get("request") or {})
        response = call.get("response") or {}
        text = response.get("text") or ""
        return {
            "call_id": call_id, "run_id": call["run_id"], "stage": call["stage"], "module": call.get("module"),
            "unit_id": call.get("unit_id"), "endpoint": call.get("endpoint"), "model": call.get("model_id"),
            "prompt_name": call.get("prompt_name"), "flags": call_flags(call, dropped),
            "attempt": (call.get("request") or {}).get("attempt"),
            "structured_mode": (call.get("request") or {}).get("structured_mode"),
            "system": system, "user": user, "reasoning": response.get("reasoning") or "", "output": text,
            "parsed": _parsed(text),
            "meta": {k: query.clean(call.get(k)) for k in (
                "tokens_in", "tokens_out", "reasoning_tokens", "tokens_in_counted", "cap_in", "cap_out",
                "latency_ms", "finish_reason", "schema_valid", "repairs", "retries", "cost_usd", "error")},
            "changes": [{"op": c["op"], "object_type": c["object_type"], "object_id": c["object_id"],
                         "rule_id": c.get("rule_id"), "before": _trim(c.get("before")),
                         "after": _trim(c.get("after"))} for c in changes],
            "unit": query.clean(unit) if unit else None,
            "unit_calls": siblings,                       # every attempt at this unit (retries after a failure)
        }

    @app.post("/api/runs/{run_id}/archive")
    def archive(run_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        if read_only:
            raise HTTPException(status_code=403, detail="demo mode: changes are disabled")
        with store.reader() as r:
            run_row(r, run_id)
        archived = bool(body.get("archived", True))
        _set_archived(run_id, archived)
        return {"run_id": run_id, "archived": archived}

