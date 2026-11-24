"""Phase B — depth and structural diversity gates (§10.4, §11).

Every assertion names the numerator, denominator and threshold it checks, so a
failure says which measurement moved rather than just "diversity failed".
"""
from __future__ import annotations

from collections import Counter

import pytest

from synthetic_data.generation import CorpusConfig, build_corpus, compute_metrics
from synthetic_data.generation.config import DEPTH_TARGETS, PROSE_TYPES

from synthetic_data.tests.v3_generation import PILOT

from synthetic_data.tests.v3_generation import checkpoint_plan, pilot_plan

@pytest.fixture(scope="module")
def v3_pilot_plan():
    return pilot_plan()


@pytest.fixture(scope="module")
def v3_checkpoint_plan():
    return checkpoint_plan()



# ---------------------------------------------------------------------------
# depth
# ---------------------------------------------------------------------------
def test_pilot_documents_meet_every_depth_range(v3_pilot_plan):
    metrics = compute_metrics(v3_pilot_plan)
    assert metrics.depth_failures == [], "\n".join(metrics.depth_failures)
    for doc_type in PROSE_TYPES:
        assert doc_type in metrics.by_type, f"{doc_type} missing from the pilot"


def test_depth_targets_are_the_architecture_table():
    """The gate table is the doc's §11 table, not a locally invented one."""
    expected = {
        "press_release": (350, 650, 28, 50),
        "correspondence": (500, 900, 38, 70),
        "case_notes": (1200, 1900, 90, 140),
        "memorandum": (1300, 2000, 100, 150),
        "board_minutes": (1400, 2200, 105, 160),
        "due_diligence": (2000, 3200, 150, 240),
        "financial_agreement": (2500, 4500, 190, 330),
    }
    for doc_type, (low_w, high_w, low_s, high_s) in expected.items():
        target = DEPTH_TARGETS[doc_type]
        assert target.words == (low_w, high_w)
        assert target.sentences == (low_s, high_s)


def test_no_document_exceeds_its_fact_or_transaction_cap(v3_pilot_plan):
    metrics = compute_metrics(v3_pilot_plan)
    for row in metrics.documents.values():
        if row.doc_type not in DEPTH_TARGETS:      # a workbook has no prose depth
            continue
        target = DEPTH_TARGETS[row.doc_type]
        assert target.facts[0] <= row.facts <= target.facts[1]
        assert target.transactions[0] <= row.transactions <= target.transactions[1]


# ---------------------------------------------------------------------------
# diversity (checkpoint corpus: 70 prose + 10 workbooks)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def checkpoint_metrics(v3_checkpoint_plan):
    return compute_metrics(v3_checkpoint_plan)


def test_template_duplication_gate(checkpoint_metrics):
    """100 * (1 - unique masked templates / total sentences) <= 80."""
    value = checkpoint_metrics.diversity["template_duplication_pct"]
    assert value <= 80, f"masked template duplication {value}%"


def test_boilerplate_gate(checkpoint_metrics):
    """100 * (exact sentences in >= 2 documents / total sentences) <= 70."""
    value = checkpoint_metrics.diversity["boilerplate_pct"]
    assert value <= 70, f"boilerplate {value}%"


def test_template_inventory_gate(checkpoint_metrics):
    """unique masked templates / documents * 1000 >= 1200."""
    assert checkpoint_metrics.diversity["unique_templates_per_1000_docs"] >= 1200
    assert checkpoint_metrics.diversity["templates_per_document"] >= 1.2


def test_max_sharing_gate(checkpoint_metrics):
    """largest set of documents sharing one exact sentence <= 15% of the corpus."""
    diversity = checkpoint_metrics.diversity
    limit = 0.15 * diversity["documents"]
    assert diversity["max_documents_sharing_one_sentence"] <= limit, (
        f"{diversity['max_documents_sharing_one_sentence']} documents share one "
        f"sentence; limit is 15% of {diversity['documents']}")


def test_structural_signature_gate(v3_checkpoint_plan, checkpoint_metrics):
    """duplicate plan_fingerprint share <= 5% (uniqueness >= 95%), per type."""
    duplicated = checkpoint_metrics.diversity["structural_signature_duplication_pct"]
    assert duplicated <= 5
    workbooks = set(v3_checkpoint_plan.workbook_ids)
    totals: Counter = Counter()
    signatures: dict[str, set[str]] = {}
    for document in v3_checkpoint_plan.documents.values():
        if document.document_id in workbooks:
            continue
        totals[document.doc_type] += 1
        signatures.setdefault(document.doc_type, set()).add(document.plan_fingerprint)
    for doc_type, total in totals.items():
        assert len(signatures[doc_type]) / total >= 0.95, (
            f"{doc_type}: {len(signatures[doc_type])} distinct signatures for "
            f"{total} documents")


def test_information_shape_gate(checkpoint_metrics):
    """distinct (relation|status, subject type, object type) sequences / documents >= 0.60."""
    assert checkpoint_metrics.diversity["information_shape_diversity"] >= 0.60


def test_section_order_entropy_gate(checkpoint_metrics):
    """mean Shannon entropy of heading permutations, per type, >= 1.5 bits."""
    assert checkpoint_metrics.diversity["section_order_entropy_bits"] >= 1.5


def test_length_spread_gate(checkpoint_metrics):
    """per type, (p95 - p5) / median words >= 0.35; the gate uses the worst type."""
    assert checkpoint_metrics.diversity["length_spread_min"] >= 0.35


def test_table_shape_gate(checkpoint_metrics):
    """distinct (columns, column types, row band) unions per document, per type >= 0.5."""
    assert checkpoint_metrics.diversity["table_shape_diversity"] >= 0.5


def test_thread_shape_gate(checkpoint_metrics):
    """distinct (length, reply targets, citation counts) shapes / multi-message threads >= 0.5."""
    assert checkpoint_metrics.diversity["thread_shape_diversity"] >= 0.5
    assert checkpoint_metrics.diversity["threads"] >= 2


def test_every_prose_type_has_a_spread(checkpoint_metrics):
    """Length variably within a type is a per-type property, so check each band."""
    for doc_type, summary in checkpoint_metrics.by_type.items():
        target = DEPTH_TARGETS[doc_type]
        span = summary["words_p95"] - summary["words_p05"]
        assert span / summary["words_mean"] >= 0.30, (
            f"{doc_type}: p95-p05 span {span:.0f} words is flat")


def test_diversity_improves_when_the_corpus_grows(v3_pilot_plan, checkpoint_metrics):
    """Structure, not wording: the checkpoint must not be a longer pilot."""
    pilot_metrics = compute_metrics(v3_pilot_plan)
    assert (checkpoint_metrics.diversity["section_order_entropy_bits"]
            > pilot_metrics.diversity["section_order_entropy_bits"])
    assert (checkpoint_metrics.diversity["structural_signature_duplication_pct"]
            <= pilot_metrics.diversity["structural_signature_duplication_pct"])
