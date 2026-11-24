"""Phase A — Layer 1 semantic model: entities, facts, transactions, time, threads."""
from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from synthetic_data.model import (
    Condition,
    Contradiction,
    CorpusWorld,
    Entity,
    EntityRef,
    Event,
    IsoDate,
    Money,
    SemanticFact,
    Thread,
    TimeScope,
    Transaction,
)

E1 = "ent:company:company:0001:aaaa"
E2 = "ent:bank:bank:0001:bbbb"
E3 = "ent:asset:asset:0001:cccc"


def entity(entity_id=E1, name="Oakhaven Holdings Limited", **kw) -> Entity:
    return Entity(entity_id=entity_id, entity_type="organization", canonical_name=name, **kw)


def ref(entity_id: str) -> EntityRef:
    return EntityRef(entity_id=entity_id)


def edge(fact_id="fact:edge:00001:01:dddd", *, status="completed", negated=False, **kw):
    return SemanticFact(fact_id=fact_id, kind="edge", subject=ref(E1), relation="owns",
                        object=ref(E3), status=status, negated=negated, **kw)


def test_valid_entities_and_facts():
    e = entity(aliases=["Oakhaven"], jurisdiction="GB")
    assert e.canonical_name.startswith("Oakhaven")
    assert edge(amount=Money(amount_minor=100, currency="GBP")).amount.amount_minor == 100


def test_entity_rejects_duplicate_alias_and_name_as_alias():
    with pytest.raises(ValidationError):
        entity(aliases=["A", "A"])
    with pytest.raises(ValidationError):
        entity(name="Oakhaven", aliases=["Oakhaven"])


def test_edge_requires_subject_relation_object():
    with pytest.raises(ValidationError):
        SemanticFact(fact_id="fact:edge:00001:01:dddd", kind="edge",
                     subject=ref(E1), object=ref(E3))
    with pytest.raises(ValidationError):
        SemanticFact(fact_id="fact:edge:00001:01:dddd", kind="edge",
                     subject=ref(E1), relation="owns")


def test_proposition_shape():
    prop = SemanticFact(fact_id="fact:prop:00001:01:eeee", kind="proposition",
                        text="The board approved the facility", status="approved")
    assert prop.text
    with pytest.raises(ValidationError):
        SemanticFact(fact_id="fact:prop:00001:01:eeee", kind="proposition", status="approved")
    with pytest.raises(ValidationError):
        SemanticFact(fact_id="fact:prop:00001:01:eeee", kind="proposition",
                     text="x", subject=ref(E1))


def test_negation_and_status_coherence():
    with pytest.raises(ValidationError):
        SemanticFact(fact_id="fact:prop:00001:01:eeee", kind="proposition",
                     text="The company denied the claim", status="denied")
    with pytest.raises(ValidationError):
        edge(status="completed", negated=True)


def test_temporal_structures():
    scope = TimeScope(kind="interval", start=IsoDate(value=dt.date(2024, 1, 1)),
                      end=IsoDate(value=dt.date(2024, 6, 30)))
    assert scope.end.value > scope.start.value
    with pytest.raises(ValidationError):
        TimeScope(kind="interval", start=IsoDate(value=dt.date(2024, 6, 30)),
                  end=IsoDate(value=dt.date(2024, 1, 1)))
    with pytest.raises(ValidationError):
        TimeScope(kind="relative")
    with pytest.raises(ValidationError):
        IsoDate(value=dt.date(2024, 3, 3), precision="year")


def test_transaction_must_reference_completed_non_negated_edge():
    good = edge()
    txn = Transaction(transaction_id="txn:0001:ffff", kind="disbursement",
                      from_entity=ref(E2), to_entity=ref(E1),
                      amount=Money(amount_minor=100, currency="GBP"),
                      date=IsoDate(value=dt.date(2024, 3, 3)), status="completed",
                      fact_id=good.fact_id)
    assert txn.status == "completed"

    world = CorpusWorld(
        corpus_id="corp:v3_test:1234", seed=7,
        entities={e.entity_id: e for e in (entity(), entity(E2, "Bank"), entity(E3, "Asset"))},
        facts={good.fact_id: good},
        transactions={txn.transaction_id: txn},
    )
    assert world.transactions

    proposed = edge(fact_id="fact:edge:00001:02:dddd", status="proposed")
    with pytest.raises(ValidationError):
        CorpusWorld(
            corpus_id="corp:v3_test:1234", seed=7,
            entities={e.entity_id: e for e in (entity(), entity(E2, "Bank"), entity(E3, "Asset"))},
            facts={proposed.fact_id: proposed},
            transactions={txn.transaction_id: txn.model_copy(update={"fact_id": proposed.fact_id})},
        )


def test_transaction_parties_must_differ():
    with pytest.raises(ValidationError):
        Transaction(transaction_id="txn:0001:ffff", kind="transfer",
                    from_entity=ref(E1), to_entity=ref(E1),
                    amount=Money(amount_minor=1, currency="GBP"),
                    date=IsoDate(value=dt.date(2024, 1, 1)), status="completed",
                    fact_id="fact:edge:00001:01:dddd")


def test_contradiction_requires_two_facts_and_two_documents():
    a = edge(fact_id="fact:edge:00001:01:aaaa", amount=Money(amount_minor=100, currency="GBP"))
    b = edge(fact_id="fact:edge:00002:01:bbbb", amount=Money(amount_minor=200, currency="GBP"))
    c = Contradiction(contradiction_id="ctr:0001:cccc", fact_a=a.fact_id, fact_b=b.fact_id,
                      dimension="amount", documents=["doc:00001:aa", "doc:00002:bb"])
    assert c.fact_a != c.fact_b
    with pytest.raises(ValidationError):
        Contradiction(contradiction_id="ctr:0001:cccc", fact_a=a.fact_id, fact_b=a.fact_id,
                      dimension="amount", documents=["doc:00001:aa", "doc:00002:bb"])
    with pytest.raises(ValidationError):
        Contradiction(contradiction_id="ctr:0001:cccc", fact_a=a.fact_id, fact_b=b.fact_id,
                      dimension="amount", documents=["doc:00001:aa", "doc:00001:aa"])


def test_thread_ordering_rules():
    t = Thread(thread_id="thr:v3_test:0001:aaaa", subject="Facility",
               participants=[ref(E1), ref(E2)], root_document="doc:00003:cc",
               messages=["doc:00003:cc", "doc:00004:dd"],
               latest_document="doc:00004:dd")
    assert t.messages[-1] == t.latest_document
    with pytest.raises(ValidationError):
        Thread(thread_id="thr:v3_test:0001:aaaa", subject="Facility",
               participants=[ref(E1)], root_document="doc:00003:cc",
               messages=["doc:00003:cc", "doc:00004:dd"],
               latest_document="doc:00003:cc")
    with pytest.raises(ValidationError):
        Thread(thread_id="thr:v3_test:0001:aaaa", subject="Facility",
               participants=[ref(E1)], root_document="doc:00003:cc",
               messages=["doc:00003:cc"], latest_document="doc:00003:cc",
               closed_by="doc:00009:zz")


def test_world_referential_integrity():
    good = edge()
    with pytest.raises(ValidationError):
        CorpusWorld(corpus_id="corp:v3_test:1234", seed=7,
                    entities={entity().entity_id: entity()},
                    facts={good.fact_id: good})       # object entity missing

    cond = Condition(condition_id="cnd:00001:01:aaaa", kind="subject_to",
                     constrains="fact:edge:99999:01:zzzz", predicate="x")
    with pytest.raises(ValidationError):
        CorpusWorld(corpus_id="corp:v3_test:1234", seed=7,
                    entities={e.entity_id: e for e in (entity(), entity(E2, "B"), entity(E3, "A"))},
                    facts={good.fact_id: good},
                    conditions={cond.condition_id: cond})


def test_world_key_must_match_object_id():
    good = edge()
    with pytest.raises(ValidationError):
        CorpusWorld(corpus_id="corp:v3_test:1234", seed=7,
                    entities={e.entity_id: e for e in (entity(), entity(E2, "B"), entity(E3, "A"))},
                    facts={"fact:edge:wrong:key": good})


def test_event_participants_and_dates():
    ev = Event(event_id="evt:0001:aaaa", name="Board meeting", kind="meeting",
               event_date=IsoDate(value=dt.date(2024, 3, 3)),
               participants=[ref(E1)])
    assert ev.event_date.value.year == 2024
    stranger = Event(event_id="evt:0002:bbbb", name="Board meeting", kind="meeting",
                     event_date=IsoDate(value=dt.date(2024, 3, 3)),
                     participants=[ref("ent:person:nobody:0000:xxxx")])
    with pytest.raises(ValidationError):
        CorpusWorld(corpus_id="corp:v3_test:1234", seed=7,
                    entities={entity().entity_id: entity()},
                    events={stranger.event_id: stranger})
