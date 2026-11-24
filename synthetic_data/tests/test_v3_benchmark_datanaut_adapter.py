"""The Datanaut export adapter (Phase J).

The payloads here are **contract fixtures** written by hand from the documented export
shape. They test the mapping, not the model: no extraction output existed when they were
written, and nothing here may be read as model performance.
"""
from __future__ import annotations

import pytest

from synthetic_data.benchmark.datanaut_adapter import (DatanautPredictionAdapter,
                                                      bundle_from_export,
                                                      document_from_export)
from synthetic_data.benchmark.evaluate import evaluate_document
from synthetic_data.benchmark.gold import GoldDocument, GoldEntity, GoldRelation

DOC = "doc:00000:test"

EXPORT = {
    "run_id": "run-test", "status": "complete",
    "run": {"id": "run-test", "model": "granite", "provider": "lmstudio"},
    "nodes": [
        {"id": "n1", "kind": "entity", "type": "organization",
         "canonical": "Dungate Industries GmbH", "variants": ["Dungate"],
         "attributes": {}, "status": "observed", "confidence": 0.9,
         "anchors": [{"id": "a1", "document_id": DOC, "page_no": 2,
                      "char_start": 10, "char_end": 42,
                      "quote": "Dungate Industries GmbH is the borrower."}]},
        {"id": "n2", "kind": "entity", "type": "organization",
         "canonical": "Junipermoor Holdings plc", "variants": [],
         "attributes": {}, "status": "observed", "confidence": 0.8,
         "anchors": [{"id": "a2", "document_id": DOC, "page_no": 2,
                      "char_start": 44, "char_end": 78,
                      "quote": "Junipermoor Holdings plc is the lender."}]},
    ],
    "edges": [
        {"id": "e1", "type": "lent_to", "source": "n1", "target": "n2",
         "role": None, "observed_verb": "advanced",
         "dates": {"start": "2023-01-23", "end": None},
         "attributes": {"amount": 12000000.0, "currency": "GBP", "status": "completed"},
         "anchors": [{"id": "a3", "document_id": DOC, "page_no": 3,
                      "quote": "GBP 12,000,000.00 was advanced on 23 January 2023."}],
         "confidence": 0.7},
        {"id": "e2", "type": "guarantees", "source": "n1", "target": "n-missing",
         "attributes": {}, "dates": {}, "anchors": [], "confidence": 0.4},
    ],
    "conflicts": [
        {"id": "c1", "subject": "the facility", "subject_id": "n1", "field": "amount",
         "claims": [{"value": "GBP 12,000,000.00",
                     "anchors": [{"document_id": DOC, "quote": "GBP 12,000,000.00"}]},
                    {"value": "GBP 12,500,000.00",
                     "anchors": [{"document_id": "doc:00004:other",
                                  "quote": "GBP 12,500,000.00"}]}]},
    ],
    "rejected": [{"reason": "model_call_failed", "detail": "connection refused"}],
    "coverage": {"documents": 1, "yielded": 1},
    "documents": [{"id": DOC}],
}


def test_entities_map_with_aliases_and_mentions():
    document = document_from_export(EXPORT, DOC, "pdf")
    assert [entity.name for entity in document.entities] == [
        "Dungate Industries GmbH", "Junipermoor Holdings plc"]
    assert document.entities[0].aliases == ["Dungate"]
    assert document.entities[0].mentions[0].page == 2
    assert document.entities[0].mentions[0].char_start == 10


def test_relations_map_with_typed_amount_status_and_date():
    document = document_from_export(EXPORT, DOC, "pdf")
    relation = document.relations[0]
    assert (relation.subject, relation.relation, relation.object) == \
        ("n1", "lent_to", "n2")
    assert relation.amount_minor == 1200000000
    assert relation.currency == "GBP"
    assert relation.status == "completed"
    assert relation.effective == "2023-01-23"
    assert relation.evidence[0].page == 3


def test_an_edge_with_an_unknown_endpoint_is_dropped_not_invented():
    document = document_from_export(EXPORT, DOC, "pdf")
    assert len(document.relations) == 1
    assert document.relations[0].relation == "lent_to"


def test_an_amount_without_a_currency_is_dropped():
    export = {"nodes": EXPORT["nodes"], "edges": [
        {"id": "e9", "type": "owes", "source": "n1", "target": "n2",
         "dates": {}, "attributes": {"amount": 500.0}, "anchors": []}]}
    document = document_from_export(export, DOC, "txt")
    assert document.relations[0].amount_minor is None


def test_conflicts_map_to_contradictions_with_their_documents():
    document = document_from_export(EXPORT, DOC, "pdf")
    contradiction = document.contradictions[0]
    assert contradiction.dimension == "amount"
    assert contradiction.values == ["GBP 12,000,000.00", "GBP 12,500,000.00"]
    assert DOC in contradiction.documents


def test_there_are_no_answers_because_datanaut_does_not_answer():
    document = document_from_export(EXPORT, DOC, "pdf")
    assert document.answers == []


def test_an_empty_export_is_an_empty_bundle_not_a_skipped_document():
    bundle = bundle_from_export({"nodes": [], "edges": []}, DOC, "docx")
    assert len(bundle.documents) == 1
    assert bundle.documents[0].entities == []


def test_an_empty_export_scores_zero_recall_against_gold():
    gold = GoldDocument(
        document_id=DOC, format="docx", doc_type="case_notes",
        entities=[GoldEntity("ent:a", ("Dungate Industries GmbH is the borrower.",))],
        relations=[GoldRelation("f1", "ent:a", "lent_to", "ent:a", False, "stated", ())])
    bundle = bundle_from_export({"nodes": [], "edges": []}, DOC, "docx")
    score = evaluate_document(gold, bundle.documents[0])
    assert score.entities.recall == 0.0
    assert score.relations.recall == 0.0
    assert score.entities.precision is None


def test_the_adapter_reads_a_file(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(__import__("json").dumps(EXPORT), encoding="utf-8")
    adapter = DatanautPredictionAdapter(path, DOC, "pdf")
    bundle = adapter.load()
    assert len(bundle.documents[0].entities) == 2
    assert bundle.adapter.startswith("datanaut:")


def test_a_relation_is_scoreable_when_gold_uses_the_same_direction():
    gold = GoldDocument(
        document_id=DOC, format="pdf", doc_type="financial_agreement",
        entities=[GoldEntity("ent:a", ("Dungate Industries GmbH is the borrower.",)),
                  GoldEntity("ent:b", ("Junipermoor Holdings plc is the lender.",))],
        relations=[GoldRelation("f1", "ent:a", "lent_to", "ent:b", False, "completed",
                                ())])
    document = document_from_export(EXPORT, DOC, "pdf")
    score = evaluate_document(gold, document)
    assert score.entities.f1 == 1.0
    assert score.relations.tp == 1
