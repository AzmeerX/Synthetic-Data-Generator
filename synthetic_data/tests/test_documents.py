"""Tests for document planning, page allocation and trap coverage."""

from __future__ import annotations

import hashlib

from synthetic_data.generator.documents import (
    DOC_TYPES,
    V2_TRAP_SCHEDULE,
    build_document_plan,
    page_counts,
    page_counts_v2,
)
from synthetic_data.generator.world import build_world

REQUIRED_TRAPS = {
    "same_name_different_people",
    "alias_same_person",
    "historical_change",
    "cross_document_contradiction",
    "negated_relationship",
    "proposed_vs_completed",
    "conditional_relationship",
    "relative_date",
    "fiscal_date",
    "changing_financial_amounts",
    "irrelevant_distractor",
    "alleged_fact",
    "cross_document_relationship",
}


def test_page_counts_are_exact():
    assert page_counts(20) == [5, 4, 3, 4, 4]
    for total in (20, 500, 1000, 5000):
        counts = page_counts(total)
        assert sum(counts) == total
        assert all(1 <= count <= 5 for count in counts)


def test_page_counts_are_cumulative_prefixes():
    assert page_counts(500)[:5] == page_counts(20)
    assert page_counts(1000)[:125] == page_counts(500)
    assert page_counts(5000)[:250] == page_counts(1000)


def test_plans_are_realistic_and_trapped(world):
    counts = page_counts(500)
    plans = [build_document_plan(world, index, counts[index]) for index in range(7)]
    assert {plan.doc_type for plan in plans} == set(DOC_TYPES)
    assert len({plan.filename for plan in plans}) == len(plans)
    assert len({hashlib.sha256(plan.render_text().encode()).hexdigest()
                for plan in plans}) == len(plans)

    trap_union = set()
    for plan in plans:
        assert 1 <= plan.pages <= 5
        assert len(plan.trap_kinds) >= 3
        assert len(set(plan.sentences)) == len(plan.sentences)
        assert plan.render_text().isascii()
        assert len(plan.render_text().split("\f")) == plan.pages
        assert plan.doc_date.year >= 2024
        trap_union.update(plan.trap_kinds)
    assert REQUIRED_TRAPS <= trap_union


def test_small_and_large_corpora_share_the_prefix(world):
    small = [build_document_plan(world, index, page_counts(20)[index])
             for index in range(5)]
    large = [build_document_plan(world, index, page_counts(5000)[index])
             for index in range(5)]
    assert [plan.render_text() for plan in small] == [plan.render_text() for plan in large]
    assert [plan.filename for plan in small] == [plan.filename for plan in large]


def test_every_document_type_can_be_built(world):
    for index, expected_type in enumerate(DOC_TYPES):
        plan = build_document_plan(world, index, 2, doc_type=expected_type)
        assert plan.doc_type == expected_type
        assert plan.pages == 2


# --------------------------------------------------------------------------- v2 plans
V2_REQUIRED_HEADINGS = {
    "board_minutes": (
        "1. Attendance", "2. Agenda", "3. Matters discussed",
        "4. Resolutions", "5. Actions"),
    "financial_agreement": (
        "1. Recitals", "2. Parties", "3. Facility terms",
        "4. Conditions precedent", "5. Repayment", "6. Execution"),
    "due_diligence": (
        "1. Scope", "2. Findings", "3. Risks", "4. Discrepancies",
        "5. Recommendations"),
    "memorandum": (
        "1. Background", "2. Analysis", "3. Recommendations"),
    "case_notes": (
        "1. Parties", "2. Procedural history", "3. Submissions",
        "4. Findings and allegations", "5. Orders and next steps"),
    "correspondence": ("1. Body", "2. Closing"),
    "press_release": (
        "1. Corporate statement", "2. Announcements", "3. Contact", "4. Closing"),
}


def test_v2_page_counts_are_exact_and_prefix_stable():
    assert page_counts_v2(20) == [3, 3, 3, 3, 3, 3, 2]
    for total in (20, 500, 1000, 5000):
        counts = page_counts_v2(total)
        assert sum(counts) == total
        assert all(1 <= count <= 5 for count in counts)
    assert page_counts_v2(500)[:7] == page_counts_v2(20)
    assert page_counts_v2(1000)[:175] == page_counts_v2(500)
    assert page_counts_v2(5000)[:350] == page_counts_v2(1000)


def test_v2_plans_cover_all_seven_types_with_structures(world):
    counts = page_counts_v2(20)
    plans = [build_document_plan(world, index, counts[index], layout="v2")
             for index in range(7)]
    assert [plan.doc_type for plan in plans] == list(DOC_TYPES)
    assert [plan.pages for plan in plans] == [3, 3, 3, 3, 3, 3, 2]
    assert len({plan.filename for plan in plans}) == len(plans)
    assert len({hashlib.sha256(plan.render_text().encode()).hexdigest()
                for plan in plans}) == len(plans)

    derived_traps = {
        "cross_document_relationship", "cross_document_contradiction",
        "historical_change", "changing_financial_amounts",
    }
    trap_union = set()
    for plan in plans:
        assert 3 <= len(plan.trap_kinds) <= 6
        assert set(plan.trap_kinds) <= (
            V2_TRAP_SCHEDULE[plan.doc_type] | derived_traps)
        assert len(set(plan.sentences)) == len(plan.sentences)
        text = plan.render_text()
        assert text.isascii()
        assert len(text.split("\f")) == plan.pages
        for heading in V2_REQUIRED_HEADINGS[plan.doc_type]:
            assert heading in plan.sentences, (
                f"{plan.doc_type} is missing section {heading!r}")
        trap_union.update(plan.trap_kinds)

    # Trap coverage is broad but deliberately not identical for every document.
    assert {
        "same_name_different_people", "alias_same_person", "historical_change",
        "cross_document_contradiction", "negated_relationship",
        "proposed_vs_completed", "conditional_relationship", "relative_date",
        "fiscal_date", "changing_financial_amounts", "irrelevant_distractor",
        "alleged_fact",
    } <= trap_union
    assert len({tuple(plan.trap_kinds) for plan in plans}) > 1

    # Document-specific prose markers keep the formats distinguishable.
    by_type = {plan.doc_type: plan for plan in plans}
    assert any(s.startswith("To: ") for s in by_type["memorandum"].sentences)
    assert any(s.startswith("From: ") for s in by_type["memorandum"].sentences)
    assert any(s.startswith("Subject: ") for s in by_type["memorandum"].sentences)
    assert any(s.startswith("Sender: ") for s in by_type["correspondence"].sentences)
    assert any(s.startswith("Recipient: ") for s in by_type["correspondence"].sentences)
    assert any(s.startswith("Publication date: ")
               for s in by_type["press_release"].sentences)


def test_v2_first_seven_documents_are_a_stable_prefix(world):
    small_world = build_world(7, target_pages=20)
    large_world = build_world(7, target_pages=500)
    small = [build_document_plan(small_world, index, 3, layout="v2")
             for index in range(7)]
    large = [build_document_plan(large_world, index, 3, layout="v2")
             for index in range(7)]
    assert [plan.render_text() for plan in small] == [
        plan.render_text() for plan in large]
    assert [plan.filename for plan in small] == [plan.filename for plan in large]
    assert [plan.subject_entity_id for plan in small] == [
        plan.subject_entity_id for plan in large]
    assert [plan.trap_kinds for plan in small] == [
        plan.trap_kinds for plan in large]


def test_v2_large_corpus_uses_distinct_document_subjects():
    world = build_world(7, target_pages=100)
    counts = page_counts_v2(100)
    plans = [build_document_plan(world, index, counts[index], layout="v2")
             for index in range(len(counts))]
    subjects = [plan.subject_entity_id for plan in plans]
    assert len(plans) == 35
    assert len(set(subjects)) == len(subjects)
    assert {plan.doc_type for plan in plans} == set(DOC_TYPES)
    assert all(3 <= len(plan.trap_kinds) <= 6 for plan in plans)
    for plan in plans:
        assert len(plan.render_text().split("\f")) == plan.pages
