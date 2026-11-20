"""Tests for answer-level evaluation metrics."""

from __future__ import annotations

import json

import pytest

from synthetic_data.generator.evaluation import (
    evaluate_files,
    evaluate_qa,
    exact_match,
    load_predictions,
    normalized_exact_match,
    token_f1,
    write_report,
)
from synthetic_data.schemas.qa import (
    PredictionCitation,
    PredictionSet,
    QAPrediction,
)


def _oracle(dataset) -> PredictionSet:
    predictions = []
    for query in dataset.queries:
        if query.answerable:
            predictions.append(QAPrediction(
                query_id=query.query_id,
                answer=query.expected_answer,
                answerable=True,
                citations=[
                    PredictionCitation(document_id=evidence.document_id,
                                       sentence_id=evidence.sentence_id)
                    for evidence in query.supporting_evidence
                ],
            ))
        else:
            predictions.append(QAPrediction(
                query_id=query.query_id, answer="", answerable=False))
    return PredictionSet(predictions=predictions)


def _record(report, query_id):
    return next(record for record in report["per_query"]
                if record["query_id"] == query_id)


@pytest.mark.parametrize(("predicted", "expected"), [
    ("GBP 12 million", "gbp 12 million."),
    ("The Borrower", "borrower"),
    ("31 July 2024", "2024-07-31"),
    ("GBP 12,000,000", "GBP 12000000"),
    ("No", "no."),
])
def test_normalized_exact_match_handles_safe_variants(predicted, expected):
    assert normalized_exact_match(predicted, expected) == 1.0


def test_normalized_exact_match_does_not_invent_semantics():
    assert normalized_exact_match("GBP 12 million", "GBP 21 million") == 0.0
    assert normalized_exact_match("Alleged", "Proven") == 0.0


def test_exact_match_is_raw_apart_from_outer_whitespace():
    assert exact_match("No", "No") == 1.0
    assert exact_match(" No ", "No") == 1.0
    assert exact_match("No", "no") == 0.0


def test_token_f1_edge_cases():
    assert token_f1("GBP 12 million", "GBP 12 million") == 1.0
    assert token_f1("GBP 12 million", "GBP 15 million") == pytest.approx(2 / 3)
    assert token_f1("yes", "no") == 0.0
    assert token_f1("", "") == 1.0
    assert token_f1("", "answer") == 0.0


def test_oracle_predictions_score_perfectly(pilot):
    report = evaluate_qa(pilot.dataset, _oracle(pilot.dataset))

    assert report["queries"] == len(pilot.dataset.queries)
    assert report["missing_predictions"] == 0
    for name, value in report["metrics"].items():
        assert value == 1.0, f"{name} = {value!r}"


def test_wrong_answer_and_missing_citation_are_penalized(pilot):
    target = next(query for query in pilot.dataset.queries if query.answerable)
    predictions = []
    for prediction in _oracle(pilot.dataset).predictions:
        if prediction.query_id == target.query_id:
            predictions.append(QAPrediction(
                query_id=prediction.query_id,
                answer="A wrong answer",
                answerable=True,
                citations=[],
            ))
        else:
            predictions.append(prediction)

    report = evaluate_qa(pilot.dataset, PredictionSet(predictions=predictions))
    record = _record(report, target.query_id)

    assert record["exact_match"] == 0.0
    assert record["normalized_exact_match"] == 0.0
    assert record["token_f1"] == 0.0
    assert record["citation_accuracy"] == 0.0
    assert record["evidence_recall"] == 0.0
    assert record["evidence_precision"] == 0.0
    assert report["metrics"]["exact_match"] < 1.0
    assert report["metrics"]["citation_accuracy"] < 1.0


def test_filename_and_sentence_index_citations_resolve(pilot):
    query = next(query for query in pilot.dataset.queries
                 if query.answerable and len(query.supporting_evidence) == 1)
    evidence = query.supporting_evidence[0]
    prediction = QAPrediction(
        query_id=query.query_id,
        answer=query.expected_answer,
        answerable=True,
        citations=[PredictionCitation(
            filename=evidence.filename, sentence_idx=evidence.sentence_idx)],
    )

    report = evaluate_qa(pilot.dataset, PredictionSet(predictions=[prediction]))
    record = _record(report, query.query_id)

    assert record["citation_accuracy"] == 1.0
    assert record["evidence_recall"] == 1.0
    assert record["evidence_precision"] == 1.0
    assert record["evidence_f1"] == 1.0


def test_unknown_citations_count_against_precision(pilot):
    query = next(query for query in pilot.dataset.queries if query.answerable)
    prediction = QAPrediction(
        query_id=query.query_id,
        answer=query.expected_answer,
        answerable=True,
        citations=[PredictionCitation(document_id="not_a_document", sentence_id="s0")],
    )

    report = evaluate_qa(pilot.dataset, PredictionSet(predictions=[prediction]))
    record = _record(report, query.query_id)

    assert record["unknown_citation_count"] == 1
    assert record["predicted_citations"] == ["unknown"]
    assert record["citation_accuracy"] == 0.0
    assert record["evidence_precision"] == 0.0
    assert record["evidence_recall"] == 0.0


def test_unanswerable_questions_score_abstention(pilot):
    oracle = _oracle(pilot.dataset)
    unanswerable = {query.query_id for query in pilot.dataset.queries
                    if not query.answerable}
    assert unanswerable
    predictions = [
        QAPrediction(query_id=prediction.query_id, answer="GBP 1",
                     answerable=True, citations=[])
        if prediction.query_id in unanswerable else prediction
        for prediction in oracle.predictions
    ]

    report = evaluate_qa(pilot.dataset, PredictionSet(predictions=predictions))
    query_id = next(iter(unanswerable))
    record = _record(report, query_id)

    assert record["answerability_correct"] is False
    assert record["abstained"] is False
    assert record["exact_match"] is None
    assert record["citation_accuracy"] is None
    assert report["metrics"]["answerability_accuracy"] < 1.0
    assert report["metrics"]["unanswerable_abstention_rate"] == 0.0


def test_missing_predictions_are_counted_and_scored_as_wrong(pilot):
    predictions = _oracle(pilot.dataset).predictions[:10]
    report = evaluate_qa(pilot.dataset, PredictionSet(predictions=predictions))

    assert report["missing_predictions"] == len(pilot.dataset.queries) - 10
    assert report["metrics"]["answerability_accuracy"] < 1.0
    missing_answerable = next(
        query for query in pilot.dataset.queries
        if query.answerable and query.query_id not in
        {prediction.query_id for prediction in predictions})
    record = _record(report, missing_answerable.query_id)
    assert record["missing_prediction"] is True
    assert record["exact_match"] == 0.0


def test_duplicate_and_unknown_predictions_are_rejected(pilot):
    query = pilot.dataset.queries[0]
    duplicate = QAPrediction(
        query_id=query.query_id, answer=query.expected_answer, answerable=True)
    with pytest.raises(ValueError, match="duplicate prediction"):
        evaluate_qa(pilot.dataset, PredictionSet(predictions=[duplicate, duplicate]))

    unknown = QAPrediction(query_id="not_a_query", answer="x", answerable=True)
    with pytest.raises(ValueError, match="unknown query ids"):
        evaluate_qa(pilot.dataset, PredictionSet(predictions=[unknown]))


def test_report_has_per_type_metrics(pilot):
    report = evaluate_qa(pilot.dataset, _oracle(pilot.dataset))

    assert set(report["by_type"]) == {
        query.query_type for query in pilot.dataset.queries}
    for type_report in report["by_type"].values():
        assert type_report["queries"] > 0
        assert "metrics" in type_report
    assert report["by_type"]["unanswerable"]["metrics"]["exact_match"] is None
    assert report["by_type"]["financial"]["metrics"]["exact_match"] == 1.0


def test_evaluate_files_and_write_report(pilot, tmp_path):
    oracle = _oracle(pilot.dataset)
    predictions_path = tmp_path / "predictions.json"
    predictions_path.write_text(
        json.dumps([prediction.model_dump() for prediction in oracle.predictions]),
        encoding="utf-8",
    )

    loaded = load_predictions(predictions_path)
    assert len(loaded.predictions) == len(pilot.dataset.queries)

    report = evaluate_files(pilot.dataset_path, predictions_path)
    assert report["metrics"]["exact_match"] == 1.0
    assert report["dataset_path"] == str(pilot.dataset_path)

    written = write_report(report, tmp_path / "nested" / "report.json")
    assert written.exists()
    saved = json.loads(written.read_text(encoding="utf-8"))
    assert saved["queries"] == len(pilot.dataset.queries)
    assert saved["metrics"]["answerability_accuracy"] == 1.0
