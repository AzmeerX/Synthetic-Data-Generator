"""Generator behaviour on a real document (Phase H).

One document is generated once per module and then inspected: answers must be
names and typed values rather than internal ids, evidence must be parser text, and
a question must not be emitted when the document does not state what it asks about.
"""
from __future__ import annotations

import re

import pytest

from synthetic_data.qa.model import QASet
from synthetic_data.qa.pilot import build_one
from synthetic_data.qa.taxonomy import cap_for

DOCUMENT = "doc:00000:qr3hgvjuuciha"


@pytest.fixture(scope="module")
def qa() -> QASet:
    return build_one(DOCUMENT, "txt")


def test_answers_are_human_values_not_internal_ids(qa: QASet):
    assert qa.questions
    for question in qa.answerable:
        answer = question.expected_answer or ""
        assert not answer.startswith(("ent:", "fact:", "doc:")), \
            f"{question.question_id} answers with an id: {question.expected_answer!r}"


def test_every_quote_is_parser_text(qa: QASet):
    for question in qa.answerable:
        for evidence in question.evidence:
            assert evidence.sentence_id.startswith("s") or ":" in evidence.sentence_id
            assert evidence.quote.strip()
            assert evidence.render_loc, question.question_id


def test_caps_are_respected(qa: QASet):
    for question_type, count in qa.counts.items():
        if question_type.startswith("unanswerable:"):
            continue
        assert count <= cap_for(question_type), question_type


def test_questions_are_unique_and_well_formed(qa: QASet):
    texts = [question.question for question in qa.questions]
    assert len(texts) == len(set(texts))
    for question in qa.questions:
        assert question.question.endswith("?"), question.question
        assert "  " not in question.question
        assert not re.search(r"\b(does|did) \w+ (represents|owns|lent)\b",
                             question.question), question.question


def test_money_and_date_answers_are_typed(qa: QASet):
    for question in qa.answerable:
        if question.answer_kind == "money":
            assert re.fullmatch(r"[A-Z]{3} [\d,]+\.\d{2}",
                                question.expected_answer or "")
        if question.answer_kind == "date":
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                                question.expected_answer or "")


def test_temporal_questions_quote_the_date(qa: QASet):
    temporal = [question for question in qa.answerable
                if question.question_type == "temporal"]
    assert temporal, "no temporal questions for this document"
    for question in temporal:
        answer = question.expected_answer or ""
        assert any(answer.split("-")[0] in evidence.quote
                   or answer in evidence.quote
                   for evidence in question.evidence), question.question_id


def test_evidence_belongs_to_the_asked_document(qa: QASet):
    for question in qa.answerable:
        assert question.canonical_document_id == DOCUMENT
        for evidence in question.evidence:
            assert evidence.render_format == qa.format
