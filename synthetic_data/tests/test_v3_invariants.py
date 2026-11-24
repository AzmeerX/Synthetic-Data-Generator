"""Phase A — the 11 architecture invariants, corpus integrity and round trips.

Each invariant from §4.4 of the architecture document has a test named for it, so
the suite maps one-to-one onto the design.

Note: Pydantic wraps a failing validator in ``ValidationError``, so violations
raised by :class:`Corpus` surface as ``ValidationError`` whose message contains
the underlying problem.  Assertions check that message, not just the exception
type, to prove the *right* invariant failed.
"""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from synthetic_data.model import (
    Corpus,
    CorpusViolation,
    EvidenceRef,
    RenderMap,
    RenderLoc,
    RenderSpan,
    assert_render_coverage,
)
from synthetic_data.model.schema import SCHEMAS, export_schemas, schema_for
from synthetic_data.tests.v3_builders import build_fixture

FIX = build_fixture()
DOC1 = FIX.id("doc:00001")
DOC2 = FIX.id("doc:00002")
DOC3 = FIX.id("doc:00003")
DOC4 = FIX.id("doc:00004")
F_LENT = FIX.id("fact:edge:00001:02")
F_ALT = FIX.id("fact:edge:00002:01")


def data() -> dict:
    return copy.deepcopy(FIX.corpus.model_dump())


def assert_invalid(payload: dict, needle: str) -> None:
    with pytest.raises((ValidationError, CorpusViolation)) as exc:
        Corpus.model_validate(payload)
    assert needle.lower() in str(exc.value).lower(), (
        f"expected {needle!r} in error, got: {exc.value}")


def test_valid_corpus_constructs():
    assert len(FIX.corpus.documents) == 4
    assert len(FIX.corpus.world.facts) == 5
    assert FIX.corpus.documents[DOC1].statements


# --------------------------------------------------------------------------
# invariant 1 — statement <-> fact, both directions
# --------------------------------------------------------------------------
def test_invariant_01_statement_fact_reciprocity():
    payload = data()
    payload["documents"][DOC1]["statements"][0]["fact_id"] = "fact:edge:99999:01:zzzz"
    assert_invalid(payload, "unknown fact")


def test_invariant_01_every_stated_fact_is_listed_on_the_fact():
    payload = data()
    fact = payload["world"]["facts"][F_LENT]
    fact["statement_ids"] = fact["statement_ids"][:1]
    assert_invalid(payload, "statement_ids disagree")


# --------------------------------------------------------------------------
# invariant 2 — table cells resolve and agree with the fact
# --------------------------------------------------------------------------
def test_invariant_02_unknown_cell_fact_is_rejected():
    payload = data()
    table = payload["documents"][DOC1]["sections"][1]["blocks"][0]["table"]
    table["cell_refs"]["r01c01"] = "fact:edge:99999:01:zzzz"
    table["body_rows"][0]["cells"][1]["fact_ref"] = "fact:edge:99999:01:zzzz"
    assert_invalid(payload, "unknown fact")


def test_invariant_02_cell_value_must_equal_fact_value():
    payload = data()
    table = payload["documents"][DOC1]["sections"][1]["blocks"][0]["table"]
    table["body_rows"][0]["cells"][1]["value"] = {"amount_minor": 999, "currency": "GBP"}
    table["body_rows"][0]["cells"][1]["display"] = "GBP 9.99"
    assert_invalid(payload, "disagrees with fact")


# --------------------------------------------------------------------------
# invariant 3 — inline run values match the fact
# --------------------------------------------------------------------------
def test_invariant_03_inline_run_value_must_equal_fact_value():
    payload = data()
    run = payload["documents"][DOC1]["sections"][0]["blocks"][1]["runs"][0]
    run["value"] = {"amount_minor": 1, "currency": "GBP"}
    assert_invalid(payload, "disagrees with fact")


# --------------------------------------------------------------------------
# invariant 4 — unique block and document ids
# --------------------------------------------------------------------------
def test_invariant_04_node_ids_unique_within_a_document():
    payload = data()
    blocks = payload["documents"][DOC1]["sections"][0]["blocks"]
    blocks[1]["block_id"] = blocks[0]["block_id"]
    assert_invalid(payload, "duplicate node id")


def test_invariant_04_node_ids_unique_across_documents():
    payload = data()
    donor = payload["documents"][DOC1]["sections"][0]["blocks"][0]["block_id"]
    doc2 = payload["documents"][DOC2]
    doc2["sections"][0]["blocks"][0]["block_id"] = donor
    doc2["statements"][0]["node_id"] = donor      # keep the document internally valid
    assert_invalid(payload, "appears in")


# --------------------------------------------------------------------------
# invariant 5 — cross references resolve
# --------------------------------------------------------------------------
def test_invariant_05_cross_reference_must_resolve():
    payload = data()
    payload["world"]["cross_references"][0]["dst_document"] = "doc:99999:zzzz"
    assert_invalid(payload, "unknown document")


# --------------------------------------------------------------------------
# invariant 6 — contradiction has two facts with different values
# --------------------------------------------------------------------------
def test_invariant_06_contradiction_requires_different_values():
    payload = data()
    payload["world"]["facts"][F_ALT]["amount"] = {"amount_minor": 400_000_000,
                                                  "currency": "GBP"}
    assert_invalid(payload, "equal amounts")


# --------------------------------------------------------------------------
# invariant 7 — evidence never comes from the intended surface
# --------------------------------------------------------------------------
def test_invariant_07_evidence_rejects_unusable_alignment():
    for kind in ("ambiguous", "missing"):
        with pytest.raises(ValidationError):
            EvidenceRef(sentence_id="s3", quote="intended surface",
                        canonical_node_id=FIX.id("blk:00001/01.01"),
                        alignment_kind=kind, render_format="txt", page=1)


# --------------------------------------------------------------------------
# invariant 8 — every rendered node appears exactly once
# --------------------------------------------------------------------------
def test_invariant_08_render_coverage_is_exact():
    span = RenderSpan(canonical_id=FIX.id("blk:00001/01.01"), kind="block",
                      format="txt", loc=RenderLoc(format="txt", page=1),
                      text="x", text_sha256="0" * 64)
    render_map = RenderMap(document_id=DOC1, format="txt", spans=[span])
    with pytest.raises(ValueError, match="missing from render map"):
        assert_render_coverage(FIX.corpus.documents[DOC1].node_ids(), render_map)


# --------------------------------------------------------------------------
# invariant 9 — one fact id per fact
# --------------------------------------------------------------------------
def test_invariant_09_duplicate_fact_payload_is_rejected():
    payload = data()
    duplicate = copy.deepcopy(payload["world"]["facts"][F_LENT])
    duplicate["fact_id"] = "fact:edge:00001:09:dddd"
    duplicate["statement_ids"] = []
    payload["world"]["facts"][duplicate["fact_id"]] = duplicate
    assert_invalid(payload, "describe the same fact")


# --------------------------------------------------------------------------
# invariant 10 — thread/message agreement
# --------------------------------------------------------------------------
def test_invariant_10_message_sequence_must_match_position():
    payload = data()
    payload["documents"][DOC4]["metadata"]["thread"]["sequence"] = 3
    assert_invalid(payload, "sequence")


def test_invariant_10_reply_must_target_an_earlier_message():
    payload = data()
    meta = payload["documents"][DOC4]["metadata"]["thread"]
    meta["references"] = [meta["message_id"]]
    assert_invalid(payload, "not earlier")


# --------------------------------------------------------------------------
# invariant 11 — contradictions are grounded in two documents
# --------------------------------------------------------------------------
def test_invariant_11_contradiction_facts_must_be_stated_in_the_named_documents():
    payload = data()
    payload["world"]["contradictions"][FIX.id("ctr:0001")]["documents"] = [DOC3, DOC4]
    assert_invalid(payload, "names no document that states")


# --------------------------------------------------------------------------
# serialization and schema
# --------------------------------------------------------------------------
def test_serialization_round_trip_is_stable():
    dumped = FIX.corpus.model_dump(mode="json")
    rebuilt = Corpus.model_validate(dumped)
    assert rebuilt.model_dump(mode="json") == dumped


# --------------------------------------------------------------------------
# fact -> statement -> occurrence chain, and one fact across documents
# --------------------------------------------------------------------------
def test_fact_statement_occurrence_chain():
    """One fact, three layers: semantic id, statements, render occurrences."""
    from synthetic_data.model import RenderOccurrence

    fact_id = FIX.id("fact:edge:00001:02")
    doc = FIX.corpus.documents[DOC1]
    statements = [s for s in doc.statements if s.fact_id == fact_id]
    assert len(statements) == 2, "the fixture states this fact in prose and in a cell"

    cell = next(node for _, node in doc.iter_nodes()
                if getattr(node, "cell_id", None) == statements[1].node_id)
    assert getattr(cell, "fact_ref", None) == fact_id

    occurrence = RenderOccurrence(
        occurrence_id="rnd:xlsx:00001:01", canonical_id=getattr(cell, "cell_id"),
        format="xlsx", loc=RenderLoc(format="xlsx", pageless=True,
                                     sheet_name="Transactions", cell_ref="B2"),
    )
    assert occurrence.canonical_id == statements[1].node_id
    assert fact_id in FIX.corpus.world.facts


def test_same_fact_from_two_documents_keeps_one_id():
    fact_id = FIX.id("fact:edge:00001:01")
    doc1 = FIX.corpus.documents[DOC1]
    doc4 = FIX.corpus.documents[DOC4]
    in_doc1 = [s for s in doc1.statements if s.fact_id == fact_id]
    in_doc4 = [s for s in doc4.statements if s.fact_id == fact_id]
    assert in_doc1 and in_doc4
    assert in_doc1[0].fact_id == in_doc4[0].fact_id
    assert {s.statement_id for s in in_doc1}.isdisjoint({s.statement_id for s in in_doc4})


def test_schemas_are_in_the_registry():
    assert set(SCHEMAS) == {"cdm_document", "cdm_workbook", "corpus",
                            "render_map", "render_result"}
    schema = schema_for("cdm_document")
    assert schema["properties"]["document_id"]
    assert "sections" in schema["properties"]


def test_schema_export_is_deterministic(tmp_path):
    first = export_schemas(tmp_path / "a")
    second = export_schemas(tmp_path / "b")
    assert set(first) == set(second)
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes()
        json.loads(first[name].read_text(encoding="utf-8"))
