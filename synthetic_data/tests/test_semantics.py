"""Independent semantic regression tests.

Each test either states the expected meaning directly (for example a family
relationship runs subject -> object) or injects one known defect into a copy of
the generated pilot and asserts that the independent validator rejects it.  The
tests never ask the generator's own fact objects whether the generator is right
without also checking the canonical sentence text.
"""

from __future__ import annotations

import copy
import dataclasses
import re
from collections import Counter

import pytest

from synthetic_data.generator import semantics
from synthetic_data.generator.documents import MentionSpec, StatedFact
from synthetic_data.generator.semantics import (
    SemanticValidationError,
    assert_semantics,
    check_contradictions,
    check_entity_identity,
    check_evidence_quotes,
    check_proposition_statuses,
    check_relationship_direction,
    check_temporal_support,
    check_transactions,
    check_unanswerable_proofs,
    check_unique_answers,
    extract_statements,
    relationship_statement_matches,
    temporal_bounds_supported,
)
from synthetic_data.generator.world import (
    FACILITY_CAPACITY_INCREASE,
    FACILITY_CAPACITY_ORIGINAL,
    Contradiction,
    Fact,
    Transaction,
)


def _facts(pilot):
    return {stated.fact.fact_id: stated.fact
            for plan in pilot.plans for stated in plan.stated_facts}


def _query(pilot, predicate):
    return next(query for query in pilot.dataset.queries if predicate(query))


def _changed_dataset(pilot, query, **updates):
    dataset = pilot.dataset.model_copy(deep=True)
    target = next(item for item in dataset.queries if item.query_id == query.query_id)
    for name, value in updates.items():
        setattr(target, name, value)
    return dataset


def _proposition(pilot, predicate):
    return next(fact for fact in _facts(pilot).values()
                if fact.kind == "proposition" and predicate(fact))


def _plan_with_fact(pilot, fact_id):
    return next(plan for plan in pilot.plans
                if any(stated.fact.fact_id == fact_id
                       for stated in plan.stated_facts))


# --------------------------------------------------------------------- direction
def test_extractor_expected_direction_cases():
    """Hard-coded expectations, independent of the generated world."""
    assert relationship_statement_matches(
        "Alice Example is a family member of Bob Example.",
        "family_of", "Alice Example", "Bob Example")
    assert not relationship_statement_matches(
        "Alice Example is a family member of Bob Example.",
        "family_of", "Bob Example", "Alice Example")
    assert relationship_statement_matches(
        "Acme Bank lent Beta Holdings GBP 5 million on 1 May 2022.",
        "lent_to", "Acme Bank", "Beta Holdings")
    assert not relationship_statement_matches(
        "Acme Bank lent Beta Holdings GBP 5 million on 1 May 2022.",
        "lent_to", "Beta Holdings", "Acme Bank")


def test_shared_statement_index_matches_per_plan_extraction(pilot):
    surfaces = semantics._SurfaceIndex.build(pilot.world)
    shared = semantics._statements_by_doc(pilot.plans, pilot.world, surfaces)

    assert set(shared) == {plan.document_id for plan in pilot.plans}
    assert (semantics._display_surfaces(pilot.world, surfaces)
            == semantics._display_surfaces(pilot.world))
    for plan in pilot.plans:
        assert shared[plan.document_id] == extract_statements(plan, pilot.world)
        assert (shared[plan.document_id]
                == extract_statements(plan, pilot.world, surfaces))


def test_family_questions_follow_subject_to_object(pilot):
    facts = _facts(pilot)
    family_queries = [query for query in pilot.dataset.queries
                      if query.query_type == "relationship_lookup"
                      and query.gold_fact_ids
                      and facts[query.gold_fact_ids[0]].relation == "family_of"]
    assert family_queries
    for query in family_queries:
        fact = facts[query.gold_fact_ids[0]]
        subject = pilot.world.display_name(fact.subject_id)
        obj = pilot.world.display_name(fact.object_id)
        assert query.expected_answer == obj
        assert f"Who is {subject} a family member of" in query.question
        assert not query.question.startswith(f"Who is {obj} a family member of")


def test_reversed_family_answer_is_rejected(pilot):
    facts = _facts(pilot)
    query = _query(pilot, lambda item: item.query_type == "relationship_lookup"
                   and item.gold_fact_ids
                   and facts[item.gold_fact_ids[0]].relation == "family_of")
    fact = facts[query.gold_fact_ids[0]]
    dataset = _changed_dataset(
        pilot, query,
        expected_answer=pilot.world.display_name(fact.subject_id))
    _checked, failures = check_relationship_direction(
        pilot.world, pilot.plans, dataset)
    assert any("family_of direction is reversed" in failure for failure in failures)


# --------------------------------------------------------------------- facilities
def test_facility_increase_is_capacity_not_a_loan(pilot):
    facts = _facts(pilot)
    increases = [fact for fact in facts.values()
                 if fact.note == FACILITY_CAPACITY_INCREASE]
    originals = [fact for fact in facts.values()
                 if fact.note == FACILITY_CAPACITY_ORIGINAL]
    assert len(increases) == len(originals) == 1
    assert increases[0].kind == "proposition"
    assert increases[0].status == "stated"
    assert increases[0].amount_gbp == 35_000_000
    assert originals[0].amount_gbp == 20_000_000
    transaction_facts = {transaction.fact_id for transaction in pilot.world.transactions}
    assert not transaction_facts & {increases[0].fact_id, originals[0].fact_id}
    assert all(transaction.kind == "loan" for transaction in pilot.world.transactions)

    query = _query(pilot, lambda item: item.query_type == "financial"
                   and item.gold_fact_ids
                   and facts[item.gold_fact_ids[0]].note == FACILITY_CAPACITY_INCREASE)
    assert query.question.startswith("What was the facility increased to")
    assert query.expected_answer == "GBP 35 million"
    assert "increased the facility available to" in query.supporting_evidence[0].quote
    assert "lent" not in query.supporting_evidence[0].quote.casefold()


def test_facility_as_lending_question_is_rejected(pilot):
    facts = _facts(pilot)
    query = _query(pilot, lambda item: item.query_type == "financial"
                   and item.gold_fact_ids
                   and facts[item.gold_fact_ids[0]].note == FACILITY_CAPACITY_INCREASE)
    dataset = _changed_dataset(
        pilot, query,
        question=("How much did Silvergate Bank Limited lend to Quarrystone "
                  "Industries Limited on 1 June 2023?"))
    _checked, failures = check_relationship_direction(
        pilot.world, pilot.plans, dataset)
    assert any("lending question cites a non-loan fact" in failure
               for failure in failures)
    _checked, failures = check_transactions(pilot.world, pilot.plans, dataset)
    assert any("facility is described as a loan" in failure for failure in failures)


# --------------------------------------------------------------------- ownership
def test_multi_ownership_document_requires_qualification(pilot):
    plan = pilot.plans[1]
    statements = extract_statements(plan, pilot.world)
    owned = [statement for statement in statements
             if statement.relation == "owns" and not statement.negated]
    kinds = Counter(pilot.world.entity(statement.object_id).kind
                    for statement in owned)
    assert kinds["company"] == 1
    assert kinds["asset"] == 1

    query = _query(pilot, lambda item: item.query_type == "relationship_lookup"
                   and item.relevant_document_ids == [plan.document_id]
                   and item.question.startswith("Which company does"))
    dataset = _changed_dataset(
        pilot, query,
        question=query.question.replace("Which company", "Which entity"))
    _checked, failures = check_unique_answers(pilot.world, pilot.plans, dataset)
    assert any("valid answers in the document" in failure for failure in failures)


# --------------------------------------------------------------------- temporal
def test_temporal_answer_must_be_explicitly_stated(pilot):
    query = _query(pilot, lambda item: item.query_type == "temporal"
                   and item.answer_type == "period")
    label = re.search(r"FY\d{4}", query.question).group(0)
    evidence = query.supporting_evidence[0]
    plan = next(item for item in pilot.plans
                if item.document_id == evidence.document_id)
    sentence = plan.sentences[evidence.sentence_idx]
    assert evidence.quote in sentence
    assert evidence.quote == query.expected_answer
    assert "runs from" in sentence
    assert label in sentence
    for boundary in query.expected_answer.split(" to "):
        assert boundary in sentence

    dataset = _changed_dataset(
        pilot, query,
        expected_answer="1 January 2023 to 31 December 2023")
    _checked, failures = check_temporal_support(pilot.world, pilot.plans, dataset)
    assert any("period answer is not stated explicitly" in failure
               for failure in failures)

    wrong_label = re.sub(r"FY\d{4}", "FY1999", query.question)
    dataset = _changed_dataset(pilot, query, question=wrong_label)
    _checked, failures = check_temporal_support(pilot.world, pilot.plans, dataset)
    assert any("fiscal-year label is not in the evidence" in failure
               for failure in failures)


def test_temporal_bound_helper_rejects_assumptions():
    sentence = ("The plan covers FY2024, which runs from 1 July 2023 "
                "to 30 June 2024.")
    assert temporal_bounds_supported(sentence, "1 July 2023 to 30 June 2024")
    assert not temporal_bounds_supported(sentence, "1 January 2023 to 31 December 2023")


# --------------------------------------------------------------------- identity
def test_namesake_surfaces_map_to_distinct_entity_ids(pilot):
    first, second = pilot.world.duplicate_names
    first_surface = pilot.world.display_name(first)
    second_surface = pilot.world.display_name(second)
    assert pilot.world.entity(first).name == pilot.world.entity(second).name
    assert first_surface != second_surface
    assert semantics._resolve_entity_ids(pilot.world, first_surface) == [first]
    assert semantics._resolve_entity_ids(pilot.world, second_surface) == [second]
    bare = pilot.world.entity(first).name
    assert len(semantics._resolve_entity_ids(pilot.world, bare)) == 2

    seen: set[str] = set()
    for plan in pilot.plans:
        for mention in plan.mentions:
            if mention.entity_id in (first, second):
                assert mention.surface == pilot.world.display_name(mention.entity_id)
                seen.add(mention.entity_id)
    assert seen == {first, second}


def test_unqualified_namesake_mention_is_rejected(pilot):
    plans = copy.deepcopy(pilot.plans)
    first = pilot.world.duplicate_names[0]
    bare = pilot.world.entity(first).name
    replaced = False
    for plan in plans:
        for index, mention in enumerate(plan.mentions):
            if mention.entity_id == first:
                plan.mentions[index] = MentionSpec(
                    entity_id=mention.entity_id,
                    sentence_text=mention.sentence_text,
                    surface=bare,
                    occurrence=mention.occurrence,
                )
                replaced = True
                break
        if replaced:
            break
    assert replaced
    _checked, failures = check_entity_identity(
        pilot.world, plans, pilot.dataset, pilot.gold_results)
    assert any("is not qualified" in failure for failure in failures)


def test_alias_resolves_to_the_same_entity(pilot):
    person = pilot.world.alias_person
    alias = pilot.world.entity(person).aliases[0]
    assert semantics._resolve_entity_ids(pilot.world, alias) == [person]


def test_surface_index_preserves_namesake_collision(pilot):
    first, second = pilot.world.duplicate_names
    index = semantics._SurfaceIndex.build(pilot.world)
    bare = pilot.world.entity(first).name

    assert index.resolve(bare) == [entity_id for entity_id in pilot.world.entities
                                   if entity_id in (first, second)]
    assert index.resolve(pilot.world.display_name(first)) == [first]
    assert index.resolve(pilot.world.display_name(second)) == [second]


def test_surface_index_prefers_display_and_canonical_names_to_aliases(pilot):
    world = copy.deepcopy(pilot.world)
    first, second = world.duplicate_names
    alias_person = world.alias_person
    entity = world.entity(alias_person)
    canonical = world.entity(second).name
    display = world.display_name(second)
    world.entities[alias_person] = dataclasses.replace(
        entity,
        aliases=tuple(dict.fromkeys((*entity.aliases, canonical, display))),
    )
    index = semantics._SurfaceIndex.build(world)

    assert index.resolve(canonical) == [entity_id for entity_id in world.entities
                                        if entity_id in (first, second)]
    assert index.resolve(display) == [second]
    assert alias_person not in index.resolve(canonical)


# --------------------------------------------------------------------- propositions
def test_allegation_speaker_is_not_the_reporter(pilot):
    fact = _proposition(pilot, lambda item: item.status == "alleged")
    plan = _plan_with_fact(pilot, fact.fact_id)
    assert fact.speaker_id is None
    assert fact.reporter_id is not None
    stated = next(stated for stated in plan.stated_facts
                  if stated.fact.fact_id == fact.fact_id)
    assert "former employee alleged" in stated.sentence_text
    assert pilot.world.display_name(fact.reporter_id) in stated.sentence_text
    gold = next(result.gold for result in pilot.gold_results
                if result.gold.doc.filename == plan.filename)
    proposition = next(item for item in gold.propositions if item.id == fact.fact_id)
    assert proposition.speaker is None
    assert proposition.status == "alleged"

    plans = copy.deepcopy(pilot.plans)
    for candidate in plans:
        for index, item in enumerate(candidate.stated_facts):
            if item.fact.fact_id == fact.fact_id:
                candidate.stated_facts[index] = StatedFact(
                    fact=dataclasses.replace(item.fact, speaker_id=fact.reporter_id),
                    sentence_text=item.sentence_text,
                )
    _checked, failures = check_proposition_statuses(pilot.world, plans, pilot.dataset)
    assert any("alleged speaker must be the unnamed former employee" in failure
               for failure in failures)


def test_negated_approval_has_a_negative_status(pilot):
    fact = _proposition(
        pilot, lambda item: item.negated and item.status == "stated"
        and "approval" in (item.text or "").casefold())
    assert fact.status == "stated"
    plans = copy.deepcopy(pilot.plans)
    for plan in plans:
        for index, stated in enumerate(plan.stated_facts):
            if stated.fact.fact_id == fact.fact_id:
                plan.stated_facts[index] = StatedFact(
                    fact=dataclasses.replace(stated.fact, status="held"),
                    sentence_text=stated.sentence_text,
                )
    _checked, failures = check_proposition_statuses(pilot.world, plans, pilot.dataset)
    assert any("negated proposition has positive status" in failure
               for failure in failures)


def test_proposed_vs_completed_documents_closed_world(pilot):
    queries = [query for query in pilot.dataset.queries
               if query.query_type == "proposed_vs_completed"
               and query.answer_type == "boolean"]
    assert queries
    for query in queries:
        assert query.expected_answer == "No"
        assert "closed-world" in query.notes.casefold()
        assert "proposed" in query.supporting_evidence[0].quote.casefold()

    dataset = _changed_dataset(pilot, queries[0], notes="Proposal, not completed.")
    _checked, failures = check_proposition_statuses(
        pilot.world, pilot.plans, dataset)
    assert any("closed-world assumption is not documented" in failure
               for failure in failures)


# --------------------------------------------------------------------- transactions
def test_only_completed_loans_are_transactions(pilot):
    eligible = {
        fact.fact_id for fact in pilot.world.facts
        if fact.kind == "edge" and fact.relation == "lent_to"
        and fact.amount_gbp is not None and fact.date is not None
        and not fact.negated
        and fact.note not in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE)
    }
    assert {transaction.fact_id for transaction in pilot.world.transactions} == eligible
    for transaction in pilot.world.transactions:
        fact = pilot.world.fact(transaction.fact_id)
        assert transaction.kind == "loan"
        assert transaction.status == "completed"
        assert fact.relation == "lent_to" and not fact.negated
        assert fact.note not in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE)


def test_facility_and_balance_transactions_are_rejected(pilot):
    facility = _proposition(
        pilot, lambda item: item.note == FACILITY_CAPACITY_INCREASE)
    company = pilot.world.entity(facility.subject_id)
    fake_world = copy.copy(pilot.world)
    fake_world.transactions = list(pilot.world.transactions) + [Transaction(
        transaction_id="txn_fake_facility",
        kind="loan",
        from_id=company.entity_id,
        to_id=company.entity_id,
        amount_gbp=facility.amount_gbp,
        date=facility.date,
        status="completed",
        fact_id=facility.fact_id,
    )]
    _checked, failures = check_transactions(
        fake_world, pilot.plans, pilot.dataset)
    assert any("facility capacity has a completed transaction" in failure
               for failure in failures)
    assert any("fact is not a completed loan" in failure for failure in failures)

    balance = next(fact for fact in pilot.world.facts if fact.relation == "owes")
    fake_world = copy.copy(pilot.world)
    fake_world.transactions = list(pilot.world.transactions) + [Transaction(
        transaction_id="txn_fake_payment",
        kind="payment",
        from_id=balance.subject_id,
        to_id=balance.object_id,
        amount_gbp=balance.amount_gbp,
        date=balance.date,
        status="completed",
        fact_id=balance.fact_id,
    )]
    _checked, failures = check_transactions(
        fake_world, pilot.plans, pilot.dataset)
    assert any("outstanding balance is treated as a payment" in failure
               for failure in failures)


# --------------------------------------------------------------------- contradictions
def _conflict_flag(gold, fact_id):
    return next(flag for flag in gold.expected_flags
                if flag.kind == "conflict" and flag.object_ids == [fact_id])


def test_contradiction_flags_are_reciprocally_paired(pilot):
    contradiction = pilot.world.contradictions[0]
    gold_by_filename = {result.gold.doc.filename: result.gold
                        for result in pilot.gold_results}
    for fact_id, companion_id in (
            (contradiction.fact_a_id, contradiction.fact_b_id),
            (contradiction.fact_b_id, contradiction.fact_a_id)):
        plan = _plan_with_fact(pilot, fact_id)
        companion_plan = _plan_with_fact(pilot, companion_id)
        note = _conflict_flag(gold_by_filename[plan.filename], fact_id).note
        assert companion_id in note
        assert companion_plan.filename in note
        reciprocal = _conflict_flag(
            gold_by_filename[companion_plan.filename], companion_id).note
        assert fact_id in reciprocal
        assert plan.filename in reciprocal


def test_unpaired_contradiction_flag_is_rejected(pilot):
    contradiction = pilot.world.contradictions[0]
    plan = _plan_with_fact(pilot, contradiction.fact_a_id)
    results = []
    for result in pilot.gold_results:
        if result.gold.doc.filename == plan.filename:
            gold = result.gold.model_copy(deep=True)
            for index, flag in enumerate(gold.expected_flags):
                if (flag.kind == "conflict"
                        and flag.object_ids == [contradiction.fact_a_id]):
                    gold.expected_flags[index] = flag.model_copy(
                        update={"note": "The same loan is stated at a different "
                                        "amount in another document."})
            result = dataclasses.replace(result, gold=gold)
        results.append(result)
    _checked, failures = check_contradictions(
        pilot.world, pilot.plans, pilot.dataset, results)
    assert any("does not name companion" in failure for failure in failures)


def test_equal_amounts_are_not_a_contradiction(pilot):
    contradiction = pilot.world.contradictions[0]
    fake_world = copy.copy(pilot.world)
    fake_world.contradictions = [Contradiction(
        contradiction_id="contradiction_fake",
        kind=contradiction.kind,
        fact_a_id=contradiction.fact_a_id,
        fact_b_id=contradiction.fact_a_id,
        description="Not a genuine conflict.",
    )]
    _checked, failures = check_contradictions(
        fake_world, pilot.plans, pilot.dataset, pilot.gold_results)
    assert any("amounts do not conflict" in failure for failure in failures)


# --------------------------------------------------------------------- grounding
def test_tampered_evidence_quote_is_rejected(pilot):
    dataset = pilot.dataset.model_copy(deep=True)
    query = next(item for item in dataset.queries if item.supporting_evidence)
    query.supporting_evidence[0].quote += " TAMPERED"
    _checked, failures = check_evidence_quotes(pilot.world, pilot.plans, dataset)
    assert any("evidence quote is not in the cited sentence" in failure
               for failure in failures)
    with pytest.raises(SemanticValidationError):
        assert_semantics(pilot.world, pilot.plans, dataset, pilot.gold_results)


def test_generated_pilot_passes_every_semantic_check(pilot):
    report = assert_semantics(
        pilot.world, pilot.plans, pilot.dataset, pilot.gold_results)
    assert report.ok
    assert report.failures == []
    assert set(report.checks) == {
        "evidence_quotes", "relationship_direction", "unique_answers",
        "temporal_support", "proposition_statuses", "entity_identity",
        "transactions", "contradictions",
    }


# --------------------------------------------------------------------- v2 proofs
def test_v2_pilot_passes_every_semantic_check(pilot_v2):
    report = assert_semantics(
        pilot_v2.world, pilot_v2.plans, pilot_v2.dataset, pilot_v2.gold_results)
    assert report.ok
    assert report.failures == []
    assert set(report.checks) == {
        "evidence_quotes", "relationship_direction", "unique_answers",
        "temporal_support", "proposition_statuses", "entity_identity",
        "transactions", "contradictions", "unanswerable_proofs",
    }
    assert report.checks["unanswerable_proofs"] == 25
    assert all(value > 0 for value in report.checks.values())


def test_present_attribute_invalidates_absent_attribute_proof(pilot_v2):
    dataset = pilot_v2.dataset.model_copy(deep=True)
    query = next(item for item in dataset.queries
                 if item.unanswerable_kind == "absent_attribute")
    proof = query.unanswerable_proof
    subject = pilot_v2.world.entity(proof.subject_id)
    # Every generated company has a jurisdiction, so this claim is false.
    assert "jurisdiction" in dict(subject.attributes)
    proof.attribute = "jurisdiction"
    _checked, failures = check_unanswerable_proofs(
        pilot_v2.world, pilot_v2.plans, dataset)
    assert any("is present on the subject" in failure for failure in failures)


def test_absent_attribute_proof_cache_is_scoped_by_subject(pilot_v2):
    dataset = pilot_v2.dataset.model_copy(deep=True)
    queries = [query for query in dataset.queries
               if query.unanswerable_kind == "absent_attribute"
               and query.unanswerable_proof.attribute == "auditor"]
    assert len(queries) >= 2
    mentioned, clean = queries[0], queries[1]
    assert mentioned.unanswerable_proof.subject_id != clean.unanswerable_proof.subject_id

    world = copy.deepcopy(pilot_v2.world)
    world.facts.append(Fact(
        fact_id="fact_auditor_subject_scope",
        kind="proposition",
        subject_id=mentioned.unanswerable_proof.subject_id,
        text="The auditor reviewed the accounts.",
    ))
    dataset.queries = [mentioned, clean]
    _checked, failures = check_unanswerable_proofs(world, pilot_v2.plans, dataset)

    assert any(failure.startswith(mentioned.query_id)
               and "mentions attribute" in failure for failure in failures)
    assert not any(failure.startswith(clean.query_id) for failure in failures)


def test_unknown_proof_fact_is_rejected(pilot_v2):
    dataset = pilot_v2.dataset.model_copy(deep=True)
    query = next(item for item in dataset.queries if not item.answerable)
    query.unanswerable_proof.checked_fact_ids = ["fact_does_not_exist"]
    _checked, failures = check_unanswerable_proofs(
        pilot_v2.world, pilot_v2.plans, dataset)
    assert any("proof cites unknown facts" in failure for failure in failures)


def test_answerable_query_may_not_carry_a_proof(pilot_v2):
    dataset = pilot_v2.dataset.model_copy(deep=True)
    answerable = next(item for item in dataset.queries if item.answerable)
    unanswerable = next(item for item in dataset.queries if not item.answerable)
    answerable.unanswerable_proof = unanswerable.unanswerable_proof.model_copy(
        deep=True)
    answerable.unanswerable_kind = unanswerable.unanswerable_kind
    _checked, failures = check_unanswerable_proofs(
        pilot_v2.world, pilot_v2.plans, dataset)
    assert any("answerable query carries an unanswerable proof" in failure
               for failure in failures)
