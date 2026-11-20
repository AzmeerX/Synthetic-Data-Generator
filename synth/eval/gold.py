"""Gold format and the gold workflow: prefill -> human edit -> validate -> promote.

Purpose: the hand-checked truth the arms are scored against (P1 §12).
File: ``data/gold/<doc>__<first>-<last>.json`` (``<doc>`` = document file stem),
one passage = sentences ``first_idx..last_idx`` of one document. Sentences are
cited by label (``s41``, portable across ingests) or by ``snt_`` id; every quote is
verbatim and is located by code (``validate``), never given as offsets. A mention
quote that occurs more than once in its sentence picks one with ``occurrence``
(0-based). Edge ``subject``/``object``, proposition ``speaker``, flag
``object_ids`` and identity-trap ends are gold entity ids (edge ids for flags
about edges); link ends are proposition ids. ``key`` on an entity names the same
real-world entity across gold files (optional).

Contract:
* :func:`prefill` ``(ctx, doc_id, first_idx, last_idx) -> Path`` — runs the frontier
  arm prompt ``arm_extract.v1`` (endpoint ``t3``, caps ``arm_in``/``arm_out``) on the
  passage and writes a gold draft for a human to edit.
* :func:`validate` ``(store, path) -> dict`` — locates every quote; ``ok`` is False
  on any ``NOT_FOUND`` or broken reference.
* :func:`promote` ``(store, path, annotator, blind) -> gold_id`` — stores a valid
  file in L8 ``gold_sets`` (``mode`` = ``blind`` | ``prefill``; versions per passage).
Stores written: L8 ``gold_sets`` (promote); L2 via the gateway (prefill).
Token caps: ``arm_in`` (2,000) in, ``arm_out`` (4,000) out (prefill). Spec: P1 §12, §10.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from synth.ids import new_id
from synth.modules.common import DocView
from synth.provenance.locator import EXACT, NOT_FOUND, Located, Locator
from synth.store.tables import doc_texts, gold_sets


class _Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldEvidence(_Gold):
    sentence_id: str = ""            # s41 (preferred) or snt_…; empty = anywhere in the passage
    quote: str
    occurrence: int = 0              # which occurrence of the quote inside the sentence


def _as_list(value: Any) -> Any:
    if value is None:
        return []
    return [value] if isinstance(value, dict) else value


#: ``evidence`` accepts one object or a list of them.
EvidenceList = Annotated[list[GoldEvidence], BeforeValidator(_as_list)]


class GoldEntity(_Gold):
    id: str
    name: str
    type: str                        # ontology class (or a subtype; mapped to its class)
    subtype: str | None = None
    key: str | None = None
    mentions: list[GoldEvidence] = Field(default_factory=list)


class GoldTime(_Gold):
    role: str                        # occurred_at|valid_from|valid_to|as_of|deadline|reported_on
    value_start: str | None = None
    value_end: str | None = None
    granularity: str | None = None


class GoldEdge(_Gold):
    id: str
    subject: str
    rel: str
    object: str
    stance: str = "stated"
    negated: bool = False
    nested: bool = False             # stated inside nested reasoning (condition, because, …)
    time: list[GoldTime] = Field(default_factory=list)
    evidence: EvidenceList = Field(default_factory=list)


class GoldProposition(_Gold):
    id: str
    text: str
    speaker: str | None = None
    status: str
    negated: bool = False
    evidence: EvidenceList = Field(default_factory=list)


class GoldLink(_Gold):
    id: str | None = None
    src: str
    dst: str
    type: str
    evidence: EvidenceList = Field(default_factory=list)


class GoldFlag(_Gold):
    kind: str
    object_ids: list[str] = Field(default_factory=list)
    note: str = ""


class GoldTrap(_Gold):
    a: str
    b: str
    note: str = ""


class GoldTimeRef(_Gold):
    id: str
    surface: str
    kind: str                        # INSTANT|INTERVAL|PERIOD|DURATION|RECURRING|RELATIVE
    value_start: str | None = None
    value_end: str | None = None
    granularity: str | None = None
    anchor_type: str | None = None
    evidence: GoldEvidence


class GoldDoc(_Gold):
    filename: str | None = None
    doc_id: str | None = None
    text_sha256: str | None = None   # sha256 of the canonical text


class GoldSet(_Gold):
    version: int = 1
    doc: GoldDoc
    first_idx: int
    last_idx: int
    annotator: str = ""
    mode: str = ""                   # blind | prefill (set by promote)
    notes: str = ""
    entities: list[GoldEntity] = Field(default_factory=list)
    edges: list[GoldEdge] = Field(default_factory=list)
    propositions: list[GoldProposition] = Field(default_factory=list)
    links: list[GoldLink] = Field(default_factory=list)
    expected_flags: list[GoldFlag] = Field(default_factory=list)
    identity_traps: list[GoldTrap] = Field(default_factory=list)
    time_refs: list[GoldTimeRef] = Field(default_factory=list)


# ------------------------------------------------------------------ files and documents
def load_gold_file(path: str | Path) -> GoldSet:
    return GoldSet.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def write_gold_file(gold: GoldSet, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gold.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def gold_filename(doc_name: str, first_idx: int, last_idx: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(doc_name).stem) or "doc"
    return f"{stem}__{first_idx}-{last_idx}.json"


def resolve_doc(r, gold: GoldSet) -> str:
    """doc_id, else canonical-text sha256, else file name (most recent document)."""
    if gold.doc.doc_id and r.get("documents", doc_id=gold.doc.doc_id):
        return gold.doc.doc_id
    if gold.doc.text_sha256:
        rows = r.rows(select(doc_texts.c.doc_id).where(doc_texts.c.text_sha256 == gold.doc.text_sha256))
        if rows:
            return _most_recent(r, [row["doc_id"] for row in rows])
    if gold.doc.filename:
        rows = r.select("documents", filename=gold.doc.filename)
        if rows:
            return _most_recent(r, [row["doc_id"] for row in rows])
    raise KeyError(f"gold document not found in the store: {gold.doc.model_dump()}")


def _most_recent(r, doc_ids: list[str]) -> str:
    docs = [r.get("documents", doc_id=d) for d in sorted(set(doc_ids))]
    docs = [d for d in docs if d]
    return max(docs, key=lambda d: (str(d.get("created_at") or ""), d["doc_id"]))["doc_id"]


# ------------------------------------------------------------------ locating
def gold_locator(doc: DocView) -> Locator:
    """Gold quotes are exact names and phrases: no widening of short quotes."""
    return Locator(doc.text, doc.refs(), min_words=0)


def passage_sentences(doc: DocView, first_idx: int, last_idx: int) -> list[dict[str, Any]]:
    return doc.range(first_idx, last_idx, include_filtered=True)


def locate_evidence(doc: DocView, locator: Locator, ev: GoldEvidence,
                    passage: list[dict[str, Any]]) -> Located:
    """Exact ``occurrence``-th hit in the cited sentence, else the locator ladder
    restricted to the passage."""
    unit = [s["sentence_id"] for s in passage]
    ref = locator.sentence(ev.sentence_id) if ev.sentence_id else None
    if ref is not None and ev.quote:
        sentence_text = doc.text[ref.char_start:ref.char_end]
        pos, hit = -1, 0
        while True:
            pos = sentence_text.find(ev.quote, pos + 1)
            if pos < 0:
                break
            if hit == ev.occurrence:
                start = ref.char_start + pos
                end = start + len(ev.quote)
                return Located(EXACT, start, end, doc.text[start:end], [ref.sentence_id],
                               None, False, ev.quote)
            hit += 1
    return locator.locate(ev.quote, [ref] if ref else [], unit=unit)


def _evidence_items(gold: GoldSet):
    for e in gold.entities:
        for i, m in enumerate(e.mentions):
            yield f"entity {e.id} mention {i}", m
    for e in gold.edges:
        for i, m in enumerate(e.evidence):
            yield f"edge {e.id} evidence {i}", m
    for p in gold.propositions:
        for i, m in enumerate(p.evidence):
            yield f"proposition {p.id} evidence {i}", m
    for k, link in enumerate(gold.links):
        for i, m in enumerate(link.evidence):
            yield f"link {link.id or k} evidence {i}", m
    for t in gold.time_refs:
        yield f"time_ref {t.id}", t.evidence


def _reference_errors(gold: GoldSet, ontology=None) -> list[str]:
    errors: list[str] = []
    ent_ids = [e.id for e in gold.entities]
    edge_ids = [e.id for e in gold.edges]
    prop_ids = [p.id for p in gold.propositions]
    for name, ids in (("entity", ent_ids), ("edge", edge_ids), ("proposition", prop_ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            errors.append(f"duplicate {name} ids: {dupes}")
    ents, props = set(ent_ids), set(prop_ids)
    for e in gold.edges:
        for end in (e.subject, e.object):
            if end not in ents:
                errors.append(f"edge {e.id}: unknown entity {end!r}")
        if ontology is not None and ontology.canonical_relation(e.rel) is None:
            errors.append(f"edge {e.id}: unknown relation {e.rel!r}")
        if not e.evidence:
            errors.append(f"edge {e.id}: no evidence")
    for p in gold.propositions:
        if p.speaker and p.speaker not in ents:
            errors.append(f"proposition {p.id}: unknown speaker {p.speaker!r}")
        if not p.evidence:
            errors.append(f"proposition {p.id}: no evidence")
    for link in gold.links:
        for end in (link.src, link.dst):
            if end not in props:
                errors.append(f"link {link.id}: unknown proposition {end!r}")
    for t in gold.identity_traps:
        for end in (t.a, t.b):
            if end not in ents:
                errors.append(f"identity trap {t.a}/{t.b}: unknown entity {end!r}")
    for f in gold.expected_flags:
        for oid in f.object_ids:
            if oid not in ents and oid not in set(edge_ids):
                errors.append(f"flag {f.kind}: unknown object {oid!r}")
    if ontology is not None:
        for e in gold.entities:
            if ontology.class_of(e.type) is None:
                errors.append(f"entity {e.id}: unknown type {e.type!r}")
    if gold.first_idx > gold.last_idx:
        errors.append("first_idx > last_idx")
    return errors


def validate(store, path: str | Path, ontology=None) -> dict[str, Any]:
    """Locate every quote of a gold file. ``ok`` False on NOT_FOUND or a broken reference."""
    path = Path(path)
    report: dict[str, Any] = {"path": str(path), "ok": False, "items": 0, "not_found": [],
                              "sentence_mismatch": [], "errors": [], "grounding": {}}
    try:
        gold = load_gold_file(path)
    except (ValidationError, ValueError, OSError) as exc:
        report["errors"].append(f"unreadable gold file: {exc}")
        return report
    with store.reader() as r:
        try:
            doc_id = resolve_doc(r, gold)
        except KeyError as exc:
            report["errors"].append(str(exc))
            return report
        doc = DocView.load(r, doc_id)
    report["doc_id"] = doc_id
    passage = passage_sentences(doc, gold.first_idx, gold.last_idx)
    if not passage:
        report["errors"].append(f"no sentences {gold.first_idx}..{gold.last_idx} in {doc_id}")
    in_passage = {s["sentence_id"] for s in passage}
    locator = gold_locator(doc)
    counts: dict[str, int] = {}
    for label, ev in _evidence_items(gold):
        report["items"] += 1
        if ev.sentence_id:
            ref = locator.sentence(ev.sentence_id)
            if ref is None or ref.sentence_id not in in_passage:
                report["errors"].append(f"{label}: sentence {ev.sentence_id!r} not in the passage")
        located = locate_evidence(doc, locator, ev, passage)
        counts[located.grounding] = counts.get(located.grounding, 0) + 1
        if located.grounding == NOT_FOUND:
            report["not_found"].append({"item": label, "sentence_id": ev.sentence_id, "quote": ev.quote})
        elif located.grounding == "SENTENCE_MISMATCH":
            report["sentence_mismatch"].append({"item": label, "sentence_id": ev.sentence_id,
                                                "found_in": located.sentence_ids})
    if ontology is None:
        ontology = _default_ontology()
    report["errors"].extend(_reference_errors(gold, ontology))
    report["grounding"] = counts
    report["ok"] = not report["not_found"] and not report["errors"]
    return report


def _default_ontology():
    try:
        from synth.config import load_config
        from synth.ontology import load_ontology

        config = load_config()
        return load_ontology(config.resolve(config.ontology))
    except Exception:  # pragma: no cover - config missing
        return None


def promote(store, path: str | Path, annotator: str, blind: bool) -> str:
    """Store a valid gold file in L8. Returns the new ``gold_id``."""
    report = validate(store, path)
    if not report["ok"]:
        raise ValueError(f"gold file {path} does not validate: "
                         f"{report['errors'] or report['not_found']}")
    gold = load_gold_file(path)
    doc_id = report["doc_id"]
    mode = "blind" if blind else "prefill"
    with store.reader() as r:
        text = DocView.load(r, doc_id).text
    body = gold.model_copy(update={
        "annotator": annotator, "mode": mode,
        "doc": gold.doc.model_copy(update={
            "doc_id": doc_id,
            "text_sha256": gold.doc.text_sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest()}),
    }).model_dump(mode="json")
    gold_id = new_id("gld")
    with store.begin(stage="GOLD", module="U12") as tx:
        prev = tx.execute(select(func.max(gold_sets.c.version)).where(
            (gold_sets.c.doc_id == doc_id) & (gold_sets.c.passage_first_idx == gold.first_idx)
            & (gold_sets.c.passage_last_idx == gold.last_idx))).scalar()
        tx.insert("gold_sets", dict(gold_id=gold_id, doc_id=doc_id, passage_first_idx=gold.first_idx,
                                    passage_last_idx=gold.last_idx, version=(prev or 0) + 1,
                                    annotator=annotator, mode=mode, body=body))
    return gold_id


def load_gold_sets(r, doc_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Latest version of every promoted passage, mapped onto ``doc_ids`` (by doc id,
    else by canonical-text sha256). Each item: gold_id, doc_id, mode, gold (GoldSet)."""
    rows = r.select("gold_sets", order_by=["doc_id", "passage_first_idx", "version"])
    latest: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        latest[(row["doc_id"], row["passage_first_idx"], row["passage_last_idx"])] = row
    sha_to_doc: dict[str, str] = {}
    if doc_ids is not None:
        for d in doc_ids:
            text = r.rows(select(doc_texts.c.text_sha256).where(doc_texts.c.doc_id == d))
            for t in text:
                sha_to_doc[t["text_sha256"]] = d
    out = []
    for key in sorted(latest, key=lambda k: (str(k[0]), k[1], k[2])):
        row = latest[key]
        gold = GoldSet.model_validate(row["body"])
        doc_id = row["doc_id"]
        if doc_ids is not None and doc_id not in doc_ids:
            doc_id = sha_to_doc.get(gold.doc.text_sha256 or "")
            if doc_id is None:
                continue
        out.append({"gold_id": row["gold_id"], "doc_id": doc_id, "mode": row["mode"] or gold.mode,
                    "version": row["version"], "gold": gold})
    return out


# ------------------------------------------------------------------ prefill
def _passage_times(r, doc: DocView, passage: list[dict[str, Any]], run_id: str | None):
    """Time refs already in S3 for the passage (this run first), deduplicated by span."""
    if not passage:
        return []
    lo, hi = passage[0]["char_start"], passage[-1]["char_end"]
    found: dict[tuple[int, int], dict[str, Any]] = {}
    refs = r.select("time_refs", doc_id=doc.doc_id)
    refs.sort(key=lambda t: (t["run_id"] != run_id, t["time_id"]))
    for t in refs:
        span = r.get("spans", span_id=t["span_id"]) if t["span_id"] else None
        if not span or span["char_start"] < lo or span["char_end"] > hi:
            continue
        found.setdefault((span["char_start"], span["char_end"]), {**t, "_span": span})
    return [found[k] for k in sorted(found)]


def _label(locator: Locator, key: str) -> str:
    ref = locator.sentence(key) if key else None
    return f"s{ref.idx}" if ref else key


def prefill(ctx, doc_id: str, first_idx: int, last_idx: int, *, out_dir: str | Path | None = None,
            prompt=None) -> Path:
    """Draft a gold passage with the frontier arm prompt, for a human to edit."""
    from synth.contracts import ArmOutput
    from synth.modules.u04_whoswho import render_time

    if prompt is None:
        from synth.prompts import load_prompt
        prompt = load_prompt("arm_extract.v1")
    doc = ctx.doc(doc_id)
    passage = passage_sentences(doc, first_idx, last_idx)
    if not passage:
        raise ValueError(f"no sentences {first_idx}..{last_idx} in {doc_id}")
    shown = [s for s in passage if not s["filtered"]]
    with ctx.reader() as r:
        times = _passage_times(r, doc, passage, ctx.run_id)
    time_labels = {f"T{i + 1}": t for i, t in enumerate(times)}
    onto = ctx.ontology
    user = "\n\n".join(part for part in [
        f"Document: {doc.doc.get('filename') or doc_id}",
        "Passage (cite sentence labels, quote verbatim):\n" + doc.render(shown),
        ("Time references:\n" + "\n".join(render_time(lbl, t) for lbl, t in time_labels.items()))
        if time_labels else "",
        "Entity types: " + ", ".join(onto.classes),
        "Relations: " + ", ".join(onto.relation_names),
        "Proposition statuses: " + ", ".join(onto.proposition_status),
        "Link types: " + ", ".join(onto.link_types),
    ] if part)
    result = ctx.gateway.call(endpoint="t3", prompt=prompt, user=user, output_model=ArmOutput,
                              cap_in=ctx.config.cap("arm_in"), cap_out=ctx.config.cap("arm_out"),
                              run_id=ctx.run_id, stage="GOLD", module="U12",
                              unit_id=f"{doc_id}:{first_idx}-{last_idx}",
                              sovereign=bool(doc.doc.get("sovereign")))
    out: ArmOutput = result.model
    locator = doc.locator

    def ev(e) -> GoldEvidence:
        return GoldEvidence(sentence_id=_label(locator, e.sentence_ids[0]) if e.sentence_ids else "",
                            quote=e.quote)

    def gold_time(t) -> GoldTime:
        ref = time_labels.get(t.ref) or {}
        return GoldTime(role=t.role, value_start=ref.get("value_start"),
                        value_end=ref.get("value_end"), granularity=ref.get("granularity"))

    entities = []
    for e in out.entities:
        cls = onto.class_of(e.type) or e.type
        entities.append(GoldEntity(id=e.id, name=e.name, type=cls,
                                   subtype=e.type if e.type != cls else None,
                                   mentions=[ev(m) for m in e.mentions]))
    traps, flags = [], []
    for i, a in enumerate(entities):
        for b in entities[i + 1:]:
            if onto.norm_key(a.name) == onto.norm_key(b.name):      # same name ≠ same entity
                traps.append(GoldTrap(a=a.id, b=b.id, note="same name; check"))
                flags.append(GoldFlag(kind="identity_ambiguity", object_ids=[a.id, b.id]))
    gold = GoldSet(
        doc=GoldDoc(filename=doc.doc.get("filename"), doc_id=doc_id,
                    text_sha256=hashlib.sha256(doc.text.encode("utf-8")).hexdigest()),
        first_idx=first_idx, last_idx=last_idx, annotator="prefill:t3", mode="prefill",
        notes=f"draft from arm_extract ({result.model_id}); edit, then validate and promote",
        entities=entities,
        edges=[GoldEdge(id=f"R{i + 1}", subject=e.src, rel=onto.canonical_relation(e.rel) or e.rel,
                        object=e.dst, stance=e.stance, negated=e.negated,
                        time=[gold_time(t) for t in e.time], evidence=[ev(e.evidence)])
               for i, e in enumerate(out.edges)],
        propositions=[GoldProposition(id=p.pid, text=p.text, speaker=p.speaker, status=p.status,
                                      negated=p.negated, evidence=[ev(p.evidence)])
                      for p in out.propositions],
        links=[GoldLink(id=f"L{i + 1}", src=link.src, dst=link.dst, type=link.type,
                        evidence=[ev(link.evidence)] if link.evidence.quote else [])
               for i, link in enumerate(out.links)],
        expected_flags=flags, identity_traps=traps,
        time_refs=[GoldTimeRef(id=lbl, surface=t["surface"] or "", kind=t["kind"],
                               value_start=t["value_start"], value_end=t["value_end"],
                               granularity=t["granularity"], anchor_type=t["anchor_type"],
                               evidence=GoldEvidence(
                                   sentence_id=_label(locator, (t["_span"]["sentence_ids"] or [""])[0]),
                                   quote=doc.text[t["_span"]["char_start"]:t["_span"]["char_end"]]))
                   for lbl, t in time_labels.items()],
    )
    base = Path(out_dir) if out_dir else ctx.config.path("gold")
    return write_gold_file(gold, base / gold_filename(doc.doc.get("filename") or doc_id,
                                                      first_idx, last_idx))
