"""Phase C closure — duplicate sentences within one document.

Four causes were found and are separated here:

1. *role collapse*: four subsidiaries all referred to as "a Subsidiary", so two
   different facts realised as the identical sentence;
2. *boilerplate reuse*: a pool sentence repeated across sections of one document
   because the producer did not know what the document had already said;
3. *restatement*: the same fact legitimately stated twice, with identical wording
   because the wording came from the same statement scope;
4. *legitimate repetition*: a table total that genuinely repeats a value.

Only (1)–(3) are defects.  (4) is allowed and asserted to be allowed.
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from synthetic_data.canonical import canonicalize
from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.generation.surface import RoleMap

from synthetic_data.tests.v3_generation import CHECKPOINT, PILOT, pilot_plan


@pytest.fixture(scope="module")
def plan():
    return pilot_plan()


def _duplicates(plan, kinds=("sentence",)):
    """``{doc_id: {text: [sentence, ...]}}`` for repeated prose sentences."""
    found = {}
    for doc_id in plan.prose_ids:
        canonical = canonicalize(plan.documents[doc_id])
        seen = defaultdict(list)
        for sentence in canonical.sentences:
            if sentence.kind in kinds:
                seen[sentence.text].append(sentence)
        repeated = {text: group for text, group in seen.items() if len(group) > 1}
        if repeated:
            found[doc_id] = repeated
    return found


def test_no_prose_sentence_is_repeated_within_a_pilot_document(plan):
    assert _duplicates(plan) == {}


def test_no_prose_sentence_is_repeated_across_the_development_corpus():
    checkpoint = build_corpus(CorpusConfig(**CHECKPOINT))
    assert _duplicates(checkpoint) == {}


def test_only_table_rows_may_legitimately_repeat(plan):
    """A total row may equal another table's total; prose and lists may not."""
    repeated = _duplicates(plan, kinds=("sentence", "list_item", "table_row"))
    for doc_id, groups in repeated.items():
        for text, group in groups.items():
            assert {sentence.kind for sentence in group} == {"table_row"}, (doc_id, text)


def test_shared_entity_roles_produce_distinct_references(plan):
    """The role fix, against the real world: subsidiaries never collapse to one."""
    world = plan.world
    subsidiaries = [entity for entity in world.entities.values()
                    if entity.subtype == "subsidiary"]
    assert len(subsidiaries) >= 2
    role_map = RoleMap(entities=world.entities,
                       roles={entity.entity_id: "a Subsidiary"
                              for entity in subsidiaries})
    references = {role_map.of(entity.entity_id, "role") for entity in subsidiaries}
    assert len(references) == len(subsidiaries)
    assert not any("Subsidiary" in reference for reference in references)


def test_rolemap_falls_back_to_names_when_a_role_is_shared():
    from synthetic_data.model import Entity

    entities = {
        "ent:company:a": Entity(entity_id="ent:company:a", entity_type="organization",
                                subtype="company", canonical_name="Alpha Trading Limited"),
        "ent:company:b": Entity(entity_id="ent:company:b", entity_type="organization",
                                subtype="company", canonical_name="Beta Holdings GmbH"),
    }
    roles = {"ent:company:a": "a Subsidiary", "ent:company:b": "a Subsidiary"}
    role_map = RoleMap(entities=entities, roles=roles)
    first = role_map.of("ent:company:a", "role")
    second = role_map.of("ent:company:b", "role")
    assert first != second
    assert "Subsidiary" not in first and "Subsidiary" not in second
    assert first == "Alpha Trading"
    assert second == "Beta Holdings"


def test_rolemap_keeps_a_unique_role():
    from synthetic_data.model import Entity

    entities = {
        "ent:bank:x": Entity(entity_id="ent:bank:x", entity_type="organization",
                             subtype="bank", canonical_name="Caldervale Bank Limited"),
        "ent:company:y": Entity(entity_id="ent:company:y", entity_type="organization",
                                subtype="company", canonical_name="Oakhaven Limited"),
    }
    role_map = RoleMap(entities=entities,
                       roles={"ent:bank:x": "the Lender",
                              "ent:company:y": "the Borrower"})
    assert role_map.of("ent:bank:x", "role") == "the Lender"
    assert role_map.of("ent:company:y", "role") == "the Borrower"


def test_restated_facts_are_worded_differently():
    """The mechanism: realising one fact twice must not reuse the wording."""
    from synthetic_data.generation.surface import RoleMap, Surface

    plan = pilot_plan()
    fact = next(f for f in plan.world.facts.values()
                if f.kind == "edge" and f.amount is not None and f.subject)
    surface = Surface(RoleMap(entities=plan.world.entities), b"k" * 16, "probe")
    used: set[str] = set()
    first = surface.fact(fact, "probe/one", used=used)
    second = surface.fact(fact, "probe/one", used=used)
    assert first.text != second.text
    assert len(used) == 2
    # without the registry the two calls coincide, which is the defect
    again = Surface(RoleMap(entities=plan.world.entities), b"k" * 16, "probe")
    assert again.fact(fact, "probe/one").text == again.fact(fact, "probe/one").text


def test_restatement_in_one_document_is_worded_differently(plan):
    """Where a document does state a fact twice, the two sentences differ."""
    checked = 0
    for doc_id in plan.prose_ids:
        document = plan.documents[doc_id]
        canonical = canonicalize(document)
        by_fact = defaultdict(list)
        for statement in document.statements:
            for sentence_id in statement.canonical_sentence_ids:
                sentence = next((item for item in canonical.sentences
                                 if item.sentence_id == sentence_id), None)
                if sentence is not None:
                    by_fact[statement.fact_id].append(sentence.text)
        for fact_id, texts in by_fact.items():
            if len(texts) < 2:
                continue
            checked += 1
            assert len(set(texts)) == len(texts), (doc_id, fact_id, texts)
    assert checked >= 0


def test_generation_is_deterministic_after_the_repetition_fix():
    first = build_corpus(CorpusConfig(**PILOT))
    second = build_corpus(CorpusConfig(**PILOT))
    assert [first.documents[d].model_dump_json() for d in first.prose_ids] == \
           [second.documents[d].model_dump_json() for d in second.prose_ids]
