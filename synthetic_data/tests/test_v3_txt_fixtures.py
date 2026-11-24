"""Phase C — the TXT regression fixture set.

The expected values live in ``v3_txt_fixtures.json`` and are written only by
``v3_txt_fixtures.py``, which is not a test.  These tests compare; they never
regenerate.  A layout change that is not intended therefore fails here instead of
silently becoming the new baseline.
"""
from __future__ import annotations

import json

import pytest

from synthetic_data.canonical import canonicalize

from synthetic_data.tests.v3_txt import (FIXTURE_HASHES, feature_document,
                                         fixture_payload, pilot_plan, txt_rendered)

PILOT_TYPES = {"case_notes", "due_diligence", "financial_agreement", "memorandum",
               "board_minutes", "press_release", "correspondence"}


@pytest.fixture(scope="module")
def expected():
    return json.loads(FIXTURE_HASHES.read_text(encoding="utf-8"))


def test_fixture_file_describes_how_it_was_produced(expected):
    assert expected["hash"] == "sha256(rendered bytes)"
    assert expected["command"].endswith("v3_txt_fixtures.py")
    assert expected["profile"]["page_width_chars"] is not None
    assert expected["config"]["seed"] == 7
    assert len(expected["documents"]) == 8


def test_rendered_bytes_match_the_recorded_hashes(expected, txt_rendered):
    recorded = fixture_payload()
    assert set(recorded) == {rendered.doc_id for rendered in txt_rendered}
    for rendered in txt_rendered:
        entry = recorded[rendered.doc_id]
        assert rendered.result.sha256 == entry["sha256"], rendered.doc_id
        assert len(rendered.text.encode("utf-8")) == entry["bytes"], rendered.doc_id
        assert len(rendered.lines) == entry["lines"], rendered.doc_id
        assert rendered.pages == entry["pages"], rendered.doc_id
        assert len(rendered.result.spans) == entry["spans"], rendered.doc_id
        assert len(canonicalize(rendered.document).sentences) == entry["sentences"]
        assert len(rendered.result.fact_surfaces) == entry["facts"]
        assert len(rendered.document.node_ids()) == entry["block_count"]


def test_fixture_set_covers_all_seven_prose_types(txt_rendered):
    types = {rendered.doc_type for rendered in txt_rendered
             if rendered.doc_id.startswith("doc:000")}
    assert types == PILOT_TYPES
    assert any(rendered.doc_id == "doc:90001" for rendered in txt_rendered)


def test_fixture_set_covers_the_required_structures(txt_rendered):
    documents = {rendered.doc_id: rendered for rendered in txt_rendered}
    features = {"footnote": False, "table": False, "list": False, "ordered_list": False,
                "signature": False, "exhibit": False, "thread": False, "header": False,
                "page_break": False, "multi_sentence_paragraph": False}
    for rendered in txt_rendered:
        document = rendered.document
        if document.footnotes:
            features["footnote"] = True
        if document.headers:
            features["header"] = True
        if document.signatures:
            features["signature"] = True
        if any(block.kind == "exhibit" for block in document.iter_blocks()):
            features["exhibit"] = True
        if document.metadata.thread is not None:
            features["thread"] = True
        for block in document.iter_blocks():
            if block.kind == "table":
                features["table"] = True
            if block.kind == "list":
                features["list"] = True
                if block.ordered:
                    features["ordered_list"] = True
            if block.kind == "page_break":
                features["page_break"] = True
        canonical = canonicalize(document)
        for block_id, group in canonical.by_block.items():
            if len(group) > 1 and all(s.kind == "sentence" for s in group):
                features["multi_sentence_paragraph"] = True
    missing = {name for name, present in features.items() if not present}
    assert not missing, missing


def test_fixture_set_covers_cross_document_facts_and_contradictions(txt_rendered):
    plan = pilot_plan()
    facts: dict[str, set[str]] = {}
    for doc_id in plan.prose_ids:
        for statement in plan.documents[doc_id].statements:
            facts.setdefault(statement.fact_id, set()).add(doc_id)
    assert any(len(documents) > 1 for documents in facts.values())
    assert list(plan.world.contradictions.values())


def test_fixture_hashes_are_not_written_by_the_test_suite():
    before = FIXTURE_HASHES.stat().st_mtime_ns
    payload = fixture_payload()
    assert payload
    after = FIXTURE_HASHES.stat().st_mtime_ns
    assert before == after
