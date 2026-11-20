"""Run audits (U12, ``synth audit --run``): provenance, coverage, budget.

Purpose: prove the invariants for one run, or say exactly which objects break them.
Contract: :func:`audit_run` ``(store, run_id) -> dict`` with ``passed: bool``,
``failures`` (one line per failing check) and per-audit lists:

* **provenance** — COMMITTED macro edges, ACTIVE/MERGED entities (via their
  mentions, aliases and roles), time refs, propositions and links without a located
  span; spans whose stored quote differs from ``canonical_text[char_start:char_end]``
  (every grounded level stores the canonical slice — see ``write_span``); logged
  objects (``LOGGED_OBJECTS``) without a ``CREATE`` change-log row; change-log
  ``call_id`` values missing from L2; MERGED entities without an L5 row.
* **coverage** — ``SILENT_DROP`` sentences; sentences never finalized; ``OPEN``
  mentions; non-filtered sentences in zero or two+ micro chunks; ``unit_status``
  rows not ``DONE``.
* **budget** — L2 rows with ``tokens_in_counted > cap_in`` or ``tokens_out > cap_out``.
  Documented retries (``retries > 0`` or ``repairs > 0``) are exempt from the stage
  out cap only up to the hard cap, which the gateway records as their ``cap_out``.

``warnings`` (not failures): facts left in a non-terminal state.
Stores written: none. Token caps: none. Spec: P1 §13 (and §5, §7, §10).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from synth import states as S
from synth.audit.coverage import compute as compute_coverage, doc_sentences, run_documents
from synth.provenance.locator import GROUNDED
from synth.store.tables import LOGGED_OBJECTS


def audit_run(store, run_id: str) -> dict[str, Any]:
    with store.reader() as r:
        texts = _Texts(r)
        provenance = _provenance(r, run_id, texts)
        coverage, warnings = _coverage(r, run_id)
        budget = _budget(r, run_id)
    failures = [f"{section}.{name}: {len(items)}"
                for section, checks in (("provenance", provenance), ("coverage", coverage),
                                        ("budget", budget))
                for name, items in checks.items() if items]
    return {"run_id": run_id, "passed": not failures, "failures": failures,
            "provenance": provenance, "coverage": coverage, "budget": budget, "warnings": warnings}


class _Texts:
    """Canonical texts by (doc_id, text_version), loaded on demand."""

    def __init__(self, r) -> None:
        self.r = r
        self._cache: dict[tuple[str, int], str | None] = {}

    def get(self, doc_id: str, version: int | None) -> str | None:
        key = (doc_id, version or 1)
        if key not in self._cache:
            row = self.r.get("doc_texts", doc_id=doc_id, text_version=version or 1)
            self._cache[key] = row["canonical_text"] if row else None
        return self._cache[key]


def _located(span: dict[str, Any] | None, texts: _Texts) -> bool:
    if not span or span.get("grounding") not in GROUNDED:
        return False
    start, end = span.get("char_start"), span.get("char_end")
    text = texts.get(span["doc_id"], span.get("text_version"))
    return (text is not None and isinstance(start, int) and isinstance(end, int)
            and 0 <= start < end <= len(text))


# ------------------------------------------------------------------ provenance
def _provenance(r, run_id: str, texts: _Texts) -> dict[str, list]:
    spans: dict[str, dict[str, Any]] = {}

    def span(span_id: str | None) -> dict[str, Any] | None:
        if not span_id:
            return None
        if span_id not in spans:
            spans[span_id] = r.get("spans", span_id=span_id)
        return spans[span_id]

    def has_located(span_ids) -> bool:
        return any(_located(span(sid), texts) for sid in span_ids)

    out: dict[str, list] = {}
    edge_spans: dict[str, list[str]] = defaultdict(list)
    for row in r.select("edge_spans"):
        edge_spans[row["edge_id"]].append(row["span_id"])
    out["committed_edges_without_span"] = sorted(
        e["edge_id"] for e in r.select("macro_edges", run_id=run_id)
        if e["status"] == S.COMMITTED and not has_located(edge_spans.get(e["edge_id"], [])))

    entities = [e for e in r.select("entities", run_id=run_id) if e["status"] in (S.ACTIVE, S.MERGED)]
    mentions = r.select("mentions", run_id=run_id)
    mention_span = {m["mention_id"]: m["span_id"] for m in mentions}
    ent_spans: dict[str, list[str]] = defaultdict(list)
    for m in mentions:
        if m["entity_id"]:
            ent_spans[m["entity_id"]].append(m["span_id"])
    ids = {e["entity_id"] for e in entities}
    for row in r.select("entity_aliases"):
        if row["entity_id"] in ids:
            ent_spans[row["entity_id"]].append(mention_span.get(row["mention_id"]) or
                                               _mention_span(r, row["mention_id"]))
    for row in r.select("entity_roles"):
        if row["entity_id"] in ids:
            ent_spans[row["entity_id"]].append(row["span_id"])
    out["entities_without_span"] = sorted(e["entity_id"] for e in entities
                                          if not has_located(ent_spans.get(e["entity_id"], [])))
    out["time_refs_without_span"] = sorted(t["time_id"] for t in r.select("time_refs", run_id=run_id)
                                           if not has_located([t["span_id"]]))
    out["propositions_without_span"] = sorted(p["prop_id"] for p in r.select("propositions", run_id=run_id)
                                              if not has_located([p["span_id"]]))
    out["links_without_span"] = sorted(link["link_id"] for link in r.select("prop_links", run_id=run_id)
                                       if not has_located([link["span_id"]]))

    for s in r.select("spans", run_id=run_id):
        spans.setdefault(s["span_id"], s)
    mismatched = []
    for span_id, s in sorted(spans.items()):
        if not s or s.get("grounding") not in GROUNDED:
            continue
        text = texts.get(s["doc_id"], s.get("text_version"))
        start, end = s.get("char_start"), s.get("char_end")
        if text is None or start is None or end is None or text[start:end] != s.get("quote"):
            mismatched.append(span_id)
    out["span_quote_mismatch"] = mismatched

    created: dict[str, set[str]] = defaultdict(set)
    for row in r.select("change_log", op="CREATE"):
        created[row["object_type"]].add(row["object_id"])
    missing = []
    for table, (object_type, pk) in sorted(LOGGED_OBJECTS.items()):
        if table == "documents":
            rows = [{"doc_id": d} for d in run_documents(r, run_id)]
        else:
            rows = r.select(table, run_id=run_id)
        missing.extend({"object_type": object_type, "object_id": row[pk]}
                       for row in rows if row[pk] not in created[object_type])
    out["missing_create"] = sorted(missing, key=lambda m: (m["object_type"], m["object_id"]))

    calls = {row["call_id"] for row in r.select("model_calls")}
    out["unknown_call_ids"] = sorted({row["call_id"] for row in r.select("change_log", run_id=run_id)
                                      if row["call_id"] and row["call_id"] not in calls})

    resolved = set()
    for row in r.select("resolutions"):
        if str(row["decision"] or "").upper().startswith("MERGE"):
            resolved.update({row["entity_a"], row["entity_b"]})
    out["merged_without_resolution"] = sorted(e["entity_id"] for e in entities
                                              if e["status"] == S.MERGED and e["entity_id"] not in resolved)
    return out


def _mention_span(r, mention_id: str | None) -> str | None:
    if not mention_id:
        return None
    row = r.get("mentions", mention_id=mention_id)
    return row["span_id"] if row else None


# ------------------------------------------------------------------ coverage
def _coverage(r, run_id: str) -> tuple[dict[str, list], dict[str, list]]:
    out: dict[str, list] = {}
    final = {row["sentence_id"]: row for row in r.select("coverage_final", run_id=run_id)}
    out["silent_drops"] = sorted(sid for sid, row in final.items() if row["terminal"] == S.SILENT_DROP)
    doc_ids = run_documents(r, run_id)
    not_finalized, zero, many = [], [], []
    for doc_id in doc_ids:
        rows = doc_sentences(r, doc_id)
        chunks = r.select("micro_chunks", doc_id=doc_id)
        for s in rows:
            if s["sentence_id"] not in final:
                not_finalized.append(s["sentence_id"])
            if s["filtered"]:
                continue
            n = sum(1 for c in chunks if c["first_sentence_idx"] <= s["idx"] <= c["last_sentence_idx"])
            if n == 0:
                zero.append(s["sentence_id"])
            elif n > 1:
                many.append(s["sentence_id"])
    out["not_finalized"] = sorted(not_finalized)
    out["open_mentions"] = sorted(m["mention_id"] for m in r.select("mentions", run_id=run_id)
                                  if m["status"] == S.OPEN)
    out["sentences_without_chunk"] = sorted(zero)
    out["sentences_in_multiple_chunks"] = sorted(many)
    out["unfinished_units"] = sorted(
        ({"stage": u["stage"], "unit_id": u["unit_id"], "status": u["status"]}
         for u in r.select("unit_status", run_id=run_id) if u["status"] != S.DONE),
        key=lambda u: (u["stage"], u["unit_id"]))
    warnings = {"unfinished_facts": compute_coverage(r, run_id).unfinished_facts if doc_ids else []}
    return out, warnings


# ------------------------------------------------------------------ budget
def _budget(r, run_id: str) -> dict[str, list]:
    over_in, over_out = [], []
    for row in r.select("model_calls", run_id=run_id):
        cap_in, cap_out = row["cap_in"], row["cap_out"]
        if cap_in is not None and (row["tokens_in_counted"] or 0) > cap_in:
            over_in.append(row["call_id"])
        # A documented retry (length retry / repair) runs at the hard cap; the gateway
        # records that hard cap as its cap_out, so the same check bounds it there.
        if cap_out is not None and (row["tokens_out"] or 0) > cap_out:
            over_out.append(row["call_id"])
    return {"over_cap_in": sorted(over_in), "over_cap_out": sorted(over_out)}
