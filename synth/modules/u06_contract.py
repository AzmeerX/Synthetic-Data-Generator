"""U6 — Merge & Contract (deterministic, no model).

Purpose: turn one document's ``CANDIDATE`` micro edges (every chunk; U5 and
GLiNER candidates alike) into the smaller, cleaner set T2 checks: relation
names canonicalized (ontology aliases), identical edges ``(src, rel, dst,
negated, stance)`` folded into one kept edge carrying every evidence span,
self-loops and type-constraint violations rejected, a confidence floor, and
rule flags for what looks inconsistent. Rejected edges are kept (status
``REJECTED`` + ``reject_reason``); their sentences still go to T2.

Contract:

* :func:`run_document` ``(ctx, doc_id) -> dict`` — summary counts plus ``E`` and ``C``.
* :func:`render_compact_rows` ``(r, run_id, edge_ids, labeler) -> (text, {"M1": med_id, ...})``
  — one line per edge, exactly ``E3 director_of E7 | stated | as_of T4 | s12 | .82        (row id M1)``
  (``NOT director_of`` when negated; ``   !IDENTITY? E3`` when the edge touches an entity in an
  open ``identity_ambiguity`` flag); order = first evidence character offset. U7 and U9 use it.
* :class:`Labeler` — prompt-local labels (``E3``/``T4``/``s12``) <-> store ids, from a U4 ``WhosWho``.
* :func:`fold_key`, :func:`amount_signature` — the dedupe code the arms share (P1 §1).

Rule flags (all ``POTENTIAL``, raised by ``U6``): a functional relation with two different targets
from one source -> ``inconsistency``; one name key with different roles -> ``identity_ambiguity``;
same source + relation + target with different amounts -> ``inconsistency``. Edges whose amounts
differ are never folded into each other (folding would hide the clash).

Stores written: ``micro_edges`` (status, canonical ``rel``, ``reject_reason`` via ``Tx.update`` -> L4),
``edge_spans`` / ``edge_times`` of kept edges (folded evidence), ``flags`` (L6), ``stage_metrics``
(stage ``CONTRACT``: source, candidate and contracted tokens; E = candidate ÷ source,
C = contracted ÷ candidate, used by the section-size formula S = (B − O) ÷ (1 + E × C)).
Token caps: none (no model call). Spec: P1 §3 U6, §10, §11.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from synth import states as S
from synth.modules.common import StageContext, Stopwatch, raise_flag, record_stage_metrics
from synth.modules.u04_whoswho import WhosWho, build_index
from synth.store.repo import Tx

MODULE = "U6"
ROW_GAP = " " * 8
IDENTITY_GAP = " " * 3
OPEN_FLAG_STATES = (S.POTENTIAL, S.FINAL)
_LABEL = re.compile(r"^\s*([ETet])(\d+)\b")
_TIME_ORDER = {role: i for i, role in enumerate(S.TIME_ROLES)}


# ------------------------------------------------------------------ labels
@dataclass
class Labeler:
    """Prompt-local labels. Unknown entities/times get a fresh label on first use."""

    entity_labels: dict[str, str] = field(default_factory=dict)    # "E3" -> ent_…
    time_labels: dict[str, str] = field(default_factory=dict)      # "T4" -> tim_…
    sentence_labels: dict[str, str] = field(default_factory=dict)  # snt_… -> "s12"

    def __post_init__(self) -> None:
        self._entity_of = {v: k for k, v in self.entity_labels.items()}
        self._time_of = {v: k for k, v in self.time_labels.items()}
        self._sentence_of_label = {v: k for k, v in self.sentence_labels.items()}

    @classmethod
    def from_whoswho(cls, ww: WhosWho, doc) -> "Labeler":
        return cls(dict(ww.entity_labels), dict(ww.time_labels),
                   {s["sentence_id"]: f"s{s['idx']}" for s in doc.sentences})

    @classmethod
    def for_document(cls, r: Tx, doc, run_id: str) -> "Labeler":
        """Every entity and time ref of the document (no cap) — for measures."""
        ww = build_index(r, doc, doc.sentences, run_id, cap=10 ** 9, time_cap=10 ** 9)
        return cls.from_whoswho(ww, doc)

    @staticmethod
    def _next(labels: dict[str, str]) -> int:
        nums = [int(k[1:]) for k in labels if k[1:].isdigit()]
        return max(nums, default=0) + 1

    def entity(self, entity_id: str | None) -> str:
        if not entity_id:
            return "?"
        label = self._entity_of.get(entity_id)
        if label is None:
            label = f"E{self._next(self.entity_labels)}"
            self.entity_labels[label] = entity_id
            self._entity_of[entity_id] = label
        return label

    def time(self, time_id: str) -> str:
        label = self._time_of.get(time_id)
        if label is None:
            label = f"T{self._next(self.time_labels)}"
            self.time_labels[label] = time_id
            self._time_of[time_id] = label
        return label

    def sentence(self, sentence_id: str) -> str:
        return self.sentence_labels.get(sentence_id, sentence_id)

    def entity_id(self, label: str | None) -> str | None:
        m = _LABEL.match(label or "")
        return self.entity_labels.get(f"E{m.group(2)}") if m and m.group(1).upper() == "E" else None

    def time_id(self, label: str | None) -> str | None:
        m = _LABEL.match(label or "")
        return self.time_labels.get(f"T{m.group(2)}") if m and m.group(1).upper() == "T" else None

    def sentence_id(self, label: str) -> str | None:
        return self._sentence_of_label.get((label or "").strip().split(":")[-1])


# ------------------------------------------------------------------ shared dedupe pieces
def fold_key(src: str, rel: str, dst: str, negated: bool | None, stance: str | None) -> tuple:
    return (src, rel, dst, bool(negated), stance or "stated")


_CUR = r"GBP|USD|EUR|PKR|INR|AED|CHF|JPY|Rs\.?|£|\$|€"
_SCALE = r"million|mn|m|billion|bn|thousand|k|crore|lakh|per\s+cent|percent|%"
_AMOUNT = re.compile(rf"(?P<cur>{_CUR})?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<scale>{_SCALE})?(?![A-Za-z])",
                     re.IGNORECASE)
_MULT = {"million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "thousand": 1e3, "k": 1e3,
         "crore": 1e7, "lakh": 1e5}
_CUR_NAME = {"£": "GBP", "$": "USD", "€": "EUR", "rs": "PKR", "rs.": "PKR"}


def amount_signature(*texts: str | None) -> frozenset:
    """Amounts named in ``texts`` (a currency or a scale word is required, so dates
    and section numbers never count): ``"GBP 4.5 million"`` -> ``{("GBP", 4500000.0)}``."""
    found = set()
    for text in texts:
        for m in _AMOUNT.finditer(text or ""):
            cur, scale = m.group("cur"), (m.group("scale") or "").lower()
            if not cur and not scale:
                continue
            value = float(m.group("num").replace(",", ""))
            if scale in ("%", "percent") or scale.startswith("per"):
                unit = "%"
            else:
                value *= _MULT.get(scale, 1.0)
                unit = _CUR_NAME.get((cur or "").lower(), (cur or "").upper())
            found.add((unit, round(value, 6)))
    return frozenset(found)


# ------------------------------------------------------------------ rows
def edge_spans_of(r: Tx, edge_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    if not edge_ids:
        return {}
    links = r.select("edge_spans", where=r.table("edge_spans").c.edge_id.in_(list(edge_ids)))
    span_ids = list({lk["span_id"] for lk in links})
    spans = {s["span_id"]: s for s in r.select("spans", where=r.table("spans").c.span_id.in_(span_ids))} \
        if span_ids else {}
    out: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        span = spans.get(link["span_id"])
        if span and all(s["span_id"] != span["span_id"] for s in out.get(link["edge_id"], [])):
            out.setdefault(link["edge_id"], []).append(span)
    for spans_ in out.values():
        spans_.sort(key=lambda s: (s["char_start"], s["span_id"]))
    return out


def edge_times_of(r: Tx, edge_ids: Sequence[str]) -> dict[str, list[tuple[str, str]]]:
    if not edge_ids:
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for t in r.select("edge_times", where=r.table("edge_times").c.edge_id.in_(list(edge_ids))):
        pair = (t["role"], t["time_id"])
        if pair not in out.setdefault(t["edge_id"], []):
            out[t["edge_id"]].append(pair)
    for pairs in out.values():
        pairs.sort(key=lambda p: (_TIME_ORDER.get(p[0], 99), p[1]))
    return out


def load_row_edges(r: Tx, run_id: str, edge_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Edges (micro ``med_`` or macro ``mad_``) with spans, sentence ids and times,
    sorted by first evidence character offset."""
    ids = list(dict.fromkeys(edge_ids))
    if not ids:
        return []
    rows: list[dict[str, Any]] = []
    for table, prefix in (("micro_edges", "med_"), ("macro_edges", "mad_")):
        group = [i for i in ids if i.startswith(prefix)]
        if group:
            rows += r.select(table, where=r.table(table).c.edge_id.in_(group))
    spans = edge_spans_of(r, ids)
    times = edge_times_of(r, ids)
    out = []
    for row in rows:
        sp = spans.get(row["edge_id"], [])
        sentence_ids: list[str] = []
        for s in sp:
            for sid in s.get("sentence_ids") or []:
                if sid not in sentence_ids:
                    sentence_ids.append(sid)
        out.append({**row, "spans": sp, "span_ids": [s["span_id"] for s in sp],
                    "sentence_ids": sentence_ids, "times": times.get(row["edge_id"], []),
                    "first_offset": sp[0]["char_start"] if sp else 10 ** 12})
    out.sort(key=lambda e: (e["first_offset"], e["edge_id"]))
    return out


def identity_entities(r: Tx, run_id: str) -> set[str]:
    """Entities named in an open ``identity_ambiguity`` flag of this run."""
    out: set[str] = set()
    for f in r.select("flags", run_id=run_id, kind=S.FLAG_IDENTITY):
        if f["state"] in OPEN_FLAG_STATES:
            out.update(f["object_ids"] or [])
    return out


def _conf(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1:
        return "1.0"
    return f"{max(value, 0.0):.2f}"[1:]


def format_row(src: str, rel: str, dst: str, *, negated: bool, stance: str | None,
               times: Iterable[tuple[str, str]], sentence_ids: Iterable[str], confidence: float | None,
               labeler: Labeler, row_id: str, identity: set[str] | frozenset = frozenset()) -> str:
    rel_text = f"NOT {rel}" if negated else rel
    time_text = ", ".join(f"{role} {labeler.time(tid)}" for role, tid in times) or "—"
    sent_text = ",".join(labeler.sentence(s) for s in sentence_ids) or "—"
    line = (f"{labeler.entity(src)} {rel_text} {labeler.entity(dst)} | {stance or 'stated'} | "
            f"{time_text} | {sent_text} | {_conf(confidence)}{ROW_GAP}(row id {row_id})")
    flagged = list(dict.fromkeys(labeler.entity(e) for e in (src, dst) if e in identity))
    if flagged:
        line += f"{IDENTITY_GAP}!IDENTITY? {' '.join(flagged)}"
    return line


def render_compact_rows(r: Tx, run_id: str, edge_ids: Sequence[str],
                        labeler: Labeler) -> tuple[str, dict[str, str]]:
    """Compact rows for ``edge_ids`` with prompt-local row ids ``M1…`` (P1 §3 U6)."""
    identity = identity_entities(r, run_id)
    lines, mapping = [], {}
    for i, e in enumerate(load_row_edges(r, run_id, edge_ids), start=1):
        row_id = f"M{i}"
        lines.append(format_row(e["src_entity_id"], e["rel"], e["dst_entity_id"],
                                negated=bool(e["negated"]), stance=e["stance"], times=e["times"],
                                sentence_ids=e["sentence_ids"], confidence=e["confidence"],
                                labeler=labeler, row_id=row_id, identity=identity))
        mapping[row_id] = e["edge_id"]
    return "\n".join(lines), mapping


# ------------------------------------------------------------------ U6
def _class(ontology, entity: dict[str, Any] | None) -> str | None:
    if entity is None:
        return None
    return ontology.class_of(entity.get("type")) or ontology.class_of(entity.get("subtype"))


def _candidate_json(e: dict[str, Any], labeler: Labeler) -> dict[str, Any]:
    """A candidate edge in the U5 output shape (for candidate-token measurement)."""
    first = e["spans"][0] if e["spans"] else {}
    return {"src": labeler.entity(e["src_entity_id"]), "rel": e["rel"], "dst": labeler.entity(e["dst_entity_id"]),
            "subclass": e["subclass"], "desc": e["description"] or "", "stance": e["stance"] or "stated",
            "negated": bool(e["negated"]), "time": [{"role": ro, "ref": labeler.time(t)} for ro, t in e["times"]],
            "conf": e["confidence"],
            "evidence": {"sentence_ids": [labeler.sentence(s) for s in e["sentence_ids"]],
                         "quote": first.get("quote", "")}}


def _partition_by_amount(members: list[dict[str, Any]], sig) -> list[list[dict[str, Any]]]:
    parts: list[tuple[frozenset, list]] = []
    for m in members:
        s = sig(m)
        for i, (psig, plist) in enumerate(parts):
            if not s or not psig or s == psig:
                plist.append(m)
                if s and not psig:
                    parts[i] = (s, plist)
                break
        else:
            parts.append((s, [m]))
    return [p for _, p in parts]


def run_document(ctx: StageContext, doc_id: str) -> dict[str, Any]:
    """Contract one document's candidate micro edges (all chunks)."""
    sw = Stopwatch()
    onto = ctx.ontology
    floor = float((ctx.config.contract or {}).get("confidence_floor", 0.35))
    doc = ctx.doc(doc_id)
    with ctx.reader() as r:
        if r.select("stage_metrics", run_id=ctx.run_id, stage=S.CONTRACT, unit_id=doc_id):
            return {"doc_id": doc_id, "skipped": True}
        chunks = r.select("micro_chunks", order_by="idx", doc_id=doc_id)
        chunk_ids = {c["chunk_id"] for c in chunks}
        source_tokens = sum(int(c["source_tokens"] or 0) for c in chunks)
        entities = {e["entity_id"]: e for e in r.select("entities", run_id=ctx.run_id, doc_id=doc_id)}
        rows = [e for e in r.select("micro_edges", run_id=ctx.run_id, status=S.CANDIDATE)
                if e["chunk_id"] in chunk_ids or (e["chunk_id"] is None and e["src_entity_id"] in entities)]
        cands = load_row_edges(r, ctx.run_id, [e["edge_id"] for e in rows])
        labeler = Labeler.for_document(r, doc, ctx.run_id)
        roles = [x for x in r.select("entity_roles", run_id=ctx.run_id) if x["entity_id"] in entities]
        known_identity = [set(f["object_ids"] or []) for f in r.select("flags", run_id=ctx.run_id,
                                                                         kind=S.FLAG_IDENTITY)]
        mention_spans: dict[str, list[str]] = {}
        for m in r.select("mentions", run_id=ctx.run_id, doc_id=doc_id):
            if m["entity_id"] and m["span_id"]:
                mention_spans.setdefault(m["entity_id"], []).append(m["span_id"])

    candidate_text = json.dumps([_candidate_json(e, labeler) for e in cands], ensure_ascii=False)
    candidate_tokens = ctx.gateway.count(candidate_text, "t2") if cands else 0

    # -- 1. canonicalize, reject self-loops / type violations -----------------------------
    decisions: dict[str, dict[str, Any]] = {}
    valid: list[dict[str, Any]] = []
    for e in cands:
        rel = onto.canonical_relation(e["rel"])
        change: dict[str, Any] = {"rel": rel} if rel and rel != e["rel"] else {}
        src, dst = entities.get(e["src_entity_id"]), entities.get(e["dst_entity_id"])
        reason = None
        if rel is None:
            reason = "unknown_relation"
        elif not e["span_ids"]:
            reason = "no_evidence"
        elif src is None or dst is None:
            reason = "unknown_entity"
        elif e["src_entity_id"] == e["dst_entity_id"]:
            reason = "self_loop"
        elif not onto.relation(rel).allows(_class(onto, src), _class(onto, dst)):
            reason = "type_violation"
        e["_rel"] = rel or e["rel"]
        if reason:
            decisions[e["edge_id"]] = {**change, "status": S.REJECTED, "reject_reason": reason}
        else:
            decisions[e["edge_id"]] = change
            valid.append(e)

    # -- 2. fold identical edges (never across different amounts) --------------------------
    def sig(e: dict[str, Any]) -> frozenset:
        dst = entities.get(e["dst_entity_id"]) or {}
        dst_name = dst.get("canonical_name") if _class(onto, dst) == "value" else None
        return amount_signature(e["description"], dst_name)

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for e in valid:
        groups.setdefault(fold_key(e["src_entity_id"], e["_rel"], e["dst_entity_id"], e["negated"],
                                   e["stance"]), []).append(e)
    kept: list[dict[str, Any]] = []
    extra_spans: list[tuple[str, str]] = []
    extra_times: list[tuple[str, str, str]] = []
    folded = 0
    for members in groups.values():
        for part in _partition_by_amount(members, sig):
            order = {id(m): i for i, m in enumerate(part)}
            keeper = sorted(part, key=lambda m: (-(m["confidence"] or 0.0), order[id(m)]))[0]
            have_spans, have_times = set(keeper["span_ids"]), set(keeper["times"])
            for other in part:
                if other is keeper:
                    continue
                folded += 1
                decisions[other["edge_id"]] = {**decisions[other["edge_id"]], "status": S.REJECTED,
                                               "reject_reason": f"duplicate_of:{keeper['edge_id']}"}
                for span_id in other["span_ids"]:
                    if span_id not in have_spans:
                        have_spans.add(span_id)
                        extra_spans.append((keeper["edge_id"], span_id))
                for pair in other["times"]:
                    if pair not in have_times:
                        have_times.add(pair)
                        extra_times.append((keeper["edge_id"], *pair))
            kept.append(keeper)

    # -- 3. confidence floor -------------------------------------------------------------------
    contracted: list[dict[str, Any]] = []
    for e in kept:
        if (e["confidence"] or 0.0) < floor:
            decisions[e["edge_id"]] = {**decisions[e["edge_id"]], "status": S.REJECTED,
                                       "reject_reason": "below_floor"}
        else:
            decisions[e["edge_id"]] = {**decisions[e["edge_id"]], "status": S.CONTRACTED}
            contracted.append(e)

    # -- 4. rule flags ---------------------------------------------------------------------------
    flags: list[tuple[str, list[str], list[str], str, str]] = []   # kind, objects, spans, note, rule
    spans_of = {e["edge_id"]: list(e["span_ids"]) for e in cands}
    for span_edge, span_id in extra_spans:
        spans_of[span_edge].append(span_id)

    def edge_spans(ids: list[str]) -> list[str]:
        return list(dict.fromkeys(s for i in ids for s in spans_of.get(i, [])))

    functional: dict[tuple, list[dict[str, Any]]] = {}
    valued: dict[tuple, list[dict[str, Any]]] = {}
    for e in contracted:
        if e["negated"]:
            continue
        if onto.is_functional(e["_rel"]):
            functional.setdefault((e["src_entity_id"], e["_rel"]), []).append(e)
        dst = entities.get(e["dst_entity_id"]) or {}
        target = "<value>" if _class(onto, dst) == "value" else e["dst_entity_id"]
        valued.setdefault((e["src_entity_id"], e["_rel"], target), []).append(e)
    for (src, rel), members in functional.items():
        if len({m["dst_entity_id"] for m in members}) >= 2:
            ids = [m["edge_id"] for m in members]
            flags.append((S.FLAG_INCONSISTENCY, ids, edge_spans(ids), f"functional:{rel}", "u6_functional"))
    for (src, rel, _t), members in valued.items():
        with_amount = [m for m in members if sig(m)]
        if len({sig(m) for m in with_amount}) >= 2:
            ids = [m["edge_id"] for m in with_amount]
            flags.append((S.FLAG_INCONSISTENCY, ids, edge_spans(ids), f"value:{rel}", "u6_value"))

    by_key: dict[tuple, list[str]] = {}
    for eid, ent in entities.items():
        if ent["status"] == S.ACTIVE and ent["norm_key"]:
            by_key.setdefault((ent["norm_key"], _class(onto, ent)), []).append(eid)
    for (key, _cls), group in by_key.items():
        group_roles = [x for x in roles if x["entity_id"] in group]
        pairs = {((x["role"] or "").strip().casefold(), x["org_entity_id"]) for x in group_roles}
        orgs = {org for _, org in pairs if org}
        if len(pairs) < 2 or not (len(group) >= 2 or len(orgs) >= 2):
            continue
        if any(set(group) <= known for known in known_identity):
            continue
        span_ids = [x["span_id"] for x in group_roles if x["span_id"]] or \
            [mention_spans[e][0] for e in group if mention_spans.get(e)]
        flags.append((S.FLAG_IDENTITY, sorted(group), list(dict.fromkeys(span_ids)),
                      f"same key {key!r}, different roles", "u6_identity"))

    # -- 5. write (one transaction) ------------------------------------------------------------
    rejected: dict[str, int] = {}
    with ctx.tx(S.CONTRACT, MODULE) as tx:
        for e in cands:
            d = decisions[e["edge_id"]]
            reason = d.get("reject_reason")
            if reason:
                bucket = reason.split(":")[0]
                rejected[bucket] = rejected.get(bucket, 0) + 1
            rule = f"u6_{reason.split(':')[0]}" if reason else "u6_contract"
            tx.update("micro_edges", {"edge_id": e["edge_id"]}, d, rule_id=rule)
        for edge_id, span_id in extra_spans:
            tx.insert("edge_spans", dict(edge_id=edge_id, span_id=span_id))
        for edge_id, role, time_id in extra_times:
            tx.insert("edge_times", dict(edge_id=edge_id, time_id=time_id, role=role))
        flag_ids = [raise_flag(tx, kind, objs, MODULE, span_ids=spans, note=note, rule_id=rule)
                    for kind, objs, spans, note, rule in flags]
        rows_text, _ = render_compact_rows(tx, ctx.run_id, [e["edge_id"] for e in contracted], labeler)
        contracted_tokens = ctx.gateway.count(rows_text, "t2") if rows_text else 0
        record_stage_metrics(tx, S.CONTRACT, doc_id, source_tokens=source_tokens,
                             candidate_tokens=candidate_tokens, contracted_tokens=contracted_tokens,
                             items_in=len(cands), items_out=len(contracted), wall_ms=sw.ms)
    return {"doc_id": doc_id, "candidates": len(cands), "contracted": len(contracted),
            "rejected": rejected, "folded": folded, "flags": len(flag_ids),
            "source_tokens": source_tokens, "candidate_tokens": candidate_tokens,
            "contracted_tokens": contracted_tokens,
            "E": candidate_tokens / source_tokens if source_tokens else None,
            "C": contracted_tokens / candidate_tokens if candidate_tokens else None}
