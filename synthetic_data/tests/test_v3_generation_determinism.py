"""Phase B — determinism across processes and the fixed regression fixture set."""
from __future__ import annotations

import json

import pytest

from synthetic_data.generation import CorpusConfig, build_corpus

from synthetic_data.tests.v3_generation import (
    CHECKPOINT,
    FIXTURE_HASHES,
    PILOT,
    corpus_digests,
    fixture_digests,
    subprocess_corpus_digest,
)

from synthetic_data.tests.v3_generation import checkpoint_plan

@pytest.fixture(scope="module")
def v3_checkpoint_plan():
    return checkpoint_plan()



@pytest.fixture(scope="module")
def first():
    return build_corpus(CorpusConfig(**PILOT))


def test_same_configuration_builds_identical_documents(first):
    second = build_corpus(CorpusConfig(**PILOT))
    assert corpus_digests(first) == corpus_digests(second)
    assert first.world.model_dump_json() == second.world.model_dump_json()


def test_determinism_holds_across_processes(first):
    remote = subprocess_corpus_digest(CorpusConfig(**PILOT))
    assert remote == corpus_digests(first)


def test_adding_documents_does_not_perturb_earlier_ids():
    """Ids are pure functions of their scope, so document 0 keeps its identity."""
    small = build_corpus(CorpusConfig(seed=7, corpus_name="v3_prefix", documents=7))
    large = build_corpus(CorpusConfig(seed=7, corpus_name="v3_prefix", documents=14))
    small_ids = sorted(small.documents)
    large_ids = sorted(large.documents)
    assert set(small_ids) < set(large_ids)
    for doc_id in small_ids:
        assert small.documents[doc_id].document_id in large.documents
        assert (small.documents[doc_id].sections[0].section_id
                in {section.section_id
                    for section in large.documents[doc_id].sections})


def test_a_different_seed_changes_the_corpus():
    other = build_corpus(CorpusConfig(seed=8, corpus_name="v3_pilot",
                                      documents=7, workbooks=1))
    assert other.corpus_id != build_corpus(CorpusConfig(**PILOT)).corpus_id
    assert set(other.world.facts) != set(
        build_corpus(CorpusConfig(**PILOT)).world.facts)


def test_regression_fixture_hashes_match(first):
    assert FIXTURE_HASHES.exists(), (
        "the canonical regression fixture hashes are missing; regenerate them "
        "with `py -3.13 synthetic_data/tests/v3_generation_fixtures.py`")
    recorded = fixture_digests()
    actual = corpus_digests(first)
    assert set(recorded) == set(actual), "the fixture set changed shape"
    drifted = {doc_id: (recorded[doc_id], actual[doc_id])
               for doc_id in recorded if recorded[doc_id] != actual[doc_id]}
    assert not drifted, f"planner drift against the recorded fixtures: {drifted}"


def test_checkpoint_is_deterministic_too(v3_checkpoint_plan):
    assert corpus_digests(v3_checkpoint_plan) == corpus_digests(
        build_corpus(CorpusConfig(**CHECKPOINT)))
