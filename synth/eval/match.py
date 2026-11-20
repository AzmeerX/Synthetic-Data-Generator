"""Deterministic matching of a run's output against one gold passage (P1 §12).

Purpose: decide, with no model and no randomness, which system object is which
gold object. Rules (spec §12):

* **mention** — character overlap (one-to-one, largest overlap first);
* **entity** — ≥ 50% mention overlap (matched mentions ÷ the larger mention count
  of the two, within the passage) + same type class;
* **edge strict** — both entities match + same relation + same negation + evidence
  in the same sentence; **lenient** — evidence in an overlapping or adjacent
  sentence. ``family_of`` is symmetric (either direction matches);
* **proposition** — evidence in the same sentence + same status + same speaker
  (both none, or matched entities) + ``rapidfuzz`` token-sort similarity ≥ 70;
* **link** — both end propositions matched + same type;
* **time** — paired by span overlap; correct when kind and granularity are equal
  and the values overlap (ISO 8601 at any precision, as date ranges).

Contract: build :class:`GoldView` / :class:`SystemView`, then the ``match_*``
functions return ``{gold id: system id}`` maps. Greedy one-to-one in gold-id order
with fixed tie-breaks, so results never depend on row order.
Stores written: none. Token caps: none. Spec: P1 §12.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Iterable

from rapidfuzz import fuzz, utils

SYMMETRIC_RELATIONS = {"family_of"}
TEXT_SIMILARITY = 70.0
ENTITY_OVERLAP = 0.5

TimeVal = tuple[str, str | None, str | None, str | None]      # role, start, end, granularity


# ------------------------------------------------------------------ views
@dataclass
class GMention:
    entity: str
    start: int
    end: int


@dataclass
class GEntity:
    id: str
    cls: str | None
    key: str
    mentions: list[int] = field(default_factory=list)          # indices into GoldView.mentions


@dataclass
class GEdge:
    id: str
    subject: str
    rel: str
    family: str
    object: str
    negated: bool
    nested: bool
    sents: frozenset[int]
    times: list[TimeVal] = field(default_factory=list)


@dataclass
class GProp:
    id: str
    text: str
    speaker: str | None
    status: str
    negated: bool
    sents: frozenset[int]


@dataclass
class GLink:
    id: str
    src: str
    dst: str
    type: str


@dataclass
class GTime:
    id: str
    kind: str
    value_start: str | None
    value_end: str | None
    granularity: str | None
    start: int
    end: int


@dataclass
class GoldView:
    gold_id: str
    doc_id: str
    first_idx: int
    last_idx: int
    mode: str
    mentions: list[GMention] = field(default_factory=list)
    entities: dict[str, GEntity] = field(default_factory=dict)
    edges: list[GEdge] = field(default_factory=list)
    props: list[GProp] = field(default_factory=list)
    links: list[GLink] = field(default_factory=list)
    times: list[GTime] = field(default_factory=list)
    flags: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    traps: list[tuple[str, str]] = field(default_factory=list)
    sentence_text: dict[int, str] = field(default_factory=dict)
    unlocated: list[str] = field(default_factory=list)

    @property
    def nested_sents(self) -> set[int]:
        out: set[int] = set()
        for p in self.props:
            out |= p.sents
        for e in self.edges:
            if e.nested:
                out |= e.sents
        return out

    def fact_sents(self) -> list[frozenset[int]]:
        return [e.sents for e in self.edges] + [p.sents for p in self.props]


@dataclass
class SMention:
    id: str
    entity: str                        # cluster root
    start: int
    end: int


@dataclass
class SEdge:
    id: str
    src: str | None                    # cluster roots
    dst: str | None
    rel: str
    family: str
    negated: bool
    status: str | None
    sents: frozenset[int]
    times: list[TimeVal] = field(default_factory=list)
    tier_last: str | None = None


@dataclass
class SProp:
    id: str
    text: str
    speaker: str | None
    status: str
    negated: bool
    sents: frozenset[int]
    state: str | None = None


@dataclass
class SLink:
    id: str
    src: str
    dst: str
    type: str


@dataclass
class STime:
    id: str
    kind: str | None
    value_start: str | None
    value_end: str | None
    granularity: str | None
    start: int
    end: int


@dataclass
class SystemView:
    mentions: list[SMention] = field(default_factory=list)
    clusters: dict[str, str | None] = field(default_factory=dict)      # root -> type class
    cluster_sizes: dict[str, int] = field(default_factory=dict)        # root -> passage mentions
    edges: list[SEdge] = field(default_factory=list)
    props: list[SProp] = field(default_factory=list)
    links: list[SLink] = field(default_factory=list)
    times: list[STime] = field(default_factory=list)
    flags: list[tuple[str, list[str]]] = field(default_factory=list)  # kind, object ids (roots)


# ------------------------------------------------------------------ helpers
def overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def similarity(a: str, b: str) -> float:
    return float(fuzz.token_sort_ratio(a or "", b or "", processor=utils.default_process))


def evidence_ok(gold_sents: Iterable[int], sys_sents: Iterable[int], lenient: bool) -> bool:
    g, s = set(gold_sents), set(sys_sents)
    if g & s:
        return True
    return lenient and any(abs(i - j) <= 1 for i in g for j in s)


def evidence_distance(gold_sents: Iterable[int], sys_sents: Iterable[int]) -> int:
    g, s = list(gold_sents), list(sys_sents)
    return min((abs(i - j) for i in g for j in s), default=10**6)


_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.IGNORECASE)
_WEEK = re.compile(r"^(\d{4})-?W(\d{2})$", re.IGNORECASE)


def iso_range(value: str | None) -> tuple[_dt.date, _dt.date] | None:
    """``2024`` -> the year; ``2024-03`` -> the month; ``2024-03-14[T…]`` -> the day;
    ``2024-Q3`` -> the quarter; ``2024-W11`` -> the ISO week. ``None`` if unparseable."""
    if not value:
        return None
    v = value.strip()
    try:
        if re.fullmatch(r"\d{4}", v):
            y = int(v)
            return _dt.date(y, 1, 1), _dt.date(y, 12, 31)
        m = re.fullmatch(r"(\d{4})-(\d{2})", v)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            nxt = _dt.date(y + (mo == 12), mo % 12 + 1, 1)
            return _dt.date(y, mo, 1), nxt - _dt.timedelta(days=1)
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
        if m:
            d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d
        m = _QUARTER.match(v)
        if m:
            y, q = int(m.group(1)), int(m.group(2))
            start = _dt.date(y, 3 * q - 2, 1)
            end = _dt.date(y + (q == 4), (3 * q) % 12 + 1, 1) - _dt.timedelta(days=1)
            return start, end
        m = _WEEK.match(v)
        if m:
            start = _dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            return start, start + _dt.timedelta(days=6)
    except ValueError:
        return None
    return None


def value_range(start: str | None, end: str | None) -> tuple[_dt.date, _dt.date] | None:
    a = iso_range(start)
    b = iso_range(end) if end else a
    if a is None and b is None:
        return None
    a = a or b
    b = b or a
    return a[0], b[1]


def values_overlap(s1: str | None, e1: str | None, s2: str | None, e2: str | None) -> bool:
    r1, r2 = value_range(s1, e1), value_range(s2, e2)
    if r1 is None and r2 is None:
        return (s1 or None, e1 or None) == (s2 or None, e2 or None)
    if r1 is None or r2 is None:
        return False
    return r1[0] <= r2[1] and r2[0] <= r1[1]


def _norm(text: str | None) -> str:
    return (text or "").strip().casefold()


def classes_equal(a: str | None, b: str | None) -> bool:
    return _norm(a) == _norm(b)


# ------------------------------------------------------------------ mentions and entities
def match_mentions(gold: GoldView, system: SystemView) -> dict[int, int]:
    """Gold mention index -> system mention index (one-to-one, largest overlap first)."""
    pairs = []
    for gi, gm in enumerate(gold.mentions):
        for si, sm in enumerate(system.mentions):
            ov = overlap(gm.start, gm.end, sm.start, sm.end)
            if ov > 0:
                pairs.append((-ov, gi, si))
    pairs.sort()
    used_g, used_s, out = set(), set(), {}
    for _ov, gi, si in pairs:
        if gi in used_g or si in used_s:
            continue
        used_g.add(gi)
        used_s.add(si)
        out[gi] = si
    return out


def match_entities(gold: GoldView, system: SystemView, mention_map: dict[int, int]) -> dict[str, str]:
    """Gold entity id -> system cluster root."""
    candidates = []
    for gid in sorted(gold.entities):
        ge = gold.entities[gid]
        counts: dict[str, int] = {}
        for gi in ge.mentions:
            if gi in mention_map:
                root = system.mentions[mention_map[gi]].entity
                counts[root] = counts.get(root, 0) + 1
        for root, n in counts.items():
            denom = max(len(ge.mentions), system.cluster_sizes.get(root, 0), 1)
            score = n / denom
            if score >= ENTITY_OVERLAP and classes_equal(ge.cls, system.clusters.get(root)):
                candidates.append((-score, -n, gid, root))
    candidates.sort()
    used_g, used_s, out = set(), set(), {}
    for _s, _n, gid, root in candidates:
        if gid in used_g or root in used_s:
            continue
        used_g.add(gid)
        used_s.add(root)
        out[gid] = root
    return out


def merged_gold_pairs(gold: GoldView, system: SystemView, mention_map: dict[int, int]) -> set[frozenset[str]]:
    """Pairs of distinct gold entities whose matched mentions share a system cluster."""
    roots: dict[str, set[str]] = {}
    for gi, si in mention_map.items():
        roots.setdefault(gold.mentions[gi].entity, set()).add(system.mentions[si].entity)
    ids = sorted(roots)
    out = set()
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if gold.entities[a].key != gold.entities[b].key and roots[a] & roots[b]:
                out.add(frozenset((a, b)))
    return out


def pairwise_resolution(gold: GoldView, system: SystemView, mention_map: dict[int, int]) -> tuple[int, int, int]:
    """Over pairs of matched gold mentions: (correct same-cluster pairs, system
    same-cluster pairs, gold same-entity pairs)."""
    matched = sorted(mention_map)
    correct = sys_same = gold_same = 0
    for i, a in enumerate(matched):
        for b in matched[i + 1:]:
            g_same = gold.entities[gold.mentions[a].entity].key == gold.entities[gold.mentions[b].entity].key
            s_same = system.mentions[mention_map[a]].entity == system.mentions[mention_map[b]].entity
            gold_same += g_same
            sys_same += s_same
            correct += g_same and s_same
    return correct, sys_same, gold_same


# ------------------------------------------------------------------ edges, propositions, links
def edge_entities_ok(g: GEdge, s: SEdge, ent_map: dict[str, str]) -> bool:
    a, b = ent_map.get(g.subject), ent_map.get(g.object)
    if a is None or b is None:
        return False
    if (s.src, s.dst) == (a, b):
        return True
    return g.rel in SYMMETRIC_RELATIONS and (s.src, s.dst) == (b, a)


def match_edges(gold_edges: list[GEdge], sys_edges: list[SEdge], ent_map: dict[str, str],
                lenient: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for g in sorted(gold_edges, key=lambda e: e.id):
        best = None
        for s in sys_edges:
            if s.id in used or s.rel != g.rel or bool(s.negated) != bool(g.negated):
                continue
            if not edge_entities_ok(g, s, ent_map) or not evidence_ok(g.sents, s.sents, lenient):
                continue
            key = (evidence_distance(g.sents, s.sents), s.id)
            if best is None or key < best[0]:
                best = (key, s.id)
        if best is not None:
            used.add(best[1])
            out[g.id] = best[1]
    return out


def prop_ok(g: GProp, s: SProp, ent_map: dict[str, str]) -> bool:
    if not (g.sents & s.sents) or _norm(g.status) != _norm(s.status):
        return False
    if g.speaker is None or s.speaker is None:
        if (g.speaker is None) != (s.speaker is None):
            return False
    elif ent_map.get(g.speaker) != s.speaker:
        return False
    return similarity(g.text, s.text) >= TEXT_SIMILARITY


def match_props(gold_props: list[GProp], sys_props: list[SProp], ent_map: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for g in sorted(gold_props, key=lambda p: p.id):
        best = None
        for s in sys_props:
            if s.id in used or not prop_ok(g, s, ent_map):
                continue
            key = (-similarity(g.text, s.text), s.id)
            if best is None or key < best[0]:
                best = (key, s.id)
        if best is not None:
            used.add(best[1])
            out[g.id] = best[1]
    return out


def match_links(gold_links: list[GLink], sys_links: list[SLink], prop_map: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for g in sorted(gold_links, key=lambda link: link.id):
        a, b = prop_map.get(g.src), prop_map.get(g.dst)
        if a is None or b is None:
            continue
        for s in sorted(sys_links, key=lambda link: link.id):
            if s.id not in used and (s.src, s.dst) == (a, b) and _norm(s.type) == _norm(g.type):
                used.add(s.id)
                out[g.id] = s.id
                break
    return out


# ------------------------------------------------------------------ time
def time_correct(g: GTime, s: STime) -> bool:
    return (_norm(g.kind) == _norm(s.kind) and _norm(g.granularity) == _norm(s.granularity)
            and values_overlap(g.value_start, g.value_end, s.value_start, s.value_end))


def match_times(gold_times: list[GTime], sys_times: list[STime]) -> tuple[dict[str, str], set[str]]:
    """(gold id -> system id paired by span overlap, gold ids whose pair is correct)."""
    pairs = sorted((-overlap(g.start, g.end, s.start, s.end), g.id, s.id)
                   for g in gold_times for s in sys_times
                   if overlap(g.start, g.end, s.start, s.end) > 0)
    used_g, used_s, out = set(), set(), {}
    for _ov, gid, sid in pairs:
        if gid in used_g or sid in used_s:
            continue
        used_g.add(gid)
        used_s.add(sid)
        out[gid] = sid
    by_g = {g.id: g for g in gold_times}
    by_s = {s.id: s for s in sys_times}
    correct = {gid for gid, sid in out.items() if time_correct(by_g[gid], by_s[sid])}
    return out, correct


def edge_time_hits(g: GEdge, s: SEdge) -> int:
    """How many of the gold edge's time roles the system edge carries correctly."""
    hits = 0
    for role, vs, ve, gran in g.times:
        if any(r == role and _norm(sg) == _norm(gran) and values_overlap(vs, ve, ss, se)
               for r, ss, se, sg in s.times):
            hits += 1
    return hits


# ------------------------------------------------------------------ flags
def flag_recalled(kind: str, gold_ids: frozenset[str], system: SystemView,
                  ent_inv: dict[str, str], edge_inv: dict[str, str]) -> bool:
    for s_kind, ids in system.flags:
        if _norm(s_kind) != _norm(kind):
            continue
        mapped = {ent_inv.get(i) or edge_inv.get(i) for i in ids} - {None}
        if (gold_ids and gold_ids <= mapped) or (not gold_ids and mapped):
            return True
    return False
