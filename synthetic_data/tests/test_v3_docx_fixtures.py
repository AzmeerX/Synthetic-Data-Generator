"""Phase E — the DOCX regression fixture set.

Expected values live in ``v3_docx_fixtures.json``, written only by
``v3_docx_fixtures.py`` (not a test).  These tests compare; they never regenerate.
Byte equality is checked **across processes** as well as in-process, because an
in-process repeat would share any cached state that could mask non-determinism.
"""
from __future__ import annotations

import pytest

from synthetic_data.canonical import canonicalize

from synthetic_data.tests.v3_docx import (FIXTURE_HASHES, PROSE_TYPES, docx_rendered,
                                         fixture_payload, render_docx, subprocess_digests)


@pytest.fixture(scope="module")
def feature(tmp_path_factory):
    from synthetic_data.tests.v3_pdf import pdf_feature_document
    return render_docx(pdf_feature_document(),
                       tmp_path_factory.mktemp("docx_feature") / "feature.docx")


@pytest.fixture(scope="module")
def expected():
    return fixture_payload()


def test_fixture_file_records_its_provenance(expected):
    assert expected["hash"] == "sha256(rendered bytes)"
    assert expected["command"].endswith("v3_docx_fixtures.py")
    assert expected["versions"]["python-docx"]
    assert expected["profile"]["table_style"] == "Table Grid"
    assert len(expected["documents"]) == 8


def test_bytes_and_structure_match_the_recorded_values(expected, docx_rendered, feature):
    recorded = expected["documents"]
    assert set(recorded) == {item.doc_id for item in docx_rendered} | {feature.doc_id}
    for rendered in [*docx_rendered, feature]:
        entry = recorded[rendered.doc_id]
        spans = rendered.spans
        assert rendered.result.sha256 == entry["sha256"], rendered.doc_id
        assert rendered.path.stat().st_size == entry["bytes"]
        assert len(spans) == entry["spans"]
        assert len(canonicalize(rendered.document).sentences) == entry["sentences"]
        assert len(rendered.result.fact_surfaces) == entry["facts"]
        assert sum(1 for span in spans if span.kind == "section") == entry["sections"]
        assert sum(1 for span in spans if span.kind == "block") == entry["paragraphs"]
        assert sum(1 for span in spans if span.kind == "table") == entry["tables"]
        assert sum(1 for span in spans if span.kind == "row") == entry["rows"]
        assert sum(1 for span in spans if span.kind == "cell") == entry["cells"]
        assert entry["pageless"] is True
        assert all(span.loc.page is None for span in spans)


def test_fixture_set_covers_all_seven_prose_types(docx_rendered, feature):
    types = {item.doc_type for item in docx_rendered if item.doc_id.startswith("doc:000")}
    assert types == PROSE_TYPES
    assert len(docx_rendered) == 7
    assert feature.doc_id == "doc:91001"


def test_a_separate_process_produces_the_same_bytes(expected):
    digests = subprocess_digests("3")
    assert set(digests) == set(expected["documents"])
    for doc_id, sha in digests.items():
        assert sha == expected["documents"][doc_id]["sha256"], doc_id


def test_two_separate_processes_agree_and_neither_matches_by_luck(expected):
    first = subprocess_digests("3")
    second = subprocess_digests("11")          # a different hash seed: set/dict order
    assert first == second
    for doc_id, sha in first.items():
        assert sha == expected["documents"][doc_id]["sha256"], doc_id


def test_fixture_hashes_are_not_written_by_the_test_suite():
    before = FIXTURE_HASHES.stat().st_mtime_ns
    assert fixture_payload()
    assert FIXTURE_HASHES.stat().st_mtime_ns == before
