"""U9 — Gate & Packet: decide which verified items need the frontier, and build their packets.

Purpose: the frontier (T3) sees only the hard leftovers. U9 looks at every
gate-reaching item of the run, gives it rule-based reason codes, escalates the
most severe ones within the budget, and builds one compact packet per escalation.

Gate-reaching items (one each):

* ``edge``        — a ``VERIFIED`` macro edge that is not part of an open conflict;
* ``conflict``    — a ``conflict`` flag in state ``FINAL`` (its VERIFIED edges go with it);
* ``entity_pair`` — an entity pair whose L5 row is ``ESCALATED`` (and no T3/human row since);
* ``logic``       — a ``nested_disagreement`` flag (its propositions and links).

Reason codes (``synth.states.GATE_REASONS``): ``FINAL_CONFLICT``, ``ER_MIDDLE_BAND``,
``GROUNDING_FUZZY_LOW`` (no span better than fuzzy < ``grounding.fuzzy_pass``),
``NESTED_DISAGREEMENT``, ``LOW_CONF_CRITICAL`` (confidence < ``gate.low_conf`` on a
critical relation), ``INFERRED_CRITICAL``, ``IDENTITY_UNRESOLVED`` (an end is in a
``POTENTIAL`` ``identity_ambiguity`` flag whose pairs have no settled L5 decision) and
``T2_INSUFFICIENT_CONTEXT``. No reason -> PASS: the edge stays ``VERIFIED`` for U11.
A VERIFIED edge without any span is ungroundable: it is REJECTED, never escalated.

Budget: at most ``floor(gate.escalate_max_share × gate-reaching items)`` escalate
(the target share is reported, not enforced), ranked by ``SEVERITY`` (below), then
by number of reasons, then lowest confidence. Overflow -> ``HELD_FOR_HUMAN``.
Sovereign guard: an item whose evidence comes from a sovereign document escalates
only when the ``t3`` endpoint is local; otherwise ``HELD_FOR_HUMAN``. Packets bound
for a non-local endpoint never contain sovereign text, names or edges.

Packet (≤ ``caps.u10_packet``): typed question, the item as compact rows with
prompt-local labels (``E1``, ``T1``, ``R1``, ``d1``), cited sentences ± 2 rendered
``[d1:s41]``, and the 1-hop subgraph (J1 ``get_edges``). Shrink order: drop the
subgraph -> ± 2 becomes ± 1 -> still too big: ``HELD_FOR_HUMAN`` + ``SKIPPED_BUDGET``.

Stores written: L7 ``escalations`` (packet, outcome ``PENDING`` or ``HELD_FOR_HUMAN``),
S5 macro edge status (``ESCALATED`` / ``HELD_FOR_HUMAN`` / ``REJECTED``, logged in L4),
L3 coverage (``ESCALATION_PENDING`` for cited sentences; ``HELD_FOR_HUMAN`` /
``SKIPPED_BUDGET`` for held ones), stage metrics. Token caps: ``caps.u10_packet``.
Spec: P1 §3 U9, §5, §7, §10, §11; CLAUDE.md invariants 6 and 10.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Iterable, Sequence

from synth.api import query as j1
from synth.ids import new_id
from synth.modules.common import StageContext, Stopwatch, emit_coverage, record_stage_metrics
from synth.modules.u04_whoswho import render_time
from synth.provenance.render import render_sentences
from synth.states import (
    APART, ESCALATED, ESCALATION_PENDING, FINAL, FLAG_CONFLICT, FLAG_IDENTITY, GATE,
    GATE_REASONS, HELD_FOR_HUMAN, MERGED, POTENTIAL, REJECTED, SKIPPED_BUDGET, VERIFIED,
)
from synth.store.repo import Tx

FLAG_NESTED = "nested_disagreement"
FLAG_T2_CONTEXT = "t2_insufficient_context"
OPEN_FLAG_STATES = (POTENTIAL, FINAL)
#: L5 ``decided_by`` values that close an ESCALATED pair.
FINAL_DECIDERS = ("T3", "human")

#: Severity order, most severe first. Structural damage to the graph first
#: (contradictions, identity), then logic, then doubts about single edges, with
#: weak grounding last (the evidence exists, it is just fuzzy).
SEVERITY = ("FINAL_CONFLICT", "ER_MIDDLE_BAND", "IDENTITY_UNRESOLVED", "NESTED_DISAGREEMENT",
            "T2_INSUFFICIENT_CONTEXT", "INFERRED_CRITICAL", "LOW_CONF_CRITICAL", "GROUNDING_FUZZY_LOW")
assert set(SEVERITY) == set(GATE_REASONS)

QUESTION_OF = {"edge": "CONFIRM_EDGE", "conflict": "CHOOSE_BETWEEN",
               "entity_pair": "SAME_ENTITY", "logic": "CHECK_LOGIC"}
QUESTION_TEXT = {
    "CONFIRM_EDGE": "Do the SOURCE sentences support row {rows}? Answer CONFIRM, REJECT, or CORRECT "
                    "(corrected_edge = the right edge).",
    "CHOOSE_BETWEEN": "Rows {rows} contradict each other (same source entity, a single-valued relation, "
                      "overlapping time). Which do the SOURCE sentences support? CONFIRM with corrected_edge = "
                      "the supported row (copy its src, rel, dst), CONFIRM with corrected_edge null if all hold, "
                      "REJECT if none holds, CORRECT with the right edge, or IRREDUCIBLE if the sources "
                      "genuinely disagree.",
    "SAME_ENTITY": "Are {rows} the same real-world entity? Answer MERGE or KEEP_APART. "
                   "The same name is not the same entity.",
    "CHECK_LOGIC": "Do the propositions and logic links {rows} follow from the SOURCE sentences? "
                   "CONFIRM if they do, REJECT if the links are wrong, IRREDUCIBLE if the text itself is ambiguous.",
}

#: Shrink order (spec U9): (window, subgraph, steps taken).
SHRINK_STEPS = ((2, True, ()), (2, False, ("subgraph",)), (1, False, ("subgraph", "window1")))
MAX_MENTIONS = 6          # mention sentences cited per entity in a SAME_ENTITY packet
MAX_ALIASES = 3


# ------------------------------------------------------------------ labels and rows
class Labeler:
    """Prompt-local labels for one packet: ``E`` entities, ``T`` times, ``d`` documents."""

    def __init__(self, entities: dict[str, str] | None = None, times: dict[str, str] | None = None,
                 documents: dict[str, str] | None = None) -> None:
        self.entities = dict(entities or {})
        self.times = dict(times or {})
        self.documents = dict(documents or {})
        self._e = {v: k for k, v in self.entities.items()}
        self._t = {v: k for k, v in self.times.items()}
        self._d = {v: k for k, v in self.documents.items()}

    @classmethod
    def from_packet(cls, packet: dict[str, Any]) -> "Labeler":
        return cls(packet.get("entities"), packet.get("times"), packet.get("documents"))

    @staticmethod
    def _label(rev: dict[str, str], fwd: dict[str, str], prefix: str, object_id: str) -> str:
        if object_id not in rev:
            label = f"{prefix}{len(fwd) + 1}"
            rev[object_id], fwd[label] = label, object_id
        return rev[object_id]

    def entity(self, entity_id: str) -> str:
        return self._label(self._e, self.entities, "E", entity_id)

    def time(self, time_id: str) -> str:
        return self._label(self._t, self.times, "T", time_id)

    def doc(self, doc_id: str) -> str:
        return self._label(self._d, self.documents, "d", doc_id)

    def doc_no(self, doc_id: str) -> int:
        return int(self.doc(doc_id)[1:])

    def sentence(self, doc_id: str, local_label: str) -> str:
        return f"{self.doc(doc_id)}:{local_label}"

    def dump(self) -> dict[str, dict[str, str]]:
        return {"entities": dict(self.entities), "times": dict(self.times),
                "documents": dict(self.documents)}


def fmt_conf(conf: float | None) -> str:
    if conf is None:
        return "—"
    text = f"{conf:.2f}"
    return text[1:] if text.startswith("0") else text


def _flat(text: str | None, limit: int = 0) -> str:
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if limit and len(flat) > limit else flat


def span_cites(spans: Iterable[dict[str, Any]], lab: Labeler) -> list[str]:
    return list(dict.fromkeys(lab.sentence(s["doc_id"], label) for s in spans
                              for label in (s.get("sentence_labels") or [])))


def edge_row(label: str, edge: dict[str, Any], lab: Labeler, *, with_status: bool = False) -> str:
    """``R1 E1 lent_to E2 | stated | occurred_at T1 | d1:s5 | .55 | "desc"`` (J1 edge dict)."""
    rel = ("NOT " if edge.get("negated") else "") + edge["rel"]
    if edge.get("subclass"):
        rel += f"/{edge['subclass']}"
    head = f"{label} {lab.entity(edge['src_entity_id'])} {rel} {lab.entity(edge['dst_entity_id'])}"
    times = ", ".join(f"{t['role']} {lab.time(t['time_id'])}" for t in edge.get("times") or []) or "—"
    bits = [head, edge.get("stance") or "stated", times,
            ", ".join(span_cites(edge.get("spans") or [], lab)) or "—", fmt_conf(edge.get("confidence"))]
    if edge.get("description"):
        bits.append('"' + _flat(edge["description"], 80) + '"')
    if with_status:
        bits.append(edge.get("status") or "")
    return " | ".join(bits)


def sovereign_docs(r: Tx) -> set[str]:
    return {d["doc_id"] for d in r.select("documents", sovereign=True)}


def entity_cards(r: Tx, entity_ids: Iterable[str], withheld: set[str]) -> dict[str, dict[str, Any]]:
    """Display name, kind and aliases per entity, hiding anything from ``withheld`` documents."""
    ids = list(dict.fromkeys(entity_ids))
    rows = {e["entity_id"]: e for e in j1.select_in(r, "entities", "entity_id", ids)}
    members = {eid: [eid] for eid in ids}
    for child in j1.select_in(r, "entities", "merged_into", ids):
        members[child["merged_into"]].append(child["entity_id"])
    owner = {m: eid for eid, ms in members.items() for m in ms}
    child_docs = {e["entity_id"]: e["doc_id"] for e in j1.select_in(r, "entities", "entity_id", owner)}
    aliases = j1.select_in(r, "entity_aliases", "entity_id", owner)
    mention_doc = {m["mention_id"]: m["doc_id"] for m in
                   j1.select_in(r, "mentions", "mention_id", [a["mention_id"] for a in aliases])}
    found: dict[str, list[str]] = {}
    for a in aliases:
        doc = mention_doc.get(a["mention_id"]) or child_docs.get(a["entity_id"])
        if doc not in withheld and a["alias"]:
            found.setdefault(owner[a["entity_id"]], []).append(_flat(a["alias"]))
    cards = {}
    for eid in ids:
        row = rows.get(eid) or {}
        alias_list = list(dict.fromkeys(found.get(eid, [])))
        name = row.get("canonical_name") or "?"
        if row.get("doc_id") in withheld:
            name = alias_list[0] if alias_list else "(withheld: sovereign document)"
        cards[eid] = dict(name=name, kind=row.get("subtype") or row.get("type") or "?",
                          aliases=[a for a in alias_list if a != name][:MAX_ALIASES],
                          doc_id=row.get("doc_id"))
    return cards


def entity_line(label: str, card: dict[str, Any]) -> str:
    line = f"{label} {card['name']} [{card['kind']}]"
    if card["aliases"]:
        line += " aka " + ",".join(f'"{a}"' for a in card["aliases"])
    return line


def definition_lines(r: Tx, lab: Labeler, *, entities: Sequence[str] = (), times: Sequence[str] = (),
                     docs: Sequence[str] = (), withheld: set[str] = frozenset()) -> list[str]:
    """``d1 = file (dated …)``, ``E1 Name [kind] aka …`` and ``T1 = …`` lines for the given labels."""
    lines = []
    if docs:
        rows = {d["doc_id"]: d for d in j1.select_in(r, "documents", "doc_id", [lab.documents[x] for x in docs])}
        for label in docs:
            d = rows.get(lab.documents[label], {})
            dated = f" (dated {d['doc_date']})" if d.get("doc_date") else ""
            lines.append(f"{label} = {d.get('filename') or '?'}{dated}")
    if entities:
        cards = entity_cards(r, [lab.entities[x] for x in entities], withheld)
        lines += [entity_line(label, cards[lab.entities[label]]) for label in entities]
    if times:
        refs = {t["time_id"]: t for t in j1.select_in(r, "time_refs", "time_id", [lab.times[x] for x in times])}
        lines += [render_time(label, refs.get(lab.times[label], {})) for label in times]
    return lines


def compose(packet: dict[str, Any], window: int | None = None, subgraph: bool | None = None,
            context: Sequence[dict[str, Any]] | None = None) -> str:
    """The user message: head + SOURCE (± window) + SUBGRAPH (if kept) + context answers."""
    parts = packet["parts"]
    window = packet["window"] if window is None else window
    subgraph = packet["subgraph"] if subgraph is None else subgraph
    blocks = [parts["head"], "SOURCE\n" + (parts["source"].get(str(window)) or "(none)")]
    if subgraph and parts.get("subgraph"):
        blocks.append(parts["subgraph"])
    for c in (packet.get("context") or []) if context is None else context:
        blocks.append(c["text"])
    return "\n\n".join(b for b in blocks if b)


# ------------------------------------------------------------------ gate items
@dataclass
class GateItem:
    item_type: str
    item_ids: list[str]
    reasons: list[str]
    edge_ids: list[str] = field(default_factory=list)      # macro edges whose status follows the item
    entity_ids: list[str] = field(default_factory=list)    # subgraph seeds
    span_ids: list[str] = field(default_factory=list)      # the item's own evidence
    doc_ids: set[str] = field(default_factory=set)
    conf: float | None = None
    flag_id: str | None = None
    decision_id: str | None = None
    sovereign: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def question_type(self) -> str:
        return QUESTION_OF[self.item_type]

    @property
    def severity(self) -> int:
        return min((SEVERITY.index(x) for x in self.reasons), default=len(SEVERITY))

    def rank_key(self) -> tuple:
        return (self.severity, -len(self.reasons), self.conf if self.conf is not None else 0.0,
                self.item_ids[0])


@dataclass
class Gate:
    items: list[GateItem]
    passes: list[str]
    ungrounded: list[str]

    @property
    def n(self) -> int:
        return len(self.items) + len(self.passes)


def _ordered_reasons(reasons: Iterable[str]) -> list[str]:
    return sorted(set(reasons), key=SEVERITY.index)


def collect_items(r: Tx, ctx: StageContext, sovereign: set[str] | None = None) -> Gate:
    """Every gate-reaching item of the run with its reason codes (no writes)."""
    run_id, cfg, onto = ctx.run_id, ctx.config, ctx.ontology
    sovereign = sovereign_docs(r) if sovereign is None else sovereign
    entities = {e["entity_id"]: e for e in r.select("entities", run_id=run_id)}
    merged = {k: e["merged_into"] for k, e in entities.items() if e.get("merged_into")}

    def S(eid: str) -> str:
        seen = {eid}
        while eid in merged and merged[eid] not in seen:
            eid = merged[eid]
            seen.add(eid)
        return eid

    flags = r.select("flags", order_by="flag_id", run_id=run_id)
    existing = {(e["item_type"], tuple(e["item_ids"] or [])) for e in r.select("escalations", run_id=run_id)}

    # ---- L5 pairs
    by_pair: dict[frozenset, list[dict[str, Any]]] = {}
    for row in r.select("resolutions", run_id=run_id):
        a, b = S(row["entity_a"]), S(row["entity_b"])
        if a != b:
            by_pair.setdefault(frozenset((a, b)), []).append(row)

    def pair_open(key: frozenset) -> bool:
        rows = by_pair.get(key, [])
        return (any(x["decision"] == ESCALATED for x in rows)
                and not any(x.get("decided_by") in FINAL_DECIDERS for x in rows))

    def pair_settled(a: str, b: str) -> bool:
        if S(a) == S(b):
            return True
        key = frozenset((S(a), S(b)))
        return any(x["decision"] in (MERGED, APART) for x in by_pair.get(key, [])) and not pair_open(key)

    # U8 moves identity flags to FINAL (settled apart) or DISMISSED (one entity);
    # only POTENTIAL ones are still open.
    unresolved: set[str] = set()
    for f in flags:
        if f["kind"] == FLAG_IDENTITY and f["state"] == POTENTIAL:
            ids = [x for x in dict.fromkeys(f.get("object_ids") or []) if x in entities]
            for a, b in combinations(ids, 2):
                if not pair_settled(a, b):
                    unresolved.update((S(a), S(b)))
    t2_context = {x for f in flags if f["kind"] == FLAG_T2_CONTEXT and f["state"] in OPEN_FLAG_STATES
                  for x in (f.get("object_ids") or [])}

    # ---- macro edges
    edges = r.select("macro_edges", order_by="edge_id", run_id=run_id, status=VERIFIED)
    details = {e["edge_id"]: e for e in j1.edge_details(r, "macro", edges)}
    sections = {s["section_id"]: s for s in
                j1.select_in(r, "sections", "section_id", [e["section_id"] for e in edges])}
    accept, pass_ = cfg.grounding.fuzzy_accept, cfg.grounding.fuzzy_pass
    _ = accept   # below accept the locator returns NOT_FOUND: such spans never exist

    def edge_reasons(e: dict[str, Any]) -> list[str]:
        reasons = []
        spans = e["spans"]
        if spans and all(s.get("fuzzy_score") is not None and s["fuzzy_score"] < pass_ for s in spans):
            reasons.append("GROUNDING_FUZZY_LOW")
        if onto.is_critical(e["rel"]):
            if e.get("confidence") is None or e["confidence"] < cfg.gate.low_conf:
                reasons.append("LOW_CONF_CRITICAL")
            if e.get("stance") == "inferred":
                reasons.append("INFERRED_CRITICAL")
        if {S(e["src_entity_id"]), S(e["dst_entity_id"])} & unresolved:
            reasons.append("IDENTITY_UNRESOLVED")
        if e["edge_id"] in t2_context or (e.get("section_id") and e["section_id"] in t2_context):
            reasons.append("T2_INSUFFICIENT_CONTEXT")
        return reasons

    def edge_docs(e: dict[str, Any]) -> set[str]:
        docs = {s["doc_id"] for s in e["spans"]}
        sec = sections.get(e.get("section_id"))
        if sec:
            docs.add(sec["doc_id"])
        return docs

    items: list[GateItem] = []
    in_conflict: set[str] = set()
    for f in flags:
        if f["kind"] != FLAG_CONFLICT or f["state"] != FINAL:
            continue
        live = [details[x] for x in f.get("object_ids") or [] if x in details]
        if not live:
            continue
        in_conflict.update(e["edge_id"] for e in live)
        ids = [f["flag_id"], *[e["edge_id"] for e in live]]
        if ("conflict", tuple(ids)) in existing:
            continue
        reasons = ["FINAL_CONFLICT"] + [x for e in live for x in edge_reasons(e)]
        docs = set().union(*(edge_docs(e) for e in live))
        items.append(GateItem(
            "conflict", ids, _ordered_reasons(reasons), edge_ids=[e["edge_id"] for e in live],
            entity_ids=list(dict.fromkeys(x for e in live for x in (e["src_entity_id"], e["dst_entity_id"]))),
            span_ids=[s["span_id"] for e in live for s in e["spans"]] + list(f.get("span_ids") or []),
            doc_ids=docs, conf=min((e["confidence"] for e in live if e.get("confidence") is not None),
                                   default=None),
            flag_id=f["flag_id"], sovereign=bool(docs & sovereign), detail={"note": f.get("note")}))

    passes, ungrounded = [], []
    for eid, e in details.items():
        if eid in in_conflict:
            continue
        if not e["spans"]:
            ungrounded.append(eid)
            continue
        reasons = edge_reasons(e)
        if not reasons:
            passes.append(eid)
            continue
        if ("edge", (eid,)) in existing:
            continue
        docs = edge_docs(e)
        items.append(GateItem("edge", [eid], _ordered_reasons(reasons), edge_ids=[eid],
                              entity_ids=[e["src_entity_id"], e["dst_entity_id"]],
                              span_ids=[s["span_id"] for s in e["spans"]], doc_ids=docs,
                              conf=e.get("confidence"), sovereign=bool(docs & sovereign)))

    # ---- escalated entity pairs
    for key, rows in sorted(by_pair.items(), key=lambda kv: sorted(kv[0])):
        if not pair_open(key):
            continue
        row = max((x for x in rows if x["decision"] == ESCALATED), key=lambda x: x["decision_id"])
        a, b = S(row["entity_a"]), S(row["entity_b"])
        if ("entity_pair", (a, b)) in existing or ("entity_pair", (b, a)) in existing:
            continue
        cards = {x: j1.get_entity(r, run_id, x) for x in (a, b)}
        docs = {entities[x]["doc_id"] for x in (a, b) if x in entities and entities[x].get("doc_id")}
        docs |= {m["doc_id"] for c in cards.values() if c for m in c["mentions"]}
        spans = [m["span_id"] for c in cards.values() if c for m in c["mentions"][:MAX_MENTIONS]
                 if m.get("span_id")]
        items.append(GateItem("entity_pair", [a, b], ["ER_MIDDLE_BAND"], entity_ids=[a, b],
                              span_ids=spans, doc_ids=docs, conf=row.get("score"),
                              decision_id=row["decision_id"],
                              sovereign=bool(docs & sovereign),
                              detail={"cards": cards, "features": row.get("features") or {},
                                      "score": row.get("score")}))

    # ---- nested disagreement (logic)
    for f in flags:
        if f["kind"] != FLAG_NESTED or f["state"] not in OPEN_FLAG_STATES:
            continue
        obj = list(f.get("object_ids") or [])
        links = j1.select_in(r, "prop_links", "link_id", obj)
        props = j1.select_in(r, "propositions", "prop_id",
                             obj + [x for lk in links for x in (lk["src_prop_id"], lk["dst_prop_id"])])
        if not links and not props:
            continue
        ids = [f["flag_id"], *obj]
        if ("logic", tuple(ids)) in existing:
            continue
        span_ids = [p["span_id"] for p in props if p.get("span_id")] + \
                   [lk["span_id"] for lk in links if lk.get("span_id")] + list(f.get("span_ids") or [])
        docs = {s["doc_id"] for s in j1.select_in(r, "spans", "span_id", span_ids)}
        docs |= {s["doc_id"] for s in j1.select_in(r, "sections", "section_id",
                                                   [p["section_id"] for p in props])}
        items.append(GateItem(
            "logic", ids, ["NESTED_DISAGREEMENT"],
            entity_ids=list(dict.fromkeys(S(p["speaker_entity_id"]) for p in props
                                          if p.get("speaker_entity_id"))),
            span_ids=span_ids, doc_ids=docs,
            conf=min((p["confidence"] for p in props if p.get("confidence") is not None), default=None),
            flag_id=f["flag_id"], sovereign=bool(docs & sovereign),
            detail={"props": props, "links": links}))
    return Gate(items=items, passes=passes, ungrounded=ungrounded)


# ------------------------------------------------------------------ packets
def _item_block(r: Tx, ctx: StageContext, item: GateItem, lab: Labeler,
                withheld: set[str]) -> tuple[list[str], list[tuple[str, str]], dict[str, dict[str, str]]]:
    """Item lines, cited ``(doc_id, sentence_id)`` pairs and the row-label maps."""
    lines: list[str] = []
    cited: list[tuple[str, str]] = []
    maps: dict[str, dict[str, str]] = {"rows": {}, "props": {}, "links": {}}

    def cite(spans: Iterable[dict[str, Any]]) -> None:
        for s in spans:
            if s["doc_id"] not in withheld:
                cited.extend((s["doc_id"], sid) for sid in s.get("sentence_ids") or [])

    if item.item_type in ("edge", "conflict"):
        rows = r.select("macro_edges", where=r.table("macro_edges").c.edge_id.in_(item.edge_ids))
        details = {e["edge_id"]: e for e in j1.edge_details(r, "macro", rows)}
        for i, eid in enumerate(item.edge_ids, 1):
            label = f"R{i}"
            maps["rows"][label] = eid
            lines.append(edge_row(label, details[eid], lab))
            cite(details[eid]["spans"])
        if item.item_type == "conflict" and item.detail.get("note"):
            lines.append(f"conflict note: {_flat(item.detail['note'], 200)}")
    elif item.item_type == "entity_pair":
        labels = [lab.entity(x) for x in item.entity_ids]
        lines.append(f"{labels[0]} vs {labels[1]} — resolver score {fmt_conf(item.detail.get('score'))}")
        for eid, label in zip(item.entity_ids, labels):
            card = item.detail["cards"].get(eid) or {}
            bits = []
            row = card.get("entity") or {}
            if row.get("doc_id") and row["doc_id"] not in withheld:
                bits.append(f"built in {lab.doc(row['doc_id'])}")
            mentions = [m["span"] for m in card.get("mentions", [])
                        if m.get("span") and m["span"]["doc_id"] not in withheld][:MAX_MENTIONS]
            cite(mentions)
            if mentions:
                bits.append("mentions " + ", ".join(span_cites(mentions, lab)))
            roles = [x for x in card.get("roles", [])
                     if not (x.get("span") and x["span"]["doc_id"] in withheld)][:4]
            if roles:
                bits.append("roles " + ", ".join(
                    x["role"] + (f"@{lab.entity(x['org_entity_id'])}" if x.get("org_entity_id") else "")
                    for x in roles))
            lines.append(f"{label}: " + (" · ".join(bits) or "no mentions in shareable documents"))
        feats = item.detail.get("features") or {}
        if feats:
            lines.append("features: " + ", ".join(
                f"{k}={v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else f"{k}={v}"
                for k, v in sorted(feats.items()))[:400])
    else:   # logic
        props, links = item.detail["props"], item.detail["links"]
        spans = j1.spans_by_id(r, [p.get("span_id") for p in props] + [lk.get("span_id") for lk in links])
        for i, p in enumerate(sorted(props, key=lambda p: p["prop_id"]), 1):
            label = f"P{i}"
            maps["props"][label] = p["prop_id"]
            span = spans.get(p.get("span_id"))
            bits = [f'{label} "{_flat(p["text"], 200)}"', p.get("status") or "?"]
            if p.get("speaker_entity_id"):
                bits.append(f"speaker {lab.entity(p['speaker_entity_id'])}")
            if p.get("negated"):
                bits.append("negated")
            bits += [", ".join(span_cites([span], lab)) if span else "—", fmt_conf(p.get("confidence"))]
            lines.append(" | ".join(bits))
            if span:
                cite([span])
        plabel = {v: k for k, v in maps["props"].items()}
        for i, lk in enumerate(sorted(links, key=lambda lk: lk["link_id"]), 1):
            label = f"L{i}"
            maps["links"][label] = lk["link_id"]
            span = spans.get(lk.get("span_id"))
            lines.append(f"{label} {plabel.get(lk['src_prop_id'], '?')} {lk['type']} "
                         f"{plabel.get(lk['dst_prop_id'], '?')} | "
                         f"{', '.join(span_cites([span], lab)) if span else '—'}")
            if span:
                cite([span])
    return lines, list(dict.fromkeys(cited)), maps


def _source_blocks(r: Tx, run_id: str, cited: Sequence[tuple[str, str]],
                   lab: Labeler) -> tuple[dict[str, str], dict[str, list[str]]]:
    by_doc: dict[str, list[str]] = {}
    for doc_id, sid in cited:
        by_doc.setdefault(doc_id, []).append(sid)
    texts, shown = {}, {}
    for window in (2, 1):
        blocks, ids = [], []
        for doc_id in sorted(by_doc, key=lab.doc_no):
            answer = j1.get_sentences(r, doc_id, by_doc[doc_id], n=window, run_id=run_id)
            rows = [s for s in answer["sentences"] if not s["filtered"] or s["cited"]]
            blocks.append(render_sentences([(s["idx"], s["text"]) for s in rows], lab.doc_no(doc_id)))
            ids += [s["sentence_id"] for s in rows]
        texts[str(window)], shown[str(window)] = "\n".join(b for b in blocks if b), ids
    return texts, shown


def _subgraph_block(r: Tx, ctx: StageContext, item: GateItem, lab: Labeler,
                    withheld: set[str]) -> tuple[str, dict[str, str]]:
    before_e, before_t, before_d = set(lab.entities), set(lab.times), set(lab.documents)
    seen = set(item.edge_ids)
    edges: list[dict[str, Any]] = []
    for seed in item.entity_ids:
        for e in j1.get_edges(r, ctx.run_id, seed, level="macro")["edges"]:
            if e["edge_id"] in seen or e["status"] == REJECTED:
                continue
            seen.add(e["edge_id"])
            docs = {s["doc_id"] for s in e["spans"]}
            if docs & withheld or not docs:
                continue
            ends = j1.select_in(r, "entities", "entity_id", [e["src_entity_id"], e["dst_entity_id"]])
            if any(x.get("doc_id") in withheld and x["entity_id"] not in lab._e for x in ends):
                continue
            edges.append(e)
    if not edges:
        return "", {}
    rows, lines = {}, []
    for i, e in enumerate(sorted(edges, key=lambda e: e["edge_id"]), 1):
        rows[f"G{i}"] = e["edge_id"]
        lines.append(edge_row(f"G{i}", e, lab, with_status=True))
    defs = definition_lines(r, lab, docs=[d for d in lab.documents if d not in before_d],
                            entities=[x for x in lab.entities if x not in before_e],
                            times=[x for x in lab.times if x not in before_t], withheld=withheld)
    return "\n".join(["SUBGRAPH (1-hop, macro graph; orientation only — cite SOURCE sentences)",
                      *defs, *lines]), rows


def build_packet(r: Tx, ctx: StageContext, item: GateItem, *, withheld: set[str],
                 cap: int | None = None) -> dict[str, Any]:
    """The packet for one item, shrunk in the declared order until it fits ``cap``."""
    cap = ctx.config.cap("u10_packet") if cap is None else cap
    lab = Labeler()
    item_lines, cited, maps = _item_block(r, ctx, item, lab, withheld)
    source, shown = _source_blocks(r, ctx.run_id, cited, lab)
    head_e, head_t, head_d = list(lab.entities), list(lab.times), list(lab.documents)
    subgraph, sub_rows = _subgraph_block(r, ctx, item, lab, withheld)
    labels = ", ".join(maps["rows"] or maps["props"] or [lab._e[x] for x in item.entity_ids])
    if maps["links"]:
        labels += ", " + ", ".join(maps["links"])
    question = QUESTION_TEXT[item.question_type].format(rows=labels)
    head = "\n".join([
        f"QUESTION ({item.question_type}): {question}",
        "GATE REASONS: " + ", ".join(item.reasons),
        "DOCUMENTS", *definition_lines(r, lab, docs=head_d),
        "ENTITIES", *definition_lines(r, lab, entities=head_e, withheld=withheld),
        *(["TIMES", *definition_lines(r, lab, times=head_t)] if head_t else []),
        "ITEM", *item_lines,
    ])
    packet: dict[str, Any] = dict(
        version=1, question_type=item.question_type, question=question, item_type=item.item_type,
        item_ids=item.item_ids, reasons=item.reasons, sovereign=item.sovereign,
        flag_id=item.flag_id, decision_id=item.decision_id, conf=item.conf,
        **lab.dump(), rows=maps["rows"], props=maps["props"], links=maps["links"],
        subgraph_rows=sub_rows, cited_sentence_ids=list(dict.fromkeys(s for _, s in cited)),
        span_ids=list(dict.fromkeys(item.span_ids)), sentence_ids=shown,
        parts={"head": head, "source": source, "subgraph": subgraph}, context=[], cap=cap)
    for window, keep_subgraph, steps in SHRINK_STEPS:
        tokens = ctx.gateway.count(compose(packet, window, keep_subgraph), "t3")
        packet.update(window=window, subgraph=keep_subgraph and bool(subgraph), shrink=list(steps),
                      tokens=tokens, fits=tokens <= cap)
        if packet["fits"]:
            break
    packet["text"] = compose(packet)
    return packet


def shown_sentence_ids(packet: dict[str, Any]) -> list[str]:
    ids = list(packet["sentence_ids"].get(str(packet["window"]), []))
    for c in packet.get("context") or []:
        ids += c.get("sentence_ids") or []
    return list(dict.fromkeys(ids))


# ------------------------------------------------------------------ the stage
def _move_edge(tx: Tx, edge_id: str, status: str, rule_id: str) -> bool:
    edge = tx.get("macro_edges", edge_id=edge_id)
    if edge is None or edge["status"] != VERIFIED:
        return False
    tx.update("macro_edges", {"edge_id": edge_id}, {"status": status}, rule_id=rule_id)
    return True


def run(ctx: StageContext) -> dict[str, Any]:
    """Gate the whole run: escalate within budget, hold the rest, leave PASS items VERIFIED."""
    sw = Stopwatch()
    cap = ctx.config.cap("u10_packet")
    t3_local = ctx.config.endpoint("t3").is_local()
    summary = dict(gate_items=0, passed=0, escalated=0, held=0, held_sovereign=0, held_overflow=0,
                   held_over_budget=0, rejected_ungrounded=0, budget=0, escalation_share=0.0,
                   target_share=ctx.config.gate.escalate_target_share)
    with ctx.tx(GATE, "U9") as tx:
        sovereign = sovereign_docs(tx)
        gate = collect_items(tx, ctx, sovereign)
        budget = int(math.floor(ctx.config.gate.escalate_max_share * gate.n + 1e-9))
        summary.update(gate_items=gate.n, passed=len(gate.passes), budget=budget)
        for edge_id in gate.ungrounded:
            if _move_edge(tx, edge_id, REJECTED, "ungrounded"):
                summary["rejected_ungrounded"] += 1
        for item in sorted(gate.items, key=GateItem.rank_key):
            held = None
            if item.sovereign and not t3_local:
                held = "sovereign"
            elif summary["escalated"] >= budget:
                held = "overflow"
            withheld = set() if (t3_local or item.sovereign) else sovereign
            packet = build_packet(tx, ctx, item, withheld=withheld, cap=cap)
            if held is None and not packet["fits"]:
                held = "over_budget"
            packet["held_reason"] = held
            esc_id = new_id("esc")
            tx.insert("escalations", dict(
                esc_id=esc_id, item_type=item.item_type, item_ids=item.item_ids, reasons=item.reasons,
                question_type=item.question_type, packet=packet, packet_tokens=packet["tokens"],
                context_asks=0, tokens_in=0, tokens_out=0,
                outcome="PENDING" if held is None else HELD_FOR_HUMAN))
            status = ESCALATED if held is None else HELD_FOR_HUMAN
            rule = f"gate_escalate:{esc_id}" if held is None else f"gate_held_{held}:{esc_id}"
            for edge_id in item.edge_ids:
                _move_edge(tx, edge_id, status, rule)
            cited = packet["cited_sentence_ids"]
            if held is None:
                summary["escalated"] += 1
                emit_coverage(tx, cited, GATE, ESCALATION_PENDING, esc_id)
            else:
                summary["held"] += 1
                summary[f"held_{held}"] += 1
                if held == "over_budget":
                    emit_coverage(tx, cited, GATE, SKIPPED_BUDGET, esc_id)
                emit_coverage(tx, cited, GATE, HELD_FOR_HUMAN, esc_id)
        summary["escalation_share"] = summary["escalated"] / gate.n if gate.n else 0.0
        record_stage_metrics(tx, GATE, ctx.run_id, items_in=gate.n, items_out=summary["escalated"],
                             wall_ms=sw.ms)
    return summary
