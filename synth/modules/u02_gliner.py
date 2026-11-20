"""U2 — GLiNER Pass: the whole text, once, fills the entity table (S2).

Purpose: Tier 1's entity pass (P1 §3 U2). Cut A windows (``gliner.window_words``
words, ``gliner.overlap_words`` overlap) over the **whole** canonical text; the
backend (``ctx.gliner``, a :class:`synth.contracts.GlinerBackend`) returns exact
offsets; overlapping duplicates across windows are merged (highest score wins;
a span that touches an interior window cut ranks after one that does not, since
it may be truncated). Then S2 is filled for this document only:

* mentions in filtered sentences -> ``DISCARDED`` (``filtered``); ``role title``
  mentions -> ``DISCARDED`` (``role_label``; U5 attaches roles); time mentions
  (``date``, ``time period``, ``duration``) stay ``OPEN`` for U3;
* the rest: same ``ontology.norm_key`` + same class (``label_to_type``) -> one
  entity (canonical name = the longest surface), **unless** a same-name
  collision rule fires between two of its mentions (person class only):
  (a) both in one sentence; (b) affiliations to different organisations (an
  organisation mention right after "of" / "director of" / "at" in the same
  sentence); (c) an explicit disambiguator ("no relation", "not related",
  "no relationship", "a different …") in or right after the sentences of two
  mentions at most one sentence apart. Then the colliding mentions seed
  separate entities, the other mentions attach to the seed whose affiliation
  they share, else to the first-seen seed, and one ``identity_ambiguity`` flag
  (POTENTIAL) lists the entities. Nothing merges across documents (U8 decides).
* ``relation_mode`` (``ctx.extra`` or ``tier1.relation_mode``) ``gliner`` or
  ``gliner+micro``: relation extraction on the same windows; ends mapped to the
  mentions just built; candidate micro edges (``source="gliner"``,
  ``CANDIDATE``, stance ``stated``) with an ``edge_spans`` row over the
  sentence(s) covering head..tail. Unknown relation names, unmapped ends,
  self-loops and ends outside the window are dropped and logged.

Contract: :func:`run_document` ``(ctx, doc_id) -> dict`` (counts). All writes of
one document happen in one transaction (a failed unit leaves nothing behind).
Stores written: S1 ``spans``; S2 ``entities``, ``entity_aliases``, ``mentions``;
S4 ``micro_edges``, ``edge_spans``; L3 ``coverage_events`` (``GLINER_SCANNED``
for every non-filtered sentence); L6 ``flags``; ``stage_metrics``; L4 via Tx.
Token caps: none (windows of 384 words, spec §10). Spec: P1 §1 E1, §2 cut A,
§3 U2, §4 S2/S4, §8.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from synth import states as S
from synth.contracts import GlinerSpan
from synth.ids import new_id
from synth.modules.common import (DocView, Stopwatch, StageContext, emit_coverage, raise_flag,
                                  record_stage_metrics, write_span)
from synth.ontology import ROLE_LABELS, TIME_LABELS

log = logging.getLogger("synth.u2")

MODULE = "u02_gliner"
#: Classes the same-name collision rules apply to. Organisations repeat their
#: full name in one sentence all the time ("X of Y, a shareholder of Y").
COLLISION_CLASSES = {"person"}
_AFFILIATION_GAP = re.compile(r"^\s*,?\s*(?:(?:a|an|the)\s+)?(?:[\w-]+\s+){0,3}?(?:of|at)\s+$", re.IGNORECASE)
_DISAMBIGUATOR = re.compile(r"\b(?:no\s+relation(?:ship)?|not\s+related|unrelated|no\s+connection|not\s+the\s+same"
                            r"\s+(?:person|man|woman|individual)|a\s+different\s+(?:person|man|woman|individual"
                            r"|entity|company|party|one))\b", re.IGNORECASE)
_DIFFERENT_NAME = re.compile(r"\ba different [A-Z]")


# ------------------------------------------------------------------ windows (cut A)
def windows(text: str, window_words: int, overlap_words: int) -> list[tuple[int, int]]:
    """Char ranges of cut-A windows over the whole text."""
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    if not words:
        return []
    stride = max(1, window_words - max(0, overlap_words))
    out, i = [], 0
    while True:
        j = min(i + window_words, len(words))
        out.append((words[i][0], words[j - 1][1]))
        if j >= len(words):
            return out
        i += stride


@dataclass
class _Cand:
    start: int
    end: int
    label: str
    score: float
    cut: bool = False                         # touches an interior window cut


@dataclass
class _Mention:
    start: int
    end: int
    surface: str
    label: str
    score: float
    sentence_ids: list[str]
    sent_idx: int | None
    cls: str | None = None
    subtype: str | None = None
    key: str = ""
    status: str = S.OPEN
    reason: str | None = None
    affiliation: str | None = None
    mention_id: str = field(default_factory=lambda: new_id("men"))
    span_id: str | None = None
    entity_id: str | None = None
    rule: str | None = None


def _scan(ctx: StageContext, doc: DocView) -> tuple[list[_Cand], list[tuple[int, int]]]:
    g = ctx.config.model_settings.gliner
    labels = list(ctx.ontology.gliner_labels)
    cands: list[_Cand] = []
    wins = windows(doc.text, g.window_words, g.overlap_words)
    for wi, (ws, we) in enumerate(wins):
        chunk = doc.text[ws:we]
        for sp in ctx.gliner.entities(chunk, labels, g.threshold):
            if not (0 <= sp.start < sp.end <= len(chunk)) or sp.label not in labels:
                continue
            start, end = ws + sp.start, ws + sp.end
            while start < end and doc.text[start].isspace():
                start += 1
            while end > start and doc.text[end - 1].isspace():
                end -= 1
            if end <= start:
                continue
            cut = (start == ws and wi > 0) or (end == we and wi < len(wins) - 1)
            cands.append(_Cand(start, end, sp.label, float(sp.score), cut))
    return cands, wins


def merge_candidates(cands: list[_Cand]) -> list[_Cand]:
    """Overlapping duplicates (within or across windows) -> one; highest score wins."""
    kept: list[_Cand] = []
    for c in sorted(cands, key=lambda c: (c.cut, -c.score, -(c.end - c.start), c.start)):
        if not any(c.start < k.end and c.end > k.start for k in kept):
            kept.append(c)
    return sorted(kept, key=lambda c: (c.start, c.end))


# ------------------------------------------------------------------ S2 fill
def _affiliations(doc: DocView, mentions: list[_Mention]) -> None:
    orgs = [m for m in mentions if m.status == S.OPEN and m.cls == "organization"]
    for m in mentions:
        if m.status != S.OPEN or m.cls != "person":
            continue
        after = [o for o in orgs if o.start >= m.end and o.sent_idx == m.sent_idx]
        if not after:
            continue
        org = min(after, key=lambda o: o.start)
        between = [x for x in mentions if x.cls == "person" and m.end <= x.start < org.start]
        if not between and _AFFILIATION_GAP.match(doc.text[m.end:org.start]):
            m.affiliation = org.key


def _disambiguated(doc: DocView, a: _Mention, b: _Mention) -> bool:
    if a.sent_idx is None or b.sent_idx is None or abs(a.sent_idx - b.sent_idx) > 1:
        return False
    lo, hi = min(a.sent_idx, b.sent_idx), max(a.sent_idx, b.sent_idx) + 1
    text = " ".join(doc.sentence_text(s) for s in doc.sentences if lo <= s["idx"] <= hi and not s["filtered"])
    return bool(_DISAMBIGUATOR.search(text) or _DIFFERENT_NAME.search(text))


def collision_rules(doc: DocView, a: _Mention, b: _Mention) -> list[str]:
    """Which same-name collision rules fire between two mentions of one key."""
    rules = []
    if a.sent_idx is not None and a.sent_idx == b.sent_idx:
        rules.append("same_sentence")
    if a.affiliation and b.affiliation and a.affiliation != b.affiliation:
        rules.append("different_affiliation")
    if _disambiguated(doc, a, b):
        rules.append("disambiguator")
    return rules


def _seeds(doc: DocView, group: list[_Mention]) -> tuple[list[list[_Mention]], set[str]]:
    """Split one name group into seeds. Returns ``(seeds, rules fired)``."""
    if len(group) < 2 or group[0].cls not in COLLISION_CLASSES:
        for m in group:
            m.rule = "u2_same_key"
        return [group], set()
    apart: dict[tuple[int, int], list[str]] = {}
    for i, a in enumerate(group):
        for j in range(i + 1, len(group)):
            fired = collision_rules(doc, a, group[j])
            if fired:
                apart[(i, j)] = fired
    if not apart:
        for m in group:
            m.rule = "u2_same_key"
        return [group], set()
    involved = sorted({i for pair in apart for i in pair})
    seeds: list[list[int]] = []
    for i in involved:
        free = [s for s in seeds if all((min(i, k), max(i, k)) not in apart for k in s)]
        same = [s for s in free if group[i].affiliation
                and any(group[k].affiliation == group[i].affiliation for k in s)]
        target = (same or free or [None])[0]
        if target is None:
            seeds.append([i])
        else:
            target.append(i)
        group[i].rule = "u2_collision_seed"
    for i, m in enumerate(group):
        if i in involved:
            continue
        same = [s for s in seeds if m.affiliation and any(group[k].affiliation == m.affiliation for k in s)]
        if same:
            same[0].append(i)
            m.rule = "u2_attach_affiliation"
        else:
            seeds[0].append(i)
            m.rule = "u2_attach_first_seen_seed"
    rules = {r for fired in apart.values() for r in fired}
    return [sorted((group[k] for k in s), key=lambda m: m.start) for s in seeds], rules


def _canonical(members: list[_Mention]) -> str:
    counts = Counter(m.surface for m in members)
    first = {}
    for m in members:
        first.setdefault(m.surface, m.start)
    return max(counts, key=lambda s: (len(s), counts[s], not s.isupper(), -first[s]))


def _subtype(members: list[_Mention]) -> str | None:
    counts = Counter(m.subtype for m in members if m.subtype)
    return counts.most_common(1)[0][0] if counts else None


# ------------------------------------------------------------------ relations (E1)
def _relations(ctx: StageContext, doc: DocView, wins: list[tuple[int, int]], mentions: list[_Mention],
               chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    g = ctx.config.model_settings.gliner
    rel_names = list(ctx.ontology.relation_names)
    entity_labels = [lb for lb in ctx.ontology.gliner_labels if lb not in TIME_LABELS and lb not in ROLE_LABELS]
    with_entity = [m for m in mentions if m.entity_id]
    best: dict[tuple, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []

    def owner(ws: int, sp: GlinerSpan) -> _Mention | None:
        start, end = ws + sp.start, ws + sp.end
        hits = [(min(end, m.end) - max(start, m.start), m) for m in with_entity if m.start < end and m.end > start]
        return max(hits, key=lambda h: h[0])[1] if hits else None

    for ws, we in wins:
        chunk = doc.text[ws:we]
        for rel in ctx.gliner.relations(chunk, rel_names, entity_labels, g.threshold):
            raw = {"head": rel.head.text, "relation": rel.relation, "tail": rel.tail.text,
                   "score": rel.score, "window": [ws, we]}
            name = ctx.ontology.canonical_relation(rel.relation)
            if name is None:
                dropped.append({**raw, "reason": "unknown_relation"})
                continue
            if not all(0 <= s.start < s.end <= len(chunk) for s in (rel.head, rel.tail)):
                dropped.append({**raw, "reason": "outside_window"})
                continue
            head, tail = owner(ws, rel.head), owner(ws, rel.tail)
            if head is None or tail is None:
                dropped.append({**raw, "reason": "unmapped_end"})
                continue
            if head.entity_id == tail.entity_id:
                dropped.append({**raw, "reason": "self_loop"})
                continue
            lo, hi = min(head.start, tail.start), max(head.end, tail.end)
            sents = doc.locator.sentences_overlapping(lo, hi)
            if not sents or any(doc.by_id[s.sentence_id]["filtered"] for s in sents):
                dropped.append({**raw, "reason": "filtered_or_no_sentence"})
                continue
            first = sents[0].idx
            chunk_row = next((c for c in chunks if c["first_sentence_idx"] <= first <= c["last_sentence_idx"]), None)
            if chunk_row is None:
                dropped.append({**raw, "reason": "no_chunk"})
                continue
            span = (min(s.char_start for s in sents), max(s.char_end for s in sents))
            key = (head.entity_id, name, tail.entity_id, span)
            if key not in best or rel.score > best[key]["score"]:
                best[key] = {"src": head.entity_id, "rel": name, "dst": tail.entity_id, "score": rel.score,
                             "span": span, "chunk_id": chunk_row["chunk_id"], "raw": raw}
    return list(best.values()), dropped


# ------------------------------------------------------------------ the unit
def run_document(ctx: StageContext, doc_id: str) -> dict[str, Any]:
    """GLiNER over one document's whole text -> S2 (and optional S4 candidates)."""
    if ctx.gliner is None:
        raise RuntimeError("U2 needs ctx.gliner (a GlinerBackend)")
    watch = Stopwatch()
    backend = getattr(ctx.gliner, "name", type(ctx.gliner).__name__)
    if getattr(ctx.gliner, "stand_in", False):
        log.warning("U2 on %s uses the %s backend, not GLiNER2", doc_id, backend)
    doc = ctx.doc(doc_id)
    onto = ctx.ontology
    cands, wins = _scan(ctx, doc)
    merged = merge_candidates(cands)

    mentions: list[_Mention] = []
    for c in merged:
        refs = doc.locator.sentences_overlapping(c.start, c.end)
        m = _Mention(c.start, c.end, doc.text[c.start:c.end], c.label, c.score,
                     [r.sentence_id for r in refs], refs[0].idx if refs else None)
        m.cls, m.subtype = onto.label_to_type(c.label)
        m.key = onto.norm_key(m.surface)
        if refs and all(doc.by_id[r.sentence_id]["filtered"] for r in refs):
            m.status, m.reason = S.DISCARDED, "filtered"
        elif c.label in TIME_LABELS:
            m.status = S.OPEN
        elif c.label in ROLE_LABELS:
            m.status, m.reason = S.DISCARDED, "role_label"
        elif m.cls is None:
            m.status, m.reason = S.DISCARDED, "unknown_label"
        elif not m.key:
            m.status, m.reason = S.DISCARDED, "empty_name"
        mentions.append(m)
    _affiliations(doc, mentions)

    groups: dict[tuple[str, str], list[_Mention]] = {}
    for m in mentions:
        if m.status == S.OPEN and m.cls is not None:
            groups.setdefault((m.key, m.cls), []).append(m)

    relation_mode = ctx.extra.get("relation_mode", ctx.config.model_settings.tier1.relation_mode)
    counts = {"backend": backend, "windows": len(wins), "raw_spans": len(cands), "mentions": len(mentions),
              "entities": 0, "discarded": 0, "time_mentions": 0, "flags": 0, "micro_edges": 0,
              "relations_dropped": 0, "sentences_scanned": 0}
    with ctx.tx(S.GLINER, MODULE) as tx:
        for m in mentions:
            m.span_id = write_span(tx, doc, doc.locator.ground_span(m.start, m.end), created_by=MODULE)
        flagged: list[tuple[list[str], list[str], set[str], str]] = []
        for (key, cls), group in groups.items():
            seeds, rules = _seeds(doc, group)
            entity_ids = []
            for members in seeds:
                entity_id = new_id("ent")
                tx.insert("entities", dict(entity_id=entity_id, doc_id=doc_id, canonical_name=_canonical(members),
                                           type=cls, subtype=_subtype(members), norm_key=key,
                                           status=S.ACTIVE, merged_into=None, source="gliner"),
                          rule_id="u2_collision_seed" if rules else "u2_same_key")
                for m in members:
                    m.entity_id, m.status = entity_id, S.RESOLVED
                entity_ids.append(entity_id)
                counts["entities"] += 1
            if rules:
                spans = [m.span_id for m in group if m.rule == "u2_collision_seed"]
                flagged.append((entity_ids, spans, rules, key))
        for m in mentions:
            tx.insert("mentions", dict(mention_id=m.mention_id, doc_id=doc_id, span_id=m.span_id,
                                       surface=m.surface, label=m.label, score=m.score, source=backend,
                                       entity_id=m.entity_id, status=m.status, discard_reason=m.reason),
                      rule_id=m.rule or (f"u2_{m.reason}" if m.reason else "u2_time_mention"))
            if m.entity_id:
                tx.insert("entity_aliases", dict(entity_id=m.entity_id, alias=m.surface, mention_id=m.mention_id))
            counts["discarded"] += m.status == S.DISCARDED
            counts["time_mentions"] += m.label in TIME_LABELS and m.status == S.OPEN
        for entity_ids, spans, rules, key in flagged:
            raise_flag(tx, S.FLAG_IDENTITY, entity_ids, MODULE, span_ids=spans, rule_id="u2_same_name_collision",
                       note=f"same name {key!r} in one document kept apart; rules: {', '.join(sorted(rules))}; "
                            "other mentions attached by shared affiliation, else to the first-seen seed")
            counts["flags"] += 1

        if relation_mode in ("gliner", "gliner+micro"):
            chunks = tx.select("micro_chunks", order_by="idx", doc_id=doc_id)
            edges, dropped = _relations(ctx, doc, wins, mentions, chunks)
            for e in edges:
                edge_id = new_id("med")
                tx.insert("micro_edges", dict(edge_id=edge_id, chunk_id=e["chunk_id"], src_entity_id=e["src"],
                                              rel=e["rel"], subclass=None, dst_entity_id=e["dst"], description="",
                                              stance="stated", negated=False, confidence=e["score"],
                                              status=S.CANDIDATE, source="gliner"), rule_id="u2_gliner_relation")
                span_id = write_span(tx, doc, doc.locator.ground_span(*e["span"]), created_by=MODULE)
                tx.insert("edge_spans", dict(edge_id=edge_id, span_id=span_id))
                counts["micro_edges"] += 1
            for raw in dropped:
                tx.log_change("micro_edge", "-", "DROPPED", None, raw, rule_id=f"gliner_rel_{raw['reason']}")
            counts["relations_dropped"] = len(dropped)

        scanned = [s["sentence_id"] for s in doc.sentences if not s["filtered"]]
        emit_coverage(tx, scanned, S.GLINER, S.GLINER_SCANNED)
        counts["sentences_scanned"] = len(scanned)
        record_stage_metrics(tx, S.GLINER, doc_id, items_in=len(cands), items_out=len(mentions),
                             wall_ms=watch.ms)
    return counts
