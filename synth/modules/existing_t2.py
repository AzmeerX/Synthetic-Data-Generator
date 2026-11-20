"""U7 adapter (``impl: existing``): the existing Datanaut graph extraction, wrapped.

Purpose: run the **existing** middle-model extraction prompt (``app.extract.prompts``:
``EXTRACT_SYSTEM`` + ``build_extract_prompt``, prompt version ``extract/1.0.0``) over a
Synth T2 section — through the Synth gateway, never ``RetryingModelClient`` — and turn its
output (entities / events / relationships, each with a ``quote``) into Synth-shaped,
grounded drafts. Wrap, don't rewrite: nothing under ``app/`` is modified and the old
path (``app.extract.extractor.Extractor``) stays runnable.

Contract (EXISTING_MAP.md "U7 adapter plan"):

* :func:`ontology_frame` — a ``Frame`` built with ``app.frames.loader.frame_from_dict`` from the
  ontology pack: entity classes as entity types, the closed relation list as relationship types,
  one event type ``proposition`` (events become Synth propositions), statuses = proposition statuses.
* :func:`section_window` — a section as one ``Window``; one ``Passage`` per sentence, id ``s{idx}``,
  no page number, so ``Window.render`` prints ``[s41] …`` (Synth's sentence-tagged format).
* :func:`build_existing_user` / :func:`call_existing` — ``build_extract_prompt(window, frame)``
  (+ an optional hint block: U7 ``full`` mode appends the compact micro rows), sent via
  ``ctx.gateway.call`` (endpoint ``t2``, prompt ``existing_extract.v1`` = ``EXTRACT_SYSTEM``,
  schema = the frame's JSON schema).
* :func:`parse_existing` — quotes grounded with the Synth locator over the section (the
  existing output has no sentence ids, so the unit is the whole section); entities mapped onto
  S2 by norm key / alias within the document (unknown -> new entity, ``source="arm"``, with a
  mention). If a quote cannot be located, the element's name / description is matched against
  the section instead (grounding ``FUZZY`` at best, logged — spec §16 risk 3).
* :func:`diff_against_rows` — the adapter-computed diff (ADD / REMOVE / CORRECT) of the existing
  graph against the micro rows.

Also defines the neutral draft shapes (:class:`EdgeDraft`, :class:`PropDraft`, :class:`LinkDraft`,
:class:`NewEntity`, :class:`ResolvedOp`, :class:`Dropped`) and :class:`EntityResolver`, which
U7 ``diff`` mode and the arm extractors reuse. Stores written: none (U7 / the arms write).
Token caps: given by the caller (U7: ``u7_in``/``u7_out``; arms: ``arm_in``/``arm_out``).
Spec: P1 §3 U7, §1 arms, §5, §16 risk 3.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from rapidfuzz import fuzz

from app.extract.prompts import EXTRACT_SYSTEM, PROMPT_VERSION, build_extract_prompt
from app.extract.windows import Window
from app.frames.loader import Frame, frame_from_dict
from app.ingest.models import Passage
from synth.ids import new_id
from synth.modules.common import DocView
from synth.modules.u06_contract import fold_key
from synth.prompts import Prompt
from synth.provenance import FUZZY, Located, Locator, SentenceRef
from synth.provenance.locator import normalize
from synth.store.repo import Tx

#: The existing system text, registered as a Synth prompt (L2 rows carry its name and hash).
EXISTING_PROMPT = Prompt(name="existing_extract.v1", text=EXTRACT_SYSTEM,
                         sha256=hashlib.sha256(EXTRACT_SYSTEM.encode("utf-8")).hexdigest())
EXISTING_PROMPT_VERSION = PROMPT_VERSION
#: The existing schema has no confidence field; edges get a fixed one (lower when grounding fell back).
EXISTING_CONFIDENCE = 0.7
FALLBACK_CONFIDENCE = 0.5
PROPOSITION_EVENT = "proposition"

_CLASS_TEXT = {
    "person": "A named individual (also judges and lawyers).",
    "organization": "A company, bank, law firm, government body or court.",
    "legal": "A statute, case, agreement or document.",
    "value": "A stated amount of money or a percentage.",
    "place": "A geographic location.",
    "thing": "An asset.",
    "event": "A named event.",
}


# ------------------------------------------------------------------ neutral drafts
@dataclass
class EdgeDraft:
    src: str
    rel: str
    dst: str
    subclass: str | None = None
    desc: str = ""
    stance: str = "stated"
    negated: bool = False
    conf: float | None = 0.5
    times: list[tuple[str, str]] = field(default_factory=list)      # (role, time_id)
    span_ids: list[str] = field(default_factory=list)               # existing spans (micro rows)
    sentence_ids: list[str] = field(default_factory=list)           # of the existing spans
    located: list[Located] = field(default_factory=list)            # new spans to write
    micro_id: str | None = None
    micro_row: dict[str, Any] | None = None
    origin: str = "ADDED"                                           # CONFIRMED | CORRECTED | ADDED
    row_id: str | None = None
    first_offset: int = 10 ** 12
    removed: bool = False
    remove_reason: str = ""
    call_id: str | None = None
    fallback: bool = False

    def key(self) -> tuple:
        return (self.src, self.rel, self.dst, bool(self.negated))

    def all_sentence_ids(self) -> list[str]:
        out = list(self.sentence_ids)
        for loc in self.located:
            out += [s for s in loc.sentence_ids if s not in out]
        return out

    def offset(self) -> int:
        offsets = [self.first_offset] + [lk.char_start for lk in self.located if lk.char_start is not None]
        return min(offsets)


@dataclass
class NewEntity:
    entity_id: str
    name: str
    type: str | None
    subtype: str | None
    norm_key: str
    mentions: list[Located] = field(default_factory=list)
    distinct: str | None = None                  # reply-local id (arm replies keep ids apart)


@dataclass
class PropDraft:
    text: str
    status: str
    located: Located
    speaker: str | None = None
    negated: bool = False
    prop_id: str = field(default_factory=lambda: new_id("prp"))
    fallback: bool = False

    def norm(self) -> str:
        return re.sub(r"[\s.]+$", "", " ".join(self.text.casefold().split()))


@dataclass
class LinkDraft:
    src: PropDraft
    dst: PropDraft
    type: str
    located: Located
    link_id: str = field(default_factory=lambda: new_id("lnk"))

    def key(self) -> tuple:
        return (id(self.src), id(self.dst), self.type)


@dataclass
class ResolvedOp:
    op: str                                  # ADD | REMOVE | CORRECT | SPLIT_ENTITY | MERGE_ENTITY
    row_id: str | None = None
    edge: EdgeDraft | None = None
    entities: list[str] = field(default_factory=list)
    located: Located | None = None
    conf: float = 0.5
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Dropped:
    object_type: str
    raw: dict[str, Any]
    rule_id: str
    call_id: str | None = None              # the model call whose output was dropped


# ------------------------------------------------------------------ grounding helpers
def unit_refs(doc: DocView, sentences: Sequence[dict[str, Any]]) -> list[SentenceRef]:
    return doc.refs(sentences)


def sentence_span(doc: DocView, refs: Sequence[SentenceRef]) -> Located | None:
    if not refs:
        return None
    return doc.locator.ground_span(min(r.char_start for r in refs), max(r.char_end for r in refs))


def mention_locator(doc: DocView) -> Locator:
    """Like ``doc.locator`` but never widens short quotes (a mention is the name itself)."""
    g = doc.config.grounding if doc.config else None
    return Locator(doc.text, doc.refs(), fuzzy_accept=g.fuzzy_accept if g else 85.0,
                   fuzzy_pass=g.fuzzy_pass if g else 92.0, min_words=0)


def as_fuzzy(located: Located, score: float | None = None) -> Located:
    """Fallback grounding is ``FUZZY`` at best (spec §16 risk 3)."""
    return replace(located, grounding=FUZZY,
                   fuzzy_score=located.fuzzy_score if located.fuzzy_score is not None else (score or 100.0))


def name_within(doc: DocView, name: str, located: Located | None) -> Located | None:
    """The exact position of ``name`` inside an already located quote (so two same-name
    mentions in one sentence keep their own offsets)."""
    if located is None or located.char_start is None or not name.strip():
        return None
    window = doc.text[located.char_start:located.char_end]
    pos = window.find(name.strip())
    if pos < 0:
        pos = window.casefold().find(name.strip().casefold())
    if pos < 0:
        return None
    start = located.char_start + pos
    return doc.locator.ground_span(start, start + len(name.strip()))


def comention_sentence(doc: DocView, sentences: Sequence[dict[str, Any]], names: Sequence[str]) -> Located | None:
    """The first sentence of the unit that mentions every name (normalized containment)."""
    keys = [normalize(n) for n in names if n and n.strip()]
    if not keys:
        return None
    for s in sentences:
        text = normalize(doc.sentence_text(s))
        if all(k in text for k in keys):
            return as_fuzzy(doc.locator.ground_span(s["char_start"], s["char_end"]), 100.0)
    return None


# ------------------------------------------------------------------ entity resolver
class EntityResolver:
    """Maps names onto the document's S2 entities (norm key or alias); unknown names become
    :class:`NewEntity` drafts with a grounded mention. Same name ≠ same entity: with several
    candidates the one whose mention is nearest the evidence wins; two reply entities with one
    key stay apart when their mentions sit at different places of one sentence, or when the
    reply gave them different ids (``distinct``)."""

    def __init__(self, r: Tx, ontology, run_id: str, doc: DocView, sentences: Sequence[dict[str, Any]]) -> None:
        self.ontology = ontology
        self.doc = doc
        self.refs = unit_refs(doc, sentences)
        self.sentences = list(sentences)
        self.locator = mention_locator(doc)
        ents = [e for e in r.select("entities", run_id=run_id, doc_id=doc.doc_id) if e["status"] == "ACTIVE"]
        self.entities = {e["entity_id"]: e for e in ents}
        self.by_key: dict[str, list[str]] = {}
        for e in ents:
            for key in {e["norm_key"], ontology.norm_key(e["canonical_name"] or "")}:
                self._add_key(key, e["entity_id"])
        if ents:
            table = r.table("entity_aliases")
            for a in r.select("entity_aliases", where=table.c.entity_id.in_(list(self.entities))):
                self._add_key(ontology.norm_key(a["alias"] or ""), a["entity_id"])
        self.mention_offsets: dict[str, list[tuple[int, int]]] = {}
        spans = {s["span_id"]: s for s in r.select("spans", run_id=run_id, doc_id=doc.doc_id)}
        for m in r.select("mentions", run_id=run_id, doc_id=doc.doc_id):
            span = spans.get(m["span_id"])
            if span and m["entity_id"] in self.entities:
                self.mention_offsets.setdefault(m["entity_id"], []).append((span["char_start"], span["char_end"]))
        self.new: list[NewEntity] = []

    def _add_key(self, key: str | None, entity_id: str) -> None:
        if key and entity_id not in self.by_key.setdefault(key, []):
            self.by_key[key].append(entity_id)

    # -- lookups -----------------------------------------------------------------
    def entity_class(self, entity_id: str | None) -> str | None:
        if entity_id in self.entities:
            e = self.entities[entity_id]
            return self.ontology.class_of(e["type"]) or self.ontology.class_of(e["subtype"])
        for n in self.new:
            if n.entity_id == entity_id:
                return n.type
        return None

    def _offsets(self, entity_id: str) -> list[tuple[int, int]]:
        if entity_id in self.mention_offsets:
            return self.mention_offsets[entity_id]
        for n in self.new:
            if n.entity_id == entity_id:
                return [(m.char_start, m.char_end) for m in n.mentions]
        return []

    def _closest(self, candidates: list[str], near: Located | None) -> str:
        if len(candidates) == 1 or near is None or near.char_start is None:
            return candidates[0]

        def distance(eid: str) -> int:
            best = 10 ** 12
            for start, end in self._offsets(eid):
                if start < near.char_end and end > near.char_start:
                    return 0
                best = min(best, abs(start - near.char_start))
            return best
        return min(candidates, key=lambda eid: (distance(eid), candidates.index(eid)))

    def locate_name(self, name: str, near: Located | None = None) -> Located | None:
        loc = self.locator.locate(name, near.sentence_ids if near else (), unit=self.refs)
        return loc if loc.grounded else None

    def _same_sentence_elsewhere(self, existing: NewEntity, mention: Located | None) -> bool:
        if mention is None or not existing.mentions:
            return False
        for m in existing.mentions:
            if set(m.sentence_ids) & set(mention.sentence_ids) and \
                    not (m.char_start < mention.char_end and m.char_end > mention.char_start):
                return True
        return False

    def resolve(self, name: str | None, type_hint: str | None = None, near: Located | None = None,
                mentions: Sequence[Located] = (), distinct: str | None = None,
                create: bool = True) -> str | None:
        key = self.ontology.norm_key(name or "")
        if not key:
            return None
        cls = self.ontology.class_of(type_hint) if type_hint else None
        anchor = near or (mentions[0] if mentions else None)
        candidates = list(self.by_key.get(key, []))
        if cls and candidates:
            typed = [c for c in candidates if self.entity_class(c) in (cls, None)]
            candidates = typed or candidates
        if candidates:
            return self._closest(candidates, anchor)
        mention = mentions[0] if mentions else None
        same_key = [n for n in self.new if n.norm_key == key]
        if not create:
            return self._closest([n.entity_id for n in same_key], anchor) if same_key else None
        if distinct is not None:
            reuse = [n for n in same_key if n.distinct == distinct]
        elif mention is None:
            reuse = same_key                        # a bare name (relationship end): nearest of that name
        else:
            reuse = [n for n in same_key if n.distinct is None and not self._same_sentence_elsewhere(n, mention)]
        if reuse:
            target_id = self._closest([n.entity_id for n in reuse], anchor)
            target = next(n for n in reuse if n.entity_id == target_id)
            for m in mentions:
                if all((m.char_start, m.char_end) != (x.char_start, x.char_end) for x in target.mentions):
                    target.mentions.append(m)
            return target.entity_id
        located = list(mentions) or ([self.locate_name(name or "", near)] if name else [])
        located = [m for m in located if m is not None]
        if not located:
            return None
        sub = None
        if type_hint:
            snake = re.sub(r"[^a-z0-9]+", "_", type_hint.strip().lower()).strip("_")
            sub = snake if cls and snake != cls and snake in self.ontology.entity_types.get(cls, []) else None
        entity = NewEntity(entity_id=new_id("ent"), name=" ".join((name or "").split()), type=cls,
                           subtype=sub, norm_key=key, mentions=located, distinct=distinct)
        self.new.append(entity)
        return entity.entity_id


# ------------------------------------------------------------------ the existing prompt
_FRAMES: dict[str, Frame] = {}


def frame_dict(ontology) -> dict[str, Any]:
    """The ontology pack in the existing frame JSON format (``frames/*.json``)."""
    entity_types = [{"name": cls, "description": _CLASS_TEXT.get(cls, cls.replace("_", " ")),
                     "examples": []} for cls in ontology.entity_types]
    for t in entity_types:
        subs = [s for s in ontology.entity_types[t["name"]] if s != t["name"]]
        if subs:
            t["description"] += f" Subtypes: {', '.join(subs)}."
    relationship_types = [{"name": rel.name, "description": rel.family,
                           "source_types": [rel.source_class], "target_types": [rel.target_class]}
                          for rel in ontology.relations.values()]
    return {
        "name": f"Synth {ontology.pack}",
        "version": f"{ontology.version}.0.0",
        "description": "Synth ontology pack (closed relation list). Proposed or planned things, decisions, "
                       "findings, allegations and reports are events of type proposition, never relationships.",
        "entity_types": entity_types,
        "event_types": [{"name": PROPOSITION_EVENT,
                         "description": "A standalone claim, decision, finding, allegation or proposal the "
                                        "text states; status says how it is held.",
                         "roles": [{"name": "speaker", "description": "who states, decides or alleges it"}]}],
        "relationship_types": relationship_types,
        "statuses": list(ontology.proposition_status) or ["stated"],
    }


def ontology_frame(ontology) -> Frame:
    key = ontology.fingerprint()
    if key not in _FRAMES:
        _FRAMES[key] = frame_from_dict(frame_dict(ontology))
    return _FRAMES[key]


def section_window(doc: DocView, sentences: Sequence[dict[str, Any]], index: int = 0) -> Window:
    passages = [Passage(id=f"s{s['idx']}", document_id=doc.doc_id, ordinal=i,
                        text=" ".join(doc.sentence_text(s).split()), char_start=s["char_start"],
                        char_end=s["char_end"], label=None, page_no=None)
                for i, s in enumerate(sorted(sentences, key=lambda s: s["idx"]))]
    return Window(id=f"{doc.doc_id}_t2", document_id=doc.doc_id, index=index, passages=passages)


def build_existing_user(doc: DocView, sentences: Sequence[dict[str, Any]], frame: Frame, hint: str = "") -> str:
    system, user = build_extract_prompt(section_window(doc, sentences), frame,
                                        document_name=doc.doc.get("filename") or doc.doc_id)
    assert system == EXTRACT_SYSTEM
    return user + ("\n\n" + hint if hint else "")


def call_existing(ctx, doc: DocView, user: str, *, stage: str, module: str, unit_id: str,
                  cap_in: int, cap_out: int):
    """One call of the existing prompt through the Synth gateway (L2 logged)."""
    frame = ontology_frame(ctx.ontology)
    return ctx.gateway.call(endpoint="t2", prompt=EXISTING_PROMPT, user=user, cap_in=cap_in,
                            cap_out=cap_out, stage=stage, module=module, unit_id=unit_id,
                            run_id=ctx.run_id, schema=frame.json_schema(),
                            sovereign=bool(doc.doc.get("sovereign")))


# ------------------------------------------------------------------ parse the existing output
@dataclass
class ExistingGraph:
    edges: list[EdgeDraft] = field(default_factory=list)
    props: list[PropDraft] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    fallbacks: int = 0


class TimeIndex:
    """Time refs of a unit, looked up by surface or ISO value (existing ``date`` strings)."""

    def __init__(self, r: Tx, run_id: str, doc: DocView, sentences: Sequence[dict[str, Any]]) -> None:
        start = min((s["char_start"] for s in sentences), default=0)
        end = max((s["char_end"] for s in sentences), default=0)
        spans = {s["span_id"]: s for s in r.select("spans", run_id=run_id, doc_id=doc.doc_id)}
        self.by_text: dict[str, str] = {}
        for t in r.select("time_refs", run_id=run_id, doc_id=doc.doc_id):
            span = spans.get(t["span_id"])
            if span and span["char_start"] < end and span["char_end"] > start:
                for text in (t["surface"], t["value_start"]):
                    if text:
                        self.by_text.setdefault(normalize(text), t["time_id"])

    def match(self, text: str | None) -> str | None:
        return self.by_text.get(normalize(text)) if text else None


def _ground(doc: DocView, quote: str, refs: Sequence[SentenceRef]) -> Located | None:
    if not (quote or "").strip():
        return None
    loc = doc.locator.locate(quote, (), unit=refs)
    return loc if loc.grounded else None


def parse_existing(data: dict[str, Any], doc: DocView, sentences: Sequence[dict[str, Any]],
                   resolver: EntityResolver, ontology, times: TimeIndex | None = None,
                   confidence: float = EXISTING_CONFIDENCE) -> ExistingGraph:
    """Ground and map the existing graph (``entities`` / ``events`` / ``relationships``)."""
    out = ExistingGraph()
    refs = unit_refs(doc, sentences)
    types: dict[str, str] = {}

    for ent in data.get("entities") or []:
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        types.setdefault(ontology.norm_key(name), ent.get("type") or "")
        quote_loc = _ground(doc, ent.get("quote") or "", refs)
        mention = name_within(doc, name, quote_loc) or resolver.locate_name(name, quote_loc)
        if mention is None and quote_loc is not None:
            mention = quote_loc
        if quote_loc is None and mention is not None:
            mention = as_fuzzy(mention)
            out.fallbacks += 1
        if mention is None:
            out.dropped.append(Dropped("entity", ent, "ungrounded"))
            continue
        if resolver.resolve(name, ent.get("type"), mentions=[mention]) is None:
            out.dropped.append(Dropped("entity", ent, "unresolved"))

    for ev in data.get("events") or []:
        summary = (ev.get("summary") or "").strip()
        located = _ground(doc, ev.get("quote") or "", refs)
        fallback = False
        if located is None and summary:
            loc = doc.locator.locate(summary, (), unit=refs)
            located, fallback = (as_fuzzy(loc), True) if loc.grounded else (None, False)
        if located is None or not summary:
            out.dropped.append(Dropped("proposition", ev, "ungrounded"))
            continue
        out.fallbacks += int(fallback)
        status = ev.get("status") if ev.get("status") in ontology.proposition_status else "stated"
        speaker = None
        for p in ev.get("participants") or []:
            if (p.get("role") or "") == "speaker":
                speaker = resolver.resolve(p.get("entity"), near=located, create=False)
        out.props.append(PropDraft(text=summary, status=status, located=located, speaker=speaker,
                                   fallback=fallback))

    for rel in data.get("relationships") or []:
        canonical = ontology.canonical_relation(rel.get("type")) or \
            ontology.canonical_relation(rel.get("observed_verb"))
        if canonical is None:
            out.dropped.append(Dropped("macro_edge", rel, "unknown_relation"))
            continue
        located = _ground(doc, rel.get("quote") or "", refs)
        fallback = located is None
        if located is None:
            located = comention_sentence(doc, sentences, [rel.get("source") or "", rel.get("target") or ""])
        if located is None:
            out.dropped.append(Dropped("macro_edge", rel, "ungrounded"))
            continue
        src = resolver.resolve(rel.get("source"), types.get(ontology.norm_key(rel.get("source") or "")), near=located)
        dst = resolver.resolve(rel.get("target"), types.get(ontology.norm_key(rel.get("target") or "")), near=located)
        if not src or not dst:
            out.dropped.append(Dropped("macro_edge", rel, "unknown_entity"))
            continue
        if src == dst:
            out.dropped.append(Dropped("macro_edge", rel, "self_loop"))
            continue
        if not ontology.relation(canonical).allows(resolver.entity_class(src), resolver.entity_class(dst)):
            out.dropped.append(Dropped("macro_edge", rel, "type_violation"))
            continue
        edge_times = []
        if times is not None:
            for field_name, role in (("date", "occurred_at"), ("date_end", "valid_to")):
                tid = times.match(rel.get(field_name))
                if tid:
                    edge_times.append((role, tid))
        out.fallbacks += int(fallback)
        out.edges.append(EdgeDraft(src=src, rel=canonical, dst=dst, desc=rel.get("observed_verb") or "",
                                   conf=FALLBACK_CONFIDENCE if fallback else confidence, times=edge_times,
                                   located=[located], fallback=fallback))
    out.edges = fold_drafts(out.edges)
    return out


def fold_drafts(edges: Sequence[EdgeDraft]) -> list[EdgeDraft]:
    """U6's fold rule on drafts: identical ``(src, rel, dst, negated, stance)`` -> one, all spans."""
    kept: dict[tuple, EdgeDraft] = {}
    for e in edges:
        key = fold_key(e.src, e.rel, e.dst, e.negated, e.stance)
        if key not in kept:
            kept[key] = e
            continue
        k = kept[key]
        for loc in e.located:
            if all((loc.char_start, loc.char_end) != (x.char_start, x.char_end) for x in k.located):
                k.located.append(loc)
        for t in e.times:
            if t not in k.times:
                k.times.append(t)
        k.conf = max(k.conf or 0.0, e.conf or 0.0)
        k.fallback = k.fallback and e.fallback
    return list(kept.values())


# ------------------------------------------------------------------ the adapter-computed diff
def diff_against_rows(edges: Sequence[EdgeDraft], rows: Sequence[EdgeDraft]) -> list[ResolvedOp]:
    """Existing graph vs micro rows: same ``(src, rel, dst, negated)`` -> confirmed (no op; the
    existing evidence is added to the row); same entity pair otherwise -> CORRECT; no row -> ADD;
    rows the existing graph does not contain -> REMOVE."""
    ops: list[ResolvedOp] = []
    matched: set[str] = set()
    unmatched: list[EdgeDraft] = []
    live = [r for r in rows if not r.removed and r.row_id]
    for e in edges:
        exact = [r for r in live if r.key() == e.key()]
        if exact:
            for r in exact:
                matched.add(r.row_id)
                for loc in e.located:
                    if all((loc.char_start, loc.char_end) != (x.char_start, x.char_end) for x in r.located):
                        r.located.append(loc)
            continue
        unmatched.append(e)
    for e in unmatched:
        pair = [r for r in live if r.row_id not in matched and {r.src, r.dst} == {e.src, e.dst}]
        if pair:
            matched.add(pair[0].row_id)
            ops.append(ResolvedOp("CORRECT", row_id=pair[0].row_id, edge=e, located=e.located[0] if e.located else None,
                                  conf=e.conf or EXISTING_CONFIDENCE, reason="existing graph differs"))
        else:
            ops.append(ResolvedOp("ADD", edge=e, located=e.located[0] if e.located else None,
                                  conf=e.conf or EXISTING_CONFIDENCE, reason="in the existing graph only"))
    for r in live:
        if r.row_id not in matched:
            ops.append(ResolvedOp("REMOVE", row_id=r.row_id, reason="not in the existing graph"))
    return ops


def similar_text(a: str, b: str) -> float:
    return float(fuzz.token_set_ratio(a, b))
