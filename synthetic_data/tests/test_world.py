"""Tests for the deterministic fictional world."""

from __future__ import annotations

from synthetic_data.generator.world import build_world


def test_same_seed_is_identical():
    first = build_world(7)
    second = build_world(7)
    assert first.content_signature() == second.content_signature()
    assert first.duplicate_names == second.duplicate_names
    assert first.alias_person == second.alias_person


def test_different_seed_changes_world():
    first = build_world(7)
    second = build_world(8)
    assert first.content_signature() != second.content_signature()
    assert first.entities.keys() == second.entities.keys()
    assert [first.entity(eid).name for eid in sorted(first.entities)] != [
        second.entity(eid).name for eid in sorted(second.entities)]


def test_ontology_and_critical_relations(world):
    world.assert_valid()
    critical = world.critical_relation_names()
    assert critical == {
        "owns", "shareholder_of", "family_of", "lent_to", "owes",
        "guarantor_for", "represents",
    }
    for entity in world.entities.values():
        assert world.ontology.class_of(entity.entity_type) == entity.entity_type
        if entity.subtype:
            assert world.ontology.class_of(entity.subtype) is not None


def test_entity_population(world):
    assert len(world.people()) == 40
    assert len(world.companies()) == 24
    assert len(world.banks()) == 8
    assert len(world.law_firms()) == 6
    assert len(world.government_bodies()) == 6
    assert len(world.places()) == 12
    assert len(world.assets()) == 20
    assert len(world.legal_documents()) == 30
    assert world.events
    assert world.transactions
    assert world.contradictions


def test_duplicate_names_are_real_duplicates(world):
    first_id, second_id = world.duplicate_names
    assert first_id != second_id
    first = world.entity(first_id)
    second = world.entity(second_id)
    assert first.name == second.name
    assert first.entity_type == "person"
    assert second.entity_type == "person"


def test_alias_person_has_distinct_alias(world):
    entity = world.entity(world.alias_person)
    assert entity.aliases
    assert entity.name not in entity.aliases
    assert entity.aliases[0] != entity.name


# --------------------------------------------------------------------------- cohorts
def test_cohort_expansion_is_append_only():
    base = build_world(7)
    mid = build_world(7, target_pages=100)
    large = build_world(7, target_pages=500)

    assert (len(base.entities), len(base.facts), len(base.events),
            len(base.transactions)) == (149, 346, 32, 26)
    assert (len(mid.entities), len(mid.facts), len(mid.events),
            len(mid.transactions)) == (259, 720, 56, 50)
    assert (len(large.entities), len(large.facts), len(large.events),
            len(large.transactions)) == (849, 3216, 216, 210)

    assert set(base.entities) < set(mid.entities) < set(large.entities)
    for entity_id in base.entities:
        assert mid.entity(entity_id).name == base.entity(entity_id).name
        assert large.entity(entity_id).name == base.entity(entity_id).name

    base_facts = {fact.fact_id for fact in base.facts}
    mid_facts = {fact.fact_id: fact for fact in mid.facts}
    large_facts = {fact.fact_id: fact for fact in large.facts}
    assert base_facts <= set(mid_facts) <= set(large_facts)
    assert all(fact_id.startswith("fact_") for fact_id in base_facts)
    assert all(fact_id.startswith("cfact_")
               for fact_id in set(mid_facts) - base_facts)
    # Frozen facts never change when cohorts are appended.
    for fact_id in base_facts:
        assert vars(base.fact(fact_id)) == vars(mid_facts[fact_id]) == vars(
            large_facts[fact_id])
    # Earlier cohort facts are stable in later checkpoints.
    mid_cohort = {fact_id for fact_id in mid_facts
                  if fact_id.startswith("cfact_")}
    for fact_id in mid_cohort:
        assert vars(mid_facts[fact_id]) == vars(large_facts[fact_id])


def test_company_index_matches_cohort_fact_bundles():
    world = build_world(7, target_pages=100)
    assert len(world.companies()) == 48
    for company in world.companies():
        index = world.company_index(company.entity_id)
        bundle = world.facts_for_company(index)
        owns = bundle.get("owns", [])
        assert any(fact.subject_id == company.entity_id for fact in owns), (
            f"{company.entity_id} is not in the bundle it resolves to")


def test_cohort_entity_diversity_is_substantial():
    world = build_world(7, target_pages=500)
    assert len(world.companies()) == 208
    assert len(world.people()) == 232
    assert len({company.name for company in world.companies()}) == 208
    # Exactly one intentional namesake pair exists in the base world.
    assert len({person.name for person in world.people()}) == 231
    assert len({bank.name for bank in world.banks()}) == len(world.banks())
    assert len({firm.name for firm in world.law_firms()}) == len(world.law_firms())
    assert len({place.name for place in world.places()}) == len(world.places())


def test_5000_page_cohorts_do_not_shadow_existing_aliases():
    world = build_world(7, target_pages=5000)
    canonical_names = {}
    for entity in world.entities.values():
        canonical_names.setdefault(entity.name, []).append(entity.entity_id)

    # Only the intentional namesake pair may share a canonical name.
    collisions = {
        name: ids for name, ids in canonical_names.items() if len(ids) > 1
    }
    assert collisions == {
        world.entity(world.duplicate_names[0]).name: list(world.duplicate_names)
    }

    # No entity's alias may be another entity's canonical name.
    for entity in world.entities.values():
        for alias in entity.aliases:
            owners = set(canonical_names.get(alias, ()))
            assert not owners - {entity.entity_id}, (
                f"alias {alias!r} of {entity.entity_id} shadows {sorted(owners)}")
