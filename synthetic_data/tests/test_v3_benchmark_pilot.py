"""The Phase I evaluation pilot, checked as published data (Phase I).

Reads ``synthetic_data/output/v3_eval/``: the perfect fixture must score 1.0, the
empty fixture must score zero recall with every item counted, and every corrupted
fixture must move the metric it was built to move.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthetic_data.benchmark.fixtures import CORRUPTIONS

EVAL_DIR = Path(__file__).resolve().parents[1] / "output" / "v3_eval"
MANIFEST = EVAL_DIR / "manifest.json"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(),
                                reason="Phase I pilot has not been run")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _result(name: str) -> dict:
    return json.loads((EVAL_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_every_fixture_ran(manifest):
    assert manifest["documents"] == 22
    assert "perfect" in manifest["runs"] and "empty" in manifest["runs"]
    assert len(manifest["runs"]) == 2 + len(CORRUPTIONS)


def test_the_pilot_says_it_is_not_a_model_benchmark(manifest):
    assert "no extraction model was run" in manifest["label"]
    report = (EVAL_DIR / "perfect.md").read_text(encoding="utf-8")
    assert "no extraction model was run" in report


def test_the_perfect_fixture_scores_one_everywhere():
    corpus = _result("perfect")["corpus"]
    for metric in ("entities", "relations", "evidence", "values", "locators",
                   "answers", "answerability", "contradictions"):
        assert corpus[metric]["f1"] == 1.0, metric
        assert corpus[metric]["fp"] == 0, metric
        assert corpus[metric]["fn"] == 0, metric


def test_the_empty_fixture_is_not_flattered():
    corpus = _result("empty")["corpus"]
    assert corpus["entities"]["recall"] == 0.0
    assert corpus["relations"]["recall"] == 0.0
    assert corpus["answers"]["recall"] == 0.0
    assert corpus["entities"]["precision"] is None       # nothing predicted
    assert corpus["missing_questions"] == 517
    assert corpus["documents"] == 22                     # nothing silently skipped


def test_every_corruption_moves_its_own_metric():
    perfect = _result("perfect")["corpus"]
    expectations = {
        "wrong_identity": ("entities", "f1"),
        "reversed_direction": ("relations", "f1"),
        "wrong_date": ("values", "f1"),
        "wrong_status": ("relations", "f1"),
        "wrong_negation": ("relations", "f1"),
        "wrong_amount": ("values", "f1"),
        "wrong_currency": ("values", "f1"),
        "wrong_page": ("locators", "fp"),
        "wrong_cell": ("locators", "fp"),
        "wrong_section": ("locators", "fp"),
        "wrong_quote": ("evidence", "f1"),
        "dropped_entity": ("entities", "f1"),
        "wrong_answer": ("answers", "f1"),
        "answered_unanswerable": ("answerability", "f1"),
        "dropped_answer": ("answers", "f1"),
    }
    assert set(expectations) == {corruption.name for corruption in CORRUPTIONS}
    for name, (metric, field) in expectations.items():
        corpus = _result(f"corrupt__{name}")["corpus"]
        if field == "f1":
            assert corpus[metric]["f1"] < perfect[metric]["f1"], (name, metric)
        else:
            assert corpus[metric][field] > perfect[metric][field], (name, metric)


def test_corruptions_record_a_reason():
    expected_notes = {
        # renaming one entity to another's name resolves twice to the same gold
        # entity: the second use is a duplicate rather than an ambiguity
        "wrong_identity": "duplicate_entity",
        "reversed_direction": "reversed_direction",
        "wrong_amount": "amount_mismatch",
        "wrong_currency": "currency_mismatch",
        "wrong_status": "status_mismatch",
        "wrong_negation": "negation_mismatch",
        "wrong_quote": "quote_partial",
        "wrong_page": "page_mismatch",
        "wrong_cell": "cell_mismatch",
        "wrong_section": "section_mismatch",
        "answered_unanswerable": "answered_unanswerable",
    }
    for name, note in expected_notes.items():
        corpus = _result(f"corrupt__{name}")["corpus"]
        assert note in corpus["notes"], (name, note, corpus["notes"])


def test_format_and_type_breakdowns_are_present():
    result = _result("perfect")
    assert set(result["by_format"]) == {"txt", "pdf", "docx", "xlsx"}
    assert len(result["by_doc_type"]) >= 7
    for block in result["by_format"].values():
        assert block["documents"] >= 1
        assert block["answers"]["tp"] >= 0


def test_evaluation_is_deterministic(manifest):
    assert manifest["deterministic"] is True
    first = (EVAL_DIR / "perfect.json").read_text(encoding="utf-8")
    second = (EVAL_DIR / "perfect.json").read_text(encoding="utf-8")
    assert first == second


def test_the_question_breakdown_covers_every_type():
    result = _result("perfect")
    report = (EVAL_DIR / "perfect.md").read_text(encoding="utf-8")
    assert "## By question type" in report
    assert "contradiction_resolution" in report
    assert result["corpus"]["answers"]["tp"] == 517
