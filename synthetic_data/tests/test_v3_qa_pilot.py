"""The generated Phase H QA corpus, checked as data (Phase H).

These tests read the artifacts under ``synthetic_data/output/v3_qa/`` and assert the
properties the brief requires of the published benchmark.  They skip when the pilot
has not been generated, so a fresh clone can run the suite before generating it.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from synthetic_data.qa.model import QASet
from synthetic_data.qa.taxonomy import QUESTION_TYPES, UNANSWERABLE_KINDS

QA_DIR = Path(__file__).resolve().parents[1] / "output" / "v3_qa"
MANIFEST = QA_DIR / "manifest.json"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(),
                                reason="Phase H pilot has not been generated")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sets(manifest) -> dict[str, QASet]:
    loaded = {}
    repo_root = QA_DIR.parents[2]
    for entry in manifest["sets"].values():
        path = repo_root / entry["path"]
        qa = QASet.model_validate_json(path.read_text(encoding="utf-8"))
        loaded[path.name] = qa
    return loaded


def test_every_artifact_has_a_set(manifest, sets):
    assert len(sets) == 22
    assert manifest["artifacts"] == 22
    assert len({qa.document_id for qa in sets.values()}) == 8


def test_the_manifest_validated_cleanly(manifest):
    assert manifest["corpus_validation"]["errors"] == []
    for name, entry in manifest["sets"].items():
        assert entry["validation"]["errors"] == [], name


def test_every_question_type_appears(manifest):
    types = Counter()
    for entry in manifest["sets"].values():
        for question_type, count in entry["counts"].items():
            if not question_type.startswith("unanswerable:"):
                types[question_type] += count
    for question_type in QUESTION_TYPES:
        if question_type == "unanswerable":
            continue
        assert types[question_type] >= 1, f"no {question_type} questions"


def test_every_unanswerable_kind_appears(manifest):
    kinds = Counter()
    for entry in manifest["sets"].values():
        for question_type, count in entry["counts"].items():
            if question_type.startswith("unanswerable:"):
                kinds[question_type.split(":", 1)[1]] += count
    for kind in UNANSWERABLE_KINDS:
        assert kinds[kind] >= 1, f"no {kind} questions"


def test_unanswerable_questions_carry_a_proof_and_no_answer(sets):
    count = 0
    for qa in sets.values():
        for question in qa.unanswerable:
            count += 1
            assert question.expected_answer is None
            assert question.proof is not None
            assert question.proof.documents_searched
            assert question.proof.text_searches or question.proof.absence_checks
            assert not question.evidence
    assert count >= 20


def test_answerable_questions_carry_exact_evidence(sets):
    for qa in sets.values():
        for question in qa.answerable:
            assert question.expected_answer
            assert question.fact_ids
            assert question.evidence, question.question_id
            for evidence in question.evidence:
                assert evidence.quote.strip()
                assert evidence.sentence_id
                assert evidence.render_format == qa.format


def test_twins_share_ids_and_answers(sets):
    by_document: dict[str, dict[str, QASet]] = {}
    for qa in sets.values():
        by_document.setdefault(qa.document_id, {})[qa.format] = qa
    compared = 0
    for document_id, formats in by_document.items():
        if len(formats) < 2:
            continue
        reference = formats.get("txt") or next(iter(formats.values()))
        for fmt, qa in formats.items():
            if fmt == reference.format:
                continue
            mine = {question.question_id: question for question in qa.questions}
            theirs = {question.question_id: question for question in reference.questions}
            shared = set(mine) & set(theirs)
            assert shared, f"{document_id}/{fmt} shares no question with txt"
            for question_id in shared:
                left, right = mine[question_id], theirs[question_id]
                assert left.answerable == right.answerable, question_id
                if left.question_type == "provenance":
                    # the same fact sits on another page in another format
                    assert left.answer_kind == right.answer_kind
                else:
                    assert left.expected_answer == right.expected_answer, question_id
                compared += 1
    assert compared >= 200


def test_no_question_text_repeats_within_a_set(sets):
    for qa in sets.values():
        texts = [question.question.strip().lower() for question in qa.questions]
        assert len(texts) == len(set(texts)), qa.document_id


def test_question_ids_are_stable_across_formats(sets):
    ids = Counter()
    for qa in sets.values():
        for question in qa.questions:
            ids[question.question_id] += 1
    assert ids, "no questions"
    # every id is used at most once per format of one document
    for question_id, count in ids.items():
        assert count <= 3, f"{question_id} used {count} times"


def test_money_answers_are_typed_amounts(sets):
    pattern = re.compile(r"^[A-Z]{3} \d{1,3}(,\d{3})*\.\d{2}$")
    checked = 0
    for qa in sets.values():
        for question in qa.answerable:
            if question.answer_kind != "money":
                continue
            assert pattern.match(question.expected_answer or ""), question.question_id
            checked += 1
    assert checked >= 60


def test_dates_are_iso_and_documents_are_named(sets):
    for qa in sets.values():
        for question in qa.answerable:
            if question.answer_kind == "date":
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", question.expected_answer or ""), \
                    question.question_id
            assert question.answer_documents
            for document_id in question.answer_documents:
                assert document_id.startswith("doc:"), question.question_id


def test_contradiction_questions_name_two_documents(sets):
    seen = 0
    for qa in sets.values():
        for question in qa.answerable:
            if question.question_type != "contradiction_resolution":
                continue
            assert len(set(question.answer_documents)) >= 2, question.question_id
            seen += 1
    assert seen >= 1


def test_fact_coverage_is_reported_and_high(manifest):
    coverage = manifest["corpus_validation"]["notes"]["fact_coverage"]
    assert coverage["facts"] > 50
    assert coverage["covered"] / coverage["facts"] >= 0.5


def test_determinism_held(manifest):
    assert manifest["determinism"]["identical"] is True
    assert manifest["determinism"]["sets"] == 22


def test_the_report_exists_and_names_the_limitations():
    report = (QA_DIR / "PHASE_H_REPORT.md").read_text(encoding="utf-8")
    assert "## 1. Taxonomy and counts" in report
    assert "does not measure" in report
    assert "Extraction accuracy" in report
