"""Tests for programmatically generated questions and answers."""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections import Counter

from synthetic_data.generator.documents import DOC_TYPES
from synthetic_data.generator.evaluation import _canonical_date
from synthetic_data.generator.queries import (
    CorpusIndex,
    FactIndex,
    QUESTION_VARIANTS,
    UNANSWERABLE_KINDS,
    _attribute_absent,
    _money,
    _ordered_companies,
    duplicate_metrics,
    generate_qa_dataset,
    generate_qa_dataset_v2,
    normalize_question,
)
from synthetic_data.generator.world import Fact

EXPECTED_QUERY_TYPES = {
    "entity_lookup",
    "relationship_lookup",
    "financial",
    "temporal",
    "historical_change",
    "multi_hop",
    "identity_resolution",
    "negation",
    "proposed_vs_completed",
    "alleged_status",
    "contradiction",
    "provenance",
    "unanswerable",
}


def _documents(pilot):
    return {document.document_id: document for document in pilot.documents}


def _by_type(pilot, query_type):
    return [query for query in pilot.dataset.queries
            if query.query_type == query_type]


def test_pilot_dataset_shape(pilot):
    dataset = pilot.dataset
    assert dataset.seed == 7
    assert dataset.target_pages == 20
    assert dataset.actual_pages == 20
    assert len(dataset.documents) == 5
    assert len(dataset.queries) == 95
    assert sum(query.answerable for query in dataset.queries) == 94
    assert sum(not query.answerable for query in dataset.queries) == 1
    assert len({query.query_id for query in dataset.queries}) == len(dataset.queries)
    assert len({query.question for query in dataset.queries}) == len(dataset.queries)
    assert {query.query_type for query in dataset.queries} == EXPECTED_QUERY_TYPES


def test_every_answerable_question_has_grounded_evidence(pilot):
    documents = _documents(pilot)
    for query in pilot.dataset.queries:
        assert query.relevant_document_ids == list(dict.fromkeys(
            evidence.document_id for evidence in query.supporting_evidence))
        if not query.answerable:
            continue
        assert query.expected_answer.strip()
        assert query.supporting_evidence
        for evidence in query.supporting_evidence:
            document = documents[evidence.document_id]
            assert evidence.filename == document.filename
            assert evidence.text_sha256 == document.text_sha256
            assert evidence.sentence_id == f"s{evidence.sentence_idx}"
            sentence = document.sentence_text_by_idx[evidence.sentence_idx]
            assert evidence.quote in sentence, (
                f"{query.query_id}: {evidence.quote!r} not in {evidence.sentence_id}")
        for fact_id in query.gold_fact_ids:
            # Gold facts are the facts stated by the generated document plans.
            # Some are document-local (for example a negated guarantee); only
            # the world-level subset also appears in ``world.facts``.
            assert any(stated.fact.fact_id == fact_id
                       for document in pilot.documents
                       for stated in document.plan.stated_facts)


def test_unanswerable_question_is_genuinely_unsupported(pilot):
    unanswerable = [query for query in pilot.dataset.queries if not query.answerable]
    assert len(unanswerable) == 1
    query = unanswerable[0]
    assert query.answer_type == "unanswerable"
    assert query.expected_answer == ""
    assert query.supporting_evidence == []
    assert query.gold_fact_ids == []
    assert query.relevant_document_ids == []

    marker = "checked absent keywords: "
    assert marker in query.notes
    keywords = query.notes.split(marker, 1)[1].split(", ")
    assert keywords
    corpus = "\n".join(document.text for document in pilot.documents).casefold()
    for keyword in keywords:
        assert keyword.casefold() not in corpus, (
            f"unanswerable question is supported by keyword {keyword!r}")


def test_multi_hop_questions_join_two_documents(pilot):
    multi_hop = _by_type(pilot, "multi_hop")
    assert len(multi_hop) == 6
    for query in multi_hop:
        sources = {evidence.document_id for evidence in query.supporting_evidence}
        assert len(sources) == 2
        assert set(query.relevant_document_ids) == sources
        assert len(query.gold_fact_ids) == 2


def test_contradiction_question_names_both_sources(pilot):
    contradiction = _by_type(pilot, "contradiction")
    assert len(contradiction) == 1
    query = contradiction[0]
    sources = [evidence.document_id for evidence in query.supporting_evidence]
    assert len(sources) == 2
    assert len(set(sources)) == 2
    assert query.expected_answer.startswith("The documents conflict:")
    assert len(query.gold_fact_ids) == 2


def test_status_and_negation_answers_follow_the_facts(pilot):
    proposed = _by_type(pilot, "proposed_vs_completed")
    assert proposed
    assert any(query.answer_type == "boolean" and query.expected_answer == "No"
               for query in proposed)
    assert any(query.answer_type == "entity" for query in proposed)

    alleged = _by_type(pilot, "alleged_status")
    assert alleged
    assert all(query.expected_answer == "Alleged" for query in alleged)

    negation = _by_type(pilot, "negation")
    assert negation
    assert all(query.expected_answer == "No" for query in negation)

    identity = _by_type(pilot, "identity_resolution")
    assert identity
    booleans = [query for query in identity if query.answer_type == "boolean"]
    assert booleans
    assert all(query.expected_answer == "No" for query in booleans)
    entity_answers = [query for query in identity if query.answer_type == "entity"]
    assert entity_answers
    assert all(query.expected_answer in {person.name for person in pilot.world.people()}
               for query in entity_answers)


def test_financial_answers_match_the_stated_amount(pilot):
    facts = {fact.fact_id: fact for fact in pilot.world.facts}
    financial = _by_type(pilot, "financial")
    assert financial
    for query in financial:
        assert re.fullmatch(r"GBP [\d,]+(?: million| thousand)?",
                            query.expected_answer)
        assert len(query.gold_fact_ids) == 1
        fact = facts[query.gold_fact_ids[0]]
        assert fact.amount_gbp is not None
        assert query.expected_answer == _money(fact.amount_gbp)


def test_temporal_answers_are_resolved_dates_or_periods(pilot):
    temporal = _by_type(pilot, "temporal")
    assert temporal
    for query in temporal:
        if query.answer_type == "date":
            assert _canonical_date(query.expected_answer) is not None
        elif query.answer_type == "period":
            parts = query.expected_answer.split(" to ")
            assert len(parts) == 2
            assert all(_canonical_date(part) is not None for part in parts)
        else:  # pragma: no cover - no other temporal answer types are emitted
            raise AssertionError(f"unexpected temporal answer type {query.answer_type!r}")


def test_historical_change_answers_name_the_previous_holder(pilot):
    historical = _by_type(pilot, "historical_change")
    assert historical
    people = {pilot.world.display_name(person.entity_id)
              for person in pilot.world.people()}
    for query in historical:
        assert query.expected_answer in people
        assert query.query_type == "historical_change"
        assert any(" until " in evidence.quote
                   or " was chair " in evidence.quote
                   for evidence in query.supporting_evidence)


def test_query_generation_is_deterministic(pilot):
    index = CorpusIndex.build(pilot.documents)
    rebuilt = generate_qa_dataset(index, pilot.world, target_pages=20)
    assert rebuilt.model_dump() == pilot.dataset.model_dump()


# --------------------------------------------------------------------------- v2 dataset
def test_v2_dataset_distribution(pilot_v2):
    dataset = pilot_v2.dataset
    assert dataset.version == 2
    assert dataset.seed == 7
    assert dataset.target_pages == dataset.actual_pages == 20
    assert len(dataset.documents) == 7
    assert {document.doc_type for document in dataset.documents} == set(DOC_TYPES)
    assert len(dataset.queries) == 95
    answerable = [query for query in dataset.queries if query.answerable]
    unanswerable = [query for query in dataset.queries if not query.answerable]
    assert len(answerable) == 70
    assert len(unanswerable) == 25
    assert len({query.query_id for query in dataset.queries}) == len(dataset.queries)
    assert {query.query_type for query in dataset.queries} == EXPECTED_QUERY_TYPES
    assert Counter(query.unanswerable_kind
                   for query in unanswerable) == {
        kind: 5 for kind in UNANSWERABLE_KINDS}
    for query in unanswerable:
        assert query.query_type == "unanswerable"
        assert query.answer_type == "unanswerable"
        assert query.expected_answer == ""
        assert query.supporting_evidence == []
        assert query.gold_fact_ids == []
        assert query.relevant_document_ids == []
        assert query.unanswerable_proof is not None
    for query in answerable:
        assert query.unanswerable_kind is None
        assert query.unanswerable_proof is None


def test_v2_every_answerable_question_is_grounded(pilot_v2):
    documents = _documents(pilot_v2)
    for query in pilot_v2.dataset.queries:
        if not query.answerable:
            continue
        assert query.expected_answer.strip()
        assert query.supporting_evidence
        assert query.relevant_document_ids == list(dict.fromkeys(
            evidence.document_id for evidence in query.supporting_evidence))
        for evidence in query.supporting_evidence:
            document = documents[evidence.document_id]
            assert evidence.filename == document.filename
            assert evidence.text_sha256 == document.text_sha256
            sentence = document.sentence_text_by_idx[evidence.sentence_idx]
            assert evidence.quote in sentence


def test_v2_unanswerable_proofs_are_structural(pilot_v2):
    world = pilot_v2.world
    plans = [ctx.plan for ctx in pilot_v2.documents]
    index = FactIndex.build(world, plans)
    known_documents = {document.document_id for document in pilot_v2.dataset.documents}
    attribute_tokens = {
        "auditor": ("auditor", "audited"),
        "vat_registration_number": ("vat", "registration number"),
    }
    for query in pilot_v2.dataset.queries:
        if query.answerable:
            continue
        proof = query.unanswerable_proof
        assert proof is not None
        assert proof.kind == query.unanswerable_kind
        assert proof.subject_id in world.entities
        assert proof.explanation
        assert proof.checked_fact_ids
        assert all(fact_id in index.facts for fact_id in proof.checked_fact_ids)
        assert all(document_id in known_documents
                   for document_id in proof.checked_document_ids)
        subject = proof.subject_id

        if proof.kind == "absent_attribute":
            assert proof.attribute in attribute_tokens
            assert proof.attribute not in index.attributes.get(subject, {})
            tokens = attribute_tokens[proof.attribute]
            for fact in index.facts.values():
                haystack = f"{fact.note} {fact.text or ''}".casefold()
                assert not any(re.search(rf"\b{re.escape(token)}\b", haystack)
                               for token in tokens), (
                    f"{query.query_id}: fact {fact.fact_id} supports {proof.attribute}")

        elif proof.kind == "absent_relationship":
            assert proof.relation == "advises"
            assert not index.has_relation(subject, proof.relation,
                                          include_negated=True)
            distractors = [index.facts[fact_id]
                           for fact_id in proof.checked_fact_ids]
            assert any(fact.relation == "represents" for fact in distractors)

        elif proof.kind == "unsupported_historical_date":
            reference = dt.date.fromisoformat(proof.reference_date)
            chairs = [
                fact for fact in index.facts.values()
                if fact.kind == "edge" and fact.relation == "chair_of"
                and fact.object_id == subject and not fact.negated
            ]
            assert chairs
            for fact in chairs:
                assert not (fact.date is not None and fact.date <= reference
                            and (fact.end_date is None or fact.end_date >= reference))

        elif proof.kind == "absent_financial_information":
            for fact in index.facts.values():
                if subject not in (fact.subject_id, fact.object_id):
                    continue
                haystack = f"{fact.note} {fact.text or ''}".casefold()
                assert "dividend" not in haystack, (
                    f"{query.query_id}: dividend fact {fact.fact_id} exists")

        else:
            assert proof.kind == "false_presupposition"
            assert proof.object_id in world.entities
            assert not index.has_relation(subject, "owns", proof.object_id,
                                          include_negated=True)
            assert not index.has_relation(proof.object_id, "subsidiary_of",
                                          subject, include_negated=True)


def test_absent_attribute_check_is_scoped_to_the_individual_company(pilot_v2):
    index = FactIndex.build(pilot_v2.world, pilot_v2.plans)
    companies = _ordered_companies(index)
    company = next(item for item in companies
                   if "auditor" not in index.attributes.get(item.entity_id, {}))
    other = next(item for item in companies
                 if item.entity_id != company.entity_id
                 and "auditor" not in index.attributes.get(item.entity_id, {}))

    world = copy.deepcopy(pilot_v2.world)
    world.facts.append(Fact(
        fact_id="fact_auditor_entity_scope",
        kind="proposition",
        subject_id=other.entity_id,
        text="The auditor reviewed the accounts.",
    ))
    scoped = FactIndex.build(world, pilot_v2.plans)

    assert scoped.mentions_attribute(other.entity_id, "auditor")
    assert not scoped.mentions_attribute(company.entity_id, "auditor")
    assert not _attribute_absent(scoped, other, "auditor")
    assert _attribute_absent(scoped, company, "auditor")


def test_v2_duplicate_rate_is_below_target(pilot_v2):
    metrics = duplicate_metrics(pilot_v2.dataset)
    assert metrics["total"] == 95
    assert metrics["max_pair_occurrences"] <= 2
    assert metrics["duplicate_normalized_percent"] < 10.0
    forms = Counter(normalize_question(query.question)
                    for query in pilot_v2.dataset.queries)
    assert len(forms) / metrics["total"] >= 0.90


def test_v2_question_variants_are_plural_and_deterministic():
    assert len(QUESTION_VARIANTS) >= 20
    for key, options in QUESTION_VARIANTS.items():
        assert len(options) >= 2, key
        assert len(set(options)) == len(options), key
        assert options
    assert normalize_question("  Who IS the chair of X? ") == normalize_question(
        "who is the chair of x")


def test_v2_proposed_vs_completed_states_closed_world(pilot_v2):
    proposed = [query for query in pilot_v2.dataset.queries
                if query.query_type == "proposed_vs_completed"]
    assert proposed
    completion = [query for query in proposed if query.answer_type == "boolean"]
    assert completion
    for query in completion:
        assert query.expected_answer == "No"
        assert "closed-world assumption" in query.question.casefold()
        assert "closed-world" in query.notes.casefold()
        assert "completed acquisition" in query.notes.casefold()


def test_v2_query_generation_is_deterministic(pilot_v2):
    index = CorpusIndex.build(pilot_v2.documents)
    rebuilt = generate_qa_dataset_v2(index, pilot_v2.world, target_pages=20)
    assert rebuilt.model_dump() == pilot_v2.dataset.model_dump()
