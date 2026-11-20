"""J3 Graph Export — the versioned contract for the standalone graph viewer.

Purpose: write one run's graph as files a separate viewer can read without
touching the database: ``nodes.json``, ``edges.json``, ``measures.json`` and
``graph.graphml``. Also home of :func:`load_run`, the one read of a run's stores
that J2 (the source viewer) shares.

Contract (``"contract": "synth.graph", "version": 1``):

- ``nodes.json`` — one node per surviving ``ACTIVE`` entity (``level: "entity"``);
  entities merged into it (``merged_into`` chains, followed to the end) are listed
  only under the survivor's ``absorbed`` ids, and their aliases, mention spans and
  documents are folded into the survivor. Entities that are neither active nor
  merged into an active entity are listed under ``excluded`` with a reason.
- ``edges.json`` — every macro edge (all statuses, ``level: "macro"``) and every
  micro edge (Tier 1, ``level: "micro"``), endpoints mapped to survivor node ids
  (the stored ids stay in ``src_entity_id`` / ``dst_entity_id``). Edges whose
  endpoint has no node are listed under ``dangling_edges`` and left out of GraphML.
- ``measures.json`` — built-in measures, each ``{id, definition, unit, version,
  applies_to, values: {nodes: {...}, edges: {...}}}``.
- ``graph.graphml`` — the same nodes and edges (a directed multigraph, edge key =
  edge id) with their key attributes and measure values (``m_<measure id>``).
  Lists are JSON-encoded strings; missing values are omitted.

Output is deterministic: ids sorted, JSON with sorted keys, no timestamps, bytes
written as UTF-8 with ``\\n`` line ends — two exports of one run are identical.

Stores written: none (read-only; files only). Token caps: none (no model calls).
Spec: P1 §3 J3, §4 (stores), §6 (time roles), §11 (statuses).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import networkx as nx
from sqlalchemy import select

from synth.store import Store
from synth.store.repo import Tx
from synth.store.tables import (
    edge_lineage, edge_spans, edge_times, entities, entity_aliases, entity_roles,
    macro_edges, micro_chunks, micro_edges, propositions, sections, spans,
)

CONTRACT = "synth.graph"
CONTRACT_VERSION = 1
FILES = ("nodes.json", "edges.json", "measures.json", "graph.graphml")

ACTIVE, MERGED, REJECTED, DISMISSED = "ACTIVE", "MERGED", "REJECTED", "DISMISSED"
#: Flag kinds that count as a conflict on an edge (U8 conflicts, U6 inconsistencies).
CONFLICT_KINDS = ("conflict", "inconsistency")
#: Time kinds whose values are calendar dates (first/last date measures).
DATED_KINDS = ("INSTANT", "INTERVAL", "PERIOD")
_LOW = "0000-01-01T00:00:00"
_HIGH = "9999-12-31T23:59:59"


# ====================================================================== loading
@dataclass
class RunData:
    """Everything one run wrote to S2–S5 and L5–L7, keyed by id (read once)."""

    run_id: str
    run: dict[str, Any]
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, list[dict[str, Any]]] = field(default_factory=dict)      # entity_id ->
    roles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)        # entity_id ->
    mentions: dict[str, dict[str, Any]] = field(default_factory=dict)
    time_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    micro_edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    macro_edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    edge_spans: dict[str, list[str]] = field(default_factory=dict)              # edge_id -> span ids
    edge_times: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # edge_id -> [{role, time_id}]
    lineage: list[dict[str, Any]] = field(default_factory=list)
    spans: dict[str, dict[str, Any]] = field(default_factory=dict)
    flags: dict[str, dict[str, Any]] = field(default_factory=dict)
    escalations: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    propositions: dict[str, dict[str, Any]] = field(default_factory=dict)
    prop_links: dict[str, dict[str, Any]] = field(default_factory=dict)
    chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- derived views -------------------------------------------------------
    def survivors(self) -> dict[str, str | None]:
        """entity id -> the ACTIVE entity it ends up in (itself if ACTIVE), or None."""
        out: dict[str, str | None] = {}
        for entity_id in self.entities:
            out[entity_id] = self._follow(entity_id)[0]
        return out

    def _follow(self, entity_id: str) -> tuple[str | None, str]:
        seen: list[str] = []
        current = entity_id
        while True:
            row = self.entities.get(current)
            if row is None:
                return None, f"merge chain ends at unknown entity {current}"
            if current in seen:
                return None, "merge cycle"
            seen.append(current)
            status = row.get("status")
            if status == ACTIVE:
                return current, ""
            if status == MERGED and row.get("merged_into"):
                current = row["merged_into"]
                continue
            if status == MERGED:
                return None, "MERGED without merged_into"
            return None, f"status {status}"

    def exclusion_reason(self, entity_id: str) -> str:
        return self._follow(entity_id)[1]

    def span_doc(self, span_id: str) -> str | None:
        span = self.spans.get(span_id)
        return span["doc_id"] if span else None

    def edge_doc_ids(self, edge_id: str) -> list[str]:
        docs = {self.span_doc(s) for s in self.edge_spans.get(edge_id, [])}
        if edge_id in self.micro_edges:
            chunk = self.chunks.get(self.micro_edges[edge_id].get("chunk_id") or "")
            docs.add(chunk["doc_id"] if chunk else None)
        elif edge_id in self.macro_edges:
            section = self.sections.get(self.macro_edges[edge_id].get("section_id") or "")
            docs.add(section["doc_id"] if section else None)
        return sorted(d for d in docs if d)

    def time_rows(self, edge_id: str) -> list[dict[str, Any]]:
        out = []
        for et in self.edge_times.get(edge_id, []):
            ref = self.time_refs.get(et["time_id"]) or {}
            out.append(dict(role=et["role"], time_id=et["time_id"], kind=ref.get("kind"),
                            value_start=ref.get("value_start"), value_end=ref.get("value_end"),
                            granularity=ref.get("granularity"), anchor_type=ref.get("anchor_type"),
                            surface=ref.get("surface")))
        return sorted(out, key=lambda t: (t["role"] or "", t["time_id"] or ""))

    def lineage_down(self, macro_edge_id: str) -> list[dict[str, Any]]:
        return sorted(({"micro_edge_id": r["micro_edge_id"], "op": r["op"]} for r in self.lineage
                       if r["macro_edge_id"] == macro_edge_id),
                      key=lambda r: (r["micro_edge_id"] or "", r["op"] or ""))

    def lineage_up(self, micro_edge_id: str) -> list[dict[str, Any]]:
        return sorted(({"macro_edge_id": r["macro_edge_id"], "op": r["op"]} for r in self.lineage
                       if r["micro_edge_id"] == micro_edge_id),
                      key=lambda r: (r["macro_edge_id"] or "", r["op"] or ""))

    def entity_name(self, entity_id: str | None) -> str:
        row = self.entities.get(entity_id or "")
        return (row.get("canonical_name") or entity_id) if row else (entity_id or "?")


def _by(rows: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {r[key]: r for r in sorted(rows, key=lambda r: r[key])}


def _group(rows: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def load_run(r: Tx, run_id: str) -> RunData:
    """Read every store object of ``run_id`` (one pass, sorted by id)."""
    run = r.get("runs", run_id=run_id) or {"run_id": run_id}
    data = RunData(run_id=run_id, run=run)
    data.entities = _by(r.select("entities", run_id=run_id), "entity_id")
    run_entities = select(entities.c.entity_id).where(entities.c.run_id == run_id)
    aliases = r.rows(select(entity_aliases).where(entity_aliases.c.entity_id.in_(run_entities)))
    data.aliases = _group(sorted(aliases, key=lambda a: (a["entity_id"], a["alias"] or "", a["mention_id"] or "")),
                          "entity_id")
    roles = r.rows(select(entity_roles).where(entity_roles.c.entity_id.in_(run_entities)))
    data.roles = _group(sorted(roles, key=lambda x: (x["entity_id"], x["role"] or "", x["org_entity_id"] or "",
                                                     x["span_id"] or "", x["time_id"] or "")), "entity_id")
    data.mentions = _by(r.select("mentions", run_id=run_id), "mention_id")
    data.time_refs = _by(r.select("time_refs", run_id=run_id), "time_id")
    data.micro_edges = _by(r.select("micro_edges", run_id=run_id), "edge_id")
    data.macro_edges = _by(r.select("macro_edges", run_id=run_id), "edge_id")

    run_edges = (select(micro_edges.c.edge_id).where(micro_edges.c.run_id == run_id)
                 .union(select(macro_edges.c.edge_id).where(macro_edges.c.run_id == run_id)))
    for row in r.rows(select(edge_spans).where(edge_spans.c.edge_id.in_(run_edges))):
        data.edge_spans.setdefault(row["edge_id"], []).append(row["span_id"])
    for edge_id in data.edge_spans:
        data.edge_spans[edge_id] = sorted(set(data.edge_spans[edge_id]))
    for row in r.rows(select(edge_times).where(edge_times.c.edge_id.in_(run_edges))):
        data.edge_times.setdefault(row["edge_id"], []).append({"role": row["role"], "time_id": row["time_id"]})
    for edge_id, rows in data.edge_times.items():
        unique = {(x["role"], x["time_id"]) for x in rows}
        data.edge_times[edge_id] = [{"role": a, "time_id": b} for a, b in sorted(unique, key=lambda t: (t[0] or "", t[1] or ""))]
    run_macro = select(macro_edges.c.edge_id).where(macro_edges.c.run_id == run_id)
    lineage = r.rows(select(edge_lineage).where(edge_lineage.c.macro_edge_id.in_(run_macro)))
    data.lineage = sorted(lineage, key=lambda x: (x["macro_edge_id"] or "", x["micro_edge_id"] or "", x["op"] or ""))

    data.flags = _by(r.select("flags", run_id=run_id), "flag_id")
    for f in data.flags.values():
        f["object_ids"] = _as_list(f.get("object_ids"))
        f["span_ids"] = _as_list(f.get("span_ids"))
    data.escalations = _by(r.select("escalations", run_id=run_id), "esc_id")
    for e in data.escalations.values():
        e["item_ids"] = _as_list(e.get("item_ids"))
        e["reasons"] = _as_list(e.get("reasons"))
    data.resolutions = sorted(r.select("resolutions", run_id=run_id), key=lambda x: x["decision_id"])
    data.propositions = _by(r.select("propositions", run_id=run_id), "prop_id")
    data.prop_links = _by(r.select("prop_links", run_id=run_id), "link_id")

    chunk_ids = select(micro_edges.c.chunk_id).where(micro_edges.c.run_id == run_id)
    data.chunks = _by(r.rows(select(micro_chunks).where(micro_chunks.c.chunk_id.in_(chunk_ids))), "chunk_id")
    section_ids = (select(macro_edges.c.section_id).where(macro_edges.c.run_id == run_id)
                   .union(select(propositions.c.section_id).where(propositions.c.run_id == run_id)))
    data.sections = _by(r.rows(select(sections).where(sections.c.section_id.in_(section_ids))), "section_id")

    # spans: the run's own, plus any other span a run object points at
    data.spans = _by(r.select("spans", run_id=run_id), "span_id")
    wanted: set[str] = set()
    for m in data.mentions.values():
        wanted.add(m.get("span_id"))
    for rows in data.roles.values():
        wanted.update(x.get("span_id") for x in rows)
    for t in data.time_refs.values():
        wanted.add(t.get("span_id"))
    for ids in data.edge_spans.values():
        wanted.update(ids)
    for f in data.flags.values():
        wanted.update(f["span_ids"])
    for p in data.propositions.values():
        wanted.add(p.get("span_id"))
    for link in data.prop_links.values():
        wanted.add(link.get("span_id"))
    missing = sorted(s for s in wanted if s and s not in data.spans)
    for i in range(0, len(missing), 500):
        for row in r.rows(select(spans).where(spans.c.span_id.in_(missing[i:i + 500]))):
            data.spans[row["span_id"]] = row
    data.spans = dict(sorted(data.spans.items()))
    for s in data.spans.values():
        s["sentence_ids"] = _as_list(s.get("sentence_ids"))
    return data


# ====================================================================== contract
def _date_key(value: str | None, pad: str) -> str | None:
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    return value + pad[len(value):] if len(value) < len(pad) else value


def _first_last(times: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    first: tuple[str, str] | None = None
    last: tuple[str, str] | None = None
    for t in times:
        if t.get("kind") not in DATED_KINDS:
            continue
        start, end = t.get("value_start"), t.get("value_end") or t.get("value_start")
        lo, hi = _date_key(start, _LOW), _date_key(end, _HIGH)
        if lo is not None and (first is None or lo < first[0]):
            first = (lo, start)
        if hi is not None and (last is None or hi > last[0]):
            last = (hi, end)
    return (first[1] if first else None), (last[1] if last else None)


def _relation_family(ontology: Any, rel: str | None) -> str | None:
    if ontology is None or not rel:
        return None
    try:
        relation = ontology.relation(rel)
    except Exception:  # pragma: no cover - defensive: a broken pack must not stop an export
        return None
    return relation.family if relation else None


def _default_ontology() -> Any:
    try:
        from synth.config import load_config
        from synth.ontology import load_ontology

        config = load_config()
        return load_ontology(config.resolve(config.ontology))
    except Exception:  # pragma: no cover - export still works, ``family`` is null
        return None


def build_contract(data: RunData, ontology: Any = None) -> dict[str, Any]:
    """The four contract documents as plain dicts (``nodes``, ``edges``, ``measures``)."""
    survivor = data.survivors()
    absorbed: dict[str, list[str]] = {}
    for entity_id, target in survivor.items():
        if target and target != entity_id:
            absorbed.setdefault(target, []).append(entity_id)

    nodes: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for entity_id, row in data.entities.items():
        target = survivor[entity_id]
        if target is None:
            excluded.append(dict(id=entity_id, status=row.get("status"), merged_into=row.get("merged_into"),
                                 reason=data.exclusion_reason(entity_id)))
            continue
        if target != entity_id:
            continue
        cluster = [entity_id] + sorted(absorbed.get(entity_id, []))
        alias_set, span_set, doc_set = set(), set(), set()
        for member in cluster:
            member_row = data.entities[member]
            if member_row.get("canonical_name"):
                alias_set.add(member_row["canonical_name"])
            if member_row.get("doc_id"):
                doc_set.add(member_row["doc_id"])
            alias_set.update(a["alias"] for a in data.aliases.get(member, []) if a.get("alias"))
        for m in data.mentions.values():
            if m.get("entity_id") in cluster and m.get("status") != "DISCARDED" and m.get("span_id"):
                span_set.add(m["span_id"])
                if data.span_doc(m["span_id"]):
                    doc_set.add(data.span_doc(m["span_id"]))
                elif m.get("doc_id"):
                    doc_set.add(m["doc_id"])
        nodes.append(dict(
            id=entity_id, label=row.get("canonical_name") or entity_id, type=row.get("type"),
            subtype=row.get("subtype"), status=row.get("status"), merged_into=row.get("merged_into"),
            source=row.get("source"), aliases=sorted(alias_set), mention_span_ids=sorted(span_set),
            doc_ids=sorted(doc_set), absorbed=cluster[1:], level="entity"))
    node_ids = {n["id"] for n in nodes}

    edges: list[dict[str, Any]] = []
    dangling: list[dict[str, Any]] = []
    for level, table in (("macro", data.macro_edges), ("micro", data.micro_edges)):
        for edge_id, row in table.items():
            src = survivor.get(row.get("src_entity_id") or "")
            dst = survivor.get(row.get("dst_entity_id") or "")
            record = dict(
                id=edge_id, source=src, target=dst,
                src_entity_id=row.get("src_entity_id"), dst_entity_id=row.get("dst_entity_id"),
                rel=row.get("rel"), family=_relation_family(ontology, row.get("rel")),
                subclass=row.get("subclass"), description=row.get("description"),
                status=row.get("status"), stance=row.get("stance"), negated=row.get("negated"),
                confidence=row.get("confidence"), level=level,
                tier_last=row.get("tier_last") if level == "macro" else "T1",
                time=[{k: t[k] for k in ("role", "time_id", "kind", "value_start", "value_end", "granularity")}
                      for t in data.time_rows(edge_id)],
                span_ids=list(data.edge_spans.get(edge_id, [])),
                doc_ids=data.edge_doc_ids(edge_id),
                lineage=data.lineage_down(edge_id) if level == "macro" else data.lineage_up(edge_id))
            if level == "macro":
                record.update(version=row.get("version"), supersedes=row.get("supersedes"),
                              section_id=row.get("section_id"))
            else:
                record.update(source_kind=row.get("source"), chunk_id=row.get("chunk_id"),
                              reject_reason=row.get("reject_reason"))
            if src in node_ids and dst in node_ids:
                edges.append(record)
            else:
                record.update(source=src or row.get("src_entity_id"), target=dst or row.get("dst_entity_id"),
                              dangling=True)
                dangling.append(record)
    edges.sort(key=lambda e: e["id"])
    dangling.sort(key=lambda e: e["id"])

    measures = _measures(data, nodes, edges, survivor)
    head = {"contract": CONTRACT, "version": CONTRACT_VERSION, "run_id": data.run_id}
    return {
        "nodes": {**head, "nodes": nodes, "excluded": excluded},
        "edges": {**head, "edges": edges, "dangling_edges": dangling},
        "measures": {**head, "measures": measures},
    }


MEASURE_DEFS = (
    ("evidence_count", "Edge: distinct evidence spans (edge_spans). Node: distinct spans of the entity's "
     "non-discarded mentions, including entities merged into it.", "spans", ("node", "edge")),
    ("source_document_count", "Distinct documents holding the evidence spans counted by evidence_count.",
     "documents", ("node", "edge")),
    ("conflict_count", "Edge: flags of kind conflict or inconsistency (state not DISMISSED) that name the edge. "
     "Node: distinct conflict flags (state not DISMISSED) that name a macro edge incident to the node.",
     "flags", ("node", "edge")),
    ("escalation_count", "Edge: escalations (L7) whose item_ids name the edge. Node: escalations whose item_ids "
     "name the entity, an entity merged into it, or a macro edge incident to it.", "escalations", ("node", "edge")),
    ("degree", "Macro edges (status not REJECTED) with the node as source or target; a self-loop counts once.",
     "edges", ("node",)),
    ("stated_share", "Share of the degree edges whose stance is 'stated'; null when degree is 0.",
     "ratio 0..1", ("node",)),
    ("first_date", "Earliest value_start among dated time references (INSTANT, INTERVAL, PERIOD) on the edge, "
     "or for a node on its degree edges, compared at stated precision (2024 sorts as 2024-01-01).",
     "ISO 8601 date at stated precision", ("node", "edge")),
    ("last_date", "Latest value_end (else value_start) among the same time references (2024 sorts as 2024-12-31).",
     "ISO 8601 date at stated precision", ("node", "edge")),
)


def _measures(data: RunData, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
              survivor: dict[str, str | None]) -> list[dict[str, Any]]:
    flags_by_edge: dict[str, set[str]] = {}
    conflict_flags_by_edge: dict[str, set[str]] = {}
    for f in data.flags.values():
        if f.get("state") == DISMISSED:
            continue
        for oid in f["object_ids"]:
            if f.get("kind") in CONFLICT_KINDS:
                flags_by_edge.setdefault(oid, set()).add(f["flag_id"])
            if f.get("kind") == "conflict":
                conflict_flags_by_edge.setdefault(oid, set()).add(f["flag_id"])
    esc_by_item: dict[str, set[str]] = {}
    for e in data.escalations.values():
        for item in e["item_ids"]:
            if isinstance(item, str):
                esc_by_item.setdefault(item, set()).add(e["esc_id"])

    values: dict[str, dict[str, dict[str, Any]]] = {m[0]: {"nodes": {}, "edges": {}} for m in MEASURE_DEFS}
    for e in edges:
        span_docs = {data.span_doc(s) for s in e["span_ids"]} - {None}
        first, last = _first_last(e["time"])
        v = values
        v["evidence_count"]["edges"][e["id"]] = len(e["span_ids"])
        v["source_document_count"]["edges"][e["id"]] = len(span_docs)
        v["conflict_count"]["edges"][e["id"]] = len(flags_by_edge.get(e["id"], ()))
        v["escalation_count"]["edges"][e["id"]] = len(esc_by_item.get(e["id"], ()))
        v["first_date"]["edges"][e["id"]] = first
        v["last_date"]["edges"][e["id"]] = last

    incident: dict[str, list[dict[str, Any]]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["level"] != "macro":
            continue
        for end in {e["source"], e["target"]}:
            incident.setdefault(end, []).append(e)
    for n in nodes:
        nid = n["id"]
        live = [e for e in incident.get(nid, []) if e["status"] != REJECTED]
        conflicts = set().union(*(conflict_flags_by_edge.get(e["id"], set()) for e in incident.get(nid, [])))
        escs = set().union(*(esc_by_item.get(x, set()) for x in [nid, *n["absorbed"]]),
                           *(esc_by_item.get(e["id"], set()) for e in incident.get(nid, [])))
        span_docs = {data.span_doc(s) for s in n["mention_span_ids"]} - {None}
        first, last = _first_last(t for e in live for t in e["time"])
        stated = sum(1 for e in live if e.get("stance") == "stated")
        v = values
        v["evidence_count"]["nodes"][nid] = len(n["mention_span_ids"])
        v["source_document_count"]["nodes"][nid] = len(span_docs)
        v["conflict_count"]["nodes"][nid] = len(conflicts)
        v["escalation_count"]["nodes"][nid] = len(escs)
        v["degree"]["nodes"][nid] = len(live)
        v["stated_share"]["nodes"][nid] = round(stated / len(live), 6) if live else None
        v["first_date"]["nodes"][nid] = first
        v["last_date"]["nodes"][nid] = last

    out = []
    for measure_id, definition, unit, applies in MEASURE_DEFS:
        vals = {scope + "s": dict(sorted(values[measure_id][scope + "s"].items())) for scope in applies}
        out.append(dict(id=measure_id, definition=definition, unit=unit, version=1,
                        applies_to=list(applies), values=vals))
    return out


# ====================================================================== GraphML
def _graphml_attrs(record: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value, sort_keys=True, ensure_ascii=False)
        elif isinstance(value, bool):
            value = bool(value)
        elif isinstance(value, int):
            value = int(value)
        elif isinstance(value, float):
            value = float(value)
        else:
            value = str(value)
        out[key] = value
    return out


NODE_ATTRS = ("label", "type", "subtype", "status", "merged_into", "source", "level", "aliases",
              "mention_span_ids", "doc_ids", "absorbed")
EDGE_ATTRS = ("rel", "family", "subclass", "description", "status", "stance", "negated", "confidence",
              "level", "tier_last", "time", "span_ids", "doc_ids", "lineage", "version", "supersedes")


def build_graph(contract: dict[str, Any]) -> nx.MultiDiGraph:
    """A networkx multigraph of the contract (node attrs, edge attrs, measure values)."""
    measures = contract["measures"]["measures"]
    graph = nx.MultiDiGraph(contract=CONTRACT, version=CONTRACT_VERSION, run_id=contract["nodes"]["run_id"])
    for n in contract["nodes"]["nodes"]:
        attrs = _graphml_attrs(n, NODE_ATTRS)
        for m in measures:
            value = m["values"].get("nodes", {}).get(n["id"])
            if value is not None:
                attrs[f"m_{m['id']}"] = value
        graph.add_node(n["id"], **attrs)
    for e in contract["edges"]["edges"]:
        attrs = _graphml_attrs(e, EDGE_ATTRS)
        for m in measures:
            value = m["values"].get("edges", {}).get(e["id"])
            if value is not None:
                attrs[f"m_{m['id']}"] = value
        graph.add_edge(e["source"], e["target"], key=e["id"], **attrs)
    return graph


# ====================================================================== entry point
def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def export_graph(store: Store, run_id: str, out_dir: str | Path, *, ontology: Any = None) -> dict[str, Path]:
    """Write ``nodes.json``, ``edges.json``, ``measures.json`` and ``graph.graphml``
    for ``run_id`` into ``out_dir``; return ``{file name: path}``.

    ``ontology`` (a :class:`synth.ontology.Ontology`) supplies each relation's
    ``family``; when omitted the default pack from the default config is used.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with store.reader() as r:
        data = load_run(r, run_id)
    contract = build_contract(data, ontology if ontology is not None else _default_ontology())
    paths = {name: out / name for name in FILES}
    paths["nodes.json"].write_bytes(_dump(contract["nodes"]))
    paths["edges.json"].write_bytes(_dump(contract["edges"]))
    paths["measures.json"].write_bytes(_dump(contract["measures"]))
    graphml = "\n".join(nx.generate_graphml(build_graph(contract), encoding="utf-8", prettyprint=True))
    paths["graph.graphml"].write_bytes((graphml + "\n").encode("utf-8"))
    return paths
