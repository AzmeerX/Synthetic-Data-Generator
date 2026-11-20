"""Coverage finalizer (U12, FINALIZE stage): every sentence gets one terminal outcome.

Purpose: CLAUDE.md invariant 3 — coverage is total. For every sentence of every
document in the run (the run's documents = documents with any coverage event in
the run) compute the terminal outcome from the L3 events and the store state, in
the spec's priority order (``synth.states.TERMINAL_ORDER``):

1. ``FACT_COMMITTED``  a COMMITTED macro edge, proposition or link span covers it
2. ``HELD_FOR_HUMAN``  a held fact (or escalation / coverage event) cites it
3. ``FACTS_REJECTED``  only rejected facts cite it (rejected macro edges, props,
                       links; micro edges rejected by U6 or removed by T2)
4. ``UNEXPRESSIBLE``   T2 said the sentence holds a fact the ontology cannot express
5. ``READ_NO_FACT``    read by T2 or T3, nothing cites it
6. ``SKIPPED_FILTER``  header/footer line (U1)
7. ``SKIPPED_BUDGET``  a module ran out of budget on it
8. ``FAILED``          its unit failed
9. ``SILENT_DROP``     none of the above — an audit failure

Facts still in a non-terminal state (VERIFIED/ESCALATED macro edges, propositions
or links not yet COMMITTED/REJECTED/HELD, CANDIDATE micro edges) do not decide an
outcome; they are listed as ``unfinished_facts`` (an audit warning).

Contract: :func:`compute` (pure read) -> :class:`CoverageResult`;
:func:`finalize` (``ctx``) writes ``coverage_final`` (one row per sentence; a rerun
updates rows whose outcome changed, never deletes) and returns a summary with the
coverage metrics (read coverage, micro-read coverage, yield, loss rate, silent
drops) and the mentions still ``OPEN`` (an audit failure).
Stores written: L3 ``coverage_final``. Token caps: none (no model). Spec: P1 §7, §13.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import func, select

from synth import states as S
from synth.store.tables import coverage_events, doc_texts, sentences

READ_EVENTS = (S.T2_READ, S.T3_READ)
LOSS_OUTCOMES = (S.SKIPPED_BUDGET, S.COVERAGE_FAILED, S.SILENT_DROP)

COMMITTED_CLASS, HELD_CLASS, REJECTED_CLASS, PENDING_CLASS = "committed", "held", "rejected", "pending"


def fact_class(status: str | None) -> str:
    """Map a fact status/state onto committed | held | rejected | pending."""
    if status == S.COMMITTED:
        return COMMITTED_CLASS
    if status == S.HELD_FOR_HUMAN:
        return HELD_CLASS
    if status == S.REJECTED:
        return REJECTED_CLASS
    return PENDING_CLASS


# ------------------------------------------------------------------ run documents
def run_documents(r, run_id: str) -> list[str]:
    """Documents with any coverage event in the run."""
    stmt = (select(sentences.c.doc_id).distinct()
            .select_from(coverage_events.join(sentences,
                                              coverage_events.c.sentence_id == sentences.c.sentence_id))
            .where(coverage_events.c.run_id == run_id))
    return sorted(row[0] for row in r.execute(stmt))


def doc_sentences(r, doc_id: str) -> list[dict[str, Any]]:
    """The sentences of a document's latest text version, by idx."""
    version = r.execute(select(func.max(doc_texts.c.text_version))
                        .where(doc_texts.c.doc_id == doc_id)).scalar()
    if version is None:
        return r.select("sentences", order_by="idx", doc_id=doc_id)
    return r.select("sentences", order_by="idx", doc_id=doc_id, text_version=version)


class SentenceIndex:
    """Char range -> covering sentences, per document."""

    def __init__(self, by_doc: dict[str, list[dict[str, Any]]]) -> None:
        self.by_doc = {d: sorted(rows, key=lambda s: s["char_start"]) for d, rows in by_doc.items()}
        self._starts = {d: [s["char_start"] for s in rows] for d, rows in self.by_doc.items()}
        self.by_id = {s["sentence_id"]: s for rows in self.by_doc.values() for s in rows}

    def covering(self, doc_id: str, start: int | None, end: int | None) -> list[dict[str, Any]]:
        rows = self.by_doc.get(doc_id)
        if not rows or start is None or end is None:
            return []
        if end <= start:
            end = start + 1
        i = max(bisect_right(self._starts[doc_id], start) - 1, 0)
        hits = []
        for s in rows[i:]:
            if s["char_start"] >= end:
                break
            if s["char_end"] > start:
                hits.append(s)
        return hits

    def span_sentence_ids(self, span: dict[str, Any] | None) -> list[str]:
        if not span:
            return []
        hits = self.covering(span["doc_id"], span.get("char_start"), span.get("char_end"))
        if hits:
            return [s["sentence_id"] for s in hits]
        return [sid for sid in (span.get("sentence_ids") or []) if sid in self.by_id]


# ------------------------------------------------------------------ result
@dataclass
class CoverageResult:
    run_id: str
    doc_ids: list[str]
    sentences: dict[str, dict[str, Any]]                     # sentence_id -> row
    events: dict[str, set[str]]                              # sentence_id -> outcomes seen
    terminals: dict[str, tuple[str, str]] = field(default_factory=dict)   # -> (terminal, reason)
    open_mentions: list[str] = field(default_factory=list)
    unfinished_facts: list[str] = field(default_factory=list)
    has_micro: bool = False

    def filtered(self, sid: str) -> bool:
        return bool(self.sentences[sid].get("filtered")) or S.SKIPPED_FILTER in self.events.get(sid, ())

    def counts(self) -> dict[str, int]:
        c = Counter(t for t, _ in self.terminals.values())
        return {name: c.get(name, 0) for name in S.TERMINAL_ORDER}

    def per_doc(self) -> dict[str, Counter]:
        """Per-document counters (for bootstrap over documents)."""
        out: dict[str, Counter] = {d: Counter() for d in self.doc_ids}
        for sid, row in self.sentences.items():
            c = out.setdefault(row["doc_id"], Counter())
            if self.filtered(sid):
                continue
            ev = self.events.get(sid, set())
            terminal = self.terminals.get(sid, (S.SILENT_DROP, ""))[0]
            c["nonfiltered"] += 1
            c["read"] += int(bool(ev & set(READ_EVENTS)))
            c["micro_read"] += int(S.MICRO_READ in ev)
            c["committed"] += int(terminal == S.FACT_COMMITTED)
            c["lost"] += int(terminal in LOSS_OUTCOMES)
            c["silent"] += int(terminal == S.SILENT_DROP)
        return out

    def metrics(self) -> dict[str, Any]:
        total = Counter()
        for c in self.per_doc().values():
            total.update(c)
        n = total["nonfiltered"]

        def share(key: str) -> float | None:
            return total[key] / n if n else None
        return {
            "sentences": len(self.sentences), "nonfiltered": n,
            "read_coverage": share("read"),
            "micro_read_coverage": share("micro_read") if self.has_micro else None,
            "yield": share("committed"), "loss_rate": share("lost"),
            "silent_drops": sum(1 for t, _ in self.terminals.values() if t == S.SILENT_DROP),
        }


# ------------------------------------------------------------------ compute
def _short(ids: Iterable[str]) -> str:
    ids = sorted(set(ids))
    return ids[0] + (f" (+{len(ids) - 1})" if len(ids) > 1 else "") if ids else ""


def compute(r, run_id: str) -> CoverageResult:
    """Terminal outcome for every sentence of the run's documents (no writes)."""
    doc_ids = run_documents(r, run_id)
    by_doc = {d: doc_sentences(r, d) for d in doc_ids}
    index = SentenceIndex(by_doc)
    events: dict[str, set[str]] = defaultdict(set)
    for ev in r.select("coverage_events", run_id=run_id):
        events[ev["sentence_id"]].add(ev["outcome"])
    result = CoverageResult(run_id=run_id, doc_ids=doc_ids, sentences=dict(index.by_id),
                            events=dict(events),
                            has_micro=any(S.MICRO_READ in e for e in events.values()))

    spans = {s["span_id"]: s for s in _spans_for_docs(r, doc_ids)}
    edge_spans: dict[str, list[str]] = defaultdict(list)
    for row in r.select("edge_spans"):
        edge_spans[row["edge_id"]].append(row["span_id"])

    cites: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def cite(sids: Iterable[str], klass: str, obj_id: str) -> None:
        for sid in sids:
            if sid in result.sentences:
                cites[sid][klass].add(obj_id)

    def sids_of_spans(span_ids: Iterable[str]) -> list[str]:
        out: list[str] = []
        for span_id in span_ids:
            out.extend(index.span_sentence_ids(spans.get(span_id)))
        return out

    macro = r.select("macro_edges", run_id=run_id)
    macro_class = {m["edge_id"]: fact_class(m["status"]) for m in macro}
    for m in macro:
        klass = macro_class[m["edge_id"]]
        if klass == PENDING_CLASS:
            result.unfinished_facts.append(m["edge_id"])
        cite(sids_of_spans(edge_spans.get(m["edge_id"], [])), klass, m["edge_id"])
    for table, pk in (("propositions", "prop_id"), ("prop_links", "link_id")):
        for row in r.select(table, run_id=run_id):
            klass = fact_class(row["state"])
            if klass == PENDING_CLASS:
                result.unfinished_facts.append(row[pk])
            cite(sids_of_spans([row["span_id"]] if row.get("span_id") else []), klass, row[pk])

    lineage: dict[str, list[str]] = defaultdict(list)
    for row in r.select("edge_lineage"):
        lineage[row["micro_edge_id"]].append(row["macro_edge_id"])
    for e in r.select("micro_edges", run_id=run_id):
        if lineage.get(e["edge_id"]):
            continue                                   # its fate is the macro edge's
        sids = sids_of_spans(edge_spans.get(e["edge_id"], []))
        if e["status"] == S.REJECTED:
            cite(sids, REJECTED_CLASS, e["edge_id"])
        elif e["status"] == S.CONTRACTED:
            read = [sid for sid in sids if S.T2_READ in events.get(sid, ())]
            cite(read, REJECTED_CLASS, e["edge_id"])  # T2 read it and kept nothing: removed
            if len(read) < len(sids):
                result.unfinished_facts.append(e["edge_id"])
        else:
            result.unfinished_facts.append(e["edge_id"])

    for esc in r.select("escalations", run_id=run_id):
        if esc["outcome"] != S.HELD_FOR_HUMAN or esc["item_type"] not in ("edge", "conflict", "logic"):
            continue
        for item in esc["item_ids"] or []:
            cite(sids_of_spans(edge_spans.get(item, [])), HELD_CLASS, esc["esc_id"])

    for sid, row in result.sentences.items():
        ev = events.get(sid, set())
        c = cites.get(sid, {})
        if c.get(COMMITTED_CLASS):
            result.terminals[sid] = (S.FACT_COMMITTED, _short(c[COMMITTED_CLASS]))
        elif c.get(HELD_CLASS) or S.HELD_FOR_HUMAN in ev:
            result.terminals[sid] = (S.HELD_FOR_HUMAN, _short(c.get(HELD_CLASS, ())) or "coverage event")
        elif c.get(REJECTED_CLASS):
            result.terminals[sid] = (S.FACTS_REJECTED, _short(c[REJECTED_CLASS]))
        elif S.UNEXPRESSIBLE in ev:
            result.terminals[sid] = (S.UNEXPRESSIBLE, "marked unexpressible by T2")
        elif ev & set(READ_EVENTS):
            result.terminals[sid] = (S.READ_NO_FACT, "+".join(sorted(ev & set(READ_EVENTS))))
        elif row.get("filtered") or S.SKIPPED_FILTER in ev:
            result.terminals[sid] = (S.SKIPPED_FILTER, "filtered")
        elif S.SKIPPED_BUDGET in ev:
            result.terminals[sid] = (S.SKIPPED_BUDGET, "budget")
        elif S.COVERAGE_FAILED in ev:
            result.terminals[sid] = (S.COVERAGE_FAILED, "unit failed")
        else:
            seen = ",".join(sorted(ev)) or "no events"
            result.terminals[sid] = (S.SILENT_DROP, f"events: {seen}")

    result.open_mentions = sorted(m["mention_id"] for m in r.select("mentions", run_id=run_id)
                                  if m["status"] == S.OPEN)
    result.unfinished_facts = sorted(set(result.unfinished_facts))
    return result


def _spans_for_docs(r, doc_ids: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc_id in doc_ids:
        out.extend(r.select("spans", doc_id=doc_id))
    return out


# ------------------------------------------------------------------ finalize
def finalize(ctx) -> dict[str, Any]:
    """Write ``coverage_final`` for the run in ``ctx`` and return the summary."""
    with ctx.reader() as r:
        result = compute(r, ctx.run_id)
    written = updated = 0
    with ctx.tx(S.FINALIZE, "U12") as tx:
        existing = {row["sentence_id"]: row for row in tx.select("coverage_final", run_id=ctx.run_id)}
        for sid, (terminal, reason) in sorted(result.terminals.items()):
            old = existing.get(sid)
            if old is None:
                tx.insert("coverage_final", dict(run_id=ctx.run_id, sentence_id=sid,
                                                 terminal=terminal, reason=reason[:500]))
                written += 1
            elif (old["terminal"], old["reason"]) != (terminal, reason[:500]):
                tx.update("coverage_final", {"run_id": ctx.run_id, "sentence_id": sid},
                          {"terminal": terminal, "reason": reason[:500]})
                updated += 1
    silent = sorted(sid for sid, (t, _) in result.terminals.items() if t == S.SILENT_DROP)
    return {
        "run_id": ctx.run_id, "documents": len(result.doc_ids), "sentences": len(result.terminals),
        "written": written, "updated": updated, "terminal": result.counts(),
        "silent_drops": silent, "open_mentions": result.open_mentions,
        "unfinished_facts": result.unfinished_facts, "metrics": result.metrics(),
    }
