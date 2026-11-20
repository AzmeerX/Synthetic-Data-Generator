"""Answer-level evaluation for the generated QA benchmark.

Synth's existing ``synth.eval.metrics.evaluate_run`` scores graph extraction.
This module is deliberately separate: it scores answers, citations, evidence
retrieval and answerability classification for the synthetic QA dataset.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from synthetic_data.schemas.qa import (
    PredictionSet,
    QADataset,
    QAPrediction,
    QAQuery,
)


_ARTICLES = {"a", "an", "the"}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%d %B, %Y",
)


def _canonical_date(text: str) -> str | None:
    value = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_answer(text: str) -> str:
    """Conservative lexical normalization used by normalized EM and F1.

    It handles case, punctuation, articles, currency symbols and common date
    formats.  It does not attempt semantic equivalence; the raw EM metric is
    reported alongside it.
    """
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", text).casefold().strip()
    date_value = _canonical_date(value)
    if date_value:
        value = date_value
    value = value.replace("&", " and ").replace("£", " gbp ")
    value = re.sub(r"(?<=\d),(?=\d)", "", value)
    value = re.sub(r"\b(\d+(?:\.\d+)?)\s*m\b", r"\1 million", value)
    value = re.sub(r"[^\w\s]", " ", value)
    tokens = [token for token in value.split() if token not in _ARTICLES]
    return " ".join(tokens)


def exact_match(predicted: str, expected: str) -> float:
    return 1.0 if predicted.strip() == expected.strip() else 0.0


def normalized_exact_match(predicted: str, expected: str) -> float:
    return 1.0 if normalize_answer(predicted) == normalize_answer(expected) else 0.0


def token_f1(predicted: str, expected: str) -> float:
    pred_tokens = normalize_answer(predicted).split()
    gold_tokens = normalize_answer(expected).split()
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(gold_tokens)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def _citation_documents(dataset: QADataset) -> tuple[dict[str, str], dict[str, str]]:
    by_id = {doc.document_id: doc.document_id for doc in dataset.documents}
    by_filename = {doc.filename: doc.document_id for doc in dataset.documents}
    return by_id, by_filename


def _resolve_prediction_citations(
    prediction: QAPrediction,
    dataset: QADataset,
) -> tuple[list[tuple[str, str] | None], set[str]]:
    """Resolve citations to ``(document_id, sentence_id)``.

    Unknown citations are returned as ``None`` and still count against
    citation precision.
    """
    by_id, by_filename = _citation_documents(dataset)
    resolved: list[tuple[str, str] | None] = []
    document_ids: set[str] = set()
    seen: set[tuple[str, str] | None] = set()
    for citation in prediction.citations:
        document_id = None
        if citation.document_id:
            if citation.document_id in by_id:
                document_id = citation.document_id
            elif citation.document_id in by_filename:
                document_id = by_filename[citation.document_id]
        if document_id is None and citation.filename:
            document_id = by_filename.get(citation.filename)
        sentence_id = citation.sentence_id
        if sentence_id is None and citation.sentence_idx is not None:
            sentence_id = f"s{citation.sentence_idx}"
        if sentence_id is not None and not sentence_id.startswith("s"):
            sentence_id = f"s{sentence_id}" if sentence_id.isdigit() else sentence_id
        key = (document_id, sentence_id) if document_id and sentence_id else None
        if key not in seen:
            seen.add(key)
            resolved.append(key)
        if document_id:
            document_ids.add(document_id)
    return resolved, document_ids


def _format_citation(citation: tuple[str, str] | None) -> str:
    if citation is None:
        return "unknown"
    document_id, sentence_id = citation
    return f"{document_id}:{sentence_id}"


def _query_record(query: QAQuery, prediction: QAPrediction | None,
                  dataset: QADataset) -> dict[str, Any]:
    missing = prediction is None
    if prediction is None:
        prediction = QAPrediction(query_id=query.query_id, answer="", answerable=False)
    expected_evidence = {
        (ev.document_id, ev.sentence_id) for ev in query.supporting_evidence
    }
    resolved, predicted_documents = _resolve_prediction_citations(prediction, dataset)
    predicted_evidence = {c for c in resolved if c is not None}
    matched = expected_evidence & predicted_evidence
    predicted_count = len([c for c in resolved if c is not None])
    unknown = sum(1 for c in resolved if c is None)

    record: dict[str, Any] = {
        "query_id": query.query_id,
        "query_type": query.query_type,
        "answerable": query.answerable,
        "predicted_answerable": prediction.answerable,
        "answerability_correct": prediction.answerable == query.answerable,
        "missing_prediction": missing,
        "expected_answer": query.expected_answer,
        "predicted_answer": prediction.answer,
        "relevant_document_ids": list(query.relevant_document_ids),
        "predicted_document_ids": sorted(predicted_documents),
        "expected_evidence": [f"{d}:{s}" for d, s in sorted(expected_evidence)],
        "predicted_citations": [_format_citation(citation) for citation in resolved],
        "matched_evidence": [f"{d}:{s}" for d, s in sorted(matched)],
        "missing_evidence": [f"{d}:{s}" for d, s in sorted(expected_evidence - matched)],
        "unexpected_citations": [
            _format_citation(citation)
            for citation in resolved if citation not in matched
        ],
        "unknown_citation_count": unknown,
    }

    if query.answerable:
        record.update({
            "exact_match": exact_match(prediction.answer, query.expected_answer),
            "normalized_exact_match": normalized_exact_match(
                prediction.answer, query.expected_answer),
            "token_f1": token_f1(prediction.answer, query.expected_answer),
            "citation_accuracy": 1.0 if matched else 0.0,
            "evidence_recall": (len(matched) / len(expected_evidence)
                                if expected_evidence else 0.0),
            "evidence_precision": (len(matched) / predicted_count
                                   if predicted_count else 0.0),
            "document_accuracy": 1.0 if predicted_documents & set(
                query.relevant_document_ids) else 0.0,
        })
        precision = record["evidence_precision"]
        recall = record["evidence_recall"]
        record["evidence_f1"] = (
            2 * precision * recall / (precision + recall)
            if precision + recall else 0.0
        )
    else:
        record["abstained"] = not prediction.answerable
        record.update({
            "exact_match": None,
            "normalized_exact_match": None,
            "token_f1": None,
            "citation_accuracy": None,
            "evidence_recall": None,
            "evidence_precision": None,
            "evidence_f1": None,
            "document_accuracy": None,
        })
    return record


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 6) if values else None


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [r for r in records if r["answerable"]]
    unanswerable = [r for r in records if not r["answerable"]]
    metrics = {
        "exact_match": _mean(r["exact_match"] for r in answerable),
        "normalized_exact_match": _mean(r["normalized_exact_match"] for r in answerable),
        "token_f1": _mean(r["token_f1"] for r in answerable),
        "citation_accuracy": _mean(r["citation_accuracy"] for r in answerable),
        "evidence_retrieval_accuracy": _mean(r["evidence_recall"] for r in answerable),
        "evidence_precision": _mean(r["evidence_precision"] for r in answerable),
        "evidence_f1": _mean(r["evidence_f1"] for r in answerable),
        "document_retrieval_accuracy": _mean(r["document_accuracy"] for r in answerable),
        "answerability_accuracy": _mean(
            [1.0 if r["answerability_correct"] else 0.0 for r in records]),
        "answerable_detection_rate": _mean(
            [1.0 if r["predicted_answerable"] else 0.0 for r in answerable]),
        "unanswerable_abstention_rate": _mean(
            [1.0 if r.get("abstained") else 0.0 for r in unanswerable]),
    }
    return {
        "queries": len(records),
        "answerable_queries": len(answerable),
        "unanswerable_queries": len(unanswerable),
        "missing_predictions": sum(1 for r in records if r["missing_prediction"]),
        "metrics": metrics,
    }


def evaluate_qa(dataset: QADataset, prediction_set: PredictionSet) -> dict[str, Any]:
    """Score predictions against the generated QA dataset.

    Answer metrics are averaged over answerable questions.  Citation and
    evidence metrics are also answerable-only.  Answerability accuracy covers
    every question.  Citation precision counts unknown citations as errors.
    """
    predictions: dict[str, QAPrediction] = {}
    for prediction in prediction_set.predictions:
        if prediction.query_id in predictions:
            raise ValueError(f"duplicate prediction for {prediction.query_id}")
        predictions[prediction.query_id] = prediction

    known = {query.query_id for query in dataset.queries}
    unknown = sorted(set(predictions) - known)
    if unknown:
        raise ValueError(f"predictions for unknown query ids: {unknown}")

    records = [_query_record(query, predictions.get(query.query_id), dataset)
               for query in dataset.queries]
    report = _aggregate(records)
    report.update({
        "seed": dataset.seed,
        "target_pages": dataset.target_pages,
        "actual_pages": dataset.actual_pages,
        "documents": len(dataset.documents),
        "query_types": sorted({query.query_type for query in dataset.queries}),
        "by_type": {},
        "per_query": records,
    })
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["query_type"]].append(record)
    report["by_type"] = {name: _aggregate(items) for name, items in sorted(groups.items())}
    return report


def load_dataset(path: str | Path) -> QADataset:
    return QADataset.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def load_predictions(path: str | Path) -> PredictionSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"predictions": data}
    return PredictionSet.model_validate(data)


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def evaluate_files(dataset_path: str | Path, predictions_path: str | Path) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    predictions = load_predictions(predictions_path)
    report = evaluate_qa(dataset, predictions)
    report["dataset_path"] = str(dataset_path)
    report["predictions_path"] = str(predictions_path)
    return report
