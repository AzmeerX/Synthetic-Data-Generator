"""Tests for gold construction from structured document plans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from synth.eval.gold import GoldEvidence, load_gold_file

from synthetic_data.generator.annotations import (
    GoldGenerationError,
    build_gold,
    write_and_validate_gold,
)
from synthetic_data.generator.documents import build_document_plan


@dataclass
class _FakeView:
    """Just enough of ``DocView`` for a broken-splitter test."""

    doc: dict
    text: str
    sentences: list[dict]

    def sentence_text(self, sentence: dict) -> str:
        return sentence["text"]


def _sentence_texts(view) -> dict[str, str]:
    return {f"s{s['idx']}": view.sentence_text(s) for s in view.sentences}


def test_gold_is_generated_from_the_plan(world, ingest_plan, tmp_path):
    plan = build_document_plan(world, 0, 5)
    _path, _record, view, _context = ingest_plan(plan, tmp_path / "docs")

    gold = build_gold(world, plan, view)

    assert gold.mode == "generated"
    assert gold.annotator == "synthetic_data.generator"
    assert "not blind" in gold.notes.casefold()
    assert gold.doc.filename == plan.filename
    assert gold.doc.doc_id is None
    assert gold.doc.text_sha256 == hashlib.sha256(view.text.encode("utf-8")).hexdigest()
    assert gold.first_idx == 0
    assert gold.last_idx == len(view.sentences) - 1
    assert len(gold.edges) == plan.relationship_count
    assert len(gold.propositions) == plan.proposition_count
    assert gold.entities
    assert gold.time_refs
    assert gold.expected_flags
    assert gold.identity_traps


def test_every_gold_quote_is_grounded_in_the_ingested_text(world, ingest_plan, tmp_path):
    plan = build_document_plan(world, 4, 4)
    _path, _record, view, _context = ingest_plan(plan, tmp_path / "docs")
    gold = build_gold(world, plan, view)
    sentences = _sentence_texts(view)

    evidence: list[GoldEvidence] = []
    for entity in gold.entities:
        evidence.extend(entity.mentions)
    for edge in gold.edges:
        evidence.extend(edge.evidence)
    for proposition in gold.propositions:
        evidence.extend(proposition.evidence)
    for link in gold.links:
        evidence.extend(link.evidence)
    for time_ref in gold.time_refs:
        evidence.append(time_ref.evidence)

    assert evidence
    for item in evidence:
        assert item.sentence_id in sentences
        sentence = sentences[item.sentence_id]
        assert item.quote in sentence, f"quote {item.quote!r} not in {item.sentence_id}"
        assert sentence.count(item.quote) > item.occurrence


def test_gold_references_only_corpus_entities_and_facts(world, ingest_plan, tmp_path):
    plan = build_document_plan(world, 2, 3)
    _path, _record, view, _context = ingest_plan(plan, tmp_path / "docs")
    gold = build_gold(world, plan, view)

    entity_ids = {entity.id for entity in gold.entities}
    edge_ids = {edge.id for edge in gold.edges}
    proposition_ids = {proposition.id for proposition in gold.propositions}
    planned_ids = {stated.fact.fact_id for stated in plan.stated_facts}

    assert entity_ids == set(plan.entity_ids)
    assert {edge.id for edge in gold.edges} <= planned_ids
    assert {proposition.id for proposition in gold.propositions} <= planned_ids
    assert {link.src for link in gold.links} <= proposition_ids
    assert {link.dst for link in gold.links} <= proposition_ids

    assert {flag.kind for flag in gold.expected_flags} <= {
        "identity_ambiguity", "inconsistency", "conflict", "merge_blocked"}
    for flag in gold.expected_flags:
        assert set(flag.object_ids) <= entity_ids | edge_ids
    for trap in gold.identity_traps:
        assert {trap.a, trap.b} <= entity_ids

    assert len({ref.id for ref in gold.time_refs}) == len(gold.time_refs)
    for time_ref in gold.time_refs:
        assert time_ref.kind in {"INSTANT", "INTERVAL", "PERIOD", "DURATION",
                                 "RECURRING", "RELATIVE"}
        assert time_ref.anchor_type in {"ABSOLUTE", "DOC_DATE", "ROLLING_BASE",
                                        "EVENT", "UNRESOLVED"}


def test_edge_time_roles_are_supported(world, ingest_plan, tmp_path):
    plan = build_document_plan(world, 0, 5)
    _path, _record, view, _context = ingest_plan(plan, tmp_path / "docs")
    gold = build_gold(world, plan, view)

    roles = {time.role for edge in gold.edges for time in edge.time}
    assert roles
    assert roles <= {"occurred_at", "valid_from", "valid_to", "as_of"}


def test_build_gold_rejects_a_changed_sentence(world, ingest_plan, tmp_path):
    plan = build_document_plan(world, 1, 4)
    _path, _record, _view, _context = ingest_plan(plan, tmp_path / "docs")

    sentences = [{"idx": index, "text": text}
                 for index, text in enumerate(plan.sentences)]
    sentences[3]["text"] = "The parser produced a different sentence."
    broken = _FakeView(
        doc={"doc_id": "not-a-real-doc"},
        text="\n".join(sentence["text"] for sentence in sentences),
        sentences=sentences,
    )

    with pytest.raises(GoldGenerationError, match="does not match"):
        build_gold(world, plan, broken)


def test_write_and_validate_gold_writes_a_reproducible_file(
        world, ingest_plan, tmp_path, store):
    plan = build_document_plan(world, 0, 5)
    _path, _record, view, _context = ingest_plan(plan, tmp_path / "docs")

    result = write_and_validate_gold(store, world, plan, view, tmp_path / "gold")

    assert result.path.exists()
    assert result.report["ok"] is True
    assert result.report["sentence_mismatch"] == []
    assert result.report["not_found"] == []
    assert result.report["errors"] == []
    assert result.report["grounding"] == {"EXACT": result.report["items"]}

    saved = load_gold_file(result.path)
    assert saved.mode == "generated"
    assert saved.doc.doc_id is None
    assert saved.doc.text_sha256 == result.gold.doc.text_sha256
    assert saved.notes == result.gold.notes
