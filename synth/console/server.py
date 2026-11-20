"""Synth Console — the operator UI's HTTP server (``py -m synth console``).

Purpose: one local web app to explore a run's verified graph, read documents with
their grounded spans, follow any object back to its source sentence, review the
escalation queue, see token/cost metrics, and start/resume runs, ingests and
smoke tests. It serves the static front end in ``synth/console/static/``.
Contract: :func:`create_console` returns a FastAPI app; the routes are listed in
``docs/CONSOLE_API.md``. Reads go through the stores (and J1's query functions);
control routes only start the same CLI commands a person would type, as jobs.
Stores written: none (jobs write through the normal pipeline). Token caps: none.
Spec: P1 §3 J1/J2 (read side), J0 (control side). Bind to 127.0.0.1 only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from synth import engines
from synth import states as S
from synth.api import query
from synth.config import REPO_ROOT, SynthConfig
from synth.console import admin, gearbox, ontologies, settings
from synth.console.jobs import JobBusy, JobManager
from synth.store import Store

STATIC = Path(__file__).parent / "static"
PIPELINE = [S.INGEST, S.GLINER, S.TIME, S.LOCAL, S.MICRO, S.CONTRACT, S.T2, S.ARM_EXTRACT,
            S.RESOLVE, S.GATE, S.FRONTIER, S.COMMIT, S.FINALIZE]
RELATION_MODES = {"micro", "gliner", "gliner+micro"}
GLINER_MODES = {"gliner2", "heuristic"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ------------------------------------------------------------------ read helpers
#: A RUNNING run with no console job and no unit started for this long was interrupted (process killed or
#: crashed: J0 never got to write its end status). One unit takes at most timeout x retries (~40 min).
STALE_AFTER = timedelta(minutes=45)
RUNNING = "RUNNING"                                  # runs.status while J0 works (runner.run)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _liveness(r, row: dict[str, Any], live_ids: set[str]) -> tuple[str, bool]:
    """(status to show, live). The stored status is never rewritten: Resume continues an interrupted run."""
    if row["status"] != RUNNING:
        return row["status"], False
    if row["run_id"] in live_ids:
        return RUNNING, True
    stamps = [_as_utc(u.get("ts")) for u in r.select("unit_status", run_id=row["run_id"])]
    stamps = [s for s in stamps + [_as_utc(row.get("started_at"))] if s]
    if stamps and datetime.now(UTC) - max(stamps) < STALE_AFTER:
        return RUNNING, True                         # running outside the console (CLI)
    return "INTERRUPTED", False


def _run_summary(r, row: dict[str, Any], live_ids: set[str] | None = None) -> dict[str, Any]:
    run_id, manifest = row["run_id"], row.get("manifest") or {}
    status, live = _liveness(r, row, live_ids or set())
    edges = Counter(e["status"] for e in r.select("macro_edges", run_id=run_id))
    escalations = r.select("escalations", run_id=run_id)
    return {
        "run_id": run_id, "arm": row["arm"], "engine": row["arm"],
        "engine_name": engines.ENGINES[row["arm"]].name if row["arm"] in engines.ENGINES else row["arm"],
        "status": status, "stored_status": row["status"], "live": live,
        "archived": run_id in gearbox.archived_runs(),
        "started_at": _iso(row.get("started_at")), "finished_at": _iso(row.get("finished_at")),
        "profile": manifest.get("profile"), "relation_mode": manifest.get("relation_mode"),
        "models": manifest.get("models") or {}, "documents": len(manifest.get("doc_ids") or []),
        "counts": {"entities": len(r.select("entities", run_id=run_id, status=S.ACTIVE)),
                   "edges_committed": edges.get(S.COMMITTED, 0), "edges_rejected": edges.get(S.REJECTED, 0),
                   "escalations": len(escalations),
                   "held": edges.get(S.HELD_FOR_HUMAN, 0)
                   + sum(1 for e in escalations if e["outcome"] == S.HELD_FOR_HUMAN)},
    }


def _live_run_ids(jobs) -> set[str]:
    return {j.run_id for j in jobs.running({"run", "resume"}) if j.run_id}


def _runs(r) -> list[dict[str, Any]]:
    rows = [x for x in r.select("runs") if x["arm"] in engines.ENGINES]
    return sorted(rows, key=lambda x: x["run_id"], reverse=True)


def _docs(r) -> dict[str, dict[str, Any]]:
    return {d["doc_id"]: d for d in r.select("documents")}


def _sentence_index(r, doc_ids) -> dict[str, dict[str, Any]]:
    """sentence_id -> sentence row, for the given documents."""
    rows = query.select_in(r, "sentences", "doc_id", list(doc_ids))
    return {s["sentence_id"]: s for s in rows}


def _span_out(span: dict[str, Any], docs: dict[str, dict[str, Any]], sentences: dict[str, dict[str, Any]]):
    ids = span.get("sentence_ids") or []
    first = sentences.get(ids[0]) if ids else None
    return {"span_id": span.get("span_id"), "doc_id": span.get("doc_id"),
            "doc": (docs.get(span.get("doc_id")) or {}).get("filename"),
            "sentence_idx": first["idx"] if first else None,
            "start": span.get("char_start"), "end": span.get("char_end"), "quote": span.get("quote"),
            "grounding": span.get("grounding"), "score": span.get("fuzzy_score")}


def _graph_edges(r, run_id: str, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [e["edge_id"] for e in edges]
    links = query.select_in(r, "edge_spans", "edge_id", ids)
    spans = query.spans_by_id(r, [x["span_id"] for x in links])
    docs_of: dict[str, set[str]] = defaultdict(set)
    for x in links:
        doc = (spans.get(x["span_id"]) or {}).get("doc_id")
        if doc:
            docs_of[x["edge_id"]].add(doc)
    times = query.select_in(r, "edge_times", "edge_id", ids)
    refs = {t["time_id"]: t for t in query.select_in(r, "time_refs", "time_id", [x["time_id"] for x in times])}
    time_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in times:
        ref = refs.get(x["time_id"]) or {}
        start, end = ref.get("value_start"), ref.get("value_end")
        label = start if not end or end == start else f"{start or '…'} – {end}"
        time_of[x["edge_id"]].append({"role": x["role"], "label": label or ref.get("surface"),
                                      "value_start": start, "value_end": end, "time_id": x["time_id"]})
    return [{"id": e["edge_id"], "source": e["src_entity_id"], "target": e["dst_entity_id"],
             "rel": e["rel"], "status": e["status"], "confidence": e.get("confidence"),
             "negated": bool(e.get("negated")), "stance": e.get("stance"), "tier": e.get("tier_last"),
             "description": e.get("description"),
             "doc_ids": sorted(docs_of.get(e["edge_id"], ())), "time": time_of.get(e["edge_id"], [])}
            for e in edges]


def _labels(r, ids) -> dict[str, str]:
    ids = [i for i in ids if isinstance(i, str)]
    out = {e["entity_id"]: e["canonical_name"] for e in query.select_in(r, "entities", "entity_id",
                                                                       [i for i in ids if i.startswith("ent_")])}
    edges = query.select_in(r, "macro_edges", "edge_id", [i for i in ids if i.startswith("mad_")])
    names = {e["entity_id"]: e["canonical_name"] for e in query.select_in(
        r, "entities", "entity_id", [x for e in edges for x in (e["src_entity_id"], e["dst_entity_id"])])}
    for e in edges:
        out[e["edge_id"]] = f"{names.get(e['src_entity_id'], '?')} —{e['rel']}→ {names.get(e['dst_entity_id'], '?')}"
    return out


def demo_store() -> Store:
    """An in-memory store holding a scripted ``full`` run of the mini corpus (the
    rule-based model stand-ins from the test suite): the console without any models."""
    from synth.gateway import ScriptedModelClient
    from synth.orchestrator import runner
    from synth.store import test_store
    from tests.synth import fake_models
    from tests.synth.factories import MINI_CORPUS

    store = test_store()
    runner.ingest(MINI_CORPUS, store=store)
    for arm in ("full", "minus0"):
        runner.run(arm=arm, store=store, scripted=ScriptedModelClient(handlers=fake_models.handlers()))
    return store


# ------------------------------------------------------------------ the app
def create_console(store: Store, config: SynthConfig | None = None,
                   jobs: JobManager | None = None, read_only: bool = False) -> FastAPI:
    """``read_only`` (demo mode) refuses every control route with 403."""
    app = FastAPI(title="Synth Console", version="1")
    jobs = jobs or JobManager()
    app.state.jobs = jobs

    def run_row(r, run_id: str) -> dict[str, Any]:
        row = r.get("runs", run_id=run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        return row

    # ---- static front end
    @app.get("/", include_in_schema=False)
    def index():
        page = STATIC / "index.html"
        if not page.exists():
            return JSONResponse({"detail": "console front end not built (synth/console/static/index.html)"}, 503)
        return FileResponse(page)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- overview, runs
    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        with store.reader() as r:
            runs = _runs(r)
            return {"runs": len(runs), "documents": len(r.select("documents")),
                    "latest_run": runs[0]["run_id"] if runs else None,
                    "db": str(store.engine.url).split("@")[-1],
                    "jobs_running": len(jobs.running()), "read_only": read_only}

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        with store.reader() as r:
            live = _live_run_ids(jobs)
            return [_run_summary(r, row, live) for row in _runs(r)]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        with store.reader() as r:
            out = _run_summary(r, run_row(r, run_id), _live_run_ids(jobs))
            units = r.select("unit_status", run_id=run_id)
            by_stage: dict[str, Counter] = defaultdict(Counter)
            for u in units:
                by_stage[u["stage"]][u["status"]] += 1
            order = [s for s in PIPELINE if s in by_stage] + sorted(set(by_stage) - set(PIPELINE))
            out["stages"] = [{"stage": s, "total": sum(by_stage[s].values()), "done": by_stage[s][S.DONE],
                              "pending": by_stage[s][S.PENDING], "failed": by_stage[s][S.FAILED],
                              "dead_letter": by_stage[s][S.DEAD_LETTER]} for s in order]
            out["coverage"] = dict(Counter(c["terminal"] for c in r.select("coverage_final", run_id=run_id)))
            out["dead_letter"] = [{"stage": u["stage"], "unit_id": u["unit_id"], "error": u["error"]}
                                  for u in units if u["status"] == S.DEAD_LETTER]
            return out

    @app.get("/api/runs/{run_id}/graph")
    def graph(run_id: str) -> dict[str, Any]:
        with store.reader() as r:
            run_row(r, run_id)
            entities = r.select("entities", run_id=run_id)
            active = {e["entity_id"]: e for e in entities if e["status"] == S.ACTIVE}
            merged: dict[str, list[str]] = defaultdict(list)
            for e in entities:
                if e.get("merged_into"):
                    merged[query.survivor_of(r, e["entity_id"])].append(e["entity_id"])
            mentions = r.select("mentions", run_id=run_id)
            survivor = {e["entity_id"]: (query.survivor_of(r, e["entity_id"]) if e.get("merged_into")
                                         else e["entity_id"]) for e in entities}
            counts, docs = Counter(), defaultdict(set)
            for m in mentions:
                owner = survivor.get(m.get("entity_id"))
                if owner:
                    counts[owner] += 1
                    docs[owner].add(m["doc_id"])
            nodes = [{"id": eid, "label": e["canonical_name"], "type": e["type"], "subtype": e.get("subtype"),
                      "status": e["status"], "mentions": counts.get(eid, 0),
                      "docs": sorted(docs.get(eid, ()) | ({e["doc_id"]} if e.get("doc_id") else set())),
                      "source": e.get("source"), "merged": sorted(merged.get(eid, []))}
                     for eid, e in active.items()]
            edges = [e for e in _graph_edges(r, run_id, r.select("macro_edges", run_id=run_id))
                     if e["source"] in active and e["target"] in active]
            return {"nodes": nodes, "edges": edges}

    @app.get("/api/runs/{run_id}/entities/{entity_id}")
    def entity(run_id: str, entity_id: str) -> dict[str, Any]:
        found = query.get_entity(store, run_id, entity_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no entity {entity_id}")
        e = found["entity"] or {}
        members = [m["entity_id"] for m in found["members"]]
        with store.reader() as r:
            docs = _docs(r)
            sentences = _sentence_index(r, {m["doc_id"] for m in found["mentions"]})
            t = r.table("macro_edges")
            rows = r.select("macro_edges", order_by="edge_id", run_id=run_id,
                            where=t.c.src_entity_id.in_(members) | t.c.dst_entity_id.in_(members))
            edges = _graph_edges(r, run_id, rows)
            names = _labels(r, [x for ed in edges for x in (ed["source"], ed["target"])]
                            + [x for res in found["resolution_history"] for x in (res["entity_a"], res["entity_b"])])
            texts = {}
            mention_out = []
            for m in found["mentions"]:
                span = m.get("span") or {}
                ids = span.get("sentence_ids") or []
                sent = sentences.get(ids[0]) if ids else None
                if sent and sent["doc_id"] not in texts:
                    row = max(r.select("doc_texts", doc_id=sent["doc_id"]), key=lambda x: x["text_version"])
                    texts[sent["doc_id"]] = row["canonical_text"]
                text = texts.get(sent["doc_id"], "") if sent else ""
                mention_out.append({
                    "id": m["mention_id"], "text": m.get("surface"), "doc_id": m["doc_id"],
                    "doc": (docs.get(m["doc_id"]) or {}).get("filename"),
                    "sentence_idx": sent["idx"] if sent else None,
                    "sentence": text[sent["char_start"]:sent["char_end"]] if sent else None,
                    "start": (span.get("char_start") - sent["char_start"]) if sent and span else None,
                    "end": (span.get("char_end") - sent["char_start"]) if sent and span else None})
        for ed in edges:
            out = ed["source"] in members
            ed.update(direction="out" if out else "in",
                      other_id=ed["target"] if out else ed["source"],
                      other_label=names.get(ed["target"] if out else ed["source"]))
        timeline = []
        for ed in edges:
            if ed["status"] == S.REJECTED:
                continue
            whens = [t.get("value_start") or t.get("value_end") for t in ed["time"] if t.get("value_start") or t.get("value_end")]
            timeline.append({"when": min(whens) if whens else None, "edge_id": ed["id"],
                             "text": f"{ed['rel']} {ed['other_label'] or ''}".strip(),
                             "direction": ed["direction"], "status": ed["status"]})
        timeline.sort(key=lambda x: (x["when"] is None, x["when"] or ""))
        return {
            "entity": {"id": found["survivor_id"], "label": e.get("canonical_name"), "type": e.get("type"),
                       "subtype": e.get("subtype"), "status": e.get("status"), "source": e.get("source"),
                       "doc_id": e.get("doc_id"), "merged_into": e.get("merged_into"),
                       "members": members, "requested_id": entity_id},
            "aliases": sorted({a["alias"] for a in found["aliases"] if a["alias"] != e.get("canonical_name")}),
            "roles": [{"role": x.get("role"), "org_id": x.get("org_entity_id"), "org_label": x.get("org_name"),
                       "time": x.get("time_id")} for x in found["roles"]],
            "mentions": mention_out, "edges": edges, "timeline": timeline,
            "resolutions": [{"other_id": (x["entity_b"] if x["entity_a"] in members else x["entity_a"]),
                             "other_label": names.get(x["entity_b"] if x["entity_a"] in members else x["entity_a"]),
                             "decision": x["decision"], "decided_by": x["decided_by"], "rule_id": x["rule_id"],
                             "score": x.get("score")} for x in found["resolution_history"]],
            "flags": [{"id": f["flag_id"], "kind": f["kind"], "state": f["state"], "note": f.get("note")}
                      for f in found["flags"]],
        }

    @app.get("/api/runs/{run_id}/provenance/{object_id}")
    def provenance(run_id: str, object_id: str) -> dict[str, Any]:
        found = query.get_provenance(store, run_id, object_id)
        if not found["found"]:
            raise HTTPException(status_code=404, detail=f"no object {object_id}")
        with store.reader() as r:
            docs = _docs(r)
            sentences = _sentence_index(r, {s.get("doc_id") for s in found["spans"] if s.get("doc_id")})
            spans = [_span_out(s, docs, sentences) for s in found["spans"]]
        return {"object_id": object_id, "object_type": found["object_type"], "object": found["object"],
                "spans": spans,
                "history": [{"ts": _iso(c.get("ts")), "op": c.get("op"), "module": c.get("module"),
                             "stage": c.get("stage"), "rule_id": c.get("rule_id"), "call_id": c.get("call_id"),
                             "before": c.get("before"), "after": c.get("after")} for c in found["changes"]],
                "model_calls": found["model_calls"]}

    # ---- documents
    @app.get("/api/documents")
    def documents() -> list[dict[str, Any]]:
        with store.reader() as r:
            counts = Counter(s["doc_id"] for s in r.select("sentences"))
            return [{"doc_id": d["doc_id"], "filename": d["filename"], "pages": d.get("pages"),
                     "doc_date": _iso(d.get("doc_date")), "doc_date_source": d.get("doc_date_source"),
                     "sentences": counts.get(d["doc_id"], 0), "status": d["status"],
                     "sovereign": bool(d.get("sovereign"))}
                    for d in sorted(r.select("documents"), key=lambda d: d.get("filename") or "")]

    @app.get("/api/runs/{run_id}/documents/{doc_id}")
    def document(run_id: str, doc_id: str) -> dict[str, Any]:
        with store.reader() as r:
            run_row(r, run_id)
            doc = r.get("documents", doc_id=doc_id)
            if doc is None:
                raise HTTPException(status_code=404, detail=f"no document {doc_id}")
            texts = r.select("doc_texts", doc_id=doc_id)
            text = max(texts, key=lambda x: x["text_version"])["canonical_text"] if texts else ""
            sents = sorted(r.select("sentences", doc_id=doc_id), key=lambda s: s["idx"])
            final = {c["sentence_id"]: c["terminal"] for c in
                     query.select_in(r, "coverage_final", "sentence_id", [s["sentence_id"] for s in sents],
                                     run_id=run_id)}
            names = {e["entity_id"]: e["canonical_name"] for e in r.select("entities", run_id=run_id)}
            highlights = []
            mentions = r.select("mentions", run_id=run_id, doc_id=doc_id)
            spans = query.spans_by_id(r, [m["span_id"] for m in mentions])
            for m in mentions:
                span = spans.get(m["span_id"])
                if not span or m.get("status") == "DISCARDED":
                    continue
                if m.get("entity_id"):
                    owner = query.survivor_of(r, m["entity_id"])
                    highlights.append({"start": span["char_start"], "end": span["char_end"], "kind": "entity",
                                       "object_id": owner, "label": names.get(owner, m.get("surface"))})
                elif m.get("time_id"):
                    highlights.append({"start": span["char_start"], "end": span["char_end"], "kind": "time",
                                       "object_id": m["time_id"], "label": m.get("surface")})
            edges = r.select("macro_edges", run_id=run_id)
            links = query.select_in(r, "edge_spans", "edge_id", [e["edge_id"] for e in edges])
            edge_spans = query.spans_by_id(r, [x["span_id"] for x in links])
            by_id = {e["edge_id"]: e for e in edges}
            for x in links:
                span, e = edge_spans.get(x["span_id"]), by_id.get(x["edge_id"])
                if span and e and span["doc_id"] == doc_id:
                    highlights.append({"start": span["char_start"], "end": span["char_end"], "kind": "fact",
                                       "object_id": e["edge_id"], "status": e["status"],
                                       "label": f"{names.get(e['src_entity_id'], '?')} —{e['rel']}→ "
                                                f"{names.get(e['dst_entity_id'], '?')}"})
            highlights.sort(key=lambda h: (h["start"], -h["end"]))
            n = len(sents)
            return {"document": {"doc_id": doc_id, "filename": doc["filename"], "pages": doc.get("pages"),
                                 "doc_date": _iso(doc.get("doc_date")), "doc_date_source": doc.get("doc_date_source"),
                                 "sentences": n, "status": doc["status"], "sovereign": bool(doc.get("sovereign"))},
                    "text": text,
                    "sentences": [{"idx": s["idx"], "id": s["sentence_id"], "start": s["char_start"],
                                   "end": s["char_end"], "page": s.get("page"), "kind": s.get("kind"),
                                   "filtered": bool(s.get("filtered")), "coverage": final.get(s["sentence_id"])}
                                  for s in sents],
                    "highlights": highlights}

    # ---- review queue, metrics, audit
    @app.get("/api/runs/{run_id}/escalations")
    def escalations(run_id: str) -> list[dict[str, Any]]:
        with store.reader() as r:
            run_row(r, run_id)
            rows = sorted(r.select("escalations", run_id=run_id), key=lambda e: e["esc_id"])
            labels = _labels(r, [i for e in rows for i in (e.get("item_ids") or [])])
            return [{"id": e["esc_id"], "item_type": e["item_type"], "item_ids": e.get("item_ids") or [],
                     "reasons": e.get("reasons") or [], "question_type": e.get("question_type"),
                     "question": (e.get("packet") or {}).get("question"), "outcome": e["outcome"],
                     "verdict": e.get("verdict"), "packet_tokens": e.get("packet_tokens"),
                     "tokens_in": e.get("tokens_in"), "tokens_out": e.get("tokens_out"),
                     "items": [{"id": i, "label": labels.get(i, i)} for i in (e.get("item_ids") or [])]}
                    for e in rows]

    @app.get("/api/runs/{run_id}/metrics")
    def metrics(run_id: str) -> dict[str, Any]:
        with store.reader() as r:
            row = run_row(r, run_id)
            groups: dict[tuple, Counter] = defaultdict(Counter)
            for c in r.select("model_calls", run_id=run_id):
                g = groups[(c["stage"], c["endpoint"], c.get("model_id"))]
                g["calls"] += 1
                g["tokens_in"] += c.get("tokens_in") or 0
                g["tokens_out"] += c.get("tokens_out") or 0
                g["reasoning_tokens"] += c.get("reasoning_tokens") or 0
                g["latency_ms"] += c.get("latency_ms") or 0
                g["failures"] += 1 if c.get("error") else 0
            by_stage = [{"stage": k[0], "endpoint": k[1], "model": k[2], **v} for k, v in groups.items()]
            by_stage.sort(key=lambda x: (PIPELINE.index(x["stage"]) if x["stage"] in PIPELINE else 99, x["endpoint"]))
            total = Counter()
            for x in by_stage:
                for key in ("calls", "tokens_in", "tokens_out", "latency_ms"):
                    total[key] += x[key]
                bucket = "frontier_tokens" if x["endpoint"] == "t3" else "local_tokens"
                total[bucket] += x["tokens_in"] + x["tokens_out"]
            facts = sum(1 for e in r.select("macro_edges", run_id=run_id) if e["status"] == S.COMMITTED)
            started, finished = row.get("started_at"), row.get("finished_at")
            wall = (finished - started).total_seconds() if started and finished and hasattr(started, "year") else None
            return {"by_stage": by_stage,
                    "totals": {k: total.get(k, 0) for k in ("calls", "tokens_in", "tokens_out", "latency_ms",
                                                            "local_tokens", "frontier_tokens")},
                    "facts_committed": facts,
                    "ftvf": round(total["frontier_tokens"] / facts, 2) if facts else None,
                    "wall_seconds": wall}

    @app.get("/api/runs/{run_id}/audit")
    def audit(run_id: str) -> dict[str, Any]:
        from synth.audit.audits import audit_run

        with store.reader() as r:
            run_row(r, run_id)
        return audit_run(store, run_id)

    # ---- control: background jobs running the CLI
    def start(kind: str, args: list[str], profile: str | None = None, command: list[str] | None = None):
        if read_only:
            raise HTTPException(status_code=403, detail="demo mode: run control is disabled")
        if profile and profile not in {p["name"] for p in settings.list_profiles()}:
            raise HTTPException(status_code=400, detail=f"unknown profile {profile!r}")
        try:
            job = jobs.start(kind, args, profile, command=command)
        except JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return JSONResponse({"job_id": job.job_id, "kind": kind, "status": job.status}, status_code=202)

    @app.post("/api/runs")
    def start_run(body: dict[str, Any] = Body(default_factory=dict)):
        arm = body.get("engine") or body.get("arm") or engines.default_engine().id
        if arm not in engines.ENGINES:
            raise HTTPException(status_code=400, detail=f"engine must be one of {sorted(engines.ENGINES)}")
        args = ["run", "--arm", arm]
        if body.get("relation_mode"):
            if body["relation_mode"] not in RELATION_MODES:
                raise HTTPException(status_code=400, detail=f"relation_mode must be one of {sorted(RELATION_MODES)}")
            args += ["--relation-mode", body["relation_mode"]]
        if body.get("gliner"):
            if body["gliner"] not in GLINER_MODES:
                raise HTTPException(status_code=400, detail=f"gliner must be one of {sorted(GLINER_MODES)}")
            args += ["--gliner", body["gliner"]]
        if body.get("ontology"):
            if body["ontology"] not in {o["name"] for o in ontologies.list_ontologies()}:
                raise HTTPException(status_code=400, detail=f"no ontology named {body['ontology']}")
            args += ["--ontology", body["ontology"]]
        return start("run", args, body.get("profile") or settings.active_profile())

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: dict[str, Any] = Body(default_factory=dict)):
        with store.reader() as r:
            row = run_row(r, run_id)
        profile = body.get("profile") or (row.get("manifest") or {}).get("profile")
        return start("resume", ["run", "--resume", run_id], profile if profile in
                     {p["name"] for p in settings.list_profiles()} else settings.active_profile())

    @app.post("/api/ingest")
    def ingest(body: dict[str, Any] = Body(default_factory=dict)):
        path = Path(body.get("path") or "data/raw")
        full = path if path.is_absolute() else REPO_ROOT / path
        if not full.exists():
            raise HTTPException(status_code=400, detail=f"no such file or folder: {full}")
        return start("ingest", ["ingest", str(full)])

    @app.post("/api/smoke")
    def smoke(body: dict[str, Any] = Body(default_factory=dict)):
        return start("smoke", ["smoke"], body.get("profile") or settings.active_profile())

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [j.as_dict(with_log=False) for j in jobs.list()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        return job.as_dict()

    admin.add_routes(app, store, start, run_row, read_only, config)
    gearbox.add_routes(app, store, run_row, read_only)
    ontologies.add_routes(app, read_only)
    return app
