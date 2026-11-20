"""J1 — Store Query API: read-only questions over the stores (the upward interface).

Purpose: one place that answers "what do the stores say about X" for the gate
(U9 packets), the frontier's context asks (U10), the viewer and any later
tool-using agent. Plain functions; :mod:`synth.api.app` exposes the same five
functions as read-only FastAPI GET routes.

Contract (every function returns plain JSON-ready dicts; datetimes as ISO text):

* ``get_sentences(store, doc_id, ids, n=0, *, run_id=None)`` — sentences by id
  (``snt_…``), label (``s41`` / ``d2:s41``) or idx, each widened by ± ``n``
  neighbours; text, offsets, page, and the coverage outcome for ``run_id`` if known.
* ``get_entity(store, run_id, entity_id)`` — the entity, following
  ``merged_into`` to the survivor; absorbed ids, aliases, roles, mentions with
  spans and quotes, resolution history (L5 rows touching the cluster), flags.
* ``get_edges(store, run_id, entity_id, level="macro"|"micro", status=None)`` —
  the 1-hop subgraph: edges with the entity (or anything merged into it) at
  either end, with spans, time roles (joined to ``time_refs``) and lineage.
* ``get_timeline(store, run_id, entity_id)`` — the entity's macro edges ordered
  by their time references (edges without a usable time last; REJECTED left out).
* ``get_provenance(store, run_id, object_id)`` — spans + change-log rows + the
  model calls (L2, via ``change_log.call_id``) for any object id.

``store`` may be a :class:`synth.store.Store` (a read connection is opened and
closed) or an open :class:`synth.store.repo.Tx` (reads run inside it — modules
that are already inside a transaction must pass their ``Tx``).
Stores written: none. Token caps: none here (U9/U10 render and budget answers).
Spec: P1 §3 J1, §5 (the provenance questions), §6 (timelines).
"""

from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from synth.provenance.render import parse_label
from synth.store import Store
from synth.store.repo import Tx

REJECTED = "REJECTED"
_CHUNK = 500

#: object-id prefix -> (table, primary key)
_OBJECT_TABLES = {
    "ent": ("entities", "entity_id"), "men": ("mentions", "mention_id"),
    "tim": ("time_refs", "time_id"), "mad": ("macro_edges", "edge_id"),
    "med": ("micro_edges", "edge_id"), "prp": ("propositions", "prop_id"),
    "lnk": ("prop_links", "link_id"), "flg": ("flags", "flag_id"),
    "esc": ("escalations", "esc_id"), "res": ("resolutions", "decision_id"),
    "spn": ("spans", "span_id"), "doc": ("documents", "doc_id"),
    "snt": ("sentences", "sentence_id"),
}
_OBJECT_TYPES = {
    "ent": "entity", "men": "mention", "tim": "time_ref", "mad": "macro_edge",
    "med": "micro_edge", "prp": "proposition", "lnk": "prop_link", "flg": "flag",
    "esc": "escalation", "res": "resolution", "spn": "span", "doc": "document",
    "snt": "sentence",
}
_CALL_FIELDS = ("call_id", "run_id", "stage", "module", "unit_id", "endpoint", "model_id",
                "prompt_name", "prompt_sha256", "tokens_in", "tokens_out", "tokens_in_counted",
                "latency_ms", "finish_reason", "schema_valid", "repairs", "retries", "error", "ts")


# ------------------------------------------------------------------ plumbing
@contextmanager
def reading(source: Store | Tx) -> Iterator[Tx]:
    """Use an open ``Tx`` as is, or open (and close) a reader on a ``Store``."""
    if isinstance(source, Tx):
        yield source
    else:
        with source.reader() as r:
            yield r


def clean(value: Any) -> Any:
    """Datetimes -> ISO text, recursively (JSON-ready answers)."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def select_in(r: Tx, table: str, column: str, values: Iterable[Any], **equals: Any) -> list[dict[str, Any]]:
    """``SELECT * FROM table WHERE column IN values`` (chunked), plus equality filters."""
    wanted = list(dict.fromkeys(v for v in values if v is not None))
    if not wanted:
        return []
    t = r.table(table)
    out: list[dict[str, Any]] = []
    for i in range(0, len(wanted), _CHUNK):
        out.extend(r.select(table, where=t.c[column].in_(wanted[i:i + _CHUNK]), **equals))
    return out


def survivor_of(r: Tx, entity_id: str) -> str:
    """Follow ``merged_into`` to the surviving entity (cycle-safe)."""
    seen = {entity_id}
    current = entity_id
    row = r.get("entities", entity_id=current)
    while row and row.get("merged_into") and row["merged_into"] not in seen:
        current = row["merged_into"]
        seen.add(current)
        row = r.get("entities", entity_id=current)
    return current


def cluster_of(r: Tx, entity_id: str) -> tuple[str, list[str]]:
    """``(survivor id, absorbed ids)`` — absorbed = every entity whose
    ``merged_into`` chain ends at the survivor."""
    survivor = survivor_of(r, entity_id)
    row = r.get("entities", entity_id=survivor)
    if row is None:
        return survivor, []
    t = r.table("entities")
    merged = r.select("entities", where=t.c.merged_into.isnot(None), run_id=row["run_id"])
    children: dict[str, list[str]] = {}
    for m in merged:
        children.setdefault(m["merged_into"], []).append(m["entity_id"])
    absorbed: list[str] = []
    frontier = [survivor]
    seen = {survivor}
    while frontier:
        nxt = []
        for parent in frontier:
            for child in sorted(children.get(parent, [])):
                if child not in seen:
                    seen.add(child)
                    absorbed.append(child)
                    nxt.append(child)
        frontier = nxt
    return survivor, absorbed


def spans_by_id(r: Tx, span_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Span rows keyed by id, each with ``sentence_labels`` (doc-local ``s41``)."""
    spans = select_in(r, "spans", "span_id", span_ids)
    sentence_ids = {sid for s in spans for sid in (s.get("sentence_ids") or [])}
    idx = {s["sentence_id"]: s["idx"] for s in select_in(r, "sentences", "sentence_id", sentence_ids)}
    out = {}
    for s in spans:
        row = clean(dict(s))
        row["sentence_labels"] = [f"s{idx[sid]}" for sid in (s.get("sentence_ids") or []) if sid in idx]
        out[s["span_id"]] = row
    return out


def _span_list(spans: dict[str, dict[str, Any]], ids: Iterable[str]) -> list[dict[str, Any]]:
    return [spans[i] for i in dict.fromkeys(ids) if i in spans]


# ------------------------------------------------------------------ sentences
def _index_of(by_id: dict[str, int], key: Any) -> int | None:
    """``snt_…`` id, ``s41`` / ``d2:s41`` label, ``"41"`` or ``41`` -> sentence idx."""
    if isinstance(key, int):
        return key
    key = str(key).strip()
    if key in by_id:
        return by_id[key]
    if key.isdigit():
        return int(key)
    parsed = parse_label(key)
    return parsed[1] if parsed is not None else None


def get_sentences(store: Store | Tx, doc_id: str, ids: Iterable[Any], n: int = 0, *,
                  run_id: str | None = None) -> dict[str, Any]:
    """Sentences ``ids`` of ``doc_id`` with ± ``n`` neighbours, in document order."""
    ids = [ids] if isinstance(ids, (str, int)) else list(ids)
    with reading(store) as r:
        doc = r.get("documents", doc_id=doc_id)
        if doc is None:
            return {"doc_id": doc_id, "found": False, "sentences": [], "missing": [str(i) for i in ids]}
        texts = r.select("doc_texts", order_by="text_version", doc_id=doc_id)
        version = texts[-1]["text_version"] if texts else 1
        text = texts[-1]["canonical_text"] if texts else ""
        sentences = r.select("sentences", order_by="idx", doc_id=doc_id, text_version=version)
        by_idx = {s["idx"]: s for s in sentences}
        by_id = {s["sentence_id"]: s["idx"] for s in sentences}
        resolved = [_index_of(by_id, k) for k in ids]
        cited = {i for i in resolved if i is not None and i in by_idx}
        missing = [str(k) for k, i in zip(ids, resolved) if i is None or i not in by_idx]
        keep: set[int] = set()
        for i in cited:
            keep.update(j for j in range(i - max(n, 0), i + max(n, 0) + 1) if j in by_idx)
        rows = [by_idx[i] for i in sorted(keep)]
        coverage: dict[str, str | None] = {}
        events: dict[str, list[str]] = {}
        if run_id and rows:
            sids = [s["sentence_id"] for s in rows]
            for f in select_in(r, "coverage_final", "sentence_id", sids, run_id=run_id):
                coverage[f["sentence_id"]] = f["terminal"]
            for e in sorted(select_in(r, "coverage_events", "sentence_id", sids, run_id=run_id),
                            key=lambda e: (str(e.get("ts") or ""),)):
                events.setdefault(e["sentence_id"], []).append(e["outcome"])
        out = []
        for s in rows:
            sid = s["sentence_id"]
            outcome = coverage.get(sid) or (events[sid][-1] if events.get(sid) else None)
            out.append(dict(sentence_id=sid, idx=s["idx"], label=f"s{s['idx']}",
                            char_start=s["char_start"], char_end=s["char_end"], page=s["page"],
                            kind=s["kind"], filtered=bool(s["filtered"]),
                            section_path=s["section_path"], cited=s["idx"] in cited,
                            text=text[s["char_start"]:s["char_end"]],
                            coverage=outcome, coverage_terminal=coverage.get(sid),
                            coverage_events=events.get(sid, [])))
        return {"doc_id": doc_id, "found": True, "filename": doc.get("filename"),
                "sovereign": bool(doc.get("sovereign")), "text_version": version, "n": n,
                "sentences": out, "missing": missing}


# ------------------------------------------------------------------ entities
def get_entity(store: Store | Tx, run_id: str, entity_id: str) -> dict[str, Any] | None:
    """The entity (its survivor if merged), with everything that explains it."""
    with reading(store) as r:
        if r.get("entities", entity_id=entity_id) is None:
            return None
        survivor, absorbed = cluster_of(r, entity_id)
        members = [survivor, *absorbed]
        rows = {e["entity_id"]: e for e in select_in(r, "entities", "entity_id", members)}
        aliases = [dict(entity_id=a["entity_id"], alias=a["alias"], mention_id=a["mention_id"])
                   for a in select_in(r, "entity_aliases", "entity_id", members)]
        roles = select_in(r, "entity_roles", "entity_id", members)
        mentions = sorted(select_in(r, "mentions", "entity_id", members),
                          key=lambda m: m["mention_id"])
        span_ids = [m["span_id"] for m in mentions] + [x["span_id"] for x in roles]
        spans = spans_by_id(r, span_ids)
        mention_rows = [dict(clean(m), span=spans.get(m["span_id"])) for m in mentions]
        org_names = {e["entity_id"]: e["canonical_name"]
                     for e in select_in(r, "entities", "entity_id", [x["org_entity_id"] for x in roles])}
        role_rows = [dict(clean(x), org_name=org_names.get(x["org_entity_id"]),
                          span=spans.get(x["span_id"])) for x in roles]
        member_set = set(members)
        t = r.table("resolutions")
        res = r.select("resolutions", where=(t.c.entity_a.in_(members) | t.c.entity_b.in_(members)),
                       run_id=run_id)
        res.sort(key=lambda x: (str(x.get("ts") or ""), x["decision_id"]))
        flags = [clean(f) for f in r.select("flags", order_by="flag_id", run_id=run_id)
                 if member_set & set(f.get("object_ids") or [])]
        return {"requested_id": entity_id, "survivor_id": survivor, "absorbed_ids": absorbed,
                "entity": clean(rows.get(survivor)),
                "members": [clean(rows[m]) for m in members if m in rows],
                "aliases": aliases, "roles": role_rows, "mentions": mention_rows,
                "resolution_history": [clean(x) for x in res], "flags": flags}


# ------------------------------------------------------------------ edges
def edge_details(r: Tx, level: str, edges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach spans, time roles, lineage and end names to edge rows (one batch)."""
    ids = [e["edge_id"] for e in edges]
    links = select_in(r, "edge_spans", "edge_id", ids)
    spans = spans_by_id(r, [x["span_id"] for x in links])
    span_ids: dict[str, list[str]] = {}
    for x in links:
        span_ids.setdefault(x["edge_id"], []).append(x["span_id"])
    times = select_in(r, "edge_times", "edge_id", ids)
    refs = {t["time_id"]: t for t in select_in(r, "time_refs", "time_id", [x["time_id"] for x in times])}
    time_rows: dict[str, list[dict[str, Any]]] = {}
    for x in times:
        ref = refs.get(x["time_id"], {})
        time_rows.setdefault(x["edge_id"], []).append(dict(
            role=x["role"], time_id=x["time_id"], surface=ref.get("surface"), kind=ref.get("kind"),
            value_start=ref.get("value_start"), value_end=ref.get("value_end"),
            granularity=ref.get("granularity"), anchor_type=ref.get("anchor_type"),
            doc_id=ref.get("doc_id"), span_id=ref.get("span_id")))
    lineage: dict[str, list[dict[str, Any]]] = {}
    if level == "macro":
        for x in select_in(r, "edge_lineage", "macro_edge_id", ids):
            lineage.setdefault(x["macro_edge_id"], []).append(
                dict(micro_edge_id=x["micro_edge_id"], op=x["op"]))
    else:
        for x in select_in(r, "edge_lineage", "micro_edge_id", ids):
            lineage.setdefault(x["micro_edge_id"], []).append(
                dict(macro_edge_id=x["macro_edge_id"], op=x["op"]))
    ends = {e["entity_id"]: e for e in select_in(
        r, "entities", "entity_id", [x for e in edges for x in (e["src_entity_id"], e["dst_entity_id"])])}
    out = []
    for e in edges:
        src, dst = ends.get(e["src_entity_id"], {}), ends.get(e["dst_entity_id"], {})
        out.append(dict(clean(e), level=level,
                        src_name=src.get("canonical_name"), dst_name=dst.get("canonical_name"),
                        src_type=src.get("subtype") or src.get("type"),
                        dst_type=dst.get("subtype") or dst.get("type"),
                        spans=_span_list(spans, span_ids.get(e["edge_id"], [])),
                        times=time_rows.get(e["edge_id"], []),
                        lineage=lineage.get(e["edge_id"], [])))
    return out


def get_edges(store: Store | Tx, run_id: str, entity_id: str, level: str = "macro",
              status: str | Sequence[str] | None = None) -> dict[str, Any]:
    """The 1-hop subgraph around ``entity_id`` (and everything merged into it)."""
    if level not in ("macro", "micro"):
        raise ValueError(f"level must be 'macro' or 'micro', not {level!r}")
    table = "macro_edges" if level == "macro" else "micro_edges"
    with reading(store) as r:
        survivor, absorbed = cluster_of(r, entity_id)
        members = [survivor, *absorbed]
        t = r.table(table)
        clause = t.c.src_entity_id.in_(members) | t.c.dst_entity_id.in_(members)
        if isinstance(status, str):
            clause = clause & (t.c.status == status)
        elif status:
            clause = clause & t.c.status.in_(list(status))
        edges = r.select(table, order_by="edge_id", where=clause, run_id=run_id)
        return {"entity_id": entity_id, "survivor_id": survivor, "members": members,
                "level": level, "edges": edge_details(r, level, edges)}


def _when(edge: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    best: tuple[str, dict[str, Any]] | None = None
    for t in edge.get("times") or []:
        key = t.get("value_start") or t.get("value_end")
        if key and (best is None or key < best[0]):
            best = (key, t)
    return best


def get_timeline(store: Store | Tx, run_id: str, entity_id: str, *,
                 include_rejected: bool = False) -> dict[str, Any]:
    """The entity's macro edges ordered by time (ISO strings sort by precision-
    preserving prefix, so ``2024`` < ``2024-03`` < ``2024-03-14``); untimed last."""
    graph = get_edges(store, run_id, entity_id, level="macro")
    events = []
    for edge in graph["edges"]:
        if edge["status"] == REJECTED and not include_rejected:
            continue
        when = _when(edge)
        events.append(dict(edge=edge, when=when[1] if when else None, sort_key=when[0] if when else None))
    events.sort(key=lambda ev: (ev["sort_key"] is None, ev["sort_key"] or "", ev["edge"]["edge_id"]))
    return {"entity_id": entity_id, "survivor_id": graph["survivor_id"], "events": events}


# ------------------------------------------------------------------ provenance
def get_provenance(store: Store | Tx, run_id: str, object_id: str) -> dict[str, Any]:
    """Spans, change-log rows and model calls behind any object id."""
    prefix = object_id.split("_", 1)[0]
    with reading(store) as r:
        table, pk = _OBJECT_TABLES.get(prefix, (None, None))
        row = r.get(table, **{pk: object_id}) if table else None
        span_ids: list[str] = []
        if row is not None:
            if prefix == "ent":
                span_ids += [m["span_id"] for m in r.select("mentions", entity_id=object_id)]
                span_ids += [x["span_id"] for x in r.select("entity_roles", entity_id=object_id)]
            elif prefix in ("mad", "med"):
                span_ids += [x["span_id"] for x in r.select("edge_spans", edge_id=object_id)]
            elif prefix in ("tim", "prp", "lnk", "men"):
                span_ids.append(row.get("span_id"))
            elif prefix == "flg":
                span_ids += list(row.get("span_ids") or [])
            elif prefix == "res":
                span_ids += list(row.get("evidence_span_ids") or [])
            elif prefix == "esc":
                packet, verdict = row.get("packet") or {}, row.get("verdict") or {}
                span_ids += list(packet.get("span_ids") or []) + list(verdict.get("span_ids") or [])
            elif prefix == "spn":
                span_ids.append(object_id)
        spans = spans_by_id(r, [s for s in span_ids if s])
        changes = r.select("change_log", object_id=object_id)
        changes.sort(key=lambda c: (str(c.get("ts") or ""), c["change_id"]))
        call_ids = [c["call_id"] for c in changes if c.get("call_id")]
        calls = [{k: clean(c.get(k)) for k in _CALL_FIELDS}
                 for c in select_in(r, "model_calls", "call_id", call_ids)]
        calls.sort(key=lambda c: c["call_id"])
        return {"object_id": object_id, "object_type": _OBJECT_TYPES.get(prefix),
                "run_id": run_id, "found": row is not None, "object": clean(row),
                "spans": _span_list(spans, [s for s in span_ids if s]),
                "changes": [clean(c) for c in changes], "model_calls": calls}
