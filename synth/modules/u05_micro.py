"""U5 — Micro LLM Pass (Ministral 3 3B on cut B micro chunks).

Purpose: read one micro chunk against the who's-who index (U4) and build the
chunk's micro graph — edges, entity fixes, missed entities and fact / nested
sentence labels — then ground every item with the locator and keep what holds.

Contract: ``run_chunk(ctx, chunk_id) -> dict`` (a summary of counts; ``error``
is set when a part failed). One ``t1_micro`` call per chunk, more only when the
chunk is split. Run a document's chunks in order: missed entities are written to
S2 in the chunk's own transaction, so the next chunk's index already shows them.

Relation modes (E1; ``ctx.extra["relation_mode"]``, else ``tier1.relation_mode``):

- ``micro`` (default): the 3B model proposes the relations.
- ``gliner``: relations are U2's GLiNER candidates alone. U5 makes **no model
  call** (no entity fixes, no missed entities, no ``MICRO_READ``): this arm
  measures GLiNER alone, at zero micro-LLM tokens. Only a ``stage_metrics`` row
  is written (candidate tokens = the GLiNER edges as compact rows).
- ``gliner+micro``: the chunk's GLiNER candidates are shown as a ``GLINER DRAFT``
  block (``G1 E3 director_of E7 | s12``). After the call, for every draft shown:
  a grounded model edge with the same ``(src, rel, dst, negated)`` *confirms* it —
  the draft stays ``CANDIDATE`` (no duplicate micro edge), gains the model's
  evidence span and time roles, and takes the model's confidence, stance and
  description (``Tx.update``, rule ``u5_draft_confirmed``); a draft not repeated
  becomes ``REJECTED`` with ``reject_reason`` ``micro_corrected`` (the model gave
  another edge on the same entity pair) or ``micro_not_confirmed`` (rule
  ``u5_draft_review``). Drafts the model never saw (an end outside the index,
  or a failed call) are left untouched.

After the call (spec §5): edge ends map through the index labels (or the exact
surface of a missed entity of the same reply); ``rel`` is canonicalized;
``T`` labels map to ``edge_times`` (role must be an ontology time role); every
edge, fix and missed entity is grounded with ``doc.locator.locate(quote, cited,
unit=<the sentences sent>)``. Unknown labels / relations / types and ungrounded
items are dropped and logged (``DROPPED`` in L4 with the raw item). A missed
entity reuses an ACTIVE entity of the same document with the same ``norm_key``
and a compatible type (mention attached); if several such entities exist the
mention is stored ``DISCARDED`` (``ambiguous_same_name``) — same name ≠ same
entity. Type fixes are ``Tx.update`` on ``entities``; roles go to
``entity_roles`` with their evidence span.

Token caps (spec §10): user content ≤ ``caps.u5_in`` (650): chunk ≤
``chunking.micro.cap_tokens`` (500) + who's-who ≤ ``caps.u4_whoswho`` (150) +
time refs ≤ ``caps.u4_time_chunk`` (60) + relation names + draft. Over budget →
rebuild the who's-who with a smaller cap, then the time refs, then split the
chunk's sentences in two halves; one sentence still over → ``SKIPPED_BUDGET``.
Reply ≤ ``caps.u5_out`` (600); ``OutputTruncated`` → split the same way (one
sentence → ``SKIPPED_BUDGET``). ``SchemaInvalid`` / any other ``GatewayError`` →
``FAILED`` for that part's sentences.

Stores written: S4 ``micro_edges`` / ``edge_spans`` / ``edge_times``; S1
``spans``; S2 ``entities`` / ``mentions`` / ``entity_aliases`` /
``entity_roles``; L2 (via the gateway); L3 ``coverage_events`` (``MICRO_READ``,
``FACT_SENTENCE``, ``NESTED_SENTENCE``, ``SKIPPED_BUDGET``, ``FAILED``, all with
``ref_id = chunk_id``); L4 change log; ``stage_metrics`` (one row per part:
source tokens, candidate tokens = the reply JSON, items in = proposed edges,
items out = stored or confirmed edges).

Idempotent: sentences that already have a ``MICRO_READ`` / ``SKIPPED_BUDGET``
event for this chunk and run are not sent again (written in the same
transaction as the part's edges, so a split chunk resumes where it stopped);
a chunk with ``source="micro"`` edges and no such events is also left alone.
Spec: P1 §3 U5, §1 E1, §5, §7, §10.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import or_, select

from synth.contracts import EntityFix, Evidence, MicroEdgeOut, MissedEntity, U5Output
from synth.gateway import BudgetExceeded, GatewayError, GatewayResult, OutputTruncated, strict_schema
from synth.ids import new_id
from synth.modules.common import (
    DocView, StageContext, Stopwatch, emit_coverage, record_stage_metrics, write_span,
)
from synth.modules.u04_whoswho import WhosWho, build_index
from synth.ontology import Ontology
from synth.prompts import load_prompt
from synth.provenance import SENTENCE_MISMATCH, Located
from synth.provenance.locator import normalize, normalize_with_map
from synth.states import (
    ACTIVE, CANDIDATE, COVERAGE_FAILED, DISCARDED, FACT_SENTENCE, MICRO, MICRO_READ,
    NESTED_SENTENCE, REJECTED, RESOLVED, SKIPPED_BUDGET,
)
from synth.store.repo import Tx
from synth.store.tables import mentions as mentions_t
from synth.store.tables import micro_edges as micro_edges_t
from synth.store.tables import spans as spans_t

MODULE = "U5"
ENDPOINT = "t1_micro"
PROMPT_NAME = "u5_micro.v1"
MODES = ("micro", "gliner", "gliner+micro")
#: Each shrink step lowers a cap by at least this many tokens (so the loop ends).
SHRINK_STEP = 10
#: Coverage outcomes that mean "this sentence is done for this chunk" (FAILED is retried).
_DONE_OUTCOMES = (MICRO_READ, SKIPPED_BUDGET)


# ------------------------------------------------------------------ small helpers
def relation_mode(ctx: StageContext) -> str:
    mode = ctx.extra.get("relation_mode") or ctx.config.model_settings.tier1.relation_mode
    if mode not in MODES:
        raise ValueError(f"unknown relation_mode {mode!r}; expected one of {MODES}")
    return mode


@lru_cache(maxsize=8)
def _schema(relations: tuple[str, ...], time_roles: tuple[str, ...]) -> dict[str, Any]:
    schema = strict_schema(U5Output)
    edge = schema["properties"]["edges"]["items"]["properties"]
    edge["rel"] = {"type": "string", "enum": list(relations)}
    if time_roles:
        edge["time"]["items"]["properties"]["role"] = {"type": "string", "enum": list(time_roles)}
    return schema


def output_schema(ontology: Ontology) -> dict[str, Any]:
    """``strict_schema(U5Output)`` with ``rel`` (and the time role) closed to the ontology."""
    return copy.deepcopy(_schema(tuple(ontology.relation_names), tuple(ontology.time_roles)))


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def type_of(ontology: Ontology, name: str | None) -> tuple[str | None, str | None]:
    """``"bank"`` -> ``("organization", "bank")``; ``"organization"`` -> ``("organization", None)``."""
    cls = ontology.class_of(name)
    if cls is None:
        return None, None
    key = _snake(name or "")
    return cls, key if key in ontology.entity_types.get(cls, []) else None


def _compatible(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


def _label(raw: str, letter: str) -> str:
    text = (raw or "").strip().strip("[]()").strip()
    if ":" in text:
        text = text.split(":")[-1]
    if len(text) > 1 and text[0].lower() == letter.lower() and text[1:].isdigit():
        return letter + text[1:]
    return text


def _find_in(text: str, needle: str, start: int, end: int) -> tuple[int, int] | None:
    """Exact, then normalized (case, quotes, dashes, spaces) match of ``needle`` in ``text[start:end]``."""
    window = text[start:end]
    pos = window.find(needle)
    if pos >= 0:
        return start + pos, start + pos + len(needle)
    norm_window, index = normalize_with_map(window)
    norm_needle = normalize(needle)
    if not norm_needle:
        return None
    pos = norm_window.find(norm_needle)
    if pos < 0:
        return None
    return start + index[pos], start + index[pos + len(norm_needle) - 1] + 1


# ------------------------------------------------------------------ prompt assembly
def render_user(chunk_text: str, index: WhosWho, relations: Sequence[str],
                draft: Sequence[str] = ()) -> str:
    blocks = [f"CHUNK\n{chunk_text}",
              f"ENTITIES\n{index.text or '(none)'}",
              f"TIMES\n{index.time_text or '(none)'}",
              "RELATIONS\n" + ", ".join(relations)]
    if draft:
        blocks.append("GLINER DRAFT\n" + "\n".join(draft))
    return "\n\n".join(blocks)


def draft_lines(ontology: Ontology, doc: DocView, drafts: Iterable[dict[str, Any]], index: WhosWho,
                part_ids: set[str] | None = None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Compact draft rows ``G1 E3 director_of E7 | s12`` for the drafts whose ends
    are in the index (and whose anchor sentence is in the part)."""
    label_of = index.label_of_entity
    shown: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    for d in drafts:
        anchor = d.get("anchor")
        if part_ids is not None and anchor is not None and anchor not in part_ids:
            continue
        src, dst = label_of.get(d["src_entity_id"]), label_of.get(d["dst_entity_id"])
        if not src or not dst:
            continue
        gid = f"G{len(shown) + 1}"
        rel = ontology.canonical_relation(d["rel"]) or d["rel"]
        neg = " NOT" if d.get("negated") else ""
        where = f" | s{doc.by_id[anchor]['idx']}" if anchor in doc.by_id else ""
        lines.append(f"{gid} {src}{neg} {rel} {dst}{where}")
        shown[gid] = d
    return shown, lines


@dataclass
class Fitted:
    """A user message that fits ``caps.u5_in``, and what it showed the model."""

    user: str
    index: WhosWho
    drafts: dict[str, dict[str, Any]]
    tokens: int
    source_tokens: int
    shrink: list[str] = field(default_factory=list)


def fit_user(ctx: StageContext, r: Tx, doc: DocView, sentences: Sequence[dict[str, Any]],
             drafts: Sequence[dict[str, Any]] = (),
             count: Callable[[str], int] | None = None) -> Fitted | None:
    """Build the user message for ``sentences``; shrink who's-who, then time refs,
    until it fits. ``None`` when the sentences alone do not fit (caller splits)."""
    count = count or (lambda text: ctx.gateway.count(text, ENDPOINT))
    chunk_text = doc.render(sentences)
    source_tokens = count(chunk_text)
    if source_tokens > ctx.config.chunking.micro.cap_tokens:
        return None
    cap_in = ctx.config.cap("u5_in")
    who_cap, time_cap = ctx.config.cap("u4_whoswho"), ctx.config.cap("u4_time_chunk")
    part_ids = {s["sentence_id"] for s in sentences}
    shrink: list[str] = []
    while True:
        index = build_index(r, doc, sentences, ctx.run_id, cap=who_cap, time_cap=time_cap, count=count)
        shown, lines = draft_lines(ctx.ontology, doc, drafts, index, part_ids)
        user = render_user(chunk_text, index, ctx.ontology.relation_names, lines)
        tokens = count(user)
        if tokens <= cap_in:
            return Fitted(user, index, shown, tokens, source_tokens, shrink)
        over = tokens - cap_in
        if who_cap > 0:
            who_cap = max(0, min(who_cap - SHRINK_STEP, index.tokens - over))
            shrink.append(f"whoswho<={who_cap}")
        elif time_cap > 0:
            time_cap = max(0, min(time_cap - SHRINK_STEP, index.time_tokens - over))
            shrink.append(f"time<={time_cap}")
        else:
            return None


# ------------------------------------------------------------------ the unit
@dataclass
class _Run:
    ctx: StageContext
    doc: DocView
    chunk_id: str
    mode: str
    drafts: list[dict[str, Any]]
    count: Callable[[str], int]
    summary: dict[str, Any]
    reviewed: set[str] = field(default_factory=set)


def _new_summary(chunk_id: str, doc_id: str | None, mode: str) -> dict[str, Any]:
    return dict(chunk_id=chunk_id, doc_id=doc_id, relation_mode=mode, status="DONE", skipped=None,
                calls=0, parts=0, splits=0, shrink=[], edges=0, dropped=0,
                missed_added=0, missed_attached=0, missed_ambiguous=0, type_fixes=0, roles=0,
                drafts_shown=0, drafts_confirmed=0, drafts_rejected=0, gliner_edges=0,
                fact_sentences=0, nested_sentences=0, sentences_read=0,
                skipped_budget=0, failed=0, error=None)


def run_chunk(ctx: StageContext, chunk_id: str) -> dict[str, Any]:
    """Run U5 on one micro chunk. Returns a summary of counts."""
    mode = relation_mode(ctx)
    with ctx.reader() as r:
        chunk = r.get("micro_chunks", chunk_id=chunk_id)
    if chunk is None:
        raise KeyError(f"no micro chunk {chunk_id}")
    doc = ctx.doc(chunk["doc_id"])
    summary = _new_summary(chunk_id, doc.doc_id, mode)
    sentences = doc.range(chunk["first_sentence_idx"], chunk["last_sentence_idx"])
    if not sentences:
        summary["skipped"] = "no_sentences"
        return summary
    with ctx.reader() as r:
        drafts = _load_drafts(r, ctx.run_id, doc, chunk) if mode != "micro" else []
        done = {e["sentence_id"] for e in r.select("coverage_events", run_id=ctx.run_id, stage=MICRO,
                                                   ref_id=chunk_id)
                if e["outcome"] in _DONE_OUTCOMES}
        has_micro_edges = bool(r.select("micro_edges", run_id=ctx.run_id, chunk_id=chunk_id,
                                        source="micro"))
    run = _Run(ctx, doc, chunk_id, mode, drafts,
               lambda text: ctx.gateway.count(text, ENDPOINT), summary)
    summary["gliner_edges"] = len(drafts)
    if mode == "gliner":
        _gliner_only(run, sentences)
        return summary
    todo = [s for s in sentences if s["sentence_id"] not in done]
    if not todo or (has_micro_edges and not done):
        summary["skipped"] = "already_done"
        return summary
    _process(run, todo)
    if summary["failed"]:
        summary["status"] = "PARTIAL" if summary["sentences_read"] or summary["skipped_budget"] else "FAILED"
    return summary


def _load_drafts(r: Tx, run_id: str, doc: DocView, chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """GLiNER candidate edges of this chunk: ``chunk_id`` set to it, or unset with
    evidence inside it. Each gets ``anchor`` = its first sentence in the chunk."""
    t = micro_edges_t
    rows = r.rows(select(t).where(t.c.run_id == run_id, t.c.source == "gliner",
                                  t.c.status == CANDIDATE,
                                  or_(t.c.chunk_id == chunk["chunk_id"], t.c.chunk_id.is_(None)))
                  .order_by(t.c.edge_id))
    in_chunk = {s["sentence_id"] for s in doc.range(chunk["first_sentence_idx"], chunk["last_sentence_idx"],
                                                     include_filtered=True)}
    out = []
    for edge in rows:
        sids: list[str] = []
        for link in r.select("edge_spans", edge_id=edge["edge_id"]):
            span = r.get("spans", span_id=link["span_id"])
            if span and span["doc_id"] == doc.doc_id:
                sids.extend(sid for sid in (span["sentence_ids"] or []) if sid in in_chunk)
        if edge["chunk_id"] != chunk["chunk_id"] and not sids:
            continue
        anchor = min(sids, key=lambda sid: doc.by_id[sid]["idx"]) if sids else None
        out.append({**edge, "anchor": anchor})
    return out


def _gliner_only(run: _Run, sentences: list[dict[str, Any]]) -> None:
    ctx = run.ctx
    watch = Stopwatch()
    with ctx.reader() as r:
        if r.select("stage_metrics", run_id=ctx.run_id, stage=MICRO, unit_id=run.chunk_id):
            run.summary["skipped"] = "already_done"
            return
        index = build_index(r, run.doc, sentences, ctx.run_id, cap=10 ** 9, time_cap=0, count=run.count)
    _, lines = draft_lines(ctx.ontology, run.doc, run.drafts, index, None)
    with ctx.tx(MICRO, MODULE) as tx:
        record_stage_metrics(tx, MICRO, run.chunk_id,
                             source_tokens=run.count(run.doc.render(sentences)),
                             candidate_tokens=run.count("\n".join(lines)) if lines else 0,
                             items_in=len(run.drafts), items_out=len(run.drafts), wall_ms=watch.ms)


def _process(run: _Run, sentences: list[dict[str, Any]]) -> None:
    ctx = run.ctx
    drafts = [d for d in run.drafts if d["edge_id"] not in run.reviewed]
    with ctx.reader() as r:
        fitted = fit_user(ctx, r, run.doc, sentences, drafts, run.count)
    if fitted is None:
        _split_or_skip(run, sentences)
        return
    watch = Stopwatch()
    try:
        result = ctx.gateway.call(
            endpoint=ENDPOINT, prompt=load_prompt(PROMPT_NAME), user=fitted.user,
            output_model=U5Output, schema=output_schema(ctx.ontology),
            cap_in=ctx.config.cap("u5_in"), cap_out=ctx.config.cap("u5_out"),
            stage=MICRO, module=MODULE, unit_id=run.chunk_id, run_id=ctx.run_id,
            sovereign=bool(run.doc.doc.get("sovereign")))
    except (BudgetExceeded, OutputTruncated):
        _split_or_skip(run, sentences)
        return
    except GatewayError as exc:
        _fail(run, sentences, exc)
        return
    run.summary["calls"] += 1
    run.summary["shrink"].extend(fitted.shrink)
    _write(run, sentences, fitted, result, watch.ms)


def _split_or_skip(run: _Run, sentences: list[dict[str, Any]]) -> None:
    if len(sentences) > 1:
        mid = len(sentences) // 2
        run.summary["splits"] += 1
        _process(run, sentences[:mid])
        _process(run, sentences[mid:])
        return
    with run.ctx.tx(MICRO, MODULE) as tx:
        emit_coverage(tx, [s["sentence_id"] for s in sentences], MICRO, SKIPPED_BUDGET, ref_id=run.chunk_id)
    run.summary["skipped_budget"] += len(sentences)


def _fail(run: _Run, sentences: list[dict[str, Any]], exc: Exception) -> None:
    with run.ctx.tx(MICRO, MODULE) as tx:
        emit_coverage(tx, [s["sentence_id"] for s in sentences], MICRO, COVERAGE_FAILED, ref_id=run.chunk_id)
    run.summary["failed"] += len(sentences)
    run.summary["error"] = run.summary["error"] or f"{type(exc).__name__}: {exc}"


def _write(run: _Run, sentences: list[dict[str, Any]], fitted: Fitted, result: GatewayResult,
           wall_ms: int) -> None:
    ctx = run.ctx
    out: U5Output = result.model  # type: ignore[assignment]
    call_id = result.call_ids[-1] if result.call_ids else None
    with ctx.tx(MICRO, MODULE) as tx:
        w = _Writer(tx, ctx, run.doc, run.chunk_id, sentences, fitted, call_id, run.summary)
        for missed in out.missed_entities:
            w.missed(missed)
        for fix in out.entity_fixes:
            w.fix(fix)
        for edge in out.edges:
            w.edge(edge)
        w.close_drafts()
        w.labels(out.fact_sentences, FACT_SENTENCE, "fact_sentences")
        w.labels(out.nested_sentences, NESTED_SENTENCE, "nested_sentences")
        ids = [s["sentence_id"] for s in sentences]
        emit_coverage(tx, ids, MICRO, MICRO_READ, ref_id=run.chunk_id)
        record_stage_metrics(tx, MICRO, run.chunk_id, source_tokens=fitted.source_tokens,
                             candidate_tokens=run.count(result.text or json.dumps(result.data)),
                             items_in=len(out.edges), items_out=w.edges_out, wall_ms=wall_ms)
    run.summary["parts"] += 1
    run.summary["sentences_read"] += len(sentences)
    run.reviewed.update(d["edge_id"] for d in fitted.drafts.values())


class _Writer:
    """Grounds and stores one reply, inside the part's transaction."""

    def __init__(self, tx: Tx, ctx: StageContext, doc: DocView, chunk_id: str,
                 sentences: Sequence[dict[str, Any]], fitted: Fitted, call_id: str | None,
                 summary: dict[str, Any]) -> None:
        self.tx, self.ctx, self.doc, self.chunk_id = tx, ctx, doc, chunk_id
        self.ontology = ctx.ontology
        self.sentences = list(sentences)
        self.by_label = {f"s{s['idx']}": s for s in sentences}
        self.unit = list(self.by_label)
        self.ids = {s["sentence_id"] for s in sentences}
        self.index = fitted.index
        self.shown = fitted.drafts
        self.call_id = call_id
        self.summary = summary
        self.surfaces: dict[str, str] = {}        # norm_key(surface) -> entity id (this reply's missed entities)
        self.confirmed: set[str] = set()          # draft edge ids confirmed by this reply
        self.pairs: set[frozenset[str]] = set()   # entity pairs of the edges stored from this reply
        self.edges_out = 0

    # -- plumbing ------------------------------------------------------------
    def drop(self, object_type: str, raw: Any, rule_id: str) -> None:
        self.tx.log_change(object_type, "-", "DROPPED", None, raw, call_id=self.call_id, rule_id=rule_id)
        self.summary["dropped"] += 1

    def ground(self, evidence: Evidence) -> Located | None:
        cited_raw = [_label(x, "s") for x in evidence.sentence_ids]
        cited = [c for c in cited_raw if c in self.by_label]
        located = self.doc.locator.locate(evidence.quote, cited, unit=self.unit)
        if not located.grounded or not set(located.sentence_ids) <= self.ids:
            return None
        if cited_raw and not cited:          # every cited id was outside the part
            located.grounding = SENTENCE_MISMATCH
        return located

    def entity(self, ref: str | None) -> str | None:
        if not ref or not ref.strip():
            return None
        label = _label(ref, "E")
        if label in self.index.entity_labels:
            return self.index.entity_labels[label]
        return self.surfaces.get(self.ontology.norm_key(ref))

    # -- missed entities -------------------------------------------------------
    def missed(self, item: MissedEntity) -> None:
        raw = item.model_dump()
        cls, subtype = type_of(self.ontology, item.type)
        if cls is None:
            return self.drop("missed_entity_candidate", raw, "unknown_type")
        located = self.ground(item.evidence)
        hit = self._surface(item.surface, located) if located else None
        if hit is None:
            return self.drop("missed_entity_candidate", raw, "ungrounded")
        start, end = hit
        surface = self.doc.text[start:end]
        key = self.ontology.norm_key(surface)
        keys = {key, self.ontology.norm_key(item.surface)}
        tx, run_id = self.tx, self.ctx.run_id

        existing = self._mention_at(start, end, cls)
        if existing:
            self.surfaces.update(dict.fromkeys(keys, existing))
            self.summary["missed_attached"] += 1
            return None
        same_key = [e for e in tx.select("entities", run_id=run_id, doc_id=self.doc.doc_id,
                                         norm_key=key, status=ACTIVE)
                    if _compatible(e["type"], cls)]
        span_id = write_span(tx, self.doc, self.doc.locator.ground_span(start, end), created_by=MODULE)
        mention_id = new_id("men")
        mention = dict(mention_id=mention_id, doc_id=self.doc.doc_id, span_id=span_id, surface=surface,
                       label=item.type, score=None, source="micro")
        if len(same_key) > 1:     # same name, several entities: never guess which one
            tx.insert("mentions", {**mention, "entity_id": None, "status": DISCARDED,
                                   "discard_reason": "ambiguous_same_name"},
                      call_id=self.call_id, rule_id="u5_missed_ambiguous")
            self.summary["missed_ambiguous"] += 1
            return None
        if same_key:
            entity_id = same_key[0]["entity_id"]
            self.summary["missed_attached"] += 1
        else:
            entity_id = new_id("ent")
            tx.insert("entities", dict(entity_id=entity_id, doc_id=self.doc.doc_id, canonical_name=surface,
                                       type=cls, subtype=subtype, norm_key=key, status=ACTIVE, source="micro"),
                      call_id=self.call_id, rule_id="u5_missed_entity")
            self.summary["missed_added"] += 1
        tx.insert("mentions", {**mention, "entity_id": entity_id, "status": RESOLVED},
                  call_id=self.call_id, rule_id="u5_missed_entity")
        tx.insert("entity_aliases", dict(entity_id=entity_id, alias=surface, mention_id=mention_id))
        self.surfaces.update(dict.fromkeys(keys, entity_id))
        return None

    def _surface(self, surface: str, located: Located) -> tuple[int, int] | None:
        needle = (surface or "").strip()
        if not needle:
            return None
        regions = [(located.char_start, located.char_end)]
        regions += [(s["char_start"], s["char_end"]) for s in self.sentences]
        for start, end in regions:
            hit = _find_in(self.doc.text, needle, start, end)
            if hit:
                return hit
        return None

    def _mention_at(self, start: int, end: int, cls: str) -> str | None:
        m, s = mentions_t, spans_t
        stmt = (select(m.c.entity_id).select_from(m.join(s, s.c.span_id == m.c.span_id))
                .where(m.c.run_id == self.ctx.run_id, m.c.doc_id == self.doc.doc_id,
                       m.c.status == RESOLVED, m.c.entity_id.is_not(None),
                       s.c.char_start == start, s.c.char_end == end))
        for row in self.tx.rows(stmt):
            entity = self.tx.get("entities", entity_id=row["entity_id"])
            if entity and entity["status"] == ACTIVE and _compatible(entity["type"], cls):
                return entity["entity_id"]
        return None

    # -- entity fixes ------------------------------------------------------------
    def fix(self, item: EntityFix) -> None:
        raw = item.model_dump()
        entity_id = self.entity(item.entity)
        if entity_id is None:
            return self.drop("entity_fix_candidate", raw, "unknown_label")
        located = self.ground(item.evidence)
        if located is None:
            return self.drop("entity_fix_candidate", raw, "ungrounded")
        if item.type and item.type.strip():
            cls, subtype = type_of(self.ontology, item.type)
            if cls is None:
                self.drop("entity_fix_candidate", raw, "unknown_type")
            else:
                entity = self.tx.get("entities", entity_id=entity_id)
                if subtype is None and entity["type"] == cls:
                    subtype = entity["subtype"]
                if (entity["type"], entity["subtype"]) != (cls, subtype):
                    self.tx.update("entities", {"entity_id": entity_id}, {"type": cls, "subtype": subtype},
                                   call_id=self.call_id, rule_id="u5_type_fix")
                    self.summary["type_fixes"] += 1
        if item.role and item.role.strip():
            org_id = None
            if item.role_org and item.role_org.strip():
                org_id = self.entity(item.role_org)
                if org_id is None:
                    return self.drop("entity_role_candidate", raw, "unknown_label")
            role = " ".join(item.role.split())
            for known in self.tx.select("entity_roles", run_id=self.ctx.run_id, entity_id=entity_id):
                if (known["role"] or "").casefold() == role.casefold() and known["org_entity_id"] == org_id:
                    return None
            span_id = write_span(self.tx, self.doc, located, created_by=MODULE)
            row = dict(entity_id=entity_id, role=role, org_entity_id=org_id, span_id=span_id,
                       time_id=None, run_id=self.ctx.run_id)
            self.tx.insert("entity_roles", row)
            self.tx.log_change("entity_role", entity_id, "CREATE", None, row,
                               call_id=self.call_id, rule_id="u5_entity_fix")
            self.summary["roles"] += 1
        return None

    # -- edges ---------------------------------------------------------------------
    def edge(self, item: MicroEdgeOut) -> None:
        raw = item.model_dump()
        src, dst = self.entity(item.src), self.entity(item.dst)
        if src is None or dst is None:
            return self.drop("micro_edge_candidate", raw, "unknown_label")
        rel = self.ontology.canonical_relation(item.rel)
        if rel is None:
            return self.drop("micro_edge_candidate", raw, "unknown_relation")
        located = self.ground(item.evidence)
        if located is None:
            return self.drop("micro_edge_candidate", raw, "ungrounded")
        times: list[tuple[str, str]] = []
        for t in item.time:
            role = (t.role or "").strip()
            time_id = self.index.time_labels.get(_label(t.ref, "T"))
            if role not in self.ontology.time_roles:
                self.drop("edge_time_candidate", {"edge": raw, "time": t.model_dump()}, "unknown_time_role")
            elif time_id is None:
                self.drop("edge_time_candidate", {"edge": raw, "time": t.model_dump()}, "unknown_label")
            elif (role, time_id) not in times:
                times.append((role, time_id))
        conf = min(1.0, max(0.0, float(item.conf)))
        desc = " ".join((item.desc or "").split())
        span_id = write_span(self.tx, self.doc, located, created_by=MODULE)

        draft = self._draft_for(src, rel, dst, bool(item.negated))
        if draft is not None:
            edge_id = draft["edge_id"]
            if edge_id not in self.confirmed:
                self.tx.update("micro_edges", {"edge_id": edge_id},
                               {"confidence": conf, "stance": item.stance,
                                "description": desc or draft.get("description"),
                                "subclass": item.subclass or draft.get("subclass")},
                               call_id=self.call_id, rule_id="u5_draft_confirmed")
                self.confirmed.add(edge_id)
                self.summary["drafts_confirmed"] += 1
            known = {(t["role"], t["time_id"]) for t in self.tx.select("edge_times", edge_id=edge_id)}
            times = [t for t in times if t not in known]
        else:
            edge_id = new_id("med")
            self.tx.insert("micro_edges", dict(
                edge_id=edge_id, chunk_id=self.chunk_id, src_entity_id=src, rel=rel, subclass=item.subclass,
                dst_entity_id=dst, description=desc, stance=item.stance, negated=bool(item.negated),
                confidence=conf, status=CANDIDATE, source="micro"), call_id=self.call_id)
            self.summary["edges"] += 1
        self.tx.insert("edge_spans", dict(edge_id=edge_id, span_id=span_id))
        for role, time_id in times:
            self.tx.insert("edge_times", dict(edge_id=edge_id, time_id=time_id, role=role))
        self.pairs.add(frozenset((src, dst)))
        self.edges_out += 1
        return None

    def _draft_for(self, src: str, rel: str, dst: str, negated: bool) -> dict[str, Any] | None:
        for draft in self.shown.values():
            draft_rel = self.ontology.canonical_relation(draft["rel"]) or draft["rel"]
            if (draft["src_entity_id"], draft_rel, draft["dst_entity_id"], bool(draft.get("negated"))) \
                    == (src, rel, dst, negated):
                return draft
        return None

    def close_drafts(self) -> None:
        """Every draft shown and not confirmed is REJECTED, with the reason."""
        for draft in self.shown.values():
            if draft["edge_id"] in self.confirmed:
                continue
            pair = frozenset((draft["src_entity_id"], draft["dst_entity_id"]))
            reason = "micro_corrected" if pair in self.pairs else "micro_not_confirmed"
            self.tx.update("micro_edges", {"edge_id": draft["edge_id"]},
                           {"status": REJECTED, "reject_reason": reason},
                           call_id=self.call_id, rule_id="u5_draft_review")
            self.summary["drafts_rejected"] += 1
        self.summary["drafts_shown"] += len(self.shown)

    # -- sentence labels -------------------------------------------------------------
    def labels(self, labels: Sequence[str], outcome: str, counter: str) -> None:
        ids: list[str] = []
        for raw in labels:
            sentence = self.by_label.get(_label(raw, "s"))
            if sentence is not None and sentence["sentence_id"] not in ids:
                ids.append(sentence["sentence_id"])
        emit_coverage(self.tx, ids, MICRO, outcome, ref_id=self.chunk_id)
        self.summary[counter] += len(ids)
