"""Phase F — the XLSX regression fixture set.

Expected values live in ``v3_xlsx_fixtures.json``, written only by
``v3_xlsx_fixtures.py`` (not a test).  Determinism is checked across processes, in both
directions: two interpreters must agree with each other *and* with the recorded bytes.
"""
from __future__ import annotations

import pytest

from synthetic_data.tests.v3_xlsx import (FIXTURE_HASHES, feature_rendered, fixture_payload,
                                          pilot_workbook, subprocess_digests, xlsx_config,
                                          xlsx_ingested, xlsx_rendered, xlsx_store)


@pytest.fixture(scope="module")
def expected():
    return fixture_payload()


def test_fixture_file_records_its_provenance(expected):
    assert expected["hash"] == "sha256(rendered bytes)"
    assert expected["command"].endswith("v3_xlsx_fixtures.py")
    assert expected["versions"]["openpyxl"]
    assert expected["versions"]["lxml"]
    assert len(expected["documents"]) == 2


def test_bytes_and_structure_match_the_recorded_values(expected, xlsx_rendered,
                                                       feature_rendered):
    recorded = expected["documents"]
    assert set(recorded) == {xlsx_rendered.doc_id, feature_rendered.doc_id}
    for rendered in (xlsx_rendered, feature_rendered):
        entry = recorded[rendered.doc_id]
        spans = rendered.spans
        assert rendered.sha256 == entry["sha256"], rendered.doc_id
        assert rendered.path.stat().st_size == entry["bytes"]
        assert len(rendered.document.workbook.sheets) == entry["sheets"]
        assert [sheet.name for sheet in rendered.document.workbook.sheets] == \
            entry["sheet_names"]
        assert len(spans) == entry["spans"]
        assert sum(1 for span in spans if span.kind == "cell") == entry["cells"]
        assert sum(1 for span in spans if span.kind == "row") == entry["rows"]
        assert sum(1 for span in spans if span.kind == "table") == entry["tables"]
        assert len(rendered.result.fact_surfaces) == entry["facts"]
        assert len(rendered.result.warnings) == entry["warnings"]
        assert entry["pageless"] is True


def test_declared_rows_match_the_model(expected, xlsx_rendered):
    entry = expected["documents"][xlsx_rendered.doc_id]
    tables = [table for sheet in xlsx_rendered.document.workbook.sheets
              for table in sheet.tables]
    assert entry["declared_rows"] == sum(
        len([*table.header_rows, *table.body_rows, *table.total_rows]) for table in tables)
    assert entry["tables"] == len(tables)


def test_a_separate_process_produces_the_same_bytes(expected):
    digests = subprocess_digests("3")
    assert set(digests) == set(expected["documents"])
    for doc_id, sha in digests.items():
        assert sha == expected["documents"][doc_id]["sha256"], doc_id


def test_two_separate_processes_agree_with_each_other(expected):
    first = subprocess_digests("3")
    second = subprocess_digests("11")
    assert first == second
    for doc_id, sha in first.items():
        assert sha == expected["documents"][doc_id]["sha256"], doc_id


def test_fixture_hashes_are_not_written_by_the_test_suite():
    before = FIXTURE_HASHES.stat().st_mtime_ns
    assert fixture_payload()
    assert FIXTURE_HASHES.stat().st_mtime_ns == before
