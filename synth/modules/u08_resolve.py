"""U8 — Resolve & Reconcile (no model in gen-1).

Purpose: decide which entities of a run are the same real-world thing (entity
resolution across documents), re-point the verified macro graph onto the
surviving entities, then reconcile verified edges that look contradictory —
without confusing a change over time with a contradiction (P1 §6).

Contract (unit = run):
  * :func:`run` ``(ctx) -> dict`` — counts: ``pairs_considered, merged, apart,
    escalated, blocked, conflicts_final, sequences, dismissed`` (+ detail).
  * :func:`reverse_decision` ``(ctx, decision_id, decided_by) -> dict`` — writes a
    new L5 row with ``reverses=<decision_id>`` and restores entity status and
    macro-edge endpoints (all logged). MERGED -> APART; APART -> MERGED (refused,
    :class:`MergeBlocked`, when a cannot-link rule forbids it).
  * :func:`apply_decision` ``(ctx, a, b, decision, decided_by=...) -> dict`` — the
    same machinery for a later decider (U11 applying a T3 MERGE / KEEP_APART).
  * :func:`edge_interval` / :func:`ref_bounds` — S3 time refs -> day intervals.

Entity resolution
-----------------
Candidates: ``ACTIVE`` entities of the run whose class is not ``value``.
Blocking (same class required): shared surname token (persons: last name token;
other classes: main token = first non-generic token) **and** compatible
initials (persons: first initials equal or one side has no given name), **or**
name-key edit distance <= ``resolver.max_edit_distance``. Pairs named together
by an ``identity_ambiguity`` or ``t2_merge_proposal`` flag are always candidates.

Features (``resolutions.features``; each in [0, 1], weighted by
``resolver.weights``):
  * ``name_similarity`` — rapidfuzz ``max(ratio, token_sort_ratio)/100`` on the
    comparison keys (``app.consolidate.canonical.normalise_name``: titles and
    trailing legal suffixes dropped).
  * ``type_match`` — 1.0 when subtypes agree or either is unknown, else 0.0
    (the class always agrees: blocking requires it).
  * ``role_org_overlap`` — affiliations are ``entity_roles`` (role @ org entity)
    plus verified affiliation edges (``director_of``, ``chair_of``, ``employed_by``,
    ``partner_at``, ``member_of``, ``shareholder_of``, ``founded``) to an
    organisation. Orgs are compared by name across documents
    (``organisation_similarity`` >= 90). 1.0 = same org and same role title,
    0.6 = same org, different or unknown role, 0.0 = no shared org.
  * ``comention_overlap`` — overlap coefficient ``|A∩B| / min(|A|,|B|)`` of the
    name keys of neighbouring entities: other ends of macro/micro edges (not
    rejected) and entities mentioned in the same sentences. Neighbours carrying
    either pair member's own name key are ignored.
  * ``document_overlap`` — Jaccard of the documents the two entities are
    mentioned in (gen-1 entities live in one document, so this is 1.0 only for
    same-document pairs, which a cannot-link rule already keeps apart).
  * ``conflicting_roles`` — 1.0 when both hold the same role title at
    different organisations at overlapping known times (role ``time_id`` / edge
    times, else the document date). Roles commonly held in plural
    (:data:`NON_EXCLUSIVE_ROLES`: director, chair, member, shareholder, …) never
    conflict.
  * ``t2_merge_proposal`` — 1.0 when a U7 ``t2_merge_proposal`` flag lists both.
  * ``t2_split_proposal`` — 1.0 when a U7 ``t2_split_proposal`` flag lists either.

Score ``sigmoid(bias + Σ weight·feature)``; bands ``>= merge_threshold`` MERGED,
``<= apart_threshold`` APART, else ESCALATED (U9 asks T3). Outside the ``full``
arm escalation is disabled: the middle band is APART (``middle_band_no_escalation``).

Cannot-link rules override the score (pair -> APART, ``rule_id`` = first of):
``explicit_disambiguator`` (a sentence mentioning both says "no relation", "not
related", "unconnected to", …), ``identity_ambiguity`` (a POTENTIAL/FINAL flag
lists both, ids followed through ``merged_into``), ``same_document``,
``simultaneous_affiliations`` (persons with the same name key whose explicit
``entity_roles`` affiliations share no organisation, at overlapping known times).

Rival candidates: when one entity is in the merge band with two entities that are kept
apart from each other and the two scores differ by less than ``resolver.rival_margin``
(0.05), neither pair is merged by order — both are ESCALATED (``rival_candidates``; APART
outside ``full``). This is the identity trap: two same-named men, one later mention.

Merges run through union-find in descending score order. Every non-merge pair
(cannot-link, APART, ESCALATED) is a constraint: a merge that would put both
ends of a constraint into one cluster is **blocked** — recorded APART (hard rule
or APART pair) or ESCALATED (an ESCALATED pair), ``rule_id='merge_blocked'``,
and a FINAL ``merge_blocked`` flag. Survivor = most mentions (tie: smallest id).
Entity status and macro-edge endpoints are a materialisation of the effective
(not reversed) MERGED rows in L5, so reversals are exact; original endpoints are
read back from the change-log before-images. Micro edges are never re-pointed.

Reconcile
---------
Verified macro edges (not REJECTED, not negated), after re-pointing:
  * functional relations (``ontology.is_functional``) are unique on one side,
    :data:`FUNCTIONAL_DIRECTION` (``resolver.functional_direction`` overrides):
    ``chair_of`` and ``judge_of`` are functional **in the target** (one chair per
    organisation, one judge per case, at a time); any other functional relation
    is functional in the source (spec text). Same key side + different other side
    -> candidate pair.
  * attribute relations (:data:`ATTRIBUTE_RELATIONS`, default ``owes``; override
    ``resolver.attribute_relations``): same src, rel and dst, different amounts
    (parsed from the description, or from a value-class target).
A candidate pair whose validity intervals do not overlap is a sequence (no
flag). Overlap, or either edge without time -> FINAL ``conflict`` flag with
``object_ids=[edge_a, edge_b]``; both edges stay.

Edge interval: ``valid_from``/``valid_to`` give ``[from.lo, to.hi]`` (a missing
end is open); else ``occurred_at`` / ``as_of`` give that ref's ``[lo, hi]``. A
ref's bounds come from ``value_start``/``value_end`` at the coarser of their
written precision and ``granularity`` ("2024" = 2024-01-01..2024-12-31).
DURATION / RECURRING / RELATIVE / UNRESOLVED refs carry no bounds.

Flags moved by U8 (``tx.update`` on ``flags``, logged):
  * U6 ``inconsistency`` (POTENTIAL): DISMISSED when every clashing pair is
    explained (``time_sequence``, ``resolved_by_merge`` — the edges coincide
    after re-pointing — or ``functional_direction``); otherwise FINAL.
  * ``identity_ambiguity`` (POTENTIAL): FINAL when its entities are settled as
    distinct parties (always, for two or more entities: the flag is itself a
    cannot-link) — the flag stays as the answer to "why are these two apart";
    DISMISSED when its ids now resolve to one entity; left POTENTIAL ("open")
    when it names a single entity (possible conflation U8 cannot split) — U9
    raises ``IDENTITY_UNRESOLVED`` for those.

Stores written: S2 entity status/merged_into, S5 macro-edge endpoints, L5
resolutions, L6 flags, L4 change log, L2 stage metrics. Token caps: none (no
model). Spec: P1 §3 U8, §6, §11, §1 H6, CLAUDE.md invariant 7.
"""

from __future__ import annotations

import math
import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations
from typing import Any, Iterable, Sequence

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from app.consolidate.canonical import canonical_amount, name_tokens, normalise_name
from app.consolidate.matching import organisation_similarity
from synth.ids import new_id
from synth.modules.common import StageContext, Stopwatch, raise_flag, record_stage_metrics
from synth.states import (
    ACTIVE, APART, DISMISSED, ESCALATED, FINAL, FLAG_CONFLICT, FLAG_IDENTITY,
    FLAG_INCONSISTENCY, FLAG_MERGE_BLOCKED, MERGED, POTENTIAL, REJECTED, RESOLVE,
)
from synth.store.repo import Tx

MODULE = "U8"

#: Used when ``resolver.weights`` / ``resolver.bias`` are absent (mirror p1.default.yaml).
DEFAULT_BIAS = -4.0
DEFAULT_WEIGHTS = {
    "name_similarity": 4.0, "type_match": 1.5, "role_org_overlap": 2.0,
    "comention_overlap": 1.0, "document_overlap": -0.5, "conflicting_roles": -3.0,
    "t2_merge_proposal": 2.5, "t2_split_proposal": -4.0,
}
FEATURES = tuple(DEFAULT_WEIGHTS)

#: Which side of a functional relation is unique ("dst": one source per target).
FUNCTIONAL_DIRECTION = {"chair_of": "dst", "judge_of": "dst"}
ATTRIBUTE_RELATIONS = ("owes",)
AFFILIATION_RELATIONS = {
    "director_of": "director", "chair_of": "chair", "employed_by": "employee",
    "partner_at": "partner", "member_of": "member", "shareholder_of": "shareholder",
    "founded": "founder",
}
NON_EXCLUSIVE_ROLES = frozenset({
    "director", "non executive director", "independent director", "board member", "chair",
    "member", "shareholder", "trustee", "adviser", "advisor", "consultant", "investor",
    "founder", "employee",
})
_ROLE_ALIASES = {
    "ned": "non executive director", "non exec director": "non executive director",
    "nonexecutive director": "non executive director",
    "cfo": "chief financial officer", "ceo": "chief executive officer",
    "coo": "chief operating officer", "md": "managing director",
    "chairman": "chair", "chairwoman": "chair", "chairperson": "chair",
}
DISAMBIGUATION_MARKERS = (
    "no relation", "not related", "unrelated to", "no relationship with", "unconnected to",
    "unconnected with", "not the same", "distinct from", "different person",
    "not to be confused with", "share a name",
)
_GENERIC_TOKENS = {"the", "of", "and", "group", "holdings", "international", "company"}
_GENERATIONAL = {"jr", "sr", "ii", "iii", "iv"}
EXCLUDED_CLASSES = {"value"}
REPOINT_RULE, REPOINT_REVERSE_RULE = "repoint_merged", "repoint_reverse"
CANNOT_LINK_ORDER = ("explicit_disambiguator", "identity_ambiguity", "same_document",
                     "simultaneous_affiliations")


class MergeBlocked(ValueError):
    """A merge that a cannot-link rule forbids."""


# =================================================================== time
_GRAN_RANK = {"year": 0, "half": 1, "quarter": 2, "month": 3, "week": 4,
              "day": 5, "hour": 5, "minute": 5}
_UNBOUNDED_KINDS = {"DURATION", "RECURRING", "RELATIVE"}


def _parse_value(value: str | None) -> tuple[date, str] | None:
    if not value:
        return None
    v = str(value).strip()
    try:
        if m := re.match(r"^(\d{4})-Q([1-4])", v):
            return date(int(m[1]), 3 * (int(m[2]) - 1) + 1, 1), "quarter"
        if m := re.match(r"^(\d{4})-H([12])", v):
            return date(int(m[1]), 1 if m[2] == "1" else 7, 1), "half"
        if m := re.match(r"^(\d{4})-W(\d{2})", v):
            return date.fromisocalendar(int(m[1]), int(m[2]), 1), "week"
        if m := re.match(r"^(\d{4})-(\d{2})-(\d{2})", v):
            return date(int(m[1]), int(m[2]), int(m[3])), "day"
        if m := re.match(r"^(\d{4})-(\d{2})$", v):
            return date(int(m[1]), int(m[2]), 1), "month"
        if m := re.match(r"^(\d{4})$", v):
            return date(int(m[1]), 1, 1), "year"
    except ValueError:
        return None
    return None


def _unit_bounds(d: date, unit: str) -> tuple[date, date]:
    if unit == "year":
        return date(d.year, 1, 1), date(d.year, 12, 31)
    if unit == "half":
        first = 1 if d.month <= 6 else 7
        return date(d.year, first, 1), date(d.year, first + 5, monthrange(d.year, first + 5)[1])
    if unit == "quarter":
        first = 3 * ((d.month - 1) // 3) + 1
        return date(d.year, first, 1), date(d.year, first + 2, monthrange(d.year, first + 2)[1])
    if unit == "month":
        return date(d.year, d.month, 1), date(d.year, d.month, monthrange(d.year, d.month)[1])
    if unit == "week":
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
    return d, d


def _coarser(precision: str, granularity: str | None) -> str:
    if granularity in _GRAN_RANK and _GRAN_RANK[granularity] < _GRAN_RANK[precision]:
        return granularity
    return precision


def ref_bounds(ref: dict[str, Any] | None) -> tuple[date, date] | None:
    """A time ref's day interval ``[lo, hi]`` at its granularity, or None."""
    if not ref or ref.get("kind") in _UNBOUNDED_KINDS or ref.get("anchor_type") == "UNRESOLVED":
        return None
    start = _parse_value(ref.get("value_start"))
    end = _parse_value(ref.get("value_end")) or start
    if start is None:
        start = end
    if start is None or end is None:
        return None
    gran = ref.get("granularity")
    lo = _unit_bounds(start[0], _coarser(start[1], gran))[0]
    hi = _unit_bounds(end[0], _coarser(end[1], gran))[1]
    return (lo, hi) if lo <= hi else (hi, lo)


@dataclass(frozen=True)
class Interval:
    """Validity interval; None = unbounded on that side. ``known`` = any usable time."""

    lo: date | None
    hi: date | None
    known: bool

    def overlaps(self, other: "Interval") -> bool:
        a_lo, a_hi = self.lo or date.min, self.hi or date.max
        b_lo, b_hi = other.lo or date.min, other.hi or date.max
        return a_lo <= b_hi and b_lo <= a_hi


NO_TIME = Interval(None, None, False)


def edge_interval(times: Iterable[tuple[str, dict[str, Any] | None]]) -> Interval:
    """``[(role, time_ref_row), ...]`` -> the edge's validity interval."""
    by_role: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for role, ref in times:
        bounds = ref_bounds(ref)
        if bounds:
            by_role[role].append(bounds)
    vf, vt = by_role.get("valid_from"), by_role.get("valid_to")
    if vf or vt:
        return Interval(min(b[0] for b in vf) if vf else None,
                        max(b[1] for b in vt) if vt else None, True)
    for role in ("occurred_at", "as_of"):
        if by_role.get(role):
            return Interval(min(b[0] for b in by_role[role]), max(b[1] for b in by_role[role]), True)
    return NO_TIME


def _doc_day(doc: dict[str, Any] | None) -> Interval:
    parsed = _parse_value((doc or {}).get("doc_date"))
    if not parsed:
        return NO_TIME
    lo, hi = _unit_bounds(*parsed)
    return Interval(lo, hi, True)


# =================================================================== names
def _key(name: str) -> str:
    return normalise_name(name or "")


def _norm_role(text: str | None) -> str:
    t = " ".join(re.sub(r"[^\w\s]", " ", (text or "").casefold()).replace("_", " ").split())
    return _ROLE_ALIASES.get(t, t)


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return round(max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0, 4)


def _same_org(a: str, b: str) -> bool:
    return bool(a and b) and organisation_similarity(a, b) >= 90


def _different_org(a: str, b: str) -> bool:
    return bool(a and b) and organisation_similarity(a, b) < 70


# =================================================================== run data
@dataclass
class _Aff:
    org_name: str
    role: str
    when: Interval
    explicit: bool               # from entity_roles (an explicit affiliation of the name)


@dataclass
class _Ent:
    row: dict[str, Any]
    cls: str | None
    key: str
    tokens: list[str]
    mention_spans: list[str] = field(default_factory=list)
    sentence_ids: set[str] = field(default_factory=set)
    docs: set[str] = field(default_factory=set)
    neighbours: set[str] = field(default_factory=set)
    affiliations: list[_Aff] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.row["entity_id"]

    @property
    def is_person(self) -> bool:
        return self.cls == "person"

    @property
    def surname(self) -> str:
        toks = [t for t in self.tokens if t not in _GENERATIONAL] or self.tokens
        if self.is_person:
            return toks[-1] if toks else ""
        main = [t for t in toks if t not in _GENERIC_TOKENS]
        return main[0] if main else (toks[0] if toks else "")

    @property
    def initial(self) -> str | None:
        toks = [t for t in self.tokens if t not in _GENERATIONAL]
        return toks[0][0] if self.is_person and len(toks) >= 2 else None


def _select_in(r: Tx, table: str, column: str, values: Iterable[Any], **equals: Any) -> list[dict]:
    values = sorted({v for v in values if v is not None})
    t = r.table(table)
    out: list[dict] = []
    for i in range(0, len(values), 500):
        out.extend(r.select(table, where=t.c[column].in_(values[i:i + 500]), **equals))
    return out


class _Data:
    """Everything U8 reads for one run, loaded once inside the transaction."""

    def __init__(self, ctx: StageContext, r: Tx) -> None:
        self.ctx, self.r, run_id = ctx, r, ctx.run_id
        self.entity_rows = {e["entity_id"]: e for e in r.select("entities", run_id=run_id)}
        self.mentions = [m for m in r.select("mentions", run_id=run_id) if m.get("entity_id")]
        spans = {s["span_id"]: s for s in _select_in(r, "spans", "span_id",
                                                     [m["span_id"] for m in self.mentions])}
        self.time_refs = {t["time_id"]: t for t in r.select("time_refs", run_id=run_id)}
        self.macro = r.select("macro_edges", run_id=run_id, order_by="edge_id")
        self.micro = r.select("micro_edges", run_id=run_id, order_by="edge_id")
        all_edge_ids = [e["edge_id"] for e in self.macro + self.micro]
        self.edge_times: dict[str, list[tuple[str, dict | None]]] = defaultdict(list)
        for row in _select_in(r, "edge_times", "edge_id", all_edge_ids):
            self.edge_times[row["edge_id"]].append((row["role"], self.time_refs.get(row["time_id"])))
        self.edge_spans: dict[str, list[str]] = defaultdict(list)
        for row in _select_in(r, "edge_spans", "edge_id", all_edge_ids):
            self.edge_spans[row["edge_id"]].append(row["span_id"])
        sections = {s["section_id"]: s for s in _select_in(
            r, "sections", "section_id", [e.get("section_id") for e in self.macro])}
        edge_span_docs = {s["span_id"]: s["doc_id"] for s in _select_in(
            r, "spans", "span_id", [sid for e in self.macro for sid in self.edge_spans[e["edge_id"]]])}
        self.edge_doc: dict[str, str | None] = {}
        for e in self.macro:
            sec = sections.get(e.get("section_id"))
            docs = [edge_span_docs.get(s) for s in self.edge_spans[e["edge_id"]]]
            self.edge_doc[e["edge_id"]] = (sec or {}).get("doc_id") or next((d for d in docs if d), None)
        doc_ids = {e.get("doc_id") for e in self.entity_rows.values()} | {
            s["doc_id"] for s in spans.values()} | set(self.edge_doc.values())
        self.documents = {d["doc_id"]: d for d in _select_in(r, "documents", "doc_id", doc_ids)}
        self.flags = r.select("flags", run_id=run_id, order_by="flag_id")
        self.resolutions = r.select("resolutions", run_id=run_id, order_by="decision_id")
        self.roles = r.select("entity_roles", run_id=run_id)
        self._texts: dict[str, tuple[str, dict[str, dict]]] = {}
        self.new_conflicts: dict[frozenset, str] = {}

        # -- entities ---------------------------------------------------------------
        self.ents: dict[str, _Ent] = {}
        for eid, row in self.entity_rows.items():
            name = row.get("canonical_name") or ""
            key = _key(name) or (row.get("norm_key") or "")
            self.ents[eid] = _Ent(row=row, cls=ctx.ontology.class_of(row.get("type")) or row.get("type"),
                                  key=key, tokens=name_tokens(name) or key.split())
            if row.get("doc_id"):
                self.ents[eid].docs.add(row["doc_id"])
        sentence_members: dict[str, set[str]] = defaultdict(set)
        self.mention_count: dict[str, int] = defaultdict(int)
        for m in self.mentions:
            ent = self.ents.get(m["entity_id"])
            if ent is None:
                continue
            self.mention_count[ent.id] += 1
            span = spans.get(m["span_id"]) or {}
            if m.get("span_id"):
                ent.mention_spans.append(m["span_id"])
            ent.docs.add(m.get("doc_id") or span.get("doc_id"))
            for sid in span.get("sentence_ids") or []:
                ent.sentence_ids.add(sid)
                sentence_members[sid].add(ent.id)
        for ent in self.ents.values():
            ent.docs.discard(None)
            ent.mention_spans = sorted(set(ent.mention_spans))
            for sid in ent.sentence_ids:
                ent.neighbours |= sentence_members[sid] - {ent.id}
        for e in self.macro + self.micro:
            if e.get("status") == REJECTED:
                continue
            s, d = e.get("src_entity_id"), e.get("dst_entity_id")
            if s in self.ents and d in self.ents and s != d:
                self.ents[s].neighbours.add(d)
                self.ents[d].neighbours.add(s)

        # -- affiliations -----------------------------------------------------------
        for role in self.roles:
            ent, org = self.ents.get(role["entity_id"]), self.entity_rows.get(role.get("org_entity_id"))
            if ent is None or org is None:
                continue
            when = edge_interval([("as_of", self.time_refs.get(role.get("time_id")))])
            if not when.known:
                when = _doc_day(self.documents.get(ent.row.get("doc_id")))
            ent.affiliations.append(_Aff(org.get("canonical_name") or "", _norm_role(role.get("role")),
                                         when, explicit=True))
        for e in self.macro:
            ent, org = self.ents.get(e.get("src_entity_id")), self.ents.get(e.get("dst_entity_id"))
            rel = ctx.ontology.canonical_relation(e.get("rel")) or e.get("rel")
            if (ent is None or org is None or org.cls != "organization" or e.get("negated")
                    or e.get("status") == REJECTED or rel not in AFFILIATION_RELATIONS):
                continue
            when = edge_interval(self.edge_times[e["edge_id"]])
            if not when.known:
                when = _doc_day(self.documents.get(self.edge_doc.get(e["edge_id"])))
            ent.affiliations.append(_Aff(org.row.get("canonical_name") or "",
                                         AFFILIATION_RELATIONS[rel], when, explicit=False))

    # -- helpers ----------------------------------------------------------------
    def current(self, eid: str) -> str:
        """Follow ``merged_into`` to the entity standing for ``eid`` now."""
        seen = set()
        while eid in self.entity_rows and self.entity_rows[eid].get("status") == MERGED \
                and self.entity_rows[eid].get("merged_into") and eid not in seen:
            seen.add(eid)
            eid = self.entity_rows[eid]["merged_into"]
        return eid

    def flag_sets(self, kind: str, states: Sequence[str] = (POTENTIAL, FINAL)) -> list[set[str]]:
        return [{self.current(x) for x in (f.get("object_ids") or [])}
                for f in self.flags if f["kind"] == kind and f["state"] in states]

    def sentence_text(self, doc_id: str, sentence_id: str) -> str:
        if doc_id not in self._texts:
            version = max((t["text_version"] for t in self.r.select("doc_texts", doc_id=doc_id)), default=1)
            text_row = self.r.get("doc_texts", doc_id=doc_id, text_version=version) or {}
            sents = {s["sentence_id"]: s for s in self.r.select("sentences", doc_id=doc_id,
                                                                text_version=version)}
            self._texts[doc_id] = (text_row.get("canonical_text") or "", sents)
        text, sents = self._texts[doc_id]
        s = sents.get(sentence_id)
        return text[s["char_start"]:s["char_end"]] if s else ""


# =================================================================== pairs
@dataclass
class _Pair:
    a: str
    b: str
    features: dict[str, float]
    score: float
    logit: float
    blocking: list[str]
    cannot_link: list[str]
    decision: str = ""
    rule_id: str = ""
    blocked_by: dict[str, Any] | None = None

    @property
    def key(self) -> frozenset:
        return frozenset((self.a, self.b))


def _weights(ctx: StageContext) -> tuple[float, dict[str, float]]:
    cfg = ctx.config.resolver
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(getattr(cfg, "weights", None) or {})
    bias = getattr(cfg, "bias", None)
    return (DEFAULT_BIAS if bias is None else float(bias)), weights


def _block(data: _Data, candidates: list[_Ent], max_ed: int) -> dict[frozenset, set[str]]:
    pairs: dict[frozenset, set[str]] = defaultdict(set)
    by_class: dict[str, list[_Ent]] = defaultdict(list)
    for ent in candidates:
        by_class[ent.cls or ""].append(ent)
    for members in by_class.values():
        by_token: dict[str, list[_Ent]] = defaultdict(list)
        for ent in members:
            if ent.surname:
                by_token[ent.surname].append(ent)
        for bucket in by_token.values():
            for x, y in combinations(bucket, 2):
                if x.initial is None or y.initial is None or x.initial == y.initial:
                    pairs[frozenset((x.id, y.id))].add("surname_initials")
        ordered = sorted(members, key=lambda e: (len(e.key), e.id))
        for i, x in enumerate(ordered):
            for y in ordered[i + 1:]:
                if len(y.key) - len(x.key) > max_ed:
                    break
                if x.key and y.key and Levenshtein.distance(x.key, y.key, score_cutoff=max_ed) <= max_ed:
                    pairs[frozenset((x.id, y.id))].add("edit_distance")
    cand = {e.id for e in candidates}
    for kind, tag in ((FLAG_IDENTITY, "identity_flag"), ("t2_merge_proposal", "t2_merge_proposal")):
        for ids in data.flag_sets(kind, (POTENTIAL, FINAL)):
            for x, y in combinations(sorted(ids & cand), 2):
                if data.ents[x].cls == data.ents[y].cls:
                    pairs[frozenset((x, y))].add(tag)
    return pairs


def _cannot_link(data: _Data, x: _Ent, y: _Ent, identity_sets: list[set[str]]) -> list[str]:
    rules = []
    if x.docs & y.docs:
        shared = x.sentence_ids & y.sentence_ids
        doc_id = next(iter(x.docs & y.docs))
        for sid in sorted(shared):
            text = data.sentence_text(doc_id, sid).casefold()
            if any(m in text for m in DISAMBIGUATION_MARKERS):
                rules.append("explicit_disambiguator")
                break
    if any(x.id in s and y.id in s for s in identity_sets):
        rules.append("identity_ambiguity")
    if x.row.get("doc_id") and x.row.get("doc_id") == y.row.get("doc_id") or (x.docs & y.docs):
        rules.append("same_document")
    if x.is_person and y.is_person and x.key and x.key == y.key:
        xa = [a for a in x.affiliations if a.explicit and a.org_name]
        ya = [a for a in y.affiliations if a.explicit and a.org_name]
        disjoint = xa and ya and not any(_same_org(p.org_name, q.org_name) for p in xa for q in ya)
        if disjoint and any(p.when.known and q.when.known and p.when.overlaps(q.when)
                            and _different_org(p.org_name, q.org_name) for p in xa for q in ya):
            rules.append("simultaneous_affiliations")
    return [r for r in CANNOT_LINK_ORDER if r in rules]


def _features(data: _Data, x: _Ent, y: _Ent, merge_sets: list[set[str]],
              split_sets: list[set[str]]) -> dict[str, float]:
    sx, sy = x.row.get("subtype"), y.row.get("subtype")
    role_org = 0.0
    conflicting = 0.0
    for p in x.affiliations:
        for q in y.affiliations:
            if _same_org(p.org_name, q.org_name):
                role_org = max(role_org, 1.0 if p.role and p.role == q.role else 0.6)
            elif (p.role and p.role == q.role and p.role not in NON_EXCLUSIVE_ROLES
                  and _different_org(p.org_name, q.org_name)
                  and p.when.known and q.when.known and p.when.overlaps(q.when)):
                conflicting = 1.0
    own = {x.key, y.key}
    nx = {data.ents[n].key for n in x.neighbours if n in data.ents} - own - {""}
    ny = {data.ents[n].key for n in y.neighbours if n in data.ents} - own - {""}
    comention = len(nx & ny) / min(len(nx), len(ny)) if nx and ny else 0.0
    docs_union = x.docs | y.docs
    return {
        "name_similarity": _name_similarity(x.key, y.key),
        "type_match": 1.0 if (not sx or not sy or sx == sy) else 0.0,
        "role_org_overlap": role_org,
        "comention_overlap": round(comention, 4),
        "document_overlap": round(len(x.docs & y.docs) / len(docs_union), 4) if docs_union else 0.0,
        "conflicting_roles": conflicting,
        "t2_merge_proposal": 1.0 if any(x.id in s and y.id in s for s in merge_sets) else 0.0,
        "t2_split_proposal": 1.0 if any(x.id in s or y.id in s for s in split_sets) else 0.0,
    }


def _score(bias: float, weights: dict[str, float], features: dict[str, float]) -> tuple[float, float]:
    logit = bias + sum(weights.get(k, 0.0) * v for k, v in features.items())
    return 1.0 / (1.0 + math.exp(-logit)), logit


class _UF:
    """Union-find that also keeps each cluster's members."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self._members: dict[str, set[str]] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x], self._members[x] = x, {x}
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            keep, drop = min(ra, rb), max(ra, rb)
            self.parent[drop] = keep
            self._members[keep] |= self._members.pop(drop)

    def members(self, x: str) -> set[str]:
        return set(self._members[self.find(x)])


def _effective(resolutions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """L5 rows not reversed by a later row."""
    reversed_ids = {r["reverses"] for r in resolutions if r.get("reverses")}
    return [r for r in resolutions if r["decision_id"] not in reversed_ids]


def _effective_merges(resolutions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in _effective(resolutions) if r["decision"] == MERGED]


def _write_decision(tx: Tx, a: str, b: str, decision: str, *, score: float | None,
                    features: dict[str, Any] | None, decided_by: str, rule_id: str | None,
                    evidence: Sequence[str], reverses: str | None = None) -> str:
    decision_id = new_id("res")
    tx.insert("resolutions", dict(decision_id=decision_id, entity_a=a, entity_b=b,
                                  score=None if score is None else round(score, 6),
                                  features=features or {}, decision=decision, decided_by=decided_by,
                                  rule_id=rule_id, evidence_span_ids=list(evidence), reverses=reverses))
    return decision_id


# =================================================================== materialise
def _materialize(tx: Tx, run_id: str, rule_id: str) -> dict[str, int]:
    """Make entity status / merged_into and macro-edge endpoints match the
    effective MERGED rows of L5. Every change goes through ``tx.update``."""
    resolutions = tx.select("resolutions", run_id=run_id, order_by="decision_id")
    touched = {x for r in resolutions if r["decision"] == MERGED for x in (r["entity_a"], r["entity_b"])}
    uf = _UF()
    for r in _effective_merges(resolutions):
        uf.union(r["entity_a"], r["entity_b"])
    counts: dict[str, int] = defaultdict(int)
    for m in _select_in(tx, "mentions", "entity_id", touched, run_id=run_id):
        counts[m["entity_id"]] += 1
    components: dict[str, list[str]] = defaultdict(list)
    for x in touched:
        components[uf.find(x)].append(x)
    survivor = {}
    for members in components.values():
        best = sorted(members, key=lambda e: (-counts[e], e))[0]
        survivor.update({m: best for m in members})
    changed_entities = 0
    for row in _select_in(tx, "entities", "entity_id", touched, run_id=run_id):
        eid = row["entity_id"]
        want = (ACTIVE, None) if survivor.get(eid, eid) == eid else (MERGED, survivor[eid])
        if (row.get("status"), row.get("merged_into")) != want and row.get("status") in (ACTIVE, MERGED):
            tx.update("entities", {"entity_id": eid}, {"status": want[0], "merged_into": want[1]},
                      rule_id=rule_id)
            changed_entities += 1
    # original endpoints = before-image of the first U8 re-point of each edge
    original: dict[str, dict[str, Any]] = {}
    log = tx.select("change_log", run_id=run_id, object_type="macro_edge", module=MODULE,
                    order_by="change_id")
    for row in log:
        if row.get("rule_id") in (REPOINT_RULE, REPOINT_REVERSE_RULE) and row["object_id"] not in original:
            original[row["object_id"]] = row.get("before") or {}
    repointed = 0
    for edge in tx.select("macro_edges", run_id=run_id, order_by="edge_id"):
        base = original.get(edge["edge_id"], edge)
        changes = {}
        for col in ("src_entity_id", "dst_entity_id"):
            want = survivor.get(base.get(col), base.get(col))
            if want != edge.get(col):
                changes[col] = want
        if changes:
            back = all(changes.get(c, edge[c]) == base.get(c) for c in ("src_entity_id", "dst_entity_id"))
            tx.update("macro_edges", {"edge_id": edge["edge_id"]}, changes,
                      rule_id=REPOINT_REVERSE_RULE if back and edge["edge_id"] in original else REPOINT_RULE)
            repointed += 1
    return {"entities_changed": changed_entities, "edges_repointed": repointed}


# =================================================================== resolve
def _hold_rivals(pairs: list[_Pair], hard: dict[frozenset, str], docs_of: dict[str, set[str]],
                 identity_sets: list[set[str]], margin: float, escalate: bool) -> None:
    """An entity in the merge band with two rivals that are kept apart from each other
    (same document, identity flag, APART pair) and score within ``margin`` of each other
    cannot be merged by order: the evidence does not say which one it is. Both pairs
    become ESCALATED (``rival_candidates``) — or APART outside the ``full`` arm."""
    def kept_apart(x: str, y: str) -> bool:
        return bool(docs_of.get(x, set()) & docs_of.get(y, set())) \
            or any(x in s and y in s for s in identity_sets) or hard.get(frozenset((x, y))) == APART

    by_entity: dict[str, list[_Pair]] = {}
    for p in pairs:
        if not p.decision:
            by_entity.setdefault(p.a, []).append(p)
            by_entity.setdefault(p.b, []).append(p)
    rivals: set[frozenset] = set()
    for eid, ps in by_entity.items():
        for i, p in enumerate(ps):
            for q in ps[i + 1:]:
                other_p, other_q = (p.b if p.a == eid else p.a), (q.b if q.a == eid else q.a)
                if abs(p.score - q.score) < margin and kept_apart(other_p, other_q):
                    rivals |= {p.key, q.key}
    for p in pairs:
        if p.key in rivals:
            p.decision = ESCALATED if escalate else APART
            p.rule_id = "rival_candidates" if escalate else "rival_candidates_no_escalation"
            hard[p.key] = p.decision


def _resolve(ctx: StageContext, tx: Tx, data: _Data) -> dict[str, int]:
    cfg = ctx.config.resolver
    bias, weights = _weights(ctx)
    merge_t, apart_t = float(cfg.merge_threshold), float(cfg.apart_threshold)
    decided = {frozenset((r["entity_a"], r["entity_b"])) for r in data.resolutions}
    candidates = [e for e in data.ents.values()
                  if e.row.get("status") == ACTIVE and e.cls not in EXCLUDED_CLASSES and e.key]
    blocks = _block(data, candidates, int(cfg.max_edit_distance))
    identity_sets = data.flag_sets(FLAG_IDENTITY)
    merge_sets = data.flag_sets("t2_merge_proposal")
    split_sets = data.flag_sets("t2_split_proposal")

    pairs: list[_Pair] = []
    for key, tags in blocks.items():
        if key in decided:
            continue
        a, b = sorted(key)
        x, y = data.ents[a], data.ents[b]
        feats = _features(data, x, y, merge_sets, split_sets)
        score, logit = _score(bias, weights, feats)
        pairs.append(_Pair(a, b, feats, score, logit, sorted(tags), _cannot_link(data, x, y, identity_sets)))

    escalate = ctx.arm == "full"
    hard: dict[frozenset, str] = {}      # constraint pair -> blocking decision (APART | ESCALATED)
    for p in pairs:
        if p.cannot_link:
            p.decision, p.rule_id = APART, p.cannot_link[0]
        elif p.score >= merge_t:
            continue
        elif p.score <= apart_t:
            p.decision, p.rule_id = APART, "score_apart"
        elif escalate:
            p.decision, p.rule_id = ESCALATED, "middle_band"
        else:
            p.decision, p.rule_id = APART, "middle_band_no_escalation"
        hard[p.key] = p.decision

    uf = _UF()
    for r in _effective_merges(data.resolutions):
        uf.union(r["entity_a"], r["entity_b"])
    # earlier decisions (re-runs): the latest effective APART / ESCALATED row of a pair stays a constraint
    latest: dict[frozenset, dict[str, Any]] = {}
    for r in _effective(data.resolutions):
        latest[frozenset((r["entity_a"], r["entity_b"]))] = r
    for pair_key, r in latest.items():
        if r["decision"] in (APART, ESCALATED):
            hard.setdefault(pair_key, r["decision"])
    docs_of = {e.id: set(e.docs) for e in data.ents.values()}
    _hold_rivals(pairs, hard, docs_of, identity_sets, float(getattr(cfg, "rival_margin", 0.05)), escalate)

    def blocked_by(a: str, b: str) -> dict[str, Any] | None:
        """The constraint a merge of the clusters of a and b would break (APART-type first)."""
        soft = None
        for x in sorted(uf.members(a)):
            for y in sorted(uf.members(b)):
                pair_key = frozenset((x, y))
                if docs_of.get(x, set()) & docs_of.get(y, set()):
                    return {"pair": [x, y], "rule": "same_document", "decision": APART}
                if any(x in s and y in s for s in identity_sets):
                    return {"pair": [x, y], "rule": "identity_ambiguity", "decision": APART}
                if hard.get(pair_key) == APART:
                    return {"pair": [x, y], "rule": "apart_pair", "decision": APART}
                if hard.get(pair_key) == ESCALATED and soft is None:
                    soft = {"pair": [x, y], "rule": "escalated_pair", "decision": ESCALATED}
        return soft

    for p in sorted((p for p in pairs if not p.decision), key=lambda p: (-p.score, p.a, p.b)):
        block = blocked_by(p.a, p.b)
        if block:
            p.decision, p.rule_id, p.blocked_by = block["decision"], "merge_blocked", block
        else:
            uf.union(p.a, p.b)
            p.decision, p.rule_id = MERGED, "score_merge"

    counts = {"pairs_considered": len(pairs), "merged": 0, "apart": 0, "escalated": 0, "blocked": 0}
    for p in sorted(pairs, key=lambda p: (p.a, p.b)):
        evidence = sorted(set(data.ents[p.a].mention_spans) | set(data.ents[p.b].mention_spans))
        features: dict[str, Any] = {**p.features, "logit": round(p.logit, 4), "blocking": p.blocking,
                                    "cannot_link": p.cannot_link}
        if p.blocked_by:
            features["blocked_by"] = p.blocked_by
        _write_decision(tx, p.a, p.b, p.decision, score=p.score, features=features, decided_by=MODULE,
                        rule_id=p.rule_id, evidence=evidence)
        counts[{MERGED: "merged", APART: "apart", ESCALATED: "escalated"}[p.decision]] += 1
        if p.blocked_by:
            counts["blocked"] += 1
            x, y = p.blocked_by["pair"]
            raise_flag(tx, FLAG_MERGE_BLOCKED, [p.a, p.b], MODULE, state=FINAL, span_ids=evidence,
                       note=f"merge would join {x} and {y} ({p.blocked_by['rule']})",
                       rule_id="merge_blocked")
    return counts


# =================================================================== reconcile
def _amount(text: str | None) -> tuple[float, str | None] | None:
    amount = canonical_amount(text or "")
    return (amount.value, amount.currency) if amount else None


def _values_differ(a: tuple[float, str | None], b: tuple[float, str | None]) -> bool:
    if a[1] and b[1] and a[1] != b[1]:
        return True
    return not math.isclose(a[0], b[0], rel_tol=1e-6, abs_tol=1e-6)


def _config_list(ctx: StageContext, name: str, default: Any) -> Any:
    value = getattr(ctx.config.resolver, name, None)
    return default if value is None else value


def _interval_with_lineage(data: _Data, edge_id: str, lineage: dict[str, list[str]]) -> Interval:
    times = list(data.edge_times.get(edge_id, []))
    for macro_id in lineage.get(edge_id, []):
        times.extend(data.edge_times.get(macro_id, []))
    return edge_interval(times)


def _clash_pairs(ctx: StageContext, data: _Data, edges: list[dict[str, Any]]) -> list[tuple[dict, dict, str]]:
    """Pairs of edges that would contradict each other if simultaneous."""
    directions = {**FUNCTIONAL_DIRECTION, **(_config_list(ctx, "functional_direction", {}) or {})}
    attributes = set(_config_list(ctx, "attribute_relations", ATTRIBUTE_RELATIONS))
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in edges:
        rel = ctx.ontology.canonical_relation(e.get("rel")) or e.get("rel")
        if ctx.ontology.is_functional(rel):
            side = "dst_entity_id" if directions.get(rel, "src") == "dst" else "src_entity_id"
            groups[("functional", rel, side, e.get(side))].append(e)
        if rel in attributes:
            dst = data.ents.get(e.get("dst_entity_id"))
            if dst is not None and dst.cls == "value":
                groups[("attribute", rel, e.get("src_entity_id"))].append(e)
            else:
                groups[("attribute", rel, e.get("src_entity_id"), e.get("dst_entity_id"))].append(e)
    out = []
    for gkey, members in groups.items():
        for a, b in combinations(members, 2):
            if gkey[0] == "functional":
                other = "src_entity_id" if gkey[2] == "dst_entity_id" else "dst_entity_id"
                if a.get(other) != b.get(other):
                    out.append((a, b, f"functional:{gkey[1]}"))
            else:
                va, vb = _edge_value(data, a), _edge_value(data, b)
                if va and vb and _values_differ(va, vb):
                    out.append((a, b, f"attribute:{gkey[1]}"))
    return out


def _edge_value(data: _Data, edge: dict[str, Any]) -> tuple[float, str | None] | None:
    value = _amount(edge.get("description"))
    if value is None:
        dst = data.ents.get(edge.get("dst_entity_id"))
        if dst is not None and dst.cls == "value":
            value = _amount(dst.row.get("canonical_name"))
    return value


def _reconcile(ctx: StageContext, tx: Tx, data: _Data) -> dict[str, int]:
    edges = [e for e in data.macro if e.get("status") != REJECTED and not e.get("negated")]
    existing = {frozenset(f.get("object_ids") or []) for f in data.flags if f["kind"] == FLAG_CONFLICT}
    counts = {"conflicts_final": 0, "sequences": 0}
    self_conflicts: dict[frozenset, str] = {}
    for a, b, rule in _clash_pairs(ctx, data, edges):
        ia, ib = edge_interval(data.edge_times[a["edge_id"]]), edge_interval(data.edge_times[b["edge_id"]])
        if ia.known and ib.known and not ia.overlaps(ib):
            counts["sequences"] += 1
            continue
        key = frozenset((a["edge_id"], b["edge_id"]))
        if key in existing:
            continue
        existing.add(key)
        why = "no time on one edge" if not (ia.known and ib.known) else "time references overlap"
        spans = sorted(set(data.edge_spans[a["edge_id"]]) | set(data.edge_spans[b["edge_id"]]))
        flag_id = raise_flag(tx, FLAG_CONFLICT, [a["edge_id"], b["edge_id"]], MODULE, state=FINAL,
                             span_ids=spans, note=f"{rule}: {why}", rule_id=rule)
        self_conflicts[key] = flag_id
        counts["conflicts_final"] += 1
    data.new_conflicts = self_conflicts
    return counts


def _update_flags(ctx: StageContext, tx: Tx, data: _Data) -> dict[str, int]:
    counts = {"inconsistency_final": 0, "inconsistency_dismissed": 0, "identity_final": 0,
              "identity_dismissed": 0, "identity_open": 0}
    directions = {**FUNCTIONAL_DIRECTION, **(_config_list(ctx, "functional_direction", {}) or {})}
    micro = {e["edge_id"]: e for e in data.micro}
    lineage: dict[str, list[str]] = defaultdict(list)       # micro edge -> macro edges built from it
    for row in _select_in(tx, "edge_lineage", "micro_edge_id", list(micro)):
        lineage[row["micro_edge_id"]].append(row["macro_edge_id"])
    for flag in data.flags:
        if flag["state"] != POTENTIAL:
            continue
        if flag["kind"] == FLAG_INCONSISTENCY:
            edges = [micro[i] for i in flag.get("object_ids") or [] if i in micro]
            explanations, clashing = [], []
            for a, b in combinations(edges, 2):
                why = _explain(ctx, data, a, b, lineage, directions)
                (explanations if why else clashing).append(why or f"{a['edge_id']}~{b['edge_id']}")
            if len(edges) >= 2 and not clashing:
                tx.update("flags", {"flag_id": flag["flag_id"]},
                          {"state": DISMISSED, "resolved_by": MODULE,
                           "note": _note(flag, "dismissed: " + ", ".join(sorted(set(explanations))))},
                          rule_id=explanations[0])
                counts["inconsistency_dismissed"] += 1
            else:
                related = sorted({fid for key, fid in data.new_conflicts.items()
                                  if key & {m for e in edges for m in lineage[e["edge_id"]]}})
                note = "still a conflict after resolution" + (f"; see {', '.join(related)}" if related else "")
                tx.update("flags", {"flag_id": flag["flag_id"]}, {"state": FINAL, "note": _note(flag, note)},
                          rule_id="inconsistency_final")
                counts["inconsistency_final"] += 1
        elif flag["kind"] == FLAG_IDENTITY:
            original = set(flag.get("object_ids") or [])
            now = {data.current(x) for x in original}
            if len(original) >= 2 and len(now) == 1:
                tx.update("flags", {"flag_id": flag["flag_id"]},
                          {"state": DISMISSED, "resolved_by": MODULE,
                           "note": _note(flag, "resolved to one entity")}, rule_id="identity_merged")
                counts["identity_dismissed"] += 1
            elif len(now) >= 2:
                tx.update("flags", {"flag_id": flag["flag_id"]},
                          {"state": FINAL, "resolved_by": MODULE,
                           "note": _note(flag, "kept apart: distinct parties (cannot-link)")},
                          rule_id="identity_kept_apart")
                counts["identity_final"] += 1
            else:
                counts["identity_open"] += 1
    return counts


def _note(flag: dict[str, Any], text: str) -> str:
    return f"{flag['note']} | U8: {text}" if flag.get("note") else f"U8: {text}"


def _explain(ctx: StageContext, data: _Data, a: dict, b: dict, lineage: dict[str, list[str]],
             directions: dict[str, str]) -> str | None:
    """Why two clashing micro edges are not a contradiction, or None."""
    cur = {eid: data.current(eid) for e in (a, b) for eid in (e.get("src_entity_id"), e.get("dst_entity_id"))}
    ends_a = (cur[a.get("src_entity_id")], cur[a.get("dst_entity_id")])
    ends_b = (cur[b.get("src_entity_id")], cur[b.get("dst_entity_id")])
    raw_a = (a.get("src_entity_id"), a.get("dst_entity_id"))
    raw_b = (b.get("src_entity_id"), b.get("dst_entity_id"))
    rel_a = ctx.ontology.canonical_relation(a.get("rel")) or a.get("rel")
    rel_b = ctx.ontology.canonical_relation(b.get("rel")) or b.get("rel")
    if raw_a != raw_b and ends_a == ends_b and rel_a == rel_b:
        return "resolved_by_merge"
    if rel_a == rel_b and ctx.ontology.is_functional(rel_a):
        key = 1 if directions.get(rel_a, "src") == "dst" else 0
        if ends_a[key] != ends_b[key]:
            return "functional_direction"
    ia = _interval_with_lineage(data, a["edge_id"], lineage)
    ib = _interval_with_lineage(data, b["edge_id"], lineage)
    if ia.known and ib.known and not ia.overlaps(ib):
        return "time_sequence"
    return None


# =================================================================== public
def run(ctx: StageContext) -> dict[str, int]:
    """Resolve entities across the run, re-point S5, reconcile, settle flags."""
    clock = Stopwatch()
    with ctx.tx(RESOLVE, MODULE) as tx:
        data = _Data(ctx, tx)
        counts: dict[str, int] = dict(_resolve(ctx, tx, data))
        counts.update(_materialize(tx, ctx.run_id, rule_id="score_merge"))
        data = _Data(ctx, tx)           # re-read: statuses and endpoints moved
        counts.update(_reconcile(ctx, tx, data))
        counts.update(_update_flags(ctx, tx, data))
        counts["dismissed"] = counts["inconsistency_dismissed"] + counts["identity_dismissed"]
        record_stage_metrics(tx, RESOLVE, ctx.run_id, items_in=counts["pairs_considered"],
                             items_out=counts["merged"], wall_ms=clock.ms)
    return counts


def _cluster_block(tx: Tx, ctx: StageContext, a: str, b: str) -> str | None:
    """A hard cannot-link between the clusters of ``a`` and ``b`` (for later deciders)."""
    data = _Data(ctx, tx)
    uf = _UF()
    for r in _effective_merges(data.resolutions):
        uf.union(r["entity_a"], r["entity_b"])
    identity_sets = data.flag_sets(FLAG_IDENTITY)
    for x in sorted(uf.members(a)):
        for y in sorted(uf.members(b)):
            if x == y or x not in data.ents or y not in data.ents:
                continue
            rules = _cannot_link(data, data.ents[x], data.ents[y], identity_sets)
            if rules:
                return f"{rules[0]} ({x}, {y})"
    return None


def apply_decision(ctx: StageContext, entity_a: str, entity_b: str, decision: str, *,
                   decided_by: str, rule_id: str | None = None, score: float | None = None,
                   features: dict[str, Any] | None = None,
                   evidence_span_ids: Sequence[str] = ()) -> dict[str, Any]:
    """Record a later decision on a pair (e.g. U11 applying T3's MERGE / KEEP_APART).
    A MERGED decision that a cannot-link rule forbids is recorded APART with
    ``rule_id='merge_blocked'`` and a ``merge_blocked`` flag."""
    if decision not in (MERGED, APART):
        raise ValueError(f"decision must be MERGED or APART, not {decision!r}")
    with ctx.tx(RESOLVE, MODULE) as tx:
        evidence = list(evidence_span_ids)
        if decision == MERGED:
            block = _cluster_block(tx, ctx, entity_a, entity_b)
            if block:
                decision_id = _write_decision(tx, entity_a, entity_b, APART, score=score,
                                              features={**(features or {}), "blocked_by": block},
                                              decided_by=decided_by, rule_id="merge_blocked", evidence=evidence)
                raise_flag(tx, FLAG_MERGE_BLOCKED, [entity_a, entity_b], MODULE, state=FINAL,
                           span_ids=evidence, note=f"merge blocked: {block}", rule_id="merge_blocked")
                return {"decision_id": decision_id, "decision": APART, "blocked": block}
        decision_id = _write_decision(tx, entity_a, entity_b, decision, score=score, features=features,
                                      decided_by=decided_by, rule_id=rule_id, evidence=evidence)
        result = _materialize(tx, ctx.run_id, rule_id=rule_id or f"decision_{decision.lower()}")
    return {"decision_id": decision_id, "decision": decision, "blocked": None, **result}


def reverse_decision(ctx: StageContext, decision_id: str, decided_by: str) -> dict[str, Any]:
    """Undo an L5 decision: a new row with ``reverses=decision_id``; entity status
    and macro-edge endpoints are restored from L5 + the change log (logged)."""
    with ctx.tx(RESOLVE, MODULE) as tx:
        row = tx.get("resolutions", decision_id=decision_id)
        if row is None:
            raise KeyError(f"no resolution {decision_id}")
        if tx.select("resolutions", reverses=decision_id):
            raise ValueError(f"{decision_id} is already reversed")
        if row["decision"] == MERGED:
            new = APART
        elif row["decision"] == APART:
            new = MERGED
            block = _cluster_block(tx, ctx, row["entity_a"], row["entity_b"])
            if block:
                raise MergeBlocked(f"cannot reverse {decision_id} into a merge: {block}")
        else:
            raise ValueError(f"an {row['decision']} decision is answered (apply_decision), not reversed")
        new_id_ = _write_decision(tx, row["entity_a"], row["entity_b"], new, score=row.get("score"),
                                  features=row.get("features"), decided_by=decided_by, rule_id="reversal",
                                  evidence=row.get("evidence_span_ids") or [], reverses=decision_id)
        result = _materialize(tx, ctx.run_id, rule_id="reversal")
    return {"decision_id": new_id_, "decision": new, "reverses": decision_id, **result}
