"""Phase G integration pilot: the smallest corpus that exercises every path.

22 primary artifacts: seven prose documents (one per type) in TXT, PDF and DOCX,
plus one workbook.  One canonical semantic world drives all of them, so this file
also proves that a format is not a document: the same fact id appears in three
renderings of one document and is never counted three times.

Alignment rates are asserted **per format** and reported with numerator and
denominator; an overall average would hide a weak format.
"""
from __future__ import annotations

import json

import pytest

from synth.eval.gold import GoldSet, load_gold_file

from synthetic_data.alignment import EVIDENCE_KINDS
from synthetic_data.gold import (GoldV2, validate_alignment, validate_v1,
                                 validate_v2, write_gold_pair)
from synthetic_data.gold.model import AlignmentArtifact
from synthetic_data.pilot_phase_g import partition_artifacts, primary_id
from synthetic_data.tests.v3_gold import gold_pilot, gold_store, pilot_plan

PROSE_TYPES = {"board_minutes", "financial_agreement", "due_diligence", "case_notes",
               "correspondence", "memorandum", "press_release"}


# -- composition -----------------------------------------------------------
def test_the_pilot_has_twenty_two_primary_artifacts(gold_pilot):
    assert len(gold_pilot) == 22


def test_seven_prose_documents_in_three_formats(gold_pilot):
    prose = [item for item in gold_pilot if item.document.doc_kind == "prose"]
    assert len(prose) == 21
    by_document: dict[str, set[str]] = {}
    for item in prose:
        by_document.setdefault(item.document_id, set()).add(item.format)
    assert len(by_document) == 7
    assert {item.document.doc_type for item in prose} == PROSE_TYPES
    for document_id, formats in by_document.items():
        assert formats == {"txt", "pdf", "docx"}, (document_id, formats)


def test_exactly_one_workbook_in_xlsx(gold_pilot):
    workbooks = [item for item in gold_pilot if item.document.doc_kind == "workbook"]
    assert len(workbooks) == 1
    assert workbooks[0].format == "xlsx"


def test_one_world_drives_every_artifact(gold_pilot):
    plan = pilot_plan()
    assert plan.world.corpus_id
    for item in gold_pilot:
        for edge in item.pair.v1.edges:
            assert edge.id in plan.world.facts, (item.document_id, edge.id)


# -- grounding -------------------------------------------------------------
def test_alignment_rate_is_at_least_95_percent_per_format(gold_pilot):
    by_format: dict[str, tuple[int, int]] = {}
    for item in gold_pilot:
        total = len(item.pair.evidence)
        grounded = sum(1 for evidence in item.pair.evidence if evidence.acceptable)
        numerator, denominator = by_format.get(item.format, (0, 0))
        by_format[item.format] = (numerator + grounded, denominator + total)
    assert set(by_format) == {"txt", "pdf", "docx", "xlsx"}
    for fmt, (grounded, total) in by_format.items():
        rate = grounded / total if total else 1.0
        assert rate >= 0.95, f"{fmt}: {grounded}/{total} = {rate:.3f}"


def test_no_missing_partial_or_ambiguous_factual_alignment(gold_pilot):
    for item in gold_pilot:
        kinds = {kind for evidence in item.pair.evidence for kind in evidence.kinds}
        assert kinds <= EVIDENCE_KINDS, (item.document_id, item.format, kinds)
        for unit in item.pair.alignment.units:
            assert unit.kind not in {"missing", "partial", "ambiguous"}, \
                (item.document_id, item.format, unit.canonical_id, unit.kind)


def test_no_unexplained_parser_sentences(gold_pilot):
    for item in gold_pilot:
        report = item.pair.accounting
        assert report is not None
        assert report.ok, report.describe()


# -- gold v1 / v2 ----------------------------------------------------------
def test_v1_is_accepted_by_the_synth_validator(tmp_path, gold_store, gold_pilot):
    checked = 0
    for item in gold_pilot:
        paths = write_gold_pair(item.pair, tmp_path / "gold", item.document_id,
                                item.document.doc_type, item.format)
        report = validate_v1(gold_store, paths[0])
        assert report.ok, (item.document_id, item.format, report.errors[:3])
        assert load_gold_file(paths[0]).version == 1
        checked += 1
    assert checked == 22


def test_v2_schema_and_references_validate(gold_pilot):
    plan = pilot_plan()
    for item in gold_pilot:
        artifact = GoldV2.model_validate(item.pair.v2.model_dump(mode="json"))
        errors = validate_v2(artifact, view=item.view, render=item.render,
                             canonical=item.canonical, document=item.document,
                             world=plan.world, row_quotes=item.row_quotes)
        assert not errors, (item.document_id, item.format, errors[:3])
        entity_ids = {entity.id for entity in artifact.entities}
        for edge in artifact.edges:
            assert {edge.subject, edge.object} <= entity_ids
            assert edge.canonical_fact_id


def test_alignment_artifacts_agree_with_both_gold_versions(gold_pilot):
    for item in gold_pilot:
        errors = validate_alignment(item.pair.alignment, view=item.view,
                                    render=item.render)
        assert not errors, (item.document_id, item.format, errors[:3])
        counts: dict[str, int] = {}
        for unit in item.pair.alignment.units:
            counts[unit.kind] = counts.get(unit.kind, 0) + 1
        assert counts == item.pair.alignment.counts
        v1_sentences = {evidence.sentence_id for edge in item.pair.v1.edges
                        for evidence in edge.evidence}
        v2_sentences = {evidence.sentence_id for edge in item.pair.v2.edges
                        for evidence in edge.evidence}
        assert v1_sentences == v2_sentences


# -- contradiction and identity -------------------------------------------
def test_a_contradiction_is_grounded_on_both_sides(gold_pilot):
    plan = pilot_plan()
    assert plan.world.contradictions, "the pilot must declare a contradiction"
    stated: dict[str, set[str]] = {}
    for item in gold_pilot:
        for edge in item.pair.v1.edges:
            stated.setdefault(edge.id, set()).add(item.document_id)
        for prop in item.pair.v1.propositions:
            stated.setdefault(prop.id, set()).add(item.document_id)
    grounded = []
    for contradiction in plan.world.contradictions.values():
        a = stated.get(contradiction.fact_a, set())
        b = stated.get(contradiction.fact_b, set())
        if a and b and (a | b):
            grounded.append((contradiction, a, b))
    assert grounded, "no contradiction has both sides stated"
    contradiction, a, b = grounded[0]
    assert a and b
    assert len(a | b) >= 2, "the two sides must be grounded in different documents"


def test_a_fact_keeps_one_identity_across_documents(gold_pilot):
    plan = pilot_plan()
    stated: dict[str, set[str]] = {}
    for item in gold_pilot:
        for edge in item.pair.v1.edges:
            stated.setdefault(edge.id, set()).add(item.document_id)
    shared = {fact: documents for fact, documents in stated.items()
              if len(documents) > 1}
    assert shared, "no fact is stated in more than one document"
    for fact_id, documents in shared.items():
        assert fact_id in plan.world.facts
        assert len(documents) >= 2


def test_renderings_of_one_document_are_not_counted_as_documents(gold_pilot):
    documents = {item.document_id for item in gold_pilot}
    assert len(documents) == 8          # 7 prose + 1 workbook, not 22
    assert len(gold_pilot) == 22


# -- equivalence separation ------------------------------------------------
def test_equivalence_fixtures_are_excluded_from_benchmark_counts():
    from synthetic_data.pilot_phase_g import Artifact
    from pathlib import Path

    def make(document_id: str, fmt: str) -> Artifact:
        return Artifact(
            document_id=document_id, doc_type="financial_agreement", doc_kind="prose",
            format=fmt, path=Path("x"), parse_doc_id="doc_x",
            render_sha256="0" * 64, pages=1, pageless=False, units=1, counts={},
            facts_total=1, facts_grounded=1, accounting_total=1, accounting_counts={},
            unexplained=0, currency_stated=0, currency_not_stated=0, derived=0,
            bbox_coverage=None, v1_ok=True)

    primary = [make("doc:1", "txt"), make("doc:1", "pdf"), make("doc:1", "docx")]
    equivalence = [make("doc:1", "txt")]
    partition = partition_artifacts(primary, equivalence)
    assert partition["primary"] == 3
    assert partition["equivalence"] == 1
    assert partition["benchmark_counts_equivalence"] == 0
    assert len(partition["primary_keys"]) == 3
    assert primary_id("doc:1", "txt") == "doc:1::txt"


def test_the_pilot_manifest_excludes_equivalence_when_present():
    from synthetic_data.pilot_phase_g import PILOT_DIR

    manifest_path = PILOT_DIR / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("pilot artifacts have not been generated in this checkout")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["primary_artifacts"] == 22
    assert manifest["equivalence"]["counted_in_benchmark"] is False
    assert manifest["partition"]["benchmark_counts_equivalence"] == 0
    assert len(manifest["partition"]["primary_keys"]) == 22
    assert all(item["ok"] for item in manifest["artifacts"])
    assert manifest["all_valid"] is True
