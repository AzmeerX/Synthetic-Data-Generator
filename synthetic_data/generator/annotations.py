"""Gold annotation generation from structured scenario facts.

The generator never asks a model what a document says.  It knows the facts it
encoded and only uses :class:`DocView` to translate planned sentences into the
canonical sentence labels and hashes that Synth's gold format requires.

Every quote is the exact surface form used by the document plan.  A plan that
does not match the ingested canonical sentences is rejected before a gold file
is written.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from synth.eval.gold import (
    GoldDoc,
    GoldEdge,
    GoldEntity,
    GoldEvidence,
    GoldFlag,
    GoldLink,
    GoldProposition,
    GoldSet,
    GoldTime,
    GoldTimeRef,
    GoldTrap,
    gold_filename,
    gold_locator,
    validate,
    write_gold_file,
)
from synth.modules.common import DocView

from synthetic_data.generator.documents import DocumentPlan, TimeSpec
from synthetic_data.generator.world import Fact, World


class GoldGenerationError(RuntimeError):
    """Raised when planned text cannot be grounded in the ingested document."""


@dataclass(frozen=True)
class GoldBuildResult:
    gold: GoldSet
    path: Path
    report: dict[str, Any]


def _canonical_sentence_index(doc: DocView, plan: DocumentPlan) -> dict[str, int]:
    """Map each planned sentence to the ingested sentence index.

    Synth's parser is deterministic for the text produced by the generator.
    We still verify the whole mapping rather than assuming it: a splitter
    change must fail the build, never shift evidence silently.
    """
    actual = [doc.sentence_text(s) for s in doc.sentences]
    if actual != plan.sentences:
        limit = max(len(actual), len(plan.sentences))
        for i in range(limit):
            planned = plan.sentences[i] if i < len(plan.sentences) else "<missing>"
            ingested = actual[i] if i < len(actual) else "<missing>"
            if planned != ingested:
                raise GoldGenerationError(
                    f"{plan.document_id}: planned sentence {i} does not match "
                    f"ingested sentence: planned={planned!r} ingested={ingested!r}")
        raise GoldGenerationError(f"{plan.document_id}: sentence count mismatch")
    return {text: s["idx"] for text, s in zip(plan.sentences, doc.sentences)}


def _gold_time_for_edge(spec: TimeSpec) -> GoldTime:
    role = "occurred_at"
    if " until " in f" {spec.sentence_text} ":
        role = "valid_to"
    elif " as at " in f" {spec.sentence_text} ":
        role = "as_of"
    elif " became " in f" {spec.sentence_text} " or spec.sentence_text.startswith("From "):
        role = "valid_from"
    return GoldTime(
        role=role,
        value_start=spec.value_start,
        value_end=spec.value_end or spec.value_start,
        granularity=spec.granularity,
    )


def _time_refs(plan: DocumentPlan, sentence_idx: dict[str, int]) -> list[GoldTimeRef]:
    refs: list[GoldTimeRef] = []
    seen: set[str] = set()
    for spec in plan.times:
        if spec.time_id in seen:
            raise GoldGenerationError(f"{plan.document_id}: duplicate time id {spec.time_id}")
        seen.add(spec.time_id)
        if spec.sentence_text not in sentence_idx:
            raise GoldGenerationError(
                f"{plan.document_id}: time {spec.time_id} cites an unknown sentence")
        refs.append(GoldTimeRef(
            id=spec.time_id,
            surface=spec.surface,
            kind=spec.kind,
            value_start=spec.value_start,
            value_end=spec.value_end or spec.value_start,
            granularity=spec.granularity,
            anchor_type=spec.anchor_type,
            evidence=GoldEvidence(
                sentence_id=f"s{sentence_idx[spec.sentence_text]}",
                quote=spec.surface,
            ),
        ))
    return refs


def _entity_mentions(
    plan: DocumentPlan,
    sentence_idx: dict[str, int],
) -> dict[str, list[GoldEvidence]]:
    mentions: dict[str, list[GoldEvidence]] = {}
    for spec in plan.mentions:
        if spec.sentence_text not in sentence_idx:
            raise GoldGenerationError(
                f"{plan.document_id}: mention cites unknown sentence {spec.sentence_text!r}")
        mentions.setdefault(spec.entity_id, []).append(GoldEvidence(
            sentence_id=f"s{sentence_idx[spec.sentence_text]}",
            quote=spec.surface,
            occurrence=spec.occurrence,
        ))
    return mentions


def _entity_ids(plan: DocumentPlan) -> list[str]:
    ids = set(plan.entity_ids)
    for stated in plan.stated_facts:
        if stated.fact.subject_id:
            ids.add(stated.fact.subject_id)
        if stated.fact.object_id:
            ids.add(stated.fact.object_id)
        if stated.fact.speaker_id:
            ids.add(stated.fact.speaker_id)
    return sorted(ids)


def _edge_time(fact: Fact, sentence_text: str, times: list[TimeSpec]) -> list[GoldTime]:
    out: list[GoldTime] = []
    for spec in times:
        if spec.sentence_text == sentence_text:
            out.append(_gold_time_for_edge(spec))
    return out


def build_gold(world: World, plan: DocumentPlan, doc: DocView) -> GoldSet:
    """Build a :class:`GoldSet` grounded in the *ingested* canonical text."""
    sentence_idx = _canonical_sentence_index(doc, plan)
    mentions = _entity_mentions(plan, sentence_idx)
    entity_ids = _entity_ids(plan)

    entities: list[GoldEntity] = []
    for entity_id in entity_ids:
        entity = world.entity(entity_id)
        subtype = entity.subtype
        if subtype == entity.entity_type:
            subtype = None
        entities.append(GoldEntity(
            id=entity_id,
            name=entity.name,
            type=entity.entity_type,
            subtype=subtype,
            key=entity_id,
            mentions=mentions.get(entity_id, []),
        ))

    times = plan.times
    edges: list[GoldEdge] = []
    propositions: list[GoldProposition] = []
    for stated in plan.stated_facts:
        fact = stated.fact
        if stated.sentence_text not in sentence_idx:
            raise GoldGenerationError(
                f"{plan.document_id}: fact {fact.fact_id} cites an unknown sentence")
        sid = f"s{sentence_idx[stated.sentence_text]}"
        evidence = [GoldEvidence(sentence_id=sid, quote=stated.sentence_text)]
        if fact.kind == "edge":
            if not fact.relation:
                raise GoldGenerationError(f"{plan.document_id}: edge {fact.fact_id} has no relation")
            edges.append(GoldEdge(
                id=fact.fact_id,
                subject=fact.subject_id or "",
                rel=fact.relation,
                object=fact.object_id or "",
                negated=fact.negated,
                nested=fact.nested,
                time=_edge_time(fact, stated.sentence_text, times),
                evidence=evidence,
            ))
        elif fact.kind == "proposition":
            propositions.append(GoldProposition(
                id=fact.fact_id,
                text=fact.text or stated.sentence_text.rstrip("."),
                speaker=fact.speaker_id,
                status=fact.status,
                negated=fact.negated,
                evidence=evidence,
            ))
        else:  # pragma: no cover - DocumentPlan never emits other kinds
            raise GoldGenerationError(
                f"{plan.document_id}: unsupported fact kind {fact.kind!r}")

    links: list[GoldLink] = []
    for spec in plan.links:
        if spec.sentence_text not in sentence_idx:
            raise GoldGenerationError(
                f"{plan.document_id}: link {spec.link_id} cites an unknown sentence")
        links.append(GoldLink(
            id=spec.link_id,
            src=spec.src,
            dst=spec.dst,
            type=spec.type,
            evidence=[GoldEvidence(
                sentence_id=f"s{sentence_idx[spec.sentence_text]}",
                quote=spec.sentence_text,
            )],
        ))

    edge_ids = {edge.id for edge in edges}
    flags: list[GoldFlag] = []
    for flag in plan.flags:
        # A conflict flag about an edge is only meaningful in the document that
        # actually states that edge.
        if any(object_id not in entity_ids and object_id not in edge_ids
               for object_id in flag.object_ids):
            continue
        flags.append(GoldFlag(
            kind=flag.kind, object_ids=list(flag.object_ids), note=flag.note))

    traps = [
        GoldTrap(a=a, b=b, note=note)
        for a, b, note in plan.identity_traps
        if a in entity_ids and b in entity_ids
    ]

    notes = plan.notes
    if plan.contradiction_id:
        notes += f" Contradiction: {plan.contradiction_id}."
    notes += " Generated from structured scenario facts; not blind."

    return GoldSet(
        version=1,
        # The Synth doc_id is a ULID and changes on every fresh ingest.  The
        # canonical-text hash is the reproducible document identity; validate()
        # resolves through it.  The manifest still records the Synth doc_id.
        doc=GoldDoc(
            filename=plan.filename,
            doc_id=None,
            text_sha256=hashlib.sha256(doc.text.encode("utf-8")).hexdigest(),
        ),
        first_idx=0,
        last_idx=len(doc.sentences) - 1,
        annotator="synthetic_data.generator",
        mode="generated",
        notes=notes,
        entities=entities,
        edges=edges,
        propositions=propositions,
        links=links,
        expected_flags=flags,
        identity_traps=traps,
        time_refs=_time_refs(plan, sentence_idx),
    )


def assert_gold_valid(store, path: str | Path, ontology=None) -> dict[str, Any]:
    """Validate a written gold file and raise on *any* grounding failure.

    ``synth.eval.gold.validate`` treats ``SENTENCE_MISMATCH`` as a soft warning
    (``ok`` remains true), so this wrapper checks that list explicitly.
    """
    report = validate(store, path, ontology=ontology)
    problems = []
    if not report.get("ok"):
        problems.append(f"errors={report.get('errors')}")
    if report.get("not_found"):
        problems.append(f"not_found={report['not_found']}")
    if report.get("sentence_mismatch"):
        problems.append(f"sentence_mismatch={report['sentence_mismatch']}")
    if problems:
        raise GoldGenerationError(f"gold validation failed for {path}: " + "; ".join(problems))
    return report


def write_and_validate_gold(
    store,
    world: World,
    plan: DocumentPlan,
    doc: DocView,
    out_dir: str | Path,
) -> GoldBuildResult:
    """Build, write and validate one gold file, returning the report."""
    gold = build_gold(world, plan, doc)
    path = Path(out_dir) / gold_filename(plan.filename, gold.first_idx, gold.last_idx)
    write_gold_file(gold, path)
    report = assert_gold_valid(store, path, ontology=world.ontology)
    return GoldBuildResult(gold=gold, path=path, report=report)
