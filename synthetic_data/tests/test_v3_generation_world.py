"""Phase B — the semantic world: determinism, ids, financial and ownership consistency."""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

import pytest

from synthetic_data.generation import CorpusConfig, build_world
from synthetic_data.generation.config import PROSE_TYPES
from synthetic_data.model import Money

from synthetic_data.tests.v3_generation import CHECKPOINT, PILOT


@pytest.fixture(scope="module")
def draft():
    return build_world(CorpusConfig(**CHECKPOINT))


def test_world_is_deterministic():
    first = build_world(CorpusConfig(**PILOT))
    second = build_world(CorpusConfig(**PILOT))
    assert first.corpus_id == second.corpus_id
    assert sorted(first.world().facts) == sorted(second.world().facts)
    for fact_id, fact in first.world().facts.items():
        assert fact.text == second.world().facts[fact_id].text
        assert fact.amount == second.world().facts[fact_id].amount


def test_entity_ids_are_unique_across_matters(draft):
    ids = [entity.entity_id for matter in draft.matters
           for entity in matter.entities.values()]
    assert len(ids) == len(set(ids))
    assert len(ids) > 100, "each matter must have its own entity roster"


def test_entity_ordinals_are_corpus_global(draft):
    """Regression: per-matter counters once restarted at 0001.

    Every matter then allocated ``ent:company:company:0001`` and the allocator
    returned the same id for the same scope, so entities collided across matters
    and the world failed with a duplicate semantic id.
    """
    ordinals: dict[tuple[str, str], list[str]] = defaultdict(list)
    for matter in draft.matters:
        for entity in matter.entities.values():
            parts = entity.entity_id.split(":")
            # ent:<prefix-kind>:<scope-kind>:<ordinal>:<tag>; the scope kind is
            # part of the readable scope, so law_firm:0001 and org:0001 are
            # legitimately different entities
            ordinals[(parts[1], parts[2])].append(parts[-2])
    for key, values in ordinals.items():
        assert len(values) == len(set(values)), f"{key} reused an ordinal across matters"
    first_matter = list(draft.matters[0].entities.values())
    second_matter = list(draft.matters[1].entities.values())
    assert {entity.entity_id for entity in first_matter}.isdisjoint(
        {entity.entity_id for entity in second_matter})
    # and the aggregate builder itself must not see a duplicate
    world = draft.world()
    assert len(world.entities) == sum(len(matter.entities) for matter in draft.matters)


def test_every_fact_has_a_home_and_every_home_exists(draft):
    for matter in draft.matters:
        planned: set[str] = set()
        for doc_id, facts in matter.doc_facts.items():
            for fact_id in facts:
                assert fact_id in matter.facts
                planned.add(fact_id)
        assert set(matter.facts) == planned, (
            f"matter {matter.ordinal} has facts no document states")


def test_facts_resolve_to_entities_conditions_and_transactions(draft):
    world = draft.world()
    entity_ids = set(world.entities)
    for fact in world.facts.values():
        for ref in (fact.subject, fact.object, fact.speaker, fact.reporter):
            if ref is not None:
                assert ref.entity_id in entity_ids
        for condition_id in fact.conditions:
            assert condition_id in world.conditions
            assert world.conditions[condition_id].constrains == fact.fact_id
    for transaction in world.transactions.values():
        fact = world.facts[transaction.fact_id]
        assert fact.kind == "edge" and fact.status == "completed" and not fact.negated
        assert fact.amount == transaction.amount


def test_financial_consistency(draft):
    """Drawdowns fund the principal; instalments repay it; the balance reconciles."""
    for matter in draft.matters:
        principal: Money = matter.typed["principal"]
        rows = matter.typed["repayment_rows"]
        repaid = sum(row["principal"].amount_minor for row in rows)
        assert repaid == principal.amount_minor
        outstanding: Money = matter.typed["outstanding"]
        paid = sum(row["principal"].amount_minor for row in rows[:5])
        assert paid + outstanding.amount_minor == principal.amount_minor
        # the balance column is a running total, so the last row must reach zero
        assert rows[-1]["balance"].amount_minor == 0
        # drawdown tranches sum to the principal
        drawdowns = [txn for txn in matter.transactions.values()
                     if txn.kind == "drawdown"]
        assert sum(txn.amount.amount_minor for txn in drawdowns) == principal.amount_minor
        # interest is computed from the opening balance at the all-in rate / 4
        rate_bp = (matter.typed["rate"].basis_points
                   + matter.typed["margin"].basis_points)
        for row in rows:
            opening = row["balance"].amount_minor + row["principal"].amount_minor
            assert row["interest"].amount_minor == opening * rate_bp // 10_000 // 4


def test_ownership_consistency(draft):
    for matter in draft.matters:
        grouped = defaultdict(int)
        for share in matter.shareholdings:
            grouped[share.parent_id] += share.percent.basis_points
            fact = matter.facts[share.fact_id]
            assert fact.relation == "owns"
            assert fact.subject.entity_id == share.parent_id
            assert fact.object.entity_id == share.child_id
        assert all(total == 10_000 for total in grouped.values()), (
            "shareholding percentages must sum to 100%")


def test_historical_changes_are_ordered(draft):
    """Appointments precede the board meeting that notes them; the acquisition
    precedes the facility that financed it."""
    for matter in draft.matters:
        meeting = matter.dates[matter.doc_by_type["board_minutes"][0]].value
        for fact in matter.facts.values():
            if fact.relation == "is_director_of":
                assert fact.effective.start.value < meeting
            if fact.relation == "acquired":
                facility = next(f for f in matter.facts.values()
                                if f.relation == "lent_to"
                                and f.object.entity_id == matter.typed["holdco"])
                assert fact.effective.start.value < facility.effective.start.value


def test_conditions_and_temporal_context(draft):
    for matter in draft.matters:
        condition_ids = matter.typed["condition_ids"]
        assert len(condition_ids) >= 3
        facility = matter.facts[matter.typed["facility_fact"]]
        assert set(facility.conditions) == set(condition_ids)
        assert facility.effective.start is not None
        assert facility.effective.granularity == "day"
        for condition_id in condition_ids:
            assert matter.conditions[condition_id].predicate


def test_corpus_covers_every_prose_type(draft):
    seen = Counter(doc_type for _, doc_type, _ in draft.document_slots)
    assert set(seen) == set(PROSE_TYPES)
    assert seen["correspondence"] >= len(draft.matters)
