"""Evaluator behaviour under deliberately corrupted predictions (Phase I).

The gold here is built by hand — three entities, two relations, two questions — so the
expected numbers are arithmetic a reader can check, not the generator's own output.
Each test breaks exactly one thing and asserts the metric that must move.
"""
from __future__ import annotations

import pytest

from synthetic_data.benchmark.evaluate import evaluate_corpus, evaluate_document
from synthetic_data.benchmark.gold import (GoldAnswer, GoldDocument, GoldEntity,
                                           GoldEvidence, GoldRelation)
from synthetic_data.benchmark.schema import (PREDICTION_SCHEMA_VERSION,
                                             PredictedAnswer, PredictedDocument,
                                             PredictedEntity, PredictedEvidence,
                                             PredictedRelation, PredictionBundle)

DOC = "doc:00000:test"
OWN_QUOTE = "| Dungate Industries GmbH | Junipermoor Holdings plc | 40.00% |"
LOAN_QUOTE = "GBP 12,000,000.00 was advanced on 23 January 2023."


def gold_document() -> GoldDocument:
    return GoldDocument(
        document_id=DOC, format="txt", doc_type="case_notes",
        entities=[GoldEntity("ent:a", ("Dungate Industries GmbH is the borrower.",)),
                  GoldEntity("ent:b", ("Junipermoor Holdings plc is the lender.",)),
                  GoldEntity("ent:c", ("Sofia Kerr is a director of Dungate.",))],
        relations=[
            GoldRelation("f1", "ent:a", "owns", "ent:b", False, "completed",
                         (GoldEvidence(OWN_QUOTE, "s1", {"page": 1}, "f1"),)),
            GoldRelation("f2", "ent:a", "lent_to", "ent:b", False, "stated",
                         (GoldEvidence(LOAN_QUOTE, "s2",
                                       {"page": 2, "sheet_name": "Ledger",
                                        "cell_ref": "E4"}, "f2"),),
                         effective="2023-01-23"),
        ],
        answers=[
            GoldAnswer("q1", DOC, "txt", "financial", True, "GBP 12,000,000.00", "money",
                       ("f2",), (GoldEvidence(LOAN_QUOTE, "s2", {"page": 2}, "f2"),)),
            GoldAnswer("q2", DOC, "txt", "unanswerable", False, None, None, (), (),
                       "absent_attribute"),
        ])


def prediction() -> PredictedDocument:
    return PredictedDocument(
        document_id=DOC, format="txt", doc_type="case_notes",
        entities=[PredictedEntity(entity_id="p1", name="Dungate Industries GmbH"),
                  PredictedEntity(entity_id="p2", name="Junipermoor Holdings plc"),
                  PredictedEntity(entity_id="p3", name="Sofia Kerr")],
        relations=[
            PredictedRelation(subject="p1", object="p2", relation="owns",
                              status="completed", negated=False,
                              evidence=[PredictedEvidence(quote=OWN_QUOTE,
                                                          sentence_id="s1", page=1)]),
            PredictedRelation(subject="p1", object="p2", relation="lent_to",
                              status="stated", negated=False,
                              amount_minor=1200000000, currency="GBP",
                              effective="2023-01-23",
                              evidence=[PredictedEvidence(quote=LOAN_QUOTE,
                                                          sentence_id="s2", page=2,
                                                          sheet="Ledger", cell="E4")]),
        ],
        answers=[PredictedAnswer(question_id="q1", answer="GBP 12,000,000.00",
                                 evidence=[PredictedEvidence(quote=LOAN_QUOTE,
                                                             sentence_id="s2", page=2)]),
                 PredictedAnswer(question_id="q2", answerable=False)])


def score(prediction_document: PredictedDocument | None, gold: GoldDocument | None = None):
    gold = gold or gold_document()
    return evaluate_document(gold, prediction_document)


def test_1_perfect_predictions_score_one_everywhere():
    result = score(prediction())
    assert result.entities.f1 == 1.0
    assert result.relations.f1 == 1.0
    assert result.evidence.f1 == 1.0
    assert result.answers.f1 == 1.0
    assert result.answerability.f1 == 1.0
    assert result.values.f1 == 1.0
    assert result.locators.f1 == 1.0


def test_2_no_prediction_at_all_is_scored_as_missing_not_skipped():
    result = score(None)
    assert result.predicted is False
    assert result.entities.recall == 0.0
    assert result.relations.recall == 0.0
    assert result.entities.fn == 3
    assert result.answers.fn == 1
    assert result.answerability.fn == 1
    assert result.missing_questions == 2


def test_2b_an_empty_bundle_still_counts_every_document():
    gold = {(DOC, "txt"): gold_document()}
    bundle = PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION,
                              adapter="test:empty", documents=[])
    result = evaluate_corpus(gold, bundle)
    assert result["corpus"]["documents"] == 1
    assert result["corpus"]["entities"]["recall"] == 0.0
    assert result["corpus"]["missing_questions"] == 2


def test_3_wrong_entity_identity_is_a_false_positive_and_a_miss():
    document = prediction()
    document.entities[0].name = "Junipermoor Holdings plc"  # a's name replaced by b's
    result = score(document)
    assert result.entities.tp == 2
    assert result.entities.fp >= 1
    assert result.entities.fn >= 1


def test_4_reversed_relation_direction_is_not_a_match():
    document = prediction()
    document.relations[0].subject, document.relations[0].object = "p2", "p1"
    result = score(document)
    assert result.relations.tp == 1
    assert result.relations.fp == 1
    assert result.relations.fn == 1
    assert result.notes.get("reversed_direction") == 1


def test_5_wrong_amount_moves_the_value_metric_only():
    document = prediction()
    document.relations[1].amount_minor += 100
    result = score(document)
    assert result.relations.f1 == 1.0
    assert result.values.tp == 1
    assert result.values.fp == 1
    assert result.notes.get("amount_mismatch") == 1


def test_6_wrong_currency_is_reported_as_a_currency_error():
    document = prediction()
    document.relations[1].currency = "USD"
    result = score(document)
    assert result.values.fp == 1
    assert result.notes.get("currency_mismatch") == 1


def test_7_wrong_date_is_reported_as_a_date_error():
    document = prediction()
    document.relations[1].effective = "2022-01-23"
    result = score(document)
    assert result.values.fp >= 1
    assert result.notes.get("date_mismatch") == 1


def test_8_wrong_proposition_status_is_not_a_match():
    document = prediction()
    document.relations[0].status = "proposed"
    result = score(document)
    assert result.relations.tp == 1
    assert result.notes.get("status_mismatch") == 1


def test_8b_wrong_negation_is_not_a_match():
    document = prediction()
    document.relations[0].negated = True
    result = score(document)
    assert result.notes.get("negation_mismatch") == 1


def test_9_an_unsupported_answer_loses_the_answer_metric():
    document = prediction()
    document.answers[0].answer = "GBP 99,000,000.00"
    result = score(document)
    # the other question is a correct refusal, which still counts as a correct answer
    assert result.answers.tp == 1
    assert result.answers.fp == 1
    assert result.answers.fn == 1
    assert result.answerability.f1 == 1.0


def test_10_a_right_answer_with_a_wrong_quote_loses_the_evidence_metric():
    document = prediction()
    document.answers[0].evidence[0].quote = "twelve million pounds"
    result = score(document)
    assert result.answers.f1 == 1.0
    assert result.evidence.fp >= 1
    assert result.evidence.recall < 1.0


def test_11_answering_an_unanswerable_question_is_a_false_positive():
    document = prediction()
    document.answers[1] = PredictedAnswer(question_id="q2", answer="GBP 1.00",
                                          answerable=True)
    result = score(document)
    assert result.answerability.fp == 1
    assert result.answerability.precision == 0.5
    assert result.answerability.recall == 1.0
    assert result.answers.fn == 1
    assert result.notes.get("answered_unanswerable") == 1


def test_12_a_correct_refusal_is_credited():
    result = score(prediction())
    assert result.answerability.tp == 2
    assert result.answerability.precision == 1.0


def test_13_a_wrong_pdf_page_is_a_locator_error():
    document = prediction()
    document.relations[1].evidence[0].page = 5
    result = score(document)
    assert result.locators.fp == 1
    assert result.notes.get("page_mismatch") == 1
    assert result.evidence.f1 == 1.0


def test_14_a_wrong_docx_section_is_a_locator_error():
    gold = gold_document()
    gold.format = "docx"
    gold.relations[1] = GoldRelation(
        "f2", "ent:a", "lent_to", "ent:b", False, "stated",
        (GoldEvidence(LOAN_QUOTE, "s2", {"section_index": 1}, "f2"),),
        effective="2023-01-23")
    document = prediction()
    document.format = "docx"
    document.relations[1].evidence[0] = PredictedEvidence(
        quote=LOAN_QUOTE, sentence_id="s2", section=3)
    result = score(document, gold)
    assert result.locators.fp == 1
    assert result.notes.get("section_mismatch") == 1


def test_15_a_wrong_xlsx_cell_is_a_locator_error():
    gold = gold_document()
    gold.format = "xlsx"
    document = prediction()
    document.format = "xlsx"
    document.relations[1].evidence[0].cell = "F9"
    result = score(document, gold)
    assert result.locators.fp == 1
    assert result.notes.get("cell_mismatch") == 1


def test_a_prediction_without_a_gold_document_is_reported():
    gold = {(DOC, "txt"): gold_document()}
    bundle = PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION,
                              documents=[prediction()])
    extra = prediction()
    extra.document_id = "doc:99999:other"
    bundle.documents.append(extra)
    result = evaluate_corpus(gold, bundle)
    assert result["unmatched_predictions"] == ["doc:99999:other/txt"]


def test_metrics_are_split_by_format_and_document_type():
    gold = {(DOC, "txt"): gold_document()}
    bundle = PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION,
                              documents=[prediction()])
    result = evaluate_corpus(gold, bundle)
    assert set(result["by_format"]) == {"txt"}
    assert set(result["by_doc_type"]) == {"case_notes"}
    assert result["by_format"]["txt"]["entities"]["f1"] == 1.0
