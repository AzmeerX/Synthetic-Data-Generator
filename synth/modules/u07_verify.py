"""U7 — T2 Graph Verifier (existing Datanaut 14B, extended; ``impl: existing``).

Purpose: one T2 section at a time, check the contracted micro edges (U6) against the
source and repair them, and add the logic structure (propositions + links) the micro
tier cannot see. Output is the verified macro graph (S5).

Contract: :func:`run_section` ``(ctx, section_id) -> dict``. Two modes (``config.t2.mode``):

* ``diff`` (default) — prompt ``u7_verify.v1`` -> :class:`synth.contracts.U7Output`: ops
  ADD / REMOVE / CORRECT / SPLIT_ENTITY / MERGE_ENTITY, propositions, links,
  ``insufficient_context``, ``unexpressible_sentence_ids``. Ops are grounded with the locator
  against the sentences sent (ungrounded ops are dropped and logged). One recheck pass when
  ops were applied (stop on empty ops or on an op reversing an earlier one; reversing ops are
  not applied). ``insufficient_context`` -> add one neighbouring paragraph and ask again,
  at most ``caps.u7_widen_asks``; still insufficient -> flag ``t2_insufficient_context``.
* ``full`` — the **existing** prompt via :mod:`synth.modules.existing_t2` with the micro rows as
  a hint; the adapter computes the diff (ADD / REMOVE / CORRECT). No recheck (the existing
  prompt has no diff semantics).

Writes (one transaction per section): macro edges ``VERIFIED`` (``section_id``, ``tier_last="T2"``)
with ``edge_spans`` / ``edge_times``; every sent CONTRACTED micro edge that was not removed ->
``edge_lineage(op="CONFIRMED")``; CORRECT -> corrected macro edge + ``CORRECTED_FROM``; ADD ->
macro edge without lineage; REMOVE -> no macro edge, change-log row ``REMOVED_BY_T2`` on the
micro edge (status stays ``CONTRACTED``); propositions and ``prop_links`` (``state="VERIFIED"``; U11 commits them);
flags ``t2_split_proposal`` / ``t2_merge_proposal`` / ``nested_disagreement`` /
``t2_insufficient_context`` (POTENTIAL); coverage ``T2_READ`` (every sentence sent),
``UNEXPRESSIBLE``, ``SKIPPED_BUDGET`` (shrunk out), ``FAILED``, ``HELD_FOR_HUMAN`` (sovereign
document, non-local ``t2``); stage metrics (stage ``T2``). L2 rows come from the gateway.

Token caps: content ≤ ``caps.u7_in`` (3,000): section (≤ 1,500, cap 2,000) + rows
(≤ ``caps.u7_rows`` 1,000) + who's-who (≤ ``caps.u4_whoswho`` 150) + time refs
(≤ ``caps.u4_time_section`` 100); out ≤ ``caps.u7_out``. Shrink order on overflow:
who's-who -> rows of other sentences (rows citing no sentence sent) -> source sentences outside
the cited ones (cited by a row, or labelled FACT/NESTED by U5); still over -> ``SKIPPED_BUDGET``.
``OutputTruncated`` -> the section is split in two and each half verified.
Spec: P1 §3 U7, §5, §7, §10, §11, §16 risk 3.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from synth import states as S
from synth.contracts import EdgeBody, Evidence, U7Output
from synth.gateway import BudgetExceeded, GatewayError, OutputTruncated
from synth.ids import new_id
from synth.modules.common import (
    DocView, StageContext, Stopwatch, emit_coverage, raise_flag, record_stage_metrics, write_span,
)
from synth.modules.existing_t2 import (
    Dropped, EdgeDraft, EntityResolver, LinkDraft, NewEntity, PropDraft, ResolvedOp, TimeIndex,
    build_existing_user, call_existing, diff_against_rows, ontology_frame, parse_existing,
    sentence_span, similar_text,
)
from synth.modules.u04_whoswho import build_index
from synth.modules.u06_contract import Labeler, format_row, identity_entities, load_row_edges, render_compact_rows
from synth.prompts import load_prompt
from synth.provenance import Located, SentenceRef
from synth.store.repo import Tx
from synth.store.tables import TABLES

MODULE = "U7"
PROMPT_NAME = "u7_verify.v1"
FLAG_SPLIT, FLAG_MERGE = "t2_split_proposal", "t2_merge_proposal"
FLAG_NESTED, FLAG_INSUFFICIENT = "nested_disagreement", "t2_insufficient_context"
_ROW = re.compile(r"M\d+")
_ENT = re.compile(r"E\d+")
_MICRO_COLUMNS = list(TABLES["micro_edges"].c.keys())


# ------------------------------------------------------------------ section data
def verify_endpoint(ctx: StageContext) -> str:
    """The model that verifies: ``t2`` (the ``full`` arm) or ``t3`` when a run sets
    ``extra["verify_endpoint"]`` (the ``minus0`` arm, where the frontier reviews the local graph)."""
    return (ctx.extra or {}).get("verify_endpoint", "t2")


def section_chunks(r: Tx, sec: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in r.select("micro_chunks", order_by="idx", doc_id=sec["doc_id"])
            if sec["first_chunk_idx"] <= c["idx"] <= sec["last_chunk_idx"]]


def section_sentences(r: Tx, doc: DocView, sec: dict[str, Any]) -> list[dict[str, Any]]:
    """The section's non-filtered sentences (filtered ones end SKIPPED_FILTER at U1)."""
    chunks = section_chunks(r, sec)
    if not chunks:
        return []
    return doc.range(min(c["first_sentence_idx"] for c in chunks), max(c["last_sentence_idx"] for c in chunks))


def micro_drafts(r: Tx, run_id: str, sec: dict[str, Any], sentences: Sequence[dict[str, Any]]) -> list[EdgeDraft]:
    """The section's CONTRACTED micro edges as drafts (an edge belongs to the section of its chunk)."""
    chunk_ids = {c["chunk_id"] for c in section_chunks(r, sec)}
    ids_in = {s["sentence_id"] for s in sentences}
    rows = [e for e in r.select("micro_edges", run_id=run_id, status=S.CONTRACTED)
            if e["chunk_id"] in chunk_ids or e["chunk_id"] is None]
    drafts = []
    for e in load_row_edges(r, run_id, [x["edge_id"] for x in rows]):
        if e["chunk_id"] is None and not (e["sentence_ids"] and e["sentence_ids"][0] in ids_in):
            continue
        drafts.append(EdgeDraft(src=e["src_entity_id"], rel=e["rel"], dst=e["dst_entity_id"],
                                subclass=e["subclass"], desc=e["description"] or "",
                                stance=e["stance"] or "stated", negated=bool(e["negated"]),
                                conf=e["confidence"], times=list(e["times"]), span_ids=list(e["span_ids"]),
                                sentence_ids=list(e["sentence_ids"]), micro_id=e["edge_id"],
                                micro_row={k: e[k] for k in _MICRO_COLUMNS}, origin="CONFIRMED",
                                first_offset=e["first_offset"]))
    return drafts


@dataclass
class Scope:
    ctx: StageContext
    doc: DocView
    section_id: str
    sentences: list[dict[str, Any]]
    fact_ids: set[str]
    entity_class: dict[str, str | None]
    identity: set[str]
    sovereign: bool

    def count(self, text: str) -> int:
        return self.ctx.gateway.count(text, verify_endpoint(self.ctx)) if text else 0


@dataclass
class Unit:
    base: list[dict[str, Any]]
    sentences: list[dict[str, Any]]
    labeler: Labeler
    whoswho: str
    time_text: str
    shown: list[EdgeDraft]
    dropped_rows: list[EdgeDraft]
    user: str
    tokens: int
    rows_tokens: int
    source_tokens: int
    steps: list[str]

    @property
    def refs(self) -> list[SentenceRef]:
        return [SentenceRef(s["sentence_id"], s["idx"], s["char_start"], s["char_end"]) for s in self.sentences]

    @property
    def row_map(self) -> dict[str, EdgeDraft]:
        return {d.row_id: d for d in self.shown if d.row_id}


@dataclass
class Outcome:
    drafts: list[EdgeDraft] = field(default_factory=list)
    props: list[PropDraft] = field(default_factory=list)
    links: list[LinkDraft] = field(default_factory=list)
    proposals: list[tuple[str, list[str], Located, str, str | None]] = field(default_factory=list)
    disagreement: list[Any] = field(default_factory=list)
    read: list[dict[str, Any]] = field(default_factory=list)
    unexpressible: list[str] = field(default_factory=list)
    skipped_budget: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    not_verified: list[tuple[EdgeDraft, str]] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    insufficient: bool = False
    context_request: str | None = None
    calls: int = 0
    rechecks: int = 0
    widen_asks: int = 0
    applied: Counter = field(default_factory=Counter)
    source_tokens: int = 0
    rows_tokens: int = 0
    fallbacks: int = 0
    shrink_steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "Outcome") -> "Outcome":
        for name in ("drafts", "props", "links", "proposals", "disagreement", "read", "unexpressible",
                     "skipped_budget", "failed", "held", "not_verified", "dropped", "shrink_steps", "errors"):
            getattr(self, name).extend(getattr(other, name))
        for name in ("calls", "rechecks", "widen_asks", "source_tokens", "rows_tokens", "fallbacks"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.applied.update(other.applied)
        self.insufficient = self.insufficient or other.insufficient
        self.context_request = self.context_request or other.context_request
        return self

    def unverified(self, drafts: Sequence[EdgeDraft], reason: str) -> None:
        self.not_verified += [(d, reason) for d in drafts if d.micro_id]


# ------------------------------------------------------------------ prompt composition
def _ontology_lists(onto) -> list[str]:
    return ["RELATIONS: " + ", ".join(onto.relation_names),
            "PROPOSITION STATUS: " + ", ".join(onto.proposition_status),
            "LINK TYPES: " + ", ".join(onto.link_types),
            "TIME ROLES: " + ", ".join(onto.time_roles or S.TIME_ROLES)]


def _diff_user(scope: Scope, recheck: bool) -> Callable[..., str]:
    def render(sentences, whoswho: str, time_text: str, rows_text: str) -> str:
        parts = [f"DOCUMENT: {scope.doc.doc.get('filename') or scope.doc.doc_id}", "",
                 "SECTION (one sentence per line; cite sentence ids, quote verbatim)",
                 scope.doc.render(sentences)]
        if whoswho:
            parts += ["", "WHO'S-WHO (entity labels)", whoswho]
        if time_text:
            parts += ["", "TIME REFERENCES (time labels)", time_text]
        parts += ["", "DRAFT ROWS (source relation target | stance | time | sentences | confidence)",
                  rows_text or "(none)", ""] + _ontology_lists(scope.ctx.ontology) + [""]
        parts.append("RECHECK: the rows above already include your earlier changes. Return only further "
                     "changes (usually none), and the propositions and links again in full." if recheck else
                     "The draft rows came from a weaker model. Check each row against the source. Return only "
                     "changes, then the propositions and logic links for sentences with nested reasoning.")
        return "\n".join(parts)
    return render


def _full_user(scope: Scope) -> Callable[..., str]:
    frame = ontology_frame(scope.ctx.ontology)

    def render(sentences, whoswho: str, time_text: str, rows_text: str) -> str:
        hint = ["DRAFT GRAPH FROM A WEAKER MODEL (a hint only: it may be wrong or incomplete. Report the "
                "complete graph the text states, naming parties as the text names them.)"]
        if whoswho:
            hint += ["ENTITIES", whoswho]
        if time_text:
            hint += ["TIMES", time_text]
        hint += ["ROWS (source relation target | stance | time | sentences | confidence)", rows_text or "(none)"]
        return build_existing_user(scope.doc, sentences, frame, "\n".join(hint))
    return render


def _rows_text(scope: Scope, r: Tx, shown: Sequence[EdgeDraft], labeler: Labeler, first_pass: bool) -> str:
    if first_pass and all(d.micro_id and d.origin == "CONFIRMED" and not d.located for d in shown):
        text, mapping = render_compact_rows(r, scope.ctx.run_id, [d.micro_id for d in shown], labeler)
        by_micro = {d.micro_id: d for d in shown}
        for row_id, micro_id in mapping.items():
            by_micro[micro_id].row_id = row_id
        return text
    return "\n".join(format_row(d.src, d.rel, d.dst, negated=d.negated, stance=d.stance, times=d.times,
                                sentence_ids=d.all_sentence_ids(), confidence=d.conf, labeler=labeler,
                                row_id=d.row_id or "M?", identity=scope.identity) for d in shown)


def _compose(scope: Scope, r: Tx, base: Sequence[dict[str, Any]], drafts: Sequence[EdgeDraft], *,
             render: Callable[..., str], first_pass: bool,
             fixed: tuple[Labeler, str, str] | None = None) -> Unit | None:
    """Build the prompt for ``base`` sentences + ``drafts``, shrinking in the declared order."""
    cfg = scope.ctx.config
    cap_in, cap_rows = cfg.cap("u7_in"), cfg.cap("u7_rows")
    if fixed is None:
        ww = build_index(r, scope.doc, base, scope.ctx.run_id, cap=cfg.cap("u4_whoswho"),
                         time_cap=cfg.cap("u4_time_section"), count=scope.count)
        labeler, whoswho, time_text = Labeler.from_whoswho(ww, scope.doc), ww.text, ww.time_text
    else:
        labeler, whoswho, time_text = fixed
    sentences = sorted(base, key=lambda s: s["idx"])
    shown = sorted([d for d in drafts if not d.removed], key=lambda d: (d.offset(), d.micro_id or "~"))
    dropped_rows: list[EdgeDraft] = []
    steps: list[str] = []
    for step in range(4):
        if not sentences:
            return None
        rows_text = _rows_text(scope, r, shown, labeler, first_pass)
        user = render(sentences, whoswho, time_text, rows_text)
        tokens, rows_tokens = scope.count(user), scope.count(rows_text)
        if tokens <= cap_in and rows_tokens <= cap_rows:
            return Unit(base=list(base), sentences=sentences, labeler=labeler, whoswho=whoswho,
                        time_text=time_text, shown=shown, dropped_rows=dropped_rows, user=user,
                        tokens=tokens, rows_tokens=rows_tokens,
                        source_tokens=scope.count(scope.doc.render(sentences)), steps=steps)
        if step == 0:
            whoswho, steps = "", steps + ["whoswho"]
        elif step == 1:
            ids = {s["sentence_id"] for s in sentences}
            keep = [d for d in shown if set(d.all_sentence_ids()) & ids]
            kept = {id(d) for d in keep}
            dropped_rows += [d for d in shown if id(d) not in kept]
            shown, steps = keep, steps + ["rows_other_sentences"]
        elif step == 2:
            cited = {sid for d in shown for sid in d.all_sentence_ids()} | scope.fact_ids
            sentences, steps = [s for s in sentences if s["sentence_id"] in cited], steps + ["uncited_sentences"]
    return None


def _widen(doc: DocView, current: Sequence[dict[str, Any]], request: str | None, asks: int) -> list[dict[str, Any]] | None:
    """``current`` + one neighbouring paragraph (sentences up to the next line break)."""
    pool = [s for s in doc.sentences if not s["filtered"]]
    have = {s["sentence_id"] for s in current}
    first, last = min(s["idx"] for s in current), max(s["idx"] for s in current)
    before = sorted([s for s in pool if s["idx"] < first], key=lambda s: -s["idx"])
    after = sorted([s for s in pool if s["idx"] > last], key=lambda s: s["idx"])

    def paragraph(side: list[dict[str, Any]], backwards: bool) -> list[dict[str, Any]]:
        para: list[dict[str, Any]] = []
        for s in side:
            if s["sentence_id"] in have:
                continue
            if para:
                a, b = (s, para[-1]) if backwards else (para[-1], s)
                if "\n" in doc.text[a["char_end"]:b["char_start"]]:
                    break
            para.append(s)
        return para

    prefer_after = bool(re.search(r"next|after|follow|later|below", request or "", re.I)) or asks % 2 == 1
    for side, backwards in ([(after, False), (before, True)] if prefer_after else [(before, True), (after, False)]):
        para = paragraph(side, backwards)
        if para:
            return sorted(list(current) + para, key=lambda s: s["idx"])
    return None


# ------------------------------------------------------------------ ops
def _evidence(doc: DocView, refs: Sequence[SentenceRef], ev: Evidence, require_quote: bool):
    quote = (ev.quote or "").strip()
    if quote:
        loc = doc.locator.locate(quote, ev.sentence_ids, unit=refs)
        return (loc, None) if loc.grounded else (None, "ungrounded")
    if require_quote:
        return None, "ungrounded"
    in_unit = {r.sentence_id for r in refs}
    cited = [ref for ref in doc.locator.resolve(ev.sentence_ids) if ref.sentence_id in in_unit]
    return (sentence_span(doc, cited), None) if cited else (None, None)


def edge_from_body(onto, labeler: Labeler, entity_class: Callable[[str], str | None],
                   body: EdgeBody, resolve_entity: Callable[[str], str | None] | None = None):
    src = (resolve_entity or labeler.entity_id)(body.src)
    dst = (resolve_entity or labeler.entity_id)(body.dst)
    if not src or not dst:
        return None, "unknown_entity"
    rel = onto.canonical_relation(body.rel)
    if rel is None:
        return None, "unknown_relation"
    if src == dst:
        return None, "self_loop"
    if not onto.relation(rel).allows(entity_class(src), entity_class(dst)):
        return None, "type_violation"
    times = []
    for t in body.time:
        tid = labeler.time_id(t.ref)
        if t.role in S.TIME_ROLES and tid and (t.role, tid) not in times:
            times.append((t.role, tid))
    return EdgeDraft(src=src, rel=rel, dst=dst, subclass=body.subclass, desc=body.desc, stance=body.stance,
                     negated=body.negated, times=times), None


def _resolve_op(scope: Scope, unit: Unit, vop) -> tuple[ResolvedOp | None, str | None]:
    raw = vop.model_dump()
    loc, why = _evidence(scope.doc, unit.refs, vop.evidence, require_quote=vop.op == "ADD")
    if why:
        return None, why
    row = None
    if vop.op in ("REMOVE", "CORRECT"):
        m = _ROW.search(vop.target or "")
        row = unit.row_map.get(m.group(0)) if m else None
        if row is None or row.removed:
            return None, "unknown_row"
        if vop.op == "REMOVE":
            return ResolvedOp("REMOVE", row_id=row.row_id, located=loc, reason=vop.reason, raw=raw), None
    if vop.op in ("ADD", "CORRECT"):
        if vop.edge is None:
            return None, "missing_edge"
        edge, why = edge_from_body(scope.ctx.ontology, unit.labeler, scope.entity_class.get, vop.edge)
        if edge is None:
            return None, why
        edge.conf = vop.conf
        edge.located = [loc] if loc else []
        return ResolvedOp(vop.op, row_id=row.row_id if row else None, edge=edge, located=loc,
                          conf=vop.conf, reason=vop.reason, raw=raw), None
    labels = _ENT.findall(vop.target or "") + ([vop.edge.src, vop.edge.dst] if vop.edge else [])
    entities = list(dict.fromkeys(e for e in (unit.labeler.entity_id(lk) for lk in labels) if e))
    need = 1 if vop.op == "SPLIT_ENTITY" else 2
    if len(entities) < need:
        return None, "unknown_entity"
    if loc is None:
        return None, "ungrounded"
    return ResolvedOp(vop.op, entities=entities[:need], located=loc, reason=vop.reason, raw=raw), None


def _next_row(drafts: Sequence[EdgeDraft]) -> str:
    nums = [int(d.row_id[1:]) for d in drafts if d.row_id and d.row_id[1:].isdigit()]
    return f"M{max(nums, default=0) + 1}"


def _merge_located(target: EdgeDraft, located: Sequence[Located]) -> None:
    for loc in located:
        if all((loc.char_start, loc.char_end) != (x.char_start, x.char_end) for x in target.located):
            target.located.append(loc)


def _apply(unit: Unit, out: Outcome, op: ResolvedOp, call_id: str | None) -> tuple:
    """Apply one resolved op to the drafts. Returns a history entry ``(op, draft, before_key, entities)``."""
    if op.op == "ADD":
        dup = next((d for d in out.drafts if not d.removed and d.key() == op.edge.key()), None)
        if dup is not None:
            _merge_located(dup, op.edge.located)
            out.applied["ADD_EXISTING"] += 1
            return ("ADD_EXISTING", dup, dup.key(), [])
        edge = op.edge
        edge.origin, edge.row_id, edge.call_id = "ADDED", _next_row(out.drafts), call_id
        out.drafts.append(edge)
        out.applied["ADD"] += 1
        return ("ADD", edge, None, [])
    if op.op in ("REMOVE", "CORRECT"):
        draft = unit.row_map[op.row_id]
        before = draft.key()
        if op.op == "REMOVE":
            draft.removed, draft.remove_reason, draft.call_id = True, op.reason, call_id
        else:
            e = op.edge
            draft.src, draft.rel, draft.dst, draft.subclass = e.src, e.rel, e.dst, e.subclass
            draft.desc, draft.stance, draft.negated, draft.conf = e.desc, e.stance, e.negated, op.conf
            if e.times:
                draft.times = list(e.times)
            if op.located is not None:
                draft.located, draft.span_ids, draft.sentence_ids = [op.located], [], []
            draft.fallback = e.fallback
            if draft.micro_id:
                draft.origin = "CORRECTED"
            draft.call_id = call_id
        out.applied[op.op] += 1
        return (op.op, draft, before, [])
    kind = FLAG_SPLIT if op.op == "SPLIT_ENTITY" else FLAG_MERGE
    out.proposals.append((kind, op.entities, op.located, op.reason, call_id))
    out.applied[op.op] += 1
    return (op.op, None, None, list(op.entities))


def _reverses(op: ResolvedOp, history: Sequence[tuple], unit: Unit) -> bool:
    target = unit.row_map.get(op.row_id) if op.row_id else None
    for name, draft, before, entities in history:
        if op.op == "REMOVE" and name == "ADD" and target is draft:
            return True
        if op.op == "ADD" and name == "REMOVE" and before == op.edge.key():
            return True
        if op.op == "CORRECT" and name == "CORRECT" and target is draft and op.edge.key() == before:
            return True
        if {op.op, name} == {"SPLIT_ENTITY", "MERGE_ENTITY"} and set(op.entities) & set(entities):
            return True
    return False


def _apply_all(scope: Scope, unit: Unit, ops, out: Outcome, call_id: str | None,
               history: Sequence[tuple] | None) -> tuple[list[tuple], bool]:
    entries, reversal = [], False
    for vop in ops:
        op, why = _resolve_op(scope, unit, vop)
        if op is None:
            out.dropped.append(Dropped("t2_op", vop.model_dump(), why or "invalid", call_id))
            continue
        if history is not None and _reverses(op, history, unit):
            out.dropped.append(Dropped("t2_op", op.raw, "recheck_reversal", call_id))
            reversal = True
            continue
        entries.append(_apply(unit, out, op, call_id))
    return entries, reversal


# ------------------------------------------------------------------ propositions and links
def parse_logic(doc: DocView, refs: Sequence[SentenceRef], onto, propositions, links,
                speaker_of: Callable[[str | None], str | None],
                dropped: list[Dropped]) -> tuple[list[PropDraft], list[LinkDraft]]:
    """Ground propositions (quote required) and links (quote, else cited sentences, else the
    source proposition's span). Shared by U7 and the arm extractors."""
    by_pid: dict[str, PropDraft] = {}
    props: list[PropDraft] = []
    for p in propositions:
        quote = (p.evidence.quote or "").strip()
        loc = doc.locator.locate(quote, p.evidence.sentence_ids, unit=refs) if quote else None
        if loc is None or not loc.grounded or not p.text.strip():
            dropped.append(Dropped("proposition", p.model_dump(), "ungrounded"))
            continue
        status = p.status if p.status in onto.proposition_status else "stated"
        draft = PropDraft(text=" ".join(p.text.split()), status=status, located=loc,
                          speaker=speaker_of(p.speaker), negated=p.negated)
        by_pid[p.pid] = draft
        props.append(draft)
    out: list[LinkDraft] = []
    types = set(onto.link_types)
    in_unit = {r.sentence_id for r in refs}
    for link in links:
        ltype = re.sub(r"[^a-z0-9]+", "_", (link.type or "").strip().lower()).strip("_")
        src, dst = by_pid.get(link.src), by_pid.get(link.dst)
        if ltype not in types:
            dropped.append(Dropped("prop_link", link.model_dump(), "unknown_link_type"))
            continue
        if src is None or dst is None or src is dst:
            dropped.append(Dropped("prop_link", link.model_dump(), "unknown_proposition"))
            continue
        quote = (link.evidence.quote or "").strip()
        if quote:
            loc = doc.locator.locate(quote, link.evidence.sentence_ids, unit=refs)
            if not loc.grounded:
                dropped.append(Dropped("prop_link", link.model_dump(), "ungrounded"))
                continue
        else:
            cited = [x for x in doc.locator.resolve(link.evidence.sentence_ids) if x.sentence_id in in_unit]
            loc = sentence_span(doc, cited) if cited else src.located
        out.append(LinkDraft(src=src, dst=dst, type=ltype, located=loc))
    return props, out


def _same_prop(a: PropDraft, b: PropDraft) -> bool:
    if a.norm() == b.norm():
        return True
    return set(a.located.sentence_ids) == set(b.located.sentence_ids) and similar_text(a.text, b.text) >= 85


def merge_logic(out_props: list[PropDraft], out_links: list[LinkDraft], props: Sequence[PropDraft],
                links: Sequence[LinkDraft]) -> tuple[set, set]:
    """Union one pass's logic into the accumulated lists; returns that pass's (link keys, prop ids)."""
    unify: dict[int, PropDraft] = {}
    for p in props:
        same = next((q for q in out_props if _same_prop(q, p)), None)
        if same is None:
            out_props.append(p)
            same = p
        unify[id(p)] = same
    keys = set()
    for link in links:
        link.src, link.dst = unify.get(id(link.src), link.src), unify.get(id(link.dst), link.dst)
        key = link.key()
        keys.add(key)
        if all(x.key() != key for x in out_links):
            out_links.append(link)
    return keys, {id(unify[id(p)]) for p in props}


# ------------------------------------------------------------------ one unit, diff mode
def _split(scope: Scope, sentences: Sequence[dict[str, Any]], drafts: Sequence[EdgeDraft],
           fn: Callable[[Scope, list, list], Outcome]) -> Outcome:
    ordered = sorted(sentences, key=lambda s: s["idx"])
    if len(ordered) <= 1:
        out = Outcome(drafts=list(drafts), failed=list(ordered), errors=["output truncated on one sentence"])
        out.unverified(drafts, "failed")
        return out
    first = ordered[: len(ordered) // 2]
    last_idx = first[-1]["idx"]
    idx_of = {s["sentence_id"]: s["idx"] for s in scope.doc.sentences}

    def in_first(d: EdgeDraft) -> bool:
        idxs = [idx_of[s] for s in d.all_sentence_ids() if s in idx_of]
        return not idxs or min(idxs) <= last_idx
    d1 = [d for d in drafts if in_first(d)]
    d2 = [d for d in drafts if not in_first(d)]
    return fn(scope, first, d1).merge(fn(scope, ordered[len(first):], d2))


def _call_verify(scope: Scope, unit: Unit, out: Outcome) -> tuple[U7Output, str | None]:
    cfg = scope.ctx.config
    res = scope.ctx.gateway.call(endpoint=verify_endpoint(scope.ctx), prompt=load_prompt(PROMPT_NAME), user=unit.user,
                                 cap_in=cfg.cap("u7_in"), cap_out=cfg.cap("u7_out"), stage=S.T2,
                                 module=MODULE, unit_id=scope.section_id, run_id=scope.ctx.run_id,
                                 output_model=U7Output, sovereign=scope.sovereign)
    out.calls += 1
    return res.model, (res.call_ids[-1] if res.call_ids else None)


def _finish_unit(out: Outcome, base: Sequence[dict[str, Any]], read: dict[str, dict[str, Any]],
                 unit: Unit) -> Outcome:
    out.read += list(read.values())
    out.skipped_budget += [s for s in base if s["sentence_id"] not in read]
    out.unverified(unit.dropped_rows, "not_sent_budget")
    out.shrink_steps += unit.steps
    out.source_tokens += unit.source_tokens
    out.rows_tokens += unit.rows_tokens
    return out


def _diff_unit(scope: Scope, sentences: list[dict[str, Any]], drafts: list[EdgeDraft]) -> Outcome:
    cfg = scope.ctx.config
    out = Outcome(drafts=drafts)
    with scope.ctx.reader() as r:
        unit = _compose(scope, r, sentences, drafts, render=_diff_user(scope, False), first_pass=True)
    if unit is None:
        out.skipped_budget += sentences
        out.unverified(drafts, "skipped_budget")
        return out
    try:
        reply, call_id = _call_verify(scope, unit, out)
    except OutputTruncated:
        return _split(scope, sentences, drafts, _diff_unit)
    except BudgetExceeded:
        out.skipped_budget += sentences
        out.unverified(drafts, "skipped_budget")
        return out
    except GatewayError as exc:
        out.failed += unit.sentences
        out.skipped_budget += [s for s in sentences if s not in unit.sentences]
        out.unverified(drafts, "failed")
        out.errors.append(f"{type(exc).__name__}: {exc}")
        return out
    read = {s["sentence_id"]: s for s in unit.sentences}

    asks = 0
    while reply.insufficient_context and asks < cfg.cap("u7_widen_asks"):
        wider = _widen(scope.doc, unit.base, reply.context_request, asks)
        if wider is None:
            break
        with scope.ctx.reader() as r:
            wunit = _compose(scope, r, wider, drafts, render=_diff_user(scope, False), first_pass=True)
        if wunit is None:
            break
        asks += 1
        try:
            wreply, wcall = _call_verify(scope, wunit, out)
        except GatewayError as exc:
            out.errors.append(f"widen: {type(exc).__name__}: {exc}")
            break
        unit, reply, call_id = wunit, wreply, wcall
        read.update({s["sentence_id"]: s for s in unit.sentences})
    out.widen_asks += asks
    out.insufficient = bool(reply.insufficient_context)
    out.context_request = reply.context_request

    history, _ = _apply_all(scope, unit, reply.ops, out, call_id, history=None)
    speaker = unit.labeler.entity_id
    mark = len(out.dropped)
    props1, links1 = parse_logic(scope.doc, unit.refs, scope.ctx.ontology, reply.propositions, reply.links,
                                 speaker, out.dropped)
    _stamp(out.dropped[mark:], call_id)
    keys1, pids1 = merge_logic(out.props, out.links, props1, links1)
    unexpressible = list(reply.unexpressible_sentence_ids)

    not_sent = {id(d) for d in unit.dropped_rows}
    if history:
        for _ in range(cfg.cap("u7_rechecks")):
            live = [d for d in out.drafts if id(d) not in not_sent]
            with scope.ctx.reader() as r:
                runit = _compose(scope, r, unit.sentences, live, render=_diff_user(scope, True),
                                 first_pass=False, fixed=(unit.labeler, unit.whoswho, unit.time_text))
            if runit is None:
                break
            try:
                rreply, rcall = _call_verify(scope, runit, out)
            except GatewayError as exc:
                out.errors.append(f"recheck: {type(exc).__name__}: {exc}")
                break
            out.rechecks += 1
            entries, reversal = _apply_all(scope, runit, rreply.ops, out, rcall, history=history)
            history += entries
            unexpressible += rreply.unexpressible_sentence_ids
            if rreply.propositions or rreply.links:
                mark = len(out.dropped)
                props2, links2 = parse_logic(scope.doc, runit.refs, scope.ctx.ontology, rreply.propositions,
                                             rreply.links, speaker, out.dropped)
                _stamp(out.dropped[mark:], rcall)
                keys2, pids2 = merge_logic(out.props, out.links, props2, links2)
                changed = keys1 ^ keys2
                if changed:
                    out.disagreement += [lk for lk in out.links if lk.key() in changed]
                    out.disagreement += [p for p in out.props if id(p) in (pids1 ^ pids2)]
            if not rreply.ops or reversal:
                break

    in_unit = {s["sentence_id"] for s in unit.sentences}
    for label in unexpressible:
        sid = unit.labeler.sentence_id(label)
        if sid in in_unit and sid not in out.unexpressible:
            out.unexpressible.append(sid)
    return _finish_unit(out, sentences, read, unit)


# ------------------------------------------------------------------ one unit, full mode
def _full_unit(scope: Scope, sentences: list[dict[str, Any]], drafts: list[EdgeDraft],
               resolver: EntityResolver) -> Outcome:
    cfg = scope.ctx.config
    out = Outcome(drafts=drafts)
    with scope.ctx.reader() as r:
        unit = _compose(scope, r, sentences, drafts, render=_full_user(scope), first_pass=True)
    if unit is None:
        out.skipped_budget += sentences
        out.unverified(drafts, "skipped_budget")
        return out
    try:
        res = call_existing(scope.ctx, scope.doc, unit.user, stage=S.T2, module=MODULE, unit_id=scope.section_id,
                            cap_in=cfg.cap("u7_in"), cap_out=cfg.cap("u7_out"))
    except OutputTruncated:
        return _split(scope, sentences, drafts, lambda sc, s, d: _full_unit(sc, s, d, resolver))
    except BudgetExceeded:
        out.skipped_budget += sentences
        out.unverified(drafts, "skipped_budget")
        return out
    except GatewayError as exc:
        out.failed += unit.sentences
        out.unverified(drafts, "failed")
        out.errors.append(f"{type(exc).__name__}: {exc}")
        return out
    out.calls += 1
    call_id = res.call_ids[-1] if res.call_ids else None
    with scope.ctx.reader() as r:
        times = TimeIndex(r, scope.ctx.run_id, scope.doc, unit.sentences)
    graph = parse_existing(res.data or {}, scope.doc, unit.sentences, resolver, scope.ctx.ontology, times)
    _stamp(graph.dropped, call_id)
    out.dropped += graph.dropped
    out.fallbacks += graph.fallbacks
    for e in graph.edges:
        e.call_id = call_id
    for op in diff_against_rows(graph.edges, unit.shown):
        _apply(unit, out, op, call_id)
    for d in unit.shown:
        if not d.removed and d.call_id is None:
            d.call_id = call_id
    merge_logic(out.props, out.links, graph.props, [])
    return _finish_unit(out, sentences, {s["sentence_id"]: s for s in unit.sentences}, unit)


# ------------------------------------------------------------------ writers (shared with the arms)
def write_new_entities(tx: Tx, doc: DocView, new: Sequence[NewEntity], created_by: str) -> int:
    for n in new:
        tx.insert("entities", dict(entity_id=n.entity_id, doc_id=doc.doc_id, canonical_name=n.name, type=n.type,
                                   subtype=n.subtype, norm_key=n.norm_key, status=S.ACTIVE, source="arm"))
        for loc in n.mentions:
            span_id = write_span(tx, doc, loc, created_by)
            mention_id = new_id("men")
            surface = doc.text[loc.char_start:loc.char_end]
            tx.insert("mentions", dict(mention_id=mention_id, doc_id=doc.doc_id, span_id=span_id, surface=surface,
                                       label=n.subtype or n.type or "entity", score=None, source="arm",
                                       entity_id=n.entity_id, status="RESOLVED"))
            tx.insert("entity_aliases", dict(entity_id=n.entity_id, alias=surface, mention_id=mention_id))
    return len(new)


def write_macro_edge(tx: Tx, doc: DocView, d: EdgeDraft, section_id: str, tier_last: str,
                     created_by: str) -> str:
    edge_id = new_id("mad")
    tx.insert("macro_edges", dict(edge_id=edge_id, section_id=section_id, src_entity_id=d.src, rel=d.rel,
                                  subclass=d.subclass, dst_entity_id=d.dst, description=d.desc,
                                  stance=d.stance or "stated", negated=bool(d.negated), confidence=d.conf,
                                  status=S.VERIFIED, tier_last=tier_last), call_id=d.call_id)
    span_ids = list(d.span_ids)
    seen: set[tuple] = set()
    for loc in d.located:
        if (loc.char_start, loc.char_end) not in seen:
            seen.add((loc.char_start, loc.char_end))
            span_ids.append(write_span(tx, doc, loc, created_by))
    for span_id in dict.fromkeys(span_ids):
        tx.insert("edge_spans", dict(edge_id=edge_id, span_id=span_id))
    for role, time_id in dict.fromkeys(d.times):
        tx.insert("edge_times", dict(edge_id=edge_id, time_id=time_id, role=role))
    if d.micro_id:
        tx.insert("edge_lineage", dict(macro_edge_id=edge_id, micro_edge_id=d.micro_id,
                                       op="CONFIRMED" if d.origin == "CONFIRMED" else "CORRECTED_FROM"))
    if d.fallback:
        tx.log_change("macro_edge", edge_id, "GROUNDING_FALLBACK", None,
                      {"grounding": "FUZZY", "reason": "quote not located; matched names in the section"},
                      call_id=d.call_id, rule_id="desc_fallback")
    return edge_id


def write_logic(tx: Tx, doc: DocView, section_id: str, props: Sequence[PropDraft], links: Sequence[LinkDraft],
                created_by: str) -> dict[str, str]:
    """Propositions and links with their spans. Returns object id -> span id."""
    span_of: dict[str, str] = {}
    for p in props:
        span_id = write_span(tx, doc, p.located, created_by)
        tx.insert("propositions", dict(prop_id=p.prop_id, section_id=section_id, text=p.text,
                                       speaker_entity_id=p.speaker, status=p.status, negated=bool(p.negated),
                                       confidence=None, state=S.VERIFIED, span_id=span_id))
        span_of[p.prop_id] = span_id
        if p.fallback:
            tx.log_change("proposition", p.prop_id, "GROUNDING_FALLBACK", None, {"grounding": "FUZZY"},
                          rule_id="desc_fallback")
    for link in links:
        span_id = write_span(tx, doc, link.located, created_by)
        tx.insert("prop_links", dict(link_id=link.link_id, src_prop_id=link.src.prop_id,
                                     dst_prop_id=link.dst.prop_id, type=link.type, span_id=span_id, state=S.VERIFIED))
        span_of[link.link_id] = span_id
    return span_of


def _stamp(dropped: Sequence[Dropped], call_id: str | None) -> None:
    """Tie dropped model output to the call that produced it (the console's gearbox shows it per call)."""
    for d in dropped:
        d.call_id = d.call_id or call_id


def log_dropped(tx: Tx, dropped: Sequence[Dropped]) -> None:
    for d in dropped:
        tx.log_change(d.object_type, "-", "DROPPED", None, d.raw, rule_id=d.rule_id,
                      call_id=getattr(d, "call_id", None))


def _ids(sentences: Sequence[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(s["sentence_id"] for s in sentences))


# ------------------------------------------------------------------ U7
def run_section(ctx: StageContext, section_id: str) -> dict[str, Any]:
    """Verify one T2 section (``diff`` or ``full`` mode, ``config.t2.mode``)."""
    sw = Stopwatch()
    with ctx.reader() as r:
        sec = r.get("sections", section_id=section_id)
        if sec is None:
            raise KeyError(f"no section {section_id}")
        if r.select("stage_metrics", run_id=ctx.run_id, stage=S.T2, unit_id=section_id):
            return {"section_id": section_id, "skipped": True}
    doc = ctx.doc(sec["doc_id"])
    mode = (ctx.config.t2 or {}).get("mode", "diff")
    with ctx.reader() as r:
        sentences = section_sentences(r, doc, sec)
        drafts = micro_drafts(r, ctx.run_id, sec, sentences)
        fact_ids = {c["sentence_id"] for c in r.select("coverage_events", run_id=ctx.run_id)
                    if c["outcome"] in (S.FACT_SENTENCE, S.NESTED_SENTENCE)}
        ents = r.select("entities", run_id=ctx.run_id, doc_id=doc.doc_id)
        entity_class = {e["entity_id"]: ctx.ontology.class_of(e["type"]) or ctx.ontology.class_of(e["subtype"])
                        for e in ents}
        identity = identity_entities(r, ctx.run_id)
        resolver = EntityResolver(r, ctx.ontology, ctx.run_id, doc, sentences) if mode == "full" else None
    scope = Scope(ctx=ctx, doc=doc, section_id=section_id, sentences=sentences, fact_ids=fact_ids,
                  entity_class=entity_class, identity=identity, sovereign=bool(doc.doc.get("sovereign")))

    if not sentences:
        out = Outcome(drafts=drafts)
        out.unverified(drafts, "empty_section")
    elif scope.sovereign and not ctx.config.endpoint(verify_endpoint(ctx)).is_local():
        out = Outcome(drafts=drafts, held=list(sentences))
        out.unverified(drafts, "held_sovereign")
    elif mode == "full":
        out = _full_unit(scope, sentences, drafts, resolver)
    else:
        out = _diff_unit(scope, sentences, drafts)
    return _write(scope, out, resolver.new if resolver else [], mode, len(drafts), sw)


def _write(scope: Scope, out: Outcome, new_entities: Sequence[NewEntity], mode: str, items_in: int,
           sw: Stopwatch) -> dict[str, Any]:
    ctx, doc = scope.ctx, scope.doc
    unverified = {id(d) for d, _ in out.not_verified}
    macro_ids: list[str] = []
    counts: Counter = Counter()
    flag_ids: list[str] = []
    with ctx.tx(S.T2, MODULE) as tx:
        write_new_entities(tx, doc, new_entities, MODULE)
        for d in out.drafts:
            if d.removed:
                if d.micro_id:
                    tx.log_change("micro_edge", d.micro_id, "REMOVED_BY_T2", d.micro_row,
                                  {**(d.micro_row or {}), "removed_by": verify_endpoint(scope.ctx).upper(),
                                   "reason": d.remove_reason},
                                  call_id=d.call_id, rule_id="u7_remove")
                    counts["removed"] += 1
                continue
            if id(d) in unverified:
                continue
            macro_ids.append(write_macro_edge(tx, doc, d, scope.section_id, verify_endpoint(scope.ctx).upper(),
                                              MODULE))
            counts[d.origin.lower()] += 1
        for d, reason in out.not_verified:
            tx.log_change("micro_edge", d.micro_id, "NOT_VERIFIED", d.micro_row, d.micro_row,
                          rule_id=f"u7_{reason}")
        span_of = write_logic(tx, doc, scope.section_id, out.props, out.links, MODULE)
        for kind, entities, loc, reason, call_id in out.proposals:
            span_id = write_span(tx, doc, loc, MODULE)
            flag_ids.append(raise_flag(tx, kind, entities, MODULE, span_ids=[span_id], note=reason,
                                       rule_id=f"u7_{kind}"))
        if out.disagreement:
            objs = list(dict.fromkeys(getattr(o, "link_id", None) or o.prop_id for o in out.disagreement))
            flag_ids.append(raise_flag(tx, FLAG_NESTED, objs, MODULE, span_ids=[span_of[o] for o in objs],
                                       note="links changed between the first pass and the recheck",
                                       rule_id="u7_nested_disagreement"))
        if out.insufficient:
            region = out.read or scope.sentences
            loc = doc.locator.ground_span(min(s["char_start"] for s in region), max(s["char_end"] for s in region))
            span_id = write_span(tx, doc, loc, MODULE)
            flag_ids.append(raise_flag(tx, FLAG_INSUFFICIENT, [scope.section_id] + macro_ids, MODULE,
                                       span_ids=[span_id], note=out.context_request or "",
                                       rule_id="u7_insufficient_context"))
        read_ids = _ids(out.read)
        emit_coverage(tx, read_ids, S.T2, S.T2_READ, ref_id=scope.section_id)
        emit_coverage(tx, out.unexpressible, S.T2, S.UNEXPRESSIBLE, ref_id=scope.section_id)
        emit_coverage(tx, [i for i in _ids(out.skipped_budget) if i not in read_ids], S.T2, S.SKIPPED_BUDGET,
                      ref_id=scope.section_id)
        emit_coverage(tx, [i for i in _ids(out.failed) if i not in read_ids], S.T2, S.COVERAGE_FAILED,
                      ref_id=scope.section_id)
        emit_coverage(tx, _ids(out.held), S.T2, S.HELD_FOR_HUMAN, ref_id=scope.section_id)
        log_dropped(tx, out.dropped)
        record_stage_metrics(tx, S.T2, scope.section_id, source_tokens=out.source_tokens,
                             candidate_tokens=out.rows_tokens, items_in=items_in, items_out=len(macro_ids),
                             wall_ms=sw.ms)
    return {"section_id": scope.section_id, "mode": mode, "calls": out.calls, "rechecks": out.rechecks,
            "widen_asks": out.widen_asks, "macro_edges": len(macro_ids), "confirmed": counts["confirmed"],
            "corrected": counts["corrected"], "added": counts["added"], "removed": counts["removed"],
            "ops_applied": dict(out.applied), "ops_dropped": sum(1 for d in out.dropped if d.object_type == "t2_op"),
            "propositions": len(out.props), "links": len(out.links), "flags": len(flag_ids),
            "new_entities": len(new_entities), "sentences_read": len(read_ids),
            "skipped_budget": len(out.skipped_budget), "failed": len(out.failed), "held": len(out.held),
            "shrink": out.shrink_steps, "fallbacks": out.fallbacks, "errors": out.errors}
