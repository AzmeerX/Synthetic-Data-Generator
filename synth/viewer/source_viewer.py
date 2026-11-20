"""J2 Source Viewer — static, fully offline HTML for one run.

Purpose: let a person read each document's canonical text with every sentence
coloured by its coverage outcome, and click any entity, mention, edge, date or
flag to see the exact characters it came from and how it got there.

Contract: :func:`build_viewer(store, run_id, out_dir) -> Path` writes
``index.html`` (run summary, one card per document) and one page per document,
``<doc_id>.html``, and returns the path of ``index.html``. Each page is
self-contained: inline CSS and JS only, no external scripts, fonts, styles or
images, and no ``http(s)://`` outside the document's own text. The run's data
for the page is embedded as JSON (``<script type="application/json"
id="synth-data">``, ``"contract": "synth.viewer"``, version 1) and rendered by a
small vanilla script:

- the text: every sentence is ``<span class="snt cov-<OUTCOME>" id="<sentence_id>">``
  cut into pieces (``.pc`` with ``data-a`` / ``data-b`` character offsets) at every
  span boundary, so a span highlights character-exactly (``.pc.mark``) and its
  sentences get ``.hl``. The outcome is ``coverage_final.terminal``; without a
  finalizer row it is the latest ``coverage_events`` outcome, marked provisional
  (``.prov`` + a banner); with neither it is ``NO_COVERAGE``.
- side panels (cards): entities (aliases, roles, mentions, status / merged_into),
  macro edges, micro edges (Tier 1), time references, flags, propositions, and
  mentions without an entity. Clicking a card highlights all its spans, scrolls
  to the first, and expands its history: change-log rows (op, stage, module,
  rule_id, call_id, before → after), the model calls behind them (prompt, model,
  tokens), lineage (micro → macro), escalations, resolution decisions, flags.
- ``<doc_id>.html#sel=<object_id>`` selects an object on load (cross-document
  links, and deep links from the J3 graph viewer).

All document text is HTML-escaped; JSON is embedded with ``<``, ``>`` and ``&``
as ``\\u`` escapes and rendered with ``textContent`` only. Strings that do not
come from the document text and span quotes have ``://`` defanged to ``[:]//``.

Stores written: none (read-only; files only). Token caps: none (no model calls).
Spec: P1 §3 J2, §5 (provenance questions), §7 (coverage outcomes), §15 P1-M8.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from synth.export.graph_export import RunData, load_run
from synth.modules.common import DocView
from synth.states import (
    COVERAGE_FAILED, ESCALATION_PENDING, FACT_COMMITTED, FACT_SENTENCE, FACTS_REJECTED,
    GLINER_SCANNED, HELD_FOR_HUMAN, MICRO_READ, NESTED_SENTENCE, READ_NO_FACT, SEGMENTED,
    SILENT_DROP, SKIPPED_BUDGET, SKIPPED_FILTER, STAGES, T2_READ, T3_READ, TERMINAL_ORDER,
    UNEXPRESSIBLE,
)
from synth.store import Store
from synth.store.repo import Tx
from synth.store.tables import coverage_events, coverage_final, sentences

VIEWER_CONTRACT = "synth.viewer"
VIEWER_VERSION = 1
NO_COVERAGE = "NO_COVERAGE"

#: outcome -> one-line meaning (legend and tooltips).
OUTCOME_TEXT = {
    FACT_COMMITTED: "a committed fact cites this sentence",
    HELD_FOR_HUMAN: "a fact citing it is held for a human",
    FACTS_REJECTED: "facts cited it, all were rejected",
    UNEXPRESSIBLE: "content the ontology cannot express",
    READ_NO_FACT: "read by T2 or T3, nothing cites it",
    SKIPPED_FILTER: "filtered (repeated header or footer)",
    SKIPPED_BUDGET: "skipped: over the token budget",
    COVERAGE_FAILED: "the unit holding it failed",
    SILENT_DROP: "no terminal outcome: audit failure",
    SEGMENTED: "segmented by U1 only",
    GLINER_SCANNED: "scanned by GLiNER (U2)",
    MICRO_READ: "read by the micro LLM (U5)",
    FACT_SENTENCE: "labelled a fact sentence by U5",
    NESTED_SENTENCE: "labelled a nested-reasoning sentence by U5",
    T2_READ: "read by T2 (U7)",
    T3_READ: "read by T3 (U10)",
    ESCALATION_PENDING: "an escalation is pending (U9)",
    NO_COVERAGE: "no coverage event in this run",
}
#: Event outcomes in pipeline order (tie-break for "latest" when timestamps are equal).
EVENT_ORDER = (SEGMENTED, SKIPPED_FILTER, GLINER_SCANNED, MICRO_READ, FACT_SENTENCE, NESTED_SENTENCE,
               T2_READ, T3_READ, UNEXPRESSIBLE, SKIPPED_BUDGET, COVERAGE_FAILED, ESCALATION_PENDING)
LEGEND_ORDER = tuple(TERMINAL_ORDER) + tuple(o for o in EVENT_ORDER if o not in TERMINAL_ORDER) + (NO_COVERAGE,)
_STAGE_RANK = {s: i for i, s in enumerate(STAGES)}
_EVENT_RANK = {o: i for i, o in enumerate(EVENT_ORDER)}


# ====================================================================== helpers
def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _css_name(outcome: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", outcome or NO_COVERAGE)


def _defang(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("://", "[:]//")
    if isinstance(value, dict):
        return {k: _defang(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_defang(v) for v in value]
    return value


def _ts(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _short(value: Any, limit: int = 160) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _embed_json(data: Any) -> str:
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return blob.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _change_summary(before: Any, after: Any) -> list[dict[str, Any]]:
    """Changed fields as ``[{field, before, after}]`` (a CREATE lists its non-empty fields)."""
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before) | set(after))
        return [dict(field=k, before=_short(before.get(k)), after=_short(after.get(k)))
                for k in keys if before.get(k) != after.get(k)]
    if isinstance(after, dict):
        return [dict(field=k, before=None, after=_short(v)) for k, v in sorted(after.items())
                if v not in (None, "", [], {}) and k != "run_id"]
    if before is None and after is None:
        return []
    return [dict(field="-", before=_short(before), after=_short(after))]


# ====================================================================== coverage
def _coverage(r: Tx, run_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    finals = {row["sentence_id"]: row for row in r.select("coverage_final", run_id=run_id)}
    events: dict[str, list[dict[str, Any]]] = {}
    for row in r.select("coverage_events", run_id=run_id):
        events.setdefault(row["sentence_id"], []).append(row)
    for rows in events.values():
        rows.sort(key=lambda e: (_ts(e.get("ts")), _STAGE_RANK.get(e.get("stage"), -1),
                                 _EVENT_RANK.get(e.get("outcome"), -1), e.get("outcome") or "",
                                 e.get("ref_id") or ""))
    return finals, events


def _sentence_outcome(sentence_id: str, finals: dict, events: dict) -> tuple[str, bool, str | None]:
    final = finals.get(sentence_id)
    if final is not None:
        return final.get("terminal") or NO_COVERAGE, False, final.get("reason")
    evs = events.get(sentence_id)
    if evs:
        return evs[-1].get("outcome") or NO_COVERAGE, True, None
    return NO_COVERAGE, True, None


# ====================================================================== documents
def _run_doc_ids(r: Tx, run_id: str, data: RunData) -> list[str]:
    docs: set[str] = set()
    for table in (coverage_events, coverage_final):
        sub = select(table.c.sentence_id).where(table.c.run_id == run_id)
        stmt = select(sentences.c.doc_id).distinct().where(sentences.c.sentence_id.in_(sub))
        docs.update(row["doc_id"] for row in r.rows(stmt))
    docs.update(s["doc_id"] for s in data.spans.values() if s.get("run_id") == run_id and s.get("doc_id"))
    docs.update(e["doc_id"] for e in data.entities.values() if e.get("doc_id"))
    docs.update(m["doc_id"] for m in data.mentions.values() if m.get("doc_id"))
    docs.update(t["doc_id"] for t in data.time_refs.values() if t.get("doc_id"))
    manifest = data.run.get("manifest") if isinstance(data.run.get("manifest"), dict) else {}
    docs.update(d for d in (manifest.get("doc_ids") or []) if isinstance(d, str))
    known = {row["doc_id"] for row in r.select("documents")}
    docs &= known
    return sorted(docs or known)


# ====================================================================== page data
class _Objects:
    """Per-object JSON entries, their spans and their documents, for one run."""

    def __init__(self, data: RunData) -> None:
        self.data = data
        self.docs: dict[str, set[str]] = {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.kind: dict[str, str] = {}
        self._build()

    def _span_docs(self, span_ids: Iterable[str]) -> set[str]:
        return {d for d in (self.data.span_doc(s) for s in span_ids) if d}

    def _add(self, kind: str, entry: dict[str, Any], docs: set[str]) -> None:
        entry["span_ids"] = sorted({s for s in entry.get("span_ids", []) if s})
        self.entries[entry["id"]] = entry
        self.kind[entry["id"]] = kind
        self.docs[entry["id"]] = {d for d in docs | self._span_docs(entry["span_ids"]) if d}

    def _build(self) -> None:
        d = self.data
        survivor = d.survivors()
        absorbed: dict[str, list[str]] = {}
        for eid, target in survivor.items():
            if target and target != eid:
                absorbed.setdefault(target, []).append(eid)
        flags_of: dict[str, list[str]] = {}
        for f in d.flags.values():
            for oid in f["object_ids"]:
                if isinstance(oid, str):
                    flags_of.setdefault(oid, []).append(f["flag_id"])
        esc_of: dict[str, list[str]] = {}
        for e in d.escalations.values():
            for item in e["item_ids"]:
                if isinstance(item, str):
                    esc_of.setdefault(item, []).append(e["esc_id"])
        mentions_of: dict[str, list[dict[str, Any]]] = {}
        for m in d.mentions.values():
            if m.get("entity_id"):
                mentions_of.setdefault(m["entity_id"], []).append(m)
        edges_of_time: dict[str, list[str]] = {}
        for edge_id, rows in d.edge_times.items():
            for et in rows:
                edges_of_time.setdefault(et["time_id"], []).append(edge_id)
        resolutions_of: dict[str, list[dict[str, Any]]] = {}
        for res in d.resolutions:
            for me, other in ((res.get("entity_a"), res.get("entity_b")), (res.get("entity_b"), res.get("entity_a"))):
                if me:
                    resolutions_of.setdefault(me, []).append(dict(
                        decision_id=res["decision_id"], other=other, decision=res.get("decision"),
                        score=res.get("score"), decided_by=res.get("decided_by"), rule_id=res.get("rule_id"),
                        reverses=res.get("reverses")))

        for m in d.mentions.values():
            self._add("mention", dict(
                id=m["mention_id"], surface=m.get("surface"), label=m.get("label"), score=m.get("score"),
                status=m.get("status"), entity_id=m.get("entity_id"), time_id=m.get("time_id"),
                discard_reason=m.get("discard_reason"), source=m.get("source"),
                span_ids=[m.get("span_id")]), {m.get("doc_id")})

        def own_spans(entity_id: str) -> list[str]:
            spans = [m.get("span_id") for m in mentions_of.get(entity_id, []) if m.get("status") != "DISCARDED"]
            spans += [x.get("span_id") for x in d.roles.get(entity_id, [])]
            return [s for s in spans if s]

        for eid, e in d.entities.items():
            members = sorted(absorbed.get(eid, []))
            span_ids = own_spans(eid) + [s for m in members for s in own_spans(m)]
            docs = {e.get("doc_id")} | {d.entities[m].get("doc_id") for m in members}
            docs |= {m.get("doc_id") for m in mentions_of.get(eid, [])}
            aliases = sorted({a["alias"] for a in d.aliases.get(eid, []) if a.get("alias")})
            self._add("entity", dict(
                id=eid, name=e.get("canonical_name"), type=e.get("type"), subtype=e.get("subtype"),
                status=e.get("status"), merged_into=e.get("merged_into"), survivor=survivor.get(eid),
                absorbed=members, source=e.get("source"), doc_id=e.get("doc_id"), version=e.get("version"),
                norm_key=e.get("norm_key"), aliases=aliases,
                roles=[dict(role=x.get("role"), org_entity_id=x.get("org_entity_id"), time_id=x.get("time_id"),
                            span_id=x.get("span_id")) for x in d.roles.get(eid, [])],
                mention_ids=sorted(m["mention_id"] for m in mentions_of.get(eid, [])),
                span_ids=span_ids, flag_ids=sorted(set(flags_of.get(eid, []))),
                escalation_ids=sorted(set(esc_of.get(eid, []))),
                resolutions=resolutions_of.get(eid, [])), docs)

        for level, table in (("macro_edge", d.macro_edges), ("micro_edge", d.micro_edges)):
            for edge_id, row in table.items():
                entry = dict(
                    id=edge_id, src=row.get("src_entity_id"), dst=row.get("dst_entity_id"), rel=row.get("rel"),
                    subclass=row.get("subclass"), description=row.get("description"), status=row.get("status"),
                    stance=row.get("stance"), negated=row.get("negated"), confidence=row.get("confidence"),
                    time=[{"role": t["role"], "time_id": t["time_id"]} for t in d.time_rows(edge_id)],
                    span_ids=list(d.edge_spans.get(edge_id, [])), flag_ids=sorted(set(flags_of.get(edge_id, []))),
                    escalation_ids=sorted(set(esc_of.get(edge_id, []))))
                if level == "macro_edge":
                    entry.update(tier_last=row.get("tier_last"), version=row.get("version"),
                                 supersedes=row.get("supersedes"), section_id=row.get("section_id"),
                                 lineage=d.lineage_down(edge_id))
                else:
                    chunk = d.chunks.get(row.get("chunk_id") or "") or {}
                    entry.update(source=row.get("source"), chunk_id=row.get("chunk_id"),
                                 chunk_idx=chunk.get("idx"), reject_reason=row.get("reject_reason"),
                                 lineage_up=d.lineage_up(edge_id))
                self._add(level, entry, set(d.edge_doc_ids(edge_id)))

        for tid, t in d.time_refs.items():
            self._add("time_ref", dict(
                id=tid, surface=t.get("surface"), kind=t.get("kind"), value_start=t.get("value_start"),
                value_end=t.get("value_end"), granularity=t.get("granularity"), anchor_type=t.get("anchor_type"),
                anchor_time_id=t.get("anchor_time_id"), confidence=t.get("confidence"), rule_id=t.get("rule_id"),
                span_ids=[t.get("span_id")], edge_ids=sorted(set(edges_of_time.get(tid, []))),
                mention_ids=sorted(m["mention_id"] for m in d.mentions.values() if m.get("time_id") == tid),
                flag_ids=sorted(set(flags_of.get(tid, [])))), {t.get("doc_id")})

        links_of: dict[str, list[dict[str, Any]]] = {}
        for link in d.prop_links.values():
            for end in (link.get("src_prop_id"), link.get("dst_prop_id")):
                if end:
                    links_of.setdefault(end, []).append(dict(
                        link_id=link["link_id"], type=link.get("type"), src=link.get("src_prop_id"),
                        dst=link.get("dst_prop_id"), state=link.get("state"), span_id=link.get("span_id")))
        for pid, p in d.propositions.items():
            section = d.sections.get(p.get("section_id") or "") or {}
            self._add("proposition", dict(
                id=pid, text=p.get("text"), status=p.get("status"), state=p.get("state"),
                negated=p.get("negated"), confidence=p.get("confidence"),
                speaker_entity_id=p.get("speaker_entity_id"), links=links_of.get(pid, []),
                span_ids=[p.get("span_id")] + [x.get("span_id") for x in links_of.get(pid, [])],
                flag_ids=sorted(set(flags_of.get(pid, [])))), {section.get("doc_id")})

        # flags last: they borrow the spans and documents of the objects they name
        for fid, f in d.flags.items():
            span_ids = list(f["span_ids"])
            docs: set[str] = set()
            for oid in f["object_ids"]:
                if isinstance(oid, str) and oid in self.entries:
                    span_ids += self.entries[oid]["span_ids"]
                    docs |= self.docs[oid]
            self._add("flag", dict(
                id=fid, kind=f.get("kind"), state=f.get("state"), object_ids=f["object_ids"],
                raised_by=f.get("raised_by"), resolved_by=f.get("resolved_by"), note=f.get("note"),
                own_span_ids=sorted(s for s in f["span_ids"] if s), span_ids=span_ids), docs)

    # -- labels -------------------------------------------------------------
    def label(self, object_id: str) -> str:
        kind = self.kind.get(object_id)
        e = self.entries.get(object_id) or {}
        if kind == "entity":
            return e.get("name") or object_id
        if kind == "mention":
            return e.get("surface") or object_id
        if kind in ("macro_edge", "micro_edge"):
            neg = "NOT " if e.get("negated") else ""
            return f"{self.data.entity_name(e.get('src'))} {neg}{e.get('rel')} {self.data.entity_name(e.get('dst'))}"
        if kind == "time_ref":
            return e.get("surface") or object_id
        if kind == "flag":
            return f"{e.get('kind')} ({e.get('state')})"
        if kind == "proposition":
            return _short(e.get("text"), 80) or object_id
        return object_id

    def referenced(self, entry: dict[str, Any]) -> set[str]:
        ids: set[str] = {entry["id"]}
        for key in ("src", "dst", "merged_into", "survivor", "entity_id", "time_id", "anchor_time_id",
                    "speaker_entity_id", "supersedes"):
            if isinstance(entry.get(key), str):
                ids.add(entry[key])
        for key in ("absorbed", "mention_ids", "flag_ids", "edge_ids", "object_ids"):
            ids.update(x for x in entry.get(key, []) or [] if isinstance(x, str))
        ids.update(t["time_id"] for t in entry.get("time", []) if t.get("time_id"))
        ids.update(x.get("org_entity_id") for x in entry.get("roles", []) if x.get("org_entity_id"))
        ids.update(x.get("time_id") for x in entry.get("roles", []) if x.get("time_id"))
        ids.update(x["micro_edge_id"] for x in entry.get("lineage", []) if x.get("micro_edge_id"))
        ids.update(x["macro_edge_id"] for x in entry.get("lineage_up", []) if x.get("macro_edge_id"))
        ids.update(x["other"] for x in entry.get("resolutions", []) if x.get("other"))
        for link in entry.get("links", []):
            ids.update(x for x in (link.get("src"), link.get("dst")) if x)
        return ids


def _page_data(doc: DocView, run: dict[str, Any], objs: _Objects, finals: dict, events: dict,
               history: dict[str, list[dict[str, Any]]], calls: dict[str, dict[str, Any]],
               doc_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    data = objs.data
    doc_id = doc.doc_id
    page_ids = sorted(oid for oid, docs in objs.docs.items() if doc_id in docs)
    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in
                                                ("entity", "mention", "macro_edge", "micro_edge", "time_ref",
                                                 "flag", "proposition")}
    for oid in page_ids:
        by_kind[objs.kind[oid]].append(objs.entries[oid])
    # an entity card lists all its mentions, so they must be selectable here too
    extra_mentions = {m for e in by_kind["entity"] for m in e["mention_ids"]} - set(page_ids)
    by_kind["mention"] += [objs.entries[m] for m in sorted(extra_mentions) if m in objs.entries]

    referenced: set[str] = set()
    for entries in by_kind.values():
        for entry in entries:
            referenced |= objs.referenced(entry)
    refs = {oid: dict(kind=objs.kind[oid], label=objs.label(oid), docs=sorted(objs.docs.get(oid, ())))
            for oid in sorted(referenced) if oid in objs.entries}
    for oid in sorted(referenced - set(refs)):
        if oid in data.entities:   # e.g. an entity of another run would never be here; keep the id readable
            refs[oid] = dict(kind="entity", label=data.entity_name(oid), docs=[])

    span_ids: set[str] = set()
    for entries in by_kind.values():
        for entry in entries:
            span_ids.update(entry["span_ids"])
    spans = {}
    for sid in sorted(span_ids):
        s = data.spans.get(sid)
        if s is None:
            continue
        spans[sid] = dict(doc_id=s.get("doc_id"), start=s.get("char_start"), end=s.get("char_end"),
                          quote=s.get("quote"), grounding=s.get("grounding"), fuzzy_score=s.get("fuzzy_score"),
                          sentence_ids=s.get("sentence_ids") or [], created_by=s.get("created_by"))

    page_history = {oid: history[oid] for entries in by_kind.values() for oid in
                    (e["id"] for e in entries) if oid in history}
    call_ids = sorted({row["call_id"] for rows in page_history.values() for row in rows if row.get("call_id")})
    page_calls = {cid: calls[cid] for cid in call_ids if cid in calls}
    esc_ids = sorted({x for entries in by_kind.values() for e in entries for x in e.get("escalation_ids", [])})
    escalations = {}
    for esc_id in esc_ids:
        e = data.escalations[esc_id]
        verdict = e.get("verdict") if isinstance(e.get("verdict"), dict) else {}
        escalations[esc_id] = dict(
            esc_id=esc_id, item_type=e.get("item_type"), item_ids=e["item_ids"], reasons=e["reasons"],
            question_type=e.get("question_type"), outcome=e.get("outcome"), model_id=e.get("model_id"),
            tokens_in=e.get("tokens_in"), tokens_out=e.get("tokens_out"), packet_tokens=e.get("packet_tokens"),
            context_asks=e.get("context_asks"), verdict=verdict.get("verdict"),
            rationale=_short(verdict.get("rationale"), 400))

    sentence_rows = []
    counts: dict[str, int] = {}
    any_provisional = False
    for s in doc.sentences:
        outcome, provisional, reason = _sentence_outcome(s["sentence_id"], finals, events)
        counts[outcome] = counts.get(outcome, 0) + 1
        any_provisional |= provisional
        sentence_rows.append(dict(
            id=s["sentence_id"], idx=s["idx"], label=f"s{s['idx']}", start=s["char_start"], end=s["char_end"],
            page=s.get("page"), kind=s.get("kind"), filtered=bool(s.get("filtered")), outcome=outcome,
            provisional=provisional, reason=reason,
            events=[dict(stage=e.get("stage"), outcome=e.get("outcome"), ref_id=e.get("ref_id"), ts=_ts(e.get("ts")))
                    for e in events.get(s["sentence_id"], [])]))
    outcomes = [dict(outcome=o, count=counts[o], terminal=o in TERMINAL_ORDER, description=OUTCOME_TEXT.get(o, ""))
                for o in _legend_order(counts)]

    page = dict(
        contract=VIEWER_CONTRACT, version=VIEWER_VERSION,
        run=dict(run_id=run.get("run_id"), arm=run.get("arm"), status=run.get("status"),
                 started_at=_ts(run.get("started_at")), finished_at=_ts(run.get("finished_at")),
                 config_hash=run.get("config_hash"), git_sha=run.get("git_sha")),
        doc=dict(doc_id=doc_id, filename=doc.doc.get("filename"), text_version=doc.text_version,
                 doc_date=doc.doc.get("doc_date"), sovereign=bool(doc.doc.get("sovereign")),
                 length=len(doc.text), pages=doc.doc.get("pages")),
        docs=doc_index, outcomes=outcomes, provisional=any_provisional, sentences=sentence_rows,
        entities=by_kind["entity"], mentions=by_kind["mention"], macro_edges=by_kind["macro_edge"],
        micro_edges=by_kind["micro_edge"], time_refs=by_kind["time_ref"], flags=by_kind["flag"],
        propositions=by_kind["proposition"], refs=refs, history=page_history, calls=page_calls,
        escalations=escalations)
    quotes = {sid: s["quote"] for sid, s in spans.items()}
    page = _defang(page)
    page["spans"] = {sid: {**_defang({k: v for k, v in s.items() if k != "quote"}), "quote": quotes[sid]}
                     for sid, s in spans.items()}
    return page


def _legend_order(counts: dict[str, int]) -> list[str]:
    known = [o for o in LEGEND_ORDER if counts.get(o)]
    return known + sorted(o for o in counts if o not in LEGEND_ORDER)


# ====================================================================== text
def _render_text(text: str, sentence_rows: list[dict[str, Any]], cuts: Iterable[int]) -> str:
    """Canonical text as escaped pieces cut at every sentence and span boundary."""
    n = len(text)

    def clamp(x: Any) -> int:
        return max(0, min(n, int(x or 0)))

    rows = sorted(sentence_rows, key=lambda s: (clamp(s["start"]), clamp(s["end"])))
    points = {0, n}
    for s in rows:
        points.update((clamp(s["start"]), clamp(s["end"])))
    points.update(clamp(c) for c in cuts)
    empty_at: dict[int, list[dict[str, Any]]] = {}
    spans = []
    for s in rows:
        if clamp(s["end"]) <= clamp(s["start"]):
            empty_at.setdefault(clamp(s["start"]), []).append(s)
        else:
            spans.append(s)
    pts = sorted(points)
    out: list[str] = []
    si = 0
    open_id: str | None = None

    def open_tag(s: dict[str, Any]) -> str:
        cls = f"snt cov-{_css_name(s['outcome'])}" + (" prov" if s["provisional"] else "")
        tip = f"{s['label']} · {s['outcome']}" + (" (provisional)" if s["provisional"] else "")
        return (f'<span class="{cls}" id="{_esc(s["id"])}" data-idx="{int(s["idx"])}" '
                f'data-label="{_esc(s["label"])}" data-outcome="{_esc(s["outcome"])}" title="{_esc(tip)}">')

    def piece(a: int, b: int) -> str:
        body = _esc(text[a:b]).replace("\f", '<span class="pb" aria-hidden="true">\f</span>')
        return f'<span class="pc" data-a="{a}" data-b="{b}">{body}</span>'

    for a, b in zip(pts, pts[1:]):
        for s in empty_at.pop(a, []):
            out.append(open_tag(s) + "</span>")
        while si < len(spans) and clamp(spans[si]["end"]) <= a:
            si += 1
        current = spans[si] if si < len(spans) and clamp(spans[si]["start"]) <= a and b <= clamp(spans[si]["end"]) else None
        current_id = current["id"] if current else None
        if current_id != open_id:
            if open_id is not None:
                out.append("</span>")
            if current is not None:
                out.append(open_tag(current))
            open_id = current_id
        out.append(piece(a, b))
    if open_id is not None:
        out.append("</span>")
    for pos in sorted(empty_at):
        for s in empty_at[pos]:
            out.append(open_tag(s) + "</span>")
    return "".join(out)


# ====================================================================== pages
def _legend_html(outcomes: list[dict[str, Any]]) -> str:
    items = []
    for o in outcomes:
        items.append(
            f'<li><span class="sw cov-{_css_name(o["outcome"])}" aria-hidden="true"></span>'
            f'<span class="lg-name">{_esc(o["outcome"])}</span> <span class="lg-count">{int(o["count"])}</span>'
            f'<span class="lg-desc">{_esc(o["description"])}</span></li>')
    return '<ul class="legend" aria-label="Coverage outcomes">' + "".join(items) + "</ul>"


def _doc_page(page: dict[str, Any], text_html: str, prev_next: tuple[str | None, str | None]) -> str:
    doc, run = page["doc"], page["run"]
    banner = ""
    if page["provisional"]:
        n = sum(1 for s in page["sentences"] if s["provisional"])
        banner = (f'<div class="banner warn" id="prov-banner"><strong>PROVISIONAL.</strong> The coverage finalizer '
                  f'has not run for {n} of {len(page["sentences"])} sentences: their colour is the latest coverage '
                  f'event, not a terminal outcome (dotted underline).</div>')
    prev_link = f'<a class="btn ghost" href="{_esc(prev_next[0])}">&larr; Previous</a>' if prev_next[0] else ""
    next_link = f'<a class="btn ghost" href="{_esc(prev_next[1])}">Next &rarr;</a>' if prev_next[1] else ""
    meta = [f"<dt>Document</dt><dd>{_esc(doc['doc_id'])}</dd>",
            f"<dt>Text version</dt><dd>{_esc(doc['text_version'])}</dd>",
            f"<dt>Date</dt><dd>{_esc(doc['doc_date'] or '—')}</dd>",
            f"<dt>Sentences</dt><dd>{len(page['sentences'])}</dd>",
            f"<dt>Run</dt><dd>{_esc(run['run_id'])} · {_esc(run['arm'])} · {_esc(run['status'])}</dd>"]
    sovereign = '<span class="chip ledger">SOVEREIGN</span>' if doc["sovereign"] else ""
    groups = "".join(
        f'<details class="group" id="g-{key}" open><summary><span>{_esc(title)}</span> '
        f'<span class="count" id="n-{key}">0</span></summary><div class="cards" id="c-{key}"></div></details>'
        for key, title in (("entities", "Entities"), ("macro_edges", "Macro edges"),
                           ("micro_edges", "Micro edges (Tier 1)"), ("time_refs", "Time references"),
                           ("flags", "Flags"), ("propositions", "Propositions"),
                           ("loose_mentions", "Mentions without an entity")))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(doc['filename'] or doc['doc_id'])} · Synth source viewer</title>
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <div class="brand"><h1>{_esc(doc['filename'] or doc['doc_id'])}</h1>
    <span class="tagline">Synth source viewer · run {_esc(run['run_id'])}</span> {sovereign}</div>
  <nav class="tabs"><a class="btn ghost" href="index.html">All documents</a>{prev_link}{next_link}</nav>
</header>
<main>
{banner}
<section class="panel docmeta"><dl class="kv">{''.join(meta)}</dl>{_legend_html(page['outcomes'])}</section>
<div class="controls">
  <label class="check"><input type="checkbox" id="show-labels"> Show sentence labels</label>
  <input type="search" id="filter" placeholder="Filter cards" aria-label="Filter cards">
  <button type="button" id="clear" class="ghost">Clear highlight</button>
  <span id="status" class="status" role="status" aria-live="polite"></span>
</div>
<div class="layout">
  <article class="text" id="text" aria-label="Canonical text">{text_html}</article>
  <aside class="side" id="side">
    <section class="panel sel" id="sel-panel"><h3>Selection</h3>
      <div id="sel-body" class="muted">Click a sentence, or a card: entity, mention, edge, date or flag.</div></section>
    {groups}
  </aside>
</div>
</main>
<script type="application/json" id="synth-data">{_embed_json(page)}</script>
<script>{JS}</script>
</body>
</html>
"""


def _index_page(run: dict[str, Any], doc_cards: list[dict[str, Any]], totals: dict[str, Any]) -> str:
    cards = []
    for c in doc_cards:
        total = max(1, c["sentences"])
        bar = "".join(
            f'<span class="seg cov-{_css_name(o["outcome"])}" style="width:{100 * o["count"] / total:.3f}%" '
            f'title="{_esc(o["outcome"])}: {int(o["count"])}"></span>' for o in c["outcomes"])
        chips = "".join(f'<span class="chip neutral">{_esc(o["outcome"])} {int(o["count"])}</span>'
                        for o in c["outcomes"])
        state = ('<span class="chip flagged">PROVISIONAL</span>' if c["provisional"]
                 else '<span class="chip yielded">FINAL</span>')
        cards.append(f"""<a class="panel doccard" href="{_esc(c['href'])}">
  <h3>{_esc(c['filename'] or c['doc_id'])}</h3>
  <div class="small mono muted">{_esc(c['doc_id'])}</div>
  <div class="obar" aria-hidden="true">{bar}</div>
  <div class="chips">{state}{chips}</div>
  <div class="small mono">{c['sentences']} sentences · {c['entities']} entities · {c['macro_edges']} macro edges ·
  {c['micro_edges']} micro edges · {c['time_refs']} time refs · {c['flags']} flags</div>
</a>""")
    stats = "".join(f'<div class="stat {cls}"><div class="value">{_esc(value)}</div>'
                    f'<div class="label">{_esc(label)}</div></div>' for label, value, cls in totals["tiles"])
    breakdown = "".join(
        f"<dt>{_esc(name)}</dt><dd>{_esc(', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or '—')}</dd>"
        for name, counts in totals["breakdown"])
    legend = _legend_html(totals["outcomes"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run {_esc(run.get('run_id'))} · Synth source viewer</title>
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <div class="brand"><h1>Source viewer</h1>
    <span class="tagline">Synth · run {_esc(run.get('run_id'))} · arm {_esc(run.get('arm'))} · {_esc(run.get('status'))}</span></div>
</header>
<main>
<section class="tiles">{stats}</section>
<section class="panel"><h2>Coverage</h2>{legend}<dl class="kv">{breakdown}</dl></section>
<h2>Documents</h2>
<section class="docgrid">{''.join(cards) or '<div class="empty-state">No documents in this run.</div>'}</section>
</main>
</body>
</html>
"""


# ====================================================================== entry point
def build_viewer(store: Store, run_id: str, out_dir: str | Path, *, doc_ids: Iterable[str] | None = None) -> Path:
    """Write the static viewer for ``run_id`` into ``out_dir``; return ``index.html``.

    Layout: ``index.html`` plus one ``<doc_id>.html`` per document of the run
    (``doc_ids`` restricts the set). Pages work from ``file://`` with no network.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with store.reader() as r:
        data = load_run(r, run_id)
        finals, events = _coverage(r, run_id)
        docs = list(doc_ids) if doc_ids is not None else _run_doc_ids(r, run_id, data)
        views = [DocView.load(r, d) for d in docs]
        history: dict[str, list[dict[str, Any]]] = {}
        dropped = 0
        for row in r.select("change_log", run_id=run_id):
            if row.get("op") == "DROPPED":
                dropped += 1
            history.setdefault(row["object_id"], []).append(row)
        calls = {}
        for c in r.select("model_calls", run_id=run_id):
            calls[c["call_id"]] = dict(
                call_id=c["call_id"], prompt_name=c.get("prompt_name"), prompt_sha256=c.get("prompt_sha256"),
                model_id=c.get("model_id"), endpoint=c.get("endpoint"), stage=c.get("stage"),
                module=c.get("module"), unit_id=c.get("unit_id"), tokens_in=c.get("tokens_in"),
                tokens_out=c.get("tokens_out"), reasoning_tokens=c.get("reasoning_tokens"),
                tokens_in_counted=c.get("tokens_in_counted"), cap_in=c.get("cap_in"), cap_out=c.get("cap_out"),
                latency_ms=c.get("latency_ms"), finish_reason=c.get("finish_reason"),
                schema_valid=c.get("schema_valid"), repairs=c.get("repairs"), retries=c.get("retries"),
                cost_usd=c.get("cost_usd"), error=_short(c.get("error"), 300))
    hist_rows: dict[str, list[dict[str, Any]]] = {}
    for oid, rows in history.items():
        rows.sort(key=lambda x: (_ts(x.get("ts")), x["change_id"]))
        hist_rows[oid] = [dict(change_id=x["change_id"], op=x.get("op"), stage=x.get("stage"), module=x.get("module"),
                               rule_id=x.get("rule_id"), call_id=x.get("call_id"), ts=_ts(x.get("ts")),
                               object_type=x.get("object_type"),
                               changes=_change_summary(x.get("before"), x.get("after"))) for x in rows]

    objs = _Objects(data)
    doc_index = {v.doc_id: dict(filename=v.doc.get("filename"), href=f"{v.doc_id}.html") for v in views}
    doc_cards = []
    all_counts: dict[str, int] = {}
    for i, view in enumerate(views):
        page = _page_data(view, data.run, objs, finals, events, hist_rows, calls, doc_index)
        cuts = []
        for s in page["spans"].values():
            if s["doc_id"] == view.doc_id and s["start"] is not None and s["end"] is not None:
                cuts += [s["start"], s["end"]]
        text_html = _render_text(view.text, page["sentences"], cuts)
        prev_href = f"{views[i - 1].doc_id}.html" if i > 0 else None
        next_href = f"{views[i + 1].doc_id}.html" if i + 1 < len(views) else None
        (out / f"{view.doc_id}.html").write_text(_doc_page(page, text_html, (prev_href, next_href)),
                                                 encoding="utf-8", newline="\n")
        for o in page["outcomes"]:
            all_counts[o["outcome"]] = all_counts.get(o["outcome"], 0) + o["count"]
        doc_cards.append(dict(
            doc_id=view.doc_id, filename=view.doc.get("filename"), href=f"{view.doc_id}.html",
            sentences=len(page["sentences"]), outcomes=page["outcomes"], provisional=page["provisional"],
            entities=len(page["entities"]), macro_edges=len(page["macro_edges"]),
            micro_edges=len(page["micro_edges"]), time_refs=len(page["time_refs"]), flags=len(page["flags"])))

    def count(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
        out_counts: dict[str, int] = {}
        for row in rows:
            out_counts[str(row.get(key))] = out_counts.get(str(row.get(key)), 0) + 1
        return out_counts

    totals = dict(
        tiles=[("documents", len(views), "yellow"), ("sentences", sum(c["sentences"] for c in doc_cards), ""),
               ("entities", len(data.entities), "lime"), ("macro edges", len(data.macro_edges), "cyan"),
               ("micro edges", len(data.micro_edges), ""), ("time refs", len(data.time_refs), ""),
               ("flags", len(data.flags), "yellow"), ("model calls", len(calls), "violet"),
               ("dropped (ungrounded)", dropped, "pink" if dropped else "")],
        breakdown=[("macro edges", count(data.macro_edges.values(), "status")),
                   ("micro edges", count(data.micro_edges.values(), "status")),
                   ("entities", count(data.entities.values(), "status")),
                   ("flags", {f"{k}": v for k, v in count(
                       ({"k": f"{f.get('kind')}/{f.get('state')}"} for f in data.flags.values()), "k").items()}),
                   ("escalations", count(data.escalations.values(), "outcome"))],
        outcomes=[dict(outcome=o, count=all_counts[o], description=OUTCOME_TEXT.get(o, ""))
                  for o in _legend_order(all_counts)])
    index = out / "index.html"
    index.write_text(_defang_html_index(_index_page(data.run, doc_cards, totals)), encoding="utf-8", newline="\n")
    return index


def _defang_html_index(page: str) -> str:
    return page.replace("://", "[:]//")


# ====================================================================== assets
CSS = """
:root{--paper:#FFFDF5;--ink:#0A0A0A;--blue:#2B4EFF;--pink:#FF3D8B;--yellow:#FFE500;--lime:#B4F000;
--orange:#FF6B1A;--violet:#8B45FF;--cyan:#00E0E0;--border:4px solid var(--ink);--shadow-sm:5px 5px 0 var(--ink);
--sans:"Archivo Black","Helvetica Neue",Helvetica,"Arial Black",Arial,sans-serif;
--body:"Inter","Helvetica Neue",Helvetica,Arial,sans-serif;
--mono:"JetBrains Mono","SFMono-Regular",Menlo,Consolas,monospace;}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--body);font-size:16px;line-height:1.5}
h1,h2,h3,h4{font-family:var(--sans);font-weight:900;text-transform:uppercase;letter-spacing:-.01em;margin:0 0 .6rem;line-height:1.1}
h1{font-size:clamp(1.5rem,3.2vw,2.4rem);overflow-wrap:anywhere}h2{font-size:1.5rem}h3{font-size:1.05rem}h4{font-size:.8rem;letter-spacing:.03em;margin:.8rem 0 .35rem}
.mono{font-family:var(--mono)}.muted{opacity:.75}.small{font-size:.82rem}
header.top{border-bottom:var(--border);background:var(--yellow);padding:1rem 1.5rem .8rem}
.brand{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap}.brand h1{margin:0}
.tagline{font-family:var(--mono);font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}
nav.tabs{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:.8rem}
main{padding:1.25rem 1.5rem;max-width:1700px;margin:0 auto}
.btn,button{display:inline-block;font-family:var(--sans);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;
padding:.5rem .9rem;border:var(--border);border-radius:0;box-shadow:4px 4px 0 var(--ink);background:var(--blue);color:var(--paper);
cursor:pointer;text-decoration:none}
.btn.ghost,button.ghost{background:var(--paper);color:var(--ink)}
.btn:hover,button:hover{transform:translate(2px,2px);box-shadow:2px 2px 0 var(--ink)}
:focus-visible{outline:4px solid var(--pink);outline-offset:2px}
.panel{border:var(--border);box-shadow:var(--shadow-sm);background:var(--paper);padding:1rem 1.1rem;margin-bottom:1.1rem}
.banner{border:var(--border);box-shadow:var(--shadow-sm);padding:.8rem 1rem;margin-bottom:1rem;font-family:var(--mono);font-size:.88rem}
.banner.warn{background:var(--yellow)}
.kv{display:grid;grid-template-columns:minmax(110px,auto) 1fr;gap:.25rem .8rem;margin:0 0 .8rem}
.kv dt{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.kv dd{margin:0;font-family:var(--mono);font-size:.84rem;overflow-wrap:anywhere}
.chip{display:inline-flex;align-items:center;gap:.3rem;font-family:var(--mono);font-size:.7rem;font-weight:700;text-transform:uppercase;
letter-spacing:.03em;padding:.12rem .45rem;border:3px solid var(--ink);background:var(--paper);white-space:nowrap;margin:0 .3rem .3rem 0}
.chip.yielded{background:var(--lime)}.chip.flagged{background:var(--yellow)}.chip.empty{background:var(--orange)}
.chip.failed,.chip.rejected{background:var(--pink);color:var(--paper)}.chip.info{background:var(--blue);color:var(--paper)}
.chip.ledger{background:var(--violet);color:var(--paper)}.chip.neutral{background:var(--paper)}
.chip.ref,.ref{cursor:pointer}
.ref{font-weight:700;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:.15em}
.ref:hover{background:var(--yellow)}
a{color:var(--ink)}
/* coverage colours (status is also in each sentence's title and the optional labels) */
.cov-FACT_COMMITTED{background:#D4F77A}.cov-HELD_FOR_HUMAN{background:#FFF07A}.cov-FACTS_REJECTED{background:#FFC2DA}
.cov-UNEXPRESSIBLE{background:#DCC8FF}.cov-READ_NO_FACT{background:#C8F4F4}.cov-SKIPPED_FILTER{background:#E6E1D3;color:#3A3A3A}
.cov-SKIPPED_BUDGET{background:#FFC59E}.cov-FAILED{background:#FF8DB9}.cov-SILENT_DROP{background:var(--ink);color:var(--yellow)}
.cov-SEGMENTED,.cov-GLINER_SCANNED{background:#F1EEE4}.cov-MICRO_READ{background:#DCE3FF}
.cov-FACT_SENTENCE,.cov-NESTED_SENTENCE{background:#C6D1FF}.cov-T2_READ,.cov-T3_READ{background:#C8F4F4}
.cov-ESCALATION_PENDING{background:#FFF07A}.cov-NO_COVERAGE{background:var(--paper);outline:2px dashed var(--pink)}
.legend{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:.45rem .9rem}
.legend li{display:flex;align-items:center;gap:.35rem;font-family:var(--mono);font-size:.74rem}
.legend .sw{display:inline-block;width:1.1rem;height:1.1rem;border:3px solid var(--ink)}
.legend .lg-name{font-weight:700}.legend .lg-count{border:2px solid var(--ink);padding:0 .3rem;background:var(--paper)}
.legend .lg-desc{opacity:.75}
.controls{display:flex;flex-wrap:wrap;gap:.8rem;align-items:center;margin:0 0 1rem}
.controls input[type=search]{font-family:var(--mono);font-size:.9rem;padding:.45rem .6rem;border:var(--border);border-radius:0;
background:var(--paper);min-width:220px}
.check{font-family:var(--mono);font-size:.78rem;text-transform:uppercase;display:flex;gap:.4rem;align-items:center;cursor:pointer}
.status{font-family:var(--mono);font-size:.8rem}
.layout{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(320px,1fr);gap:1.25rem;align-items:start}
@media (max-width:1000px){.layout{grid-template-columns:1fr}.side{position:static!important;max-height:none!important}}
.text{border:var(--border);box-shadow:var(--shadow-sm);background:var(--paper);padding:1.25rem 1.5rem;margin:0;
font-size:1.2rem;line-height:1.9;white-space:pre-wrap;overflow-wrap:break-word;max-width:80ch}
.snt{cursor:pointer;-webkit-box-decoration-break:clone;box-decoration-break:clone;padding:.08em 0}
.snt.prov{text-decoration:underline dotted 2px;text-underline-offset:.3em}
.snt.hl{outline:3px solid var(--ink);outline-offset:1px}
.snt.focus{outline:3px dashed var(--blue)}
.pc.mark{background:var(--blue);color:var(--paper)}
.pb{display:block;height:0;border-top:4px dashed var(--ink);margin:.9rem 0;font-size:0;overflow:hidden}
.show-labels .snt::before{content:attr(data-label) " " attr(data-outcome);font:700 .6rem var(--mono);letter-spacing:.03em;
background:var(--ink);color:var(--paper);padding:0 .25rem;margin-right:.3rem;vertical-align:.2em;white-space:nowrap}
.side{position:sticky;top:0;max-height:100vh;overflow-y:auto;padding:0 .5rem 1rem 0}
.sel{background:var(--cyan)}
.sel .quote{font-family:var(--mono);font-size:.85rem;background:var(--paper);border:3px solid var(--ink);padding:.4rem .55rem;margin:.3rem 0}
details.group{border:var(--border);box-shadow:var(--shadow-sm);background:var(--paper);margin-bottom:1.1rem}
details.group>summary{font-family:var(--sans);text-transform:uppercase;font-size:.95rem;padding:.6rem .8rem;cursor:pointer;
background:var(--ink);color:var(--yellow);display:flex;justify-content:space-between;gap:.5rem}
details.group .count{font-family:var(--mono);background:var(--yellow);color:var(--ink);padding:0 .4rem}
.cards{padding:.7rem}
.card{border:3px solid var(--ink);box-shadow:4px 4px 0 var(--ink);padding:.55rem .7rem;margin-bottom:.7rem;background:var(--paper);cursor:pointer}
.card:hover{background:#FFF8C2}
.card.selected{background:var(--yellow);box-shadow:6px 6px 0 var(--blue)}
.card .title{font-weight:800;font-size:1rem;margin-bottom:.3rem;overflow-wrap:anywhere}
.card .line{font-size:.88rem;margin:.15rem 0;overflow-wrap:anywhere}
.card .line .k{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;margin-right:.35rem}
details.hist{margin-top:.4rem;border-top:3px solid var(--ink);padding-top:.3rem;cursor:auto}
details.hist>summary{font-family:var(--mono);font-size:.74rem;text-transform:uppercase;font-weight:700;cursor:pointer}
.hist-body table{border-collapse:collapse;width:100%;font-size:.74rem;font-family:var(--mono);background:var(--paper)}
.hist-body th,.hist-body td{border:2px solid var(--ink);padding:.2rem .3rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}
.hist-body th{background:var(--ink);color:var(--yellow);font-size:.66rem;text-transform:uppercase}
.hist-body ul{margin:.2rem 0;padding-left:1.1rem;font-size:.8rem}
.hrow{border:2px solid var(--ink);background:var(--paper);padding:.3rem .45rem;margin-bottom:.4rem}
.hrow .hhead{font-family:var(--mono);font-size:.72rem;font-weight:700;overflow-wrap:anywhere}
.hrow .hts{font-family:var(--mono);font-size:.66rem;opacity:.7}
.hrow ul{font-family:var(--mono);font-size:.72rem;overflow-wrap:anywhere}
.chip.mchip{text-transform:none;white-space:normal}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin-bottom:1.25rem}
.stat{border:var(--border);box-shadow:var(--shadow-sm);padding:.8rem .9rem;background:var(--paper)}
.stat .value{font-family:var(--sans);font-size:2rem;line-height:1}
.stat .label{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;margin-top:.3rem}
.stat.lime{background:var(--lime)}.stat.yellow{background:var(--yellow)}.stat.cyan{background:var(--cyan)}
.stat.violet{background:var(--violet);color:var(--paper)}.stat.pink{background:var(--pink);color:var(--paper)}
.docgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1.25rem}
a.doccard{display:block;text-decoration:none;color:var(--ink)}a.doccard:hover{background:var(--yellow)}
.obar{display:flex;height:22px;border:3px solid var(--ink);margin:.6rem 0;background:var(--paper)}
.obar .seg{display:block;height:100%;border-right:2px solid var(--ink)}.obar .seg:last-child{border-right:none}
.chips{margin:.3rem 0}
.empty-state{border:4px dashed var(--ink);padding:1.5rem;text-align:center;font-family:var(--mono)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}.btn:hover,button:hover{transform:none}}
"""

JS = r"""
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("synth-data").textContent);
  var DOC = D.doc.doc_id;
  var GROUPS = [["entities", "entity"], ["macro_edges", "macro_edge"], ["micro_edges", "micro_edge"],
                ["time_refs", "time_ref"], ["flags", "flag"], ["propositions", "proposition"]];
  var OBJ = {};
  GROUPS.forEach(function (g) { (D[g[0]] || []).forEach(function (o) { OBJ[o.id] = {kind: g[1], o: o}; }); });
  (D.mentions || []).forEach(function (o) { OBJ[o.id] = {kind: "mention", o: o}; });
  var SENT = {};
  D.sentences.forEach(function (s) { SENT[s.id] = s; });

  var pieces = Array.prototype.slice.call(document.querySelectorAll("#text .pc"));
  var A = pieces.map(function (p) { return +p.getAttribute("data-a"); });
  var B = pieces.map(function (p) { return +p.getAttribute("data-b"); });
  function firstEndingAfter(pos) {
    var lo = 0, hi = B.length;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (B[mid] <= pos) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // sentence -> objects whose spans touch it (for the sentence panel)
  var CITED = {};
  Object.keys(OBJ).forEach(function (id) {
    if (OBJ[id].kind === "mention") return;
    (OBJ[id].o.span_ids || []).forEach(function (sid) {
      var sp = D.spans[sid];
      if (!sp || sp.doc_id !== DOC) return;
      (sp.sentence_ids || []).forEach(function (x) {
        (CITED[x] = CITED[x] || []);
        if (CITED[x].indexOf(id) < 0) CITED[x].push(id);
      });
    });
  });

  // ---------------------------------------------------------------- dom helpers
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  var STATUS = {COMMITTED: "yielded", CONTRACTED: "yielded", ACTIVE: "yielded", RESOLVED: "yielded",
    VERIFIED: "info", CANDIDATE: "neutral", ESCALATED: "flagged", POTENTIAL: "flagged", FINAL: "flagged",
    OPEN: "flagged", HELD_FOR_HUMAN: "empty", REJECTED: "rejected", DISCARDED: "rejected", MERGED: "ledger",
    IRREDUCIBLE: "failed", DISMISSED: "neutral", PENDING: "flagged", ANSWERED: "yielded", FAILED: "failed"};
  function chip(text, cls) { return el("span", "chip " + (cls || "neutral"), text); }
  function statusChip(s) { return chip(s || "-", STATUS[s] || "neutral"); }
  function label(id) { var r = D.refs[id]; return r ? r.label : id; }
  function docName(docId) { var d = D.docs[docId]; return d ? (d.filename || docId) : docId; }
  function sentLabels(spanIds) {
    var out = [];
    (spanIds || []).forEach(function (sid) {
      var sp = D.spans[sid];
      if (!sp) return;
      if (sp.doc_id !== DOC) { var n = docName(sp.doc_id); if (out.indexOf(n) < 0) out.push(n); return; }
      (sp.sentence_ids || []).forEach(function (x) {
        var l = SENT[x] ? SENT[x].label : x;
        if (out.indexOf(l) < 0) out.push(l);
      });
    });
    return out.join(" ");
  }
  function ref(id, text) {
    if (!id) return el("span", "muted", "-");
    if (OBJ[id]) {
      var r = el("span", "ref", text || label(id));
      r.tabIndex = 0;
      r.setAttribute("role", "button");
      r.addEventListener("click", function (ev) { ev.stopPropagation(); select(id); });
      r.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.stopPropagation(); select(id); } });
      return r;
    }
    var info = D.refs[id];
    if (info && info.docs && info.docs.length && D.docs[info.docs[0]]) {
      var a = el("a", "ref", (text || info.label) + " (" + docName(info.docs[0]) + ")");
      a.href = D.docs[info.docs[0]].href + "#sel=" + encodeURIComponent(id);
      a.addEventListener("click", function (ev) { ev.stopPropagation(); });
      return a;
    }
    return el("span", "", text || label(id));
  }
  function line(parent, key, content) {
    var l = el("div", "line");
    if (key) l.appendChild(el("span", "k", key));
    (Array.isArray(content) ? content : [content]).forEach(function (c, i) {
      if (i) l.appendChild(document.createTextNode(" · "));
      l.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    parent.appendChild(l);
    return l;
  }
  function conf(x) { return (x === null || x === undefined) ? "-" : (+x).toFixed(2); }
  function timeValue(t) {
    if (!t) return "";
    var v = t.value_start || "";
    if (t.value_end && t.value_end !== t.value_start) v += " .. " + t.value_end;
    return v + (t.granularity ? " (" + t.granularity + ")" : "");
  }

  // ---------------------------------------------------------------- cards
  var RENDER = {
    entity: function (o, head, body) {
      head.appendChild(el("div", "title", o.name || o.id));
      head.appendChild(chip((o.type || "-") + (o.subtype ? " / " + o.subtype : ""), "info"));
      head.appendChild(statusChip(o.status));
      if (o.source) head.appendChild(chip(o.source));
      if (o.merged_into) line(body, "merged into", ref(o.merged_into));
      if (o.absorbed && o.absorbed.length) line(body, "absorbed", o.absorbed.map(function (x) { return ref(x); }));
      if (o.aliases.length) line(body, "aliases", o.aliases.join(" · "));
      o.roles.forEach(function (r) {
        var parts = [r.role || "-"];
        if (r.org_entity_id) { parts.push("@"); parts.push(ref(r.org_entity_id)); }
        if (r.time_id) parts.push(ref(r.time_id));
        line(body, "role", parts);
      });
      if (o.mention_ids.length) {
        var l = line(body, "mentions", []);
        o.mention_ids.forEach(function (mid) {
          var m = OBJ[mid] ? OBJ[mid].o : null;
          var c = ref(mid, (m ? m.surface : label(mid)) + (m ? " · " + sentLabels(m.span_ids) : ""));
          c.className += " chip neutral mchip";
          l.appendChild(c);
        });
      }
      if (o.flag_ids.length) line(body, "flags", o.flag_ids.map(function (x) { return ref(x); }));
    },
    macro_edge: function (o, head, body) { edgeCard(o, head, body, true); },
    micro_edge: function (o, head, body) { edgeCard(o, head, body, false); },
    time_ref: function (o, head, body) {
      head.appendChild(el("div", "title", o.surface || o.id));
      head.appendChild(chip(o.kind, "info"));
      if (o.granularity) head.appendChild(chip(o.granularity));
      head.appendChild(chip(o.anchor_type, o.anchor_type === "UNRESOLVED" ? "flagged" : "neutral"));
      line(body, "value", timeValue(o) || "-");
      if (o.anchor_time_id) line(body, "anchored to", ref(o.anchor_time_id));
      line(body, "rule", [o.rule_id || "-", "conf " + conf(o.confidence), sentLabels(o.span_ids)]);
      if (o.edge_ids.length) line(body, "on edges", o.edge_ids.map(function (x) { return ref(x); }));
      if (o.flag_ids.length) line(body, "flags", o.flag_ids.map(function (x) { return ref(x); }));
    },
    flag: function (o, head, body) {
      head.appendChild(el("div", "title", o.kind));
      head.appendChild(statusChip(o.state));
      if (o.raised_by) head.appendChild(chip("raised by " + o.raised_by));
      if (o.resolved_by) head.appendChild(chip("resolved by " + o.resolved_by));
      if (o.note) line(body, "note", o.note);
      line(body, "objects", o.object_ids.map(function (x) { return ref(x); }));
      if (o.span_ids.length) line(body, "evidence", sentLabels(o.span_ids));
    },
    proposition: function (o, head, body) {
      head.appendChild(el("div", "title", o.text || o.id));
      if (o.status) head.appendChild(chip(o.status, "info"));
      if (o.state) head.appendChild(statusChip(o.state));
      if (o.negated) head.appendChild(chip("negated", "rejected"));
      head.appendChild(chip("conf " + conf(o.confidence)));
      if (o.speaker_entity_id) line(body, "speaker", ref(o.speaker_entity_id));
      o.links.forEach(function (k) { line(body, "link", [ref(k.src), k.type || "-", ref(k.dst), k.state || ""]); });
      line(body, "evidence", sentLabels(o.span_ids));
    },
    mention: function (o, head, body) {
      head.appendChild(el("div", "title", o.surface || o.id));
      if (o.label) head.appendChild(chip(o.label, "info"));
      head.appendChild(statusChip(o.status));
      if (o.discard_reason) line(body, "discarded", o.discard_reason);
      if (o.entity_id) line(body, "entity", ref(o.entity_id));
      if (o.time_id) line(body, "time ref", ref(o.time_id));
      line(body, "evidence", [sentLabels(o.span_ids), "score " + conf(o.score)]);
    }
  };
  function edgeCard(o, head, body, macro) {
    var t = el("div", "title");
    t.appendChild(ref(o.src));
    t.appendChild(document.createTextNode(" " + (o.negated ? "NOT " : "") + o.rel + " "));
    t.appendChild(ref(o.dst));
    head.appendChild(t);
    head.appendChild(statusChip(o.status));
    if (o.stance) head.appendChild(chip(o.stance, o.stance === "stated" ? "neutral" : "flagged"));
    if (o.negated) head.appendChild(chip("negated", "rejected"));
    head.appendChild(chip("conf " + conf(o.confidence)));
    if (macro && o.tier_last) head.appendChild(chip(o.tier_last));
    if (macro && o.version > 1) head.appendChild(chip("v" + o.version));
    if (!macro && o.source) head.appendChild(chip(o.source));
    if (o.description) line(body, "desc", o.description);
    if (o.subclass) line(body, "subclass", o.subclass);
    o.time.forEach(function (t) {
      var tr = OBJ[t.time_id] ? OBJ[t.time_id].o : null;
      line(body, t.role, [ref(t.time_id), tr ? timeValue(tr) : ""]);
    });
    line(body, "evidence", sentLabels(o.span_ids) || "-");
    if (macro && o.supersedes) line(body, "supersedes", ref(o.supersedes));
    if (macro && o.lineage.length) line(body, "from micro", o.lineage.map(function (x) { return ref(x.micro_edge_id, x.op); }));
    if (!macro && o.lineage_up.length) line(body, "into macro", o.lineage_up.map(function (x) { return ref(x.macro_edge_id, x.op); }));
    if (!macro && o.reject_reason) line(body, "rejected", o.reject_reason);
    if (!macro && o.chunk_idx !== null && o.chunk_idx !== undefined) line(body, "chunk", "#" + o.chunk_idx);
    if (o.flag_ids.length) line(body, "flags", o.flag_ids.map(function (x) { return ref(x); }));
  }

  function card(kind, o) {
    var c = el("div", "card k-" + kind);
    c.id = "card-" + o.id;
    c.tabIndex = 0;
    c.setAttribute("role", "button");
    c.setAttribute("data-id", o.id);
    var head = el("div", "card-head"), body = el("div", "card-body");
    RENDER[kind](o, head, body);
    c.appendChild(head);
    c.appendChild(body);
    var hist = el("details", "hist");
    hist.appendChild(el("summary", null, "History"));
    var holder = el("div", "hist-body");
    hist.appendChild(holder);
    hist.addEventListener("toggle", function () { if (hist.open && !holder.firstChild) renderHistory(holder, o.id, kind); });
    c.appendChild(hist);
    c.addEventListener("click", function (ev) {
      if (ev.target.closest && ev.target.closest("summary, .hist-body, a, .ref")) return;
      select(o.id, {fromCard: true});
    });
    c.addEventListener("keydown", function (ev) {
      if (ev.target !== c) return;
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(o.id, {fromCard: true}); }
    });
    return c;
  }

  function table(parent, headers, rows) {
    var t = el("table"), tr = el("tr");
    headers.forEach(function (h) { tr.appendChild(el("th", null, h)); });
    t.appendChild(tr);
    rows.forEach(function (r) {
      var row = el("tr");
      r.forEach(function (c) { var td = el("td"); if (typeof c === "string") td.textContent = c; else if (c) td.appendChild(c); row.appendChild(td); });
      t.appendChild(row);
    });
    parent.appendChild(t);
  }

  function renderHistory(holder, id, kind) {
    var o = OBJ[id].o;
    var rows = D.history[id] || [];
    holder.appendChild(el("h4", null, "Change log (" + rows.length + ")"));
    rows.forEach(function (r) {
      var box = el("div", "hrow");
      var call = r.call_id ? (D.calls[r.call_id] ? D.calls[r.call_id].prompt_name + " (" + r.call_id + ")" : r.call_id) : "-";
      box.appendChild(el("div", "hhead", (r.op || "-") + " · stage " + (r.stage || "-") + " · module " + (r.module || "-") +
        " · rule " + (r.rule_id || "-") + " · call " + call));
      if (r.ts) box.appendChild(el("div", "hts", r.ts));
      var ul = el("ul");
      r.changes.forEach(function (c) {
        if (c.after === id && c.before === null) return;
        ul.appendChild(el("li", null, c.field + ": " + (c.before === null ? "" : c.before + " -> ") + (c.after === null ? "null" : c.after)));
      });
      box.appendChild(ul);
      holder.appendChild(box);
    });
    var callIds = [];
    rows.forEach(function (r) { if (r.call_id && callIds.indexOf(r.call_id) < 0) callIds.push(r.call_id); });
    if (callIds.length) {
      holder.appendChild(el("h4", null, "Model calls"));
      table(holder, ["call", "prompt", "model", "endpoint", "tokens in / out / reasoning", "schema"], callIds.map(function (cid) {
        var c = D.calls[cid];
        if (!c) return [cid, "(not in L2)", "-", "-", "-", "-"];
        return [cid, c.prompt_name || "-", c.model_id || "-", c.endpoint || "-",
                (c.tokens_in || 0) + " / " + (c.tokens_out || 0) + " / " + (c.reasoning_tokens || 0),
                c.schema_valid ? "valid" : "invalid"];
      }));
    }
    if (kind === "macro_edge") {
      holder.appendChild(el("h4", null, "Lineage (micro -> macro)"));
      if (!o.lineage.length) holder.appendChild(el("div", "muted small", "no micro edge (added by T2/T3)"));
      else table(holder, ["op", "micro edge"], o.lineage.map(function (x) { return [x.op || "-", ref(x.micro_edge_id)]; }));
      if (o.supersedes) { var s = el("div", "small"); s.appendChild(document.createTextNode("supersedes ")); s.appendChild(ref(o.supersedes)); holder.appendChild(s); }
    }
    if (kind === "micro_edge" && o.lineage_up.length) {
      holder.appendChild(el("h4", null, "Lineage (micro -> macro)"));
      table(holder, ["op", "macro edge"], o.lineage_up.map(function (x) { return [x.op || "-", ref(x.macro_edge_id)]; }));
    }
    if (o.escalation_ids && o.escalation_ids.length) {
      holder.appendChild(el("h4", null, "Escalations"));
      table(holder, ["question", "outcome", "reasons", "model", "tokens", "verdict"], o.escalation_ids.map(function (eid) {
        var e = D.escalations[eid] || {};
        return [e.question_type || "-", e.outcome || "-", (e.reasons || []).join(", "), e.model_id || "-",
                (e.tokens_in || 0) + " / " + (e.tokens_out || 0), (e.verdict || "-") + (e.rationale ? ": " + e.rationale : "")];
      }));
    }
    if (o.resolutions && o.resolutions.length) {
      holder.appendChild(el("h4", null, "Resolution decisions (L5)"));
      table(holder, ["decision", "other entity", "score", "by", "rule"], o.resolutions.map(function (x) {
        return [x.decision || "-", ref(x.other), conf(x.score), x.decided_by || "-", x.rule_id || "-"];
      }));
    }
    var elsewhere = {};
    (o.span_ids || []).forEach(function (sid) { var sp = D.spans[sid]; if (sp && sp.doc_id !== DOC) elsewhere[sp.doc_id] = (elsewhere[sp.doc_id] || 0) + 1; });
    var docs = Object.keys(elsewhere).sort();
    if (docs.length) {
      holder.appendChild(el("h4", null, "Also in other documents"));
      var ul = el("ul");
      docs.forEach(function (d) {
        var li = el("li"), a = el("a", null, docName(d) + " (" + elsewhere[d] + " spans)");
        a.href = (D.docs[d] ? D.docs[d].href : d + ".html") + "#sel=" + encodeURIComponent(id);
        li.appendChild(a); ul.appendChild(li);
      });
      holder.appendChild(ul);
    }
  }

  // ---------------------------------------------------------------- selection
  var status = document.getElementById("status");
  var selBody = document.getElementById("sel-body");
  function clearSel() {
    Array.prototype.forEach.call(document.querySelectorAll(".pc.mark"), function (e) { e.classList.remove("mark"); });
    Array.prototype.forEach.call(document.querySelectorAll(".snt.hl, .snt.focus"), function (e) { e.classList.remove("hl"); e.classList.remove("focus"); });
    Array.prototype.forEach.call(document.querySelectorAll(".card.selected"), function (e) { e.classList.remove("selected"); });
  }
  function select(id, opts) {
    opts = opts || {};
    var entry = OBJ[id];
    if (!entry) { status.textContent = "Not on this page: " + id; return; }
    clearSel();
    var o = entry.o, first = null, firstPos = Infinity, here = 0, away = 0, sents = [];
    (o.span_ids || []).forEach(function (sid) {
      var sp = D.spans[sid];
      if (!sp) return;
      if (sp.doc_id !== DOC) { away++; return; }
      here++;
      for (var i = firstEndingAfter(sp.start); i < pieces.length && A[i] < sp.end; i++) {
        if (A[i] >= sp.start && B[i] <= sp.end) {
          pieces[i].classList.add("mark");
          var s = pieces[i].closest(".snt");
          if (s) { s.classList.add("hl"); if (sents.indexOf(s.id) < 0) sents.push(s.id); }
          if (A[i] < firstPos) { firstPos = A[i]; first = pieces[i]; }
        }
      }
      (sp.sentence_ids || []).forEach(function (x) {
        var e = document.getElementById(x);
        if (e) { e.classList.add("hl"); if (sents.indexOf(x) < 0) sents.push(x); if (!first) first = e; }
      });
    });
    var c = document.getElementById("card-" + id);
    if (!c && entry.kind === "mention" && o.entity_id) c = document.getElementById("card-" + o.entity_id);
    if (c) {
      c.classList.add("selected");
      var group = c.closest("details.group");
      if (group) group.open = true;
      var hist = c.querySelector("details.hist");
      if (hist && !hist.open) hist.open = true;
      if (!opts.fromCard) c.scrollIntoView({block: "nearest"});
    }
    if (first) first.scrollIntoView({block: "center"});
    selBody.textContent = "";
    selBody.classList.remove("muted");
    selBody.appendChild(el("div", "title", entry.kind.replace("_", " ") + ": " + label(id)));
    selBody.appendChild(el("div", "small mono", here + " span(s) here" + (sents.length ? " in " + sents.map(function (x) { return SENT[x] ? SENT[x].label : x; }).join(" ") : "") + (away ? "; " + away + " in other documents" : "")));
    (o.span_ids || []).slice(0, 6).forEach(function (sid) {
      var sp = D.spans[sid];
      if (sp && sp.doc_id === DOC) selBody.appendChild(el("div", "quote", sp.quote + "  [" + sp.grounding + "]"));
    });
    status.textContent = "Selected " + label(id) + ": " + here + " span(s) highlighted" + (away ? ", " + away + " elsewhere" : "");
  }
  function showSentence(sid) {
    var s = SENT[sid];
    if (!s) return;
    Array.prototype.forEach.call(document.querySelectorAll(".snt.focus"), function (e) { e.classList.remove("focus"); });
    var node = document.getElementById(sid);
    if (node) node.classList.add("focus");
    selBody.textContent = "";
    selBody.classList.remove("muted");
    selBody.appendChild(el("div", "title", s.label + " · " + s.outcome + (s.provisional ? " (provisional)" : " (final)")));
    if (s.reason) selBody.appendChild(el("div", "small", "reason: " + s.reason));
    if (s.filtered) selBody.appendChild(el("div", "small", "filtered by U1"));
    if (s.events.length) table(selBody, ["stage", "event", "ref"], s.events.map(function (e) { return [e.stage || "-", e.outcome || "-", e.ref_id ? ref(e.ref_id) : "-"]; }));
    var cited = CITED[sid] || [];
    selBody.appendChild(el("h4", null, "Cited by (" + cited.length + ")"));
    var ul = el("ul");
    cited.forEach(function (id) { var li = el("li"); li.appendChild(document.createTextNode(OBJ[id].kind.replace("_", " ") + ": ")); li.appendChild(ref(id)); ul.appendChild(li); });
    selBody.appendChild(ul);
  }

  // ---------------------------------------------------------------- build
  function fill(key, kind, list) {
    var box = document.getElementById("c-" + key);
    document.getElementById("n-" + key).textContent = String(list.length);
    if (!list.length) { box.appendChild(el("div", "muted small", "none on this page")); return; }
    list.forEach(function (o) { box.appendChild(card(kind, o)); });
  }
  GROUPS.forEach(function (g) { fill(g[0], g[1], D[g[0]] || []); });
  fill("loose_mentions", "mention", (D.mentions || []).filter(function (m) { return !m.entity_id && !m.time_id; }));

  document.getElementById("text").addEventListener("click", function (ev) {
    var s = ev.target.closest && ev.target.closest(".snt");
    if (s) showSentence(s.id);
  });
  document.getElementById("clear").addEventListener("click", function () {
    clearSel(); status.textContent = ""; selBody.textContent = "Click a sentence, or a card."; selBody.classList.add("muted");
  });
  document.getElementById("show-labels").addEventListener("change", function (ev) {
    document.body.classList.toggle("show-labels", ev.target.checked);
  });
  document.getElementById("filter").addEventListener("input", function (ev) {
    var q = ev.target.value.toLowerCase();
    Array.prototype.forEach.call(document.querySelectorAll(".card"), function (c) {
      c.style.display = !q || c.textContent.toLowerCase().indexOf(q) >= 0 ? "" : "none";
    });
  });
  function fromHash() {
    var m = /^#sel=(.+)$/.exec(location.hash || "");
    if (m) select(decodeURIComponent(m[1]));
  }
  window.addEventListener("hashchange", fromHash);
  fromHash();
  window.SynthViewer = {select: select, showSentence: showSentence, data: D};
})();
"""
