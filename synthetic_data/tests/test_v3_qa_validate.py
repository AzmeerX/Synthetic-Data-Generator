"""Independent validator behaviour under mutation (Phase H).

Every test here takes a *valid* QA set and breaks exactly one thing, then requires
the validator to notice.  A validator that passes a mutated set is worthless, so the
mutations are applied to the same fixture the happy-path test uses.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from synthetic_data.model import Money
from synthetic_data.qa.build import CorpusTexts
from synthetic_data.qa.model import (AbsenceCheck, QAEvidence, QAQuestion, QASet,
                                     TextSearch, UnanswerableProof)
from synthetic_data.qa.validate import ValidationInput, validate_corpus, validate_set

TEXT = "Acme Holdings owns Beta Trading Limited. The facility was approved in 2023."
DOC = "doc:00000:abc"
FACT = "fact:edge:1"


def _world() -> Any:
    return SimpleNamespace(
        entities={
            "ent:a": SimpleNamespace(entity_id="ent:a", canonical_name="Acme Holdings",
                                     entity_type="organization"),
            "ent:b": SimpleNamespace(entity_id="ent:b",
                                     canonical_name="Beta Trading Limited",
                                     entity_type="organization"),
        },
        facts={
            FACT: SimpleNamespace(fact_id=FACT, kind="edge", relation="owns",
                                  subject=SimpleNamespace(entity_id="ent:a"),
                                  object=SimpleNamespace(entity_id="ent:b"),
                                  amount=None, effective=None, status="completed",
                                  text=""),
        })


def _gold() -> dict[str, Any]:
    return {"edges": [{"id": FACT, "evidence": [
        {"sentence_id": "sent:0001",
         "quote": "Acme Holdings owns Beta Trading Limited",
         "render_loc": {"page": 1}}]}], "propositions": []}


def _corpus() -> CorpusTexts:
    return CorpusTexts.build([(DOC, "txt", TEXT, "case_notes")])


def _question(**overrides) -> QAQuestion:
    data: dict[str, Any] = dict(
        question_id="qa:00000/relationship_lookup/abc", canonical_document_id=DOC,
        doc_type="case_notes", format="txt", question_type="relationship_lookup",
        question="How is Acme Holdings related to Beta Trading Limited?",
        answerable=True, expected_answer="owns", answer_kind="text", fact_ids=[FACT],
        evidence=[QAEvidence(sentence_id="sent:0001",
                             quote="Acme Holdings owns Beta Trading Limited",
                             render_format="txt", render_loc={"page": 1},
                             fact_ids=[FACT])],
        answer_documents=[DOC])
    data.update(overrides)
    return QAQuestion(**data)


def _set(questions, **overrides) -> QASet:
    data: dict[str, Any] = dict(corpus="v3_pilot", seed=7, document_id=DOC,
                                doc_type="case_notes", format="txt",
                                questions=list(questions))
    data.update(overrides)
    return QASet(**data)


def _input(qa: QASet, **overrides) -> ValidationInput:
    data: dict[str, Any] = dict(qa=qa, gold_v2=_gold(), alignment=None,
                                world=_world(), plan=None, fresh_text=TEXT,
                                corpus=_corpus(), stated_by_document={DOC: [FACT]})
    data.update(overrides)
    return ValidationInput(**data)


def _proof(**overrides) -> UnanswerableProof:
    data: dict[str, Any] = dict(
        kind="absent_attribute", subject="Acme Holdings", predicate="registered office",
        documents_searched=[DOC],
        text_searches=[TextSearch(needle="registered office", hits=0)])
    data.update(overrides)
    return UnanswerableProof(**data)


def _unanswerable(proof: UnanswerableProof | None = None) -> QAQuestion:
    return QAQuestion(
        question_id="qa:00000/unanswerable/xyz", canonical_document_id=DOC,
        doc_type="case_notes", format="txt", question_type="unanswerable",
        question="What is the registered office of Acme Holdings?", answerable=False,
        unanswerable_kind="absent_attribute", proof=proof or _proof())


# ---------------------------------------------------------------------------
def test_a_clean_set_passes():
    report = validate_set(_input(_set([_question()])))
    assert report.ok, report.errors
    assert report.checked == 1


def test_a_wrong_answer_is_caught():
    report = validate_set(_input(_set([_question(expected_answer="leases")])))
    assert any("does not state the world relation" in error for error in report.errors)


def test_a_quote_that_the_parser_never_produced_is_caught():
    bad = QAEvidence(sentence_id="sent:0001", quote="Acme Holdings leases Beta",
                     render_format="txt", render_loc={"page": 1}, fact_ids=[FACT])
    report = validate_set(_input(_set([_question(evidence=[bad])])))
    assert any("not found in a fresh parse" in error for error in report.errors)


def test_a_sentence_id_the_gold_artifact_does_not_use_is_caught():
    bad = QAEvidence(sentence_id="sent:9999",
                     quote="Acme Holdings owns Beta Trading Limited",
                     render_format="txt", render_loc={"page": 1}, fact_ids=[FACT])
    report = validate_set(_input(_set([_question(evidence=[bad])])))
    assert any("is not the sentence the gold artifact cites" in error
               for error in report.errors)


def test_a_fact_the_document_does_not_state_is_caught():
    report = validate_set(_input(_set([_question()]),
                                 stated_by_document={DOC: []}))
    assert any("is not stated in" in error for error in report.errors)


def test_a_fact_absent_from_the_world_is_caught():
    report = validate_set(_input(_set([_question(fact_ids=["fact:edge:999"])]),
                                 gold_v2={"edges": [], "propositions": []}))
    assert any("is not in the world" in error for error in report.errors)


def test_duplicate_questions_are_caught():
    first = _question()
    second = _question(question_id="qa:00000/relationship_lookup/def")
    report = validate_set(_input(_set([first, second])))
    assert any("duplicate of" in error for error in report.errors)


def test_an_unanswerable_question_with_a_stale_count_is_caught():
    stale = _proof(text_searches=[],
                   absence_checks=[AbsenceCheck(subject="Acme Holdings",
                                                object="Beta Trading Limited",
                                                relation_words=["owns"],
                                                shared_sentences=0,
                                                relation_mentions=0)])
    report = validate_set(_input(_set([_unanswerable(stale)])))
    assert any("shared sentences" in error for error in report.errors)


def test_a_proof_citing_a_needle_that_exists_is_caught_even_if_it_claims_zero():
    # a JSON file can carry this; the model would refuse to build it
    proof = UnanswerableProof.model_construct(
        kind="absent_attribute", subject="Acme Holdings", predicate="facility",
        documents_searched=[DOC],
        text_searches=[TextSearch(needle="facility", hits=0)],
        absence_checks=[], facts_considered=[], nearest_alias=None,
        nearest_document=None, contradicted_by=[], proposed_only=[], notes="")
    report = validate_set(_input(_set([_unanswerable(proof)])))
    assert any("occurs in" in error for error in report.errors)


def test_an_unanswerable_question_that_the_world_can_answer_is_caught():
    proof = _proof(kind="absent_relationship",
                   predicate="is_director_of Beta Trading Limited",
                   text_searches=[],
                   absence_checks=[AbsenceCheck(subject="Acme Holdings",
                                                object="Beta Trading Limited",
                                                relation_words=["director"],
                                                shared_sentences=1,
                                                relation_mentions=0)])
    world = _world()
    world.facts["fact:edge:2"] = SimpleNamespace(
        fact_id="fact:edge:2", kind="edge", relation="is_director_of",
        subject=SimpleNamespace(entity_id="ent:a"),
        object=SimpleNamespace(entity_id="ent:b"), amount=None, effective=None,
        status="completed", text="")
    question = QAQuestion(question_id="qa:00000/unanswerable/rel", canonical_document_id=DOC,
                          doc_type="case_notes", format="txt",
                          question_type="unanswerable",
                          question="Is Acme Holdings a director of Beta Trading Limited?",
                          answerable=False, unanswerable_kind="absent_relationship",
                          proof=proof)
    report = validate_set(_input(_set([question]), world=world))
    assert any("the world states" in error for error in report.errors)


def test_cross_format_answer_disagreement_is_caught():
    txt = _set([_question()])
    pdf = _set([_question(format="pdf", expected_answer="leases")], format="pdf")
    report = validate_corpus([txt, pdf], _corpus(), {DOC: [FACT]})
    assert any("answers" in error for error in report.errors)


def test_a_format_specific_location_is_not_treated_as_disagreement():
    txt = _set([_question(question_type="provenance", answer_kind="location",
                          expected_answer="page 1")])
    pdf = _set([_question(question_type="provenance", answer_kind="location",
                          expected_answer="page 3", format="pdf")], format="pdf")
    report = validate_corpus([txt, pdf], _corpus(), {DOC: [FACT]})
    assert not any("answers" in error for error in report.errors)
    assert report.notes.get("format_specific_locations") == 1


def test_missing_question_types_are_reported():
    report = validate_corpus([_set([_question()])], _corpus(), {DOC: [FACT]})
    assert any("no financial questions" in error for error in report.errors)
    assert any("no unanswerable question of kind" in error for error in report.errors)


# ---------------------------------------------------------------------------
# The five distinctions the brief requires an absence proof to make
def test_a_fact_present_in_another_document_is_not_unanswerable():
    corpus = CorpusTexts.build([
        (DOC, "txt", TEXT, "case_notes"),
        ("doc:00001:other", "txt", "The registered office is in Leeds.", "memorandum"),
    ])
    report = validate_set(_input(_set([_unanswerable()]), corpus=corpus))
    assert any("occurs in" in error for error in report.errors), report.errors


def test_a_fact_present_under_another_alias_is_not_unanswerable():
    corpus = CorpusTexts.build([
        (DOC, "txt", TEXT + " The registered office is at 4 Mill Lane.", "case_notes")])
    report = validate_set(_input(_set([_unanswerable()]), corpus=corpus))
    assert any("occurs in" in error for error in report.errors), report.errors


def test_a_needle_matches_regardless_of_case():
    corpus = CorpusTexts.build([(DOC, "txt", "ACME HOLDINGS (UK) LIMITED.", "case_notes")])
    assert corpus.search("Acme Holdings").hits == 1


def test_a_financial_fact_the_world_states_is_not_treated_as_absent():
    world = _world()
    world.facts["fact:prop:9"] = SimpleNamespace(
        fact_id="fact:prop:9", kind="proposition", relation=None, subject=None,
        object=None, amount=None, effective=None, status="stated",
        text="A dividend of 2 pence per share was declared.")
    proof = _proof(kind="absent_financial_information", subject="Acme Holdings",
                   predicate="dividend per share",
                   text_searches=[TextSearch(needle="dividend per share", hits=0)])
    question = QAQuestion(
        question_id="qa:00000/unanswerable/fin", canonical_document_id=DOC,
        doc_type="case_notes", format="txt", question_type="unanswerable",
        question="What dividend per share did Acme Holdings declare?",
        answerable=False, unanswerable_kind="absent_financial_information",
        proof=proof)
    report = validate_set(_input(_set([question]), world=world))
    assert any("a dividend fact exists" in error for error in report.errors), report.errors
