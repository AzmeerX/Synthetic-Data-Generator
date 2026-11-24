"""Phase D — the PDF regression fixture set.

Expected values live in ``v3_pdf_fixtures.json``, written only by
``v3_pdf_fixtures.py`` (not a test).  These tests compare; they never regenerate.
"""
from __future__ import annotations

import pytest

from synthetic_data.canonical import canonicalize

from synthetic_data.tests.v3_pdf import (FIXTURE_HASHES, PROSE_TYPES, fixture_payload,
                                         pdf_rendered)


@pytest.fixture(scope="module")
def expected():
    return fixture_payload()


def test_fixture_file_records_its_provenance(expected):
    assert expected["hash"] == "sha256(rendered bytes)"
    assert expected["command"].endswith("v3_pdf_fixtures.py")
    assert expected["versions"]["reportlab"]
    assert expected["versions"]["pypdfium2"]
    assert len(expected["documents"]) == 8


def test_bytes_match_the_recorded_hashes(expected, pdf_rendered):
    recorded = expected["documents"]
    assert set(recorded) == {item.doc_id for item in pdf_rendered}
    for rendered in pdf_rendered:
        entry = recorded[rendered.doc_id]
        assert rendered.result.sha256 == entry["sha256"], rendered.doc_id
        assert rendered.path.stat().st_size == entry["bytes"]
        assert rendered.page_count == entry["pages"]
        assert max(span.loc.page for span in rendered.spans) == entry["renderer_pages"]
        assert len(rendered.spans) == entry["spans"]
        assert len(canonicalize(rendered.document).sentences) == entry["sentences"]
        assert len(rendered.result.fact_surfaces) == entry["facts"]
        assert sum(1 for span in rendered.spans if span.loc.bbox is not None) == \
            entry["bbox_spans"]
        assert sum(1 for span in rendered.spans if span.kind == "cell") == entry["cells"]
        assert sum(1 for span in rendered.spans if span.kind == "table") == entry["tables"]


def test_page_parity_is_recorded_for_every_document(expected):
    for doc_id, entry in expected["documents"].items():
        assert entry["pages"] == entry["renderer_pages"], doc_id


def test_fixture_set_covers_all_seven_prose_types(pdf_rendered):
    types = {item.doc_type for item in pdf_rendered if item.doc_id.startswith("doc:000")}
    assert types == PROSE_TYPES
    assert any(item.doc_id == "doc:91001" for item in pdf_rendered)


def test_fixture_hashes_are_not_written_by_the_test_suite():
    before = FIXTURE_HASHES.stat().st_mtime_ns
    assert fixture_payload()
    assert FIXTURE_HASHES.stat().st_mtime_ns == before
