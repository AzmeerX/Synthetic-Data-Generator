"""QA model invariants (Phase H).

The model is where an unanswerable question is stopped from carrying an answer and
an answerable one from carrying a proof.  These tests exercise the guards directly,
because a benchmark that silently accepts a malformed item is worse than one that
refuses to build.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from synthetic_data.qa.model import (AbsenceCheck, QAEvidence, QAQuestion, QASet,
                                     TextSearch, UnanswerableProof)
from synthetic_data.qa.taxonomy import QUESTION_TYPES, UNANSWERABLE_KINDS


def _evidence(**overrides) -> QAEvidence:
    data = dict(sentence_id="sent:1", quote="Acme owns Beta", render_format="txt",
                render_loc={"page": 1}, fact_ids=["fact:1"])
    data.update(overrides)
    return QAEvidence(**data)


def _question(**overrides) -> QAQuestion:
    data = dict(
        question_id="qa:00000/relationship_lookup/abc", canonical_document_id="doc:00000:x",
        doc_type="case_notes", format="txt", question_type="relationship_lookup",
        question="How is Acme related to Beta?", answerable=True,
        expected_answer="owns", answer_kind="text", fact_ids=["fact:1"],
        evidence=[_evidence()])
    data.update(overrides)
    return QAQuestion(**data)


def _proof(**overrides) -> UnanswerableProof:
    data = dict(kind="absent_attribute", subject="Acme", predicate="registered office",
                documents_searched=["doc:00000:x"],
                text_searches=[TextSearch(needle="registered office", hits=0)])
    data.update(overrides)
    return UnanswerableProof(**data)


def test_a_well_formed_answerable_question_is_accepted():
    question = _question()
    assert question.answerable and question.expected_answer == "owns"


def test_an_answerable_question_needs_evidence():
    with pytest.raises(ValidationError, match="needs evidence"):
        _question(evidence=[])


def test_an_answerable_question_needs_an_answer():
    with pytest.raises(ValidationError, match="needs an answer"):
        _question(expected_answer=None)


def test_an_answerable_question_needs_an_answer_kind():
    with pytest.raises(ValidationError, match="needs a kind"):
        _question(answer_kind=None)


def test_an_answerable_question_may_not_carry_a_proof():
    with pytest.raises(ValidationError, match="carries a proof"):
        _question(unanswerable_kind="absent_attribute", proof=_proof())


def test_an_unanswerable_question_needs_a_proof():
    with pytest.raises(ValidationError, match="needs a proof"):
        _question(question_type="unanswerable", answerable=False, expected_answer=None,
                  answer_kind=None, evidence=[], unanswerable_kind="absent_attribute")


def test_an_unanswerable_question_may_not_state_an_answer():
    with pytest.raises(ValidationError, match="states an answer"):
        _question(question_type="unanswerable", answerable=False, expected_answer="42",
                  answer_kind=None, evidence=[], unanswerable_kind="absent_attribute",
                  proof=_proof())


def test_an_unanswerable_question_may_not_cite_evidence():
    with pytest.raises(ValidationError, match="must not cite evidence"):
        _question(question_type="unanswerable", answerable=False, expected_answer=None,
                  answer_kind=None, unanswerable_kind="absent_attribute", proof=_proof())


def test_the_proof_kind_must_match_the_question_kind():
    with pytest.raises(ValidationError, match="proof kind disagrees"):
        _question(question_type="unanswerable", answerable=False, expected_answer=None,
                  answer_kind=None, evidence=[], unanswerable_kind="absent_relationship",
                  proof=_proof())


def test_a_proof_that_found_something_is_refused():
    with pytest.raises(ValidationError, match="found something"):
        _proof(text_searches=[TextSearch(needle="registered office", hits=2,
                                         documents=["doc:00000:x"])])


def test_a_proof_without_any_check_is_refused():
    with pytest.raises(ValidationError, match="cites no search"):
        _proof(text_searches=[])


def test_a_co_occurrence_check_that_found_the_relation_is_refused():
    with pytest.raises(ValidationError, match="found the relation"):
        _proof(kind="absent_relationship",
               absence_checks=[AbsenceCheck(subject="Ann", object="Acme",
                                            relation_words=["director"],
                                            shared_sentences=2, relation_mentions=1)])


def test_an_absence_check_alone_is_enough():
    proof = _proof(text_searches=[], absence_checks=[
        AbsenceCheck(subject="Ann", object="Acme", relation_words=["director"],
                     shared_sentences=3, relation_mentions=0)])
    assert proof.absence_checks[0].shared_sentences == 3


def test_an_unknown_unanswerable_kind_is_refused():
    with pytest.raises(ValidationError):
        _proof(kind="not_a_kind")


def test_an_unknown_question_type_is_refused():
    with pytest.raises(ValidationError):
        _question(question_type="vibes")


def test_a_set_recomputes_its_counts():
    question = _question()
    other = _question(question_id="qa:00000/unanswerable/xyz",
                      question_type="unanswerable", answerable=False,
                      expected_answer=None, answer_kind=None, evidence=[],
                      fact_ids=[], unanswerable_kind="absent_attribute",
                      proof=_proof())
    qa = QASet(corpus="v3_pilot", seed=7, document_id="doc:00000:x",
               doc_type="case_notes", format="txt", questions=[question, other])
    assert qa.counts == {"relationship_lookup": 1, "unanswerable:absent_attribute": 1}


def test_a_set_refuses_declared_counts_that_disagree():
    with pytest.raises(ValidationError, match="do not match"):
        QASet(corpus="v3_pilot", seed=7, document_id="doc:00000:x",
              doc_type="case_notes", format="txt", questions=[_question()],
              counts={"relationship_lookup": 5})


def test_the_taxonomy_covers_every_capability_the_brief_names():
    required = {
        "entity_lookup", "relationship_lookup", "financial", "temporal",
        "historical_change", "multi_hop", "identity_resolution", "negation",
        "proposed_vs_completed", "alleged_vs_established",
        "contradiction_resolution", "provenance",
    }
    assert required <= set(QUESTION_TYPES)
    assert set(UNANSWERABLE_KINDS) == {
        "absent_attribute", "absent_relationship", "unsupported_historical_date",
        "absent_financial_information", "false_presupposition"}
