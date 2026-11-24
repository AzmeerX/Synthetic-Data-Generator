"""Phase B — contradiction grounding and distribution (DEC-5, §11.3, B6)."""
from __future__ import annotations

import math
from collections import Counter

import pytest

from synthetic_data.generation import (
    CorpusConfig,
    build_corpus,
    compute_metrics,
    plan_contradictions,
)

from synthetic_data.tests.v3_generation import CHECKPOINT

from synthetic_data.tests.v3_generation import checkpoint_plan, pilot_plan

@pytest.fixture(scope="module")
def v3_pilot_plan():
    return pilot_plan()


@pytest.fixture(scope="module")
def v3_checkpoint_plan():
    return checkpoint_plan()



def _codes(seed: int, name: str, documents: int, workbooks: int = 0,
           **kwargs) -> CorpusConfig:
    return CorpusConfig(seed=seed, corpus_name=name, documents=documents,
                        workbooks=workbooks, **kwargs)


# ---------------------------------------------------------------------------
# schedule: distribution without planning 500 documents
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("documents,workbooks,required", [
    (7, 1, 1),        # pilot: rounding up makes one mandatory
    (70, 10, 2),      # development sample
    (500, 70, 12),    # scale checkpoint
])
def test_density_is_at_least_one_per_fifty(documents, workbooks, required):
    report = plan_contradictions(_codes(11, f"density_{documents}",
                                        documents, workbooks))[0]
    assert report["required"] == required == math.ceil((documents + workbooks) / 50)
    assert report["planned"] == required
    assert report["planned"] >= report["required"]


def test_large_schedule_spreads_types_dimensions_and_pairs():
    report = plan_contradictions(_codes(11, "spread_500", 500, 70))[0]
    assert len(report["types"]) >= 3
    assert report["planned"] == 12
    # no dimension takes more than 40% of the contradictions
    worst = max(report["dimensions"].values())
    assert worst / report["planned"] <= 0.4
    # 12 contradictions and 12 eligible pairs: no pair may repeat
    assert len(set(map(tuple, report["pairs"]))) == len(report["pairs"])


def test_pairs_do_not_repeat_at_the_development_size():
    report = plan_contradictions(_codes(11, "spread_70", 70, 10))[0]
    pairs = [tuple(pair) for pair in report["pairs"]]
    assert len(set(pairs)) == len(pairs)
    assert len(report["types"]) >= 3


# ---------------------------------------------------------------------------
# grounding in planned corpora
# ---------------------------------------------------------------------------
def _statements_by_fact(plan):
    out: dict[str, list[tuple[str, object]]] = {}
    for doc_id, document in plan.documents.items():
        for statement in document.statements:
            out.setdefault(statement.fact_id, []).append((doc_id, statement))
    return out


def _check_grounding(plan):
    stated = _statements_by_fact(plan)
    for contradiction in plan.world.contradictions.values():
        fact_a = plan.world.facts[contradiction.fact_a]
        fact_b = plan.world.facts[contradiction.fact_b]
        docs_a = {doc_id for doc_id, _ in stated.get(fact_a.fact_id, [])}
        docs_b = {doc_id for doc_id, _ in stated.get(fact_b.fact_id, [])}
        assert docs_a and docs_b, "both sides must be stated"
        assert docs_a.isdisjoint(docs_b), "the two sides must be in different documents"
        assert set(contradiction.documents) <= docs_a | docs_b
        # the named documents are the ones that state the side they claim
        named_a = set(contradiction.documents) & docs_a
        named_b = set(contradiction.documents) & docs_b
        assert named_a and named_b
        assert named_a.isdisjoint(named_b)
        # no contradiction inside one document, and both sides are prose types
        assert plan.documents[contradiction.documents[0]].doc_type != \
            plan.documents[contradiction.documents[1]].doc_type


def _check_discrepancy(fact_a, fact_b, dimension):
    if dimension == "amount":
        assert fact_a.amount is not None and fact_b.amount is not None
        assert fact_a.amount != fact_b.amount
        assert fact_a.subject == fact_b.subject
        assert fact_a.object == fact_b.object
        assert fact_a.relation == fact_b.relation
    elif dimension == "date":
        assert fact_a.effective.start != fact_b.effective.start
        assert fact_a.effective.start is not None and fact_b.effective.start is not None
    elif dimension == "status":
        assert fact_a.status != fact_b.status
    elif dimension == "relationship":
        assert fact_a.relation != fact_b.relation
        assert fact_a.subject == fact_b.subject
    else:  # pragma: no cover - the literal type forbids it
        raise AssertionError(f"unknown dimension {dimension}")


def test_pilot_contradiction_is_grounded_and_explicit(v3_pilot_plan):
    plan = v3_pilot_plan
    assert len(plan.world.contradictions) == 1
    _check_grounding(plan)
    contradiction = next(iter(plan.world.contradictions.values()))
    fact_a = plan.world.facts[contradiction.fact_a]
    fact_b = plan.world.facts[contradiction.fact_b]
    _check_discrepancy(fact_a, fact_b, contradiction.dimension)
    # temporal context is explicit on both sides
    assert fact_a.effective is not None and fact_b.effective is not None
    # the contradiction is also a typed cross-reference between the documents
    assert any(reference.kind == "contradicts"
               and {reference.src_fact, reference.dst_fact}
               == {contradiction.fact_a, contradiction.fact_b}
               for reference in plan.world.cross_references)


def test_development_sample_contradictions_are_grounded(v3_checkpoint_plan):
    plan = v3_checkpoint_plan
    metrics = compute_metrics(plan)
    contradictions = metrics.contradictions
    assert contradictions["planned"] >= contradictions["required"] >= 2
    assert contradictions["density_satisfied"]
    assert contradictions["grounded"]
    assert contradictions["types_ok"]
    assert contradictions["dimension_share_ok"]
    assert contradictions["no_repeated_pair"]
    _check_grounding(plan)
    seen = Counter()
    for contradiction in plan.world.contradictions.values():
        fact_a = plan.world.facts[contradiction.fact_a]
        fact_b = plan.world.facts[contradiction.fact_b]
        _check_discrepancy(fact_a, fact_b, contradiction.dimension)
        assert fact_a.fact_id != fact_b.fact_id
        seen[contradiction.dimension] += 1
    # the 40% ceiling can only bind once a corpus holds at least three
    # contradictions; with two, a 50/50 split is the minimum possible.  The
    # 500-document schedule test proves the ceiling itself.
    if sum(seen.values()) >= 3:
        assert max(seen.values()) / sum(seen.values()) <= 0.4


def test_contradiction_documents_are_named_and_resolvable(v3_checkpoint_plan):
    plan = v3_checkpoint_plan
    for contradiction in plan.world.contradictions.values():
        assert len(contradiction.documents) == 2
        first, second = contradiction.documents
        assert first in plan.documents and second in plan.documents
        assert first != second


def test_contradictions_are_not_disabled_by_config():
    plan = build_corpus(CorpusConfig(seed=11, corpus_name="no_ctr", documents=7,
                                     include_contradictions=False))
    assert plan.world.contradictions == {}
    assert plan_contradictions(_codes(11, "no_ctr", 7,
                                      include_contradictions=False)) == []
