"""Phase F — the XLSX renderer writes a real spreadsheet, not a table dump.

Structure is asserted against the OOXML parts (``xl/workbook.xml``, the worksheet
parts, ``xl/styles.xml``) and against what ``openpyxl`` reads back in both modes, so a
renderer that believed it wrote numbers but wrote strings would fail here.
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile

import pytest

from openpyxl import load_workbook

from synthetic_data.model import Money, Percentage, RenderMap, assert_render_coverage
from synthetic_data.renderers import XLSX_CAPABILITIES, XlsxRenderer

from synthetic_data.tests.v3_xlsx import (feature_rendered, pilot_workbook, render_xlsx,
                                          workbook_feature, xlsx_config, xlsx_ingested,
                                          xlsx_rendered, xlsx_store)


def package(path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# -- package ----------------------------------------------------------------
def test_the_output_is_a_real_xlsx_package(xlsx_rendered, feature_rendered):
    for rendered in (xlsx_rendered, feature_rendered):
        parts = package(rendered.path)
        assert "[Content_Types].xml" in parts
        assert "xl/workbook.xml" in parts
        assert "xl/worksheets/sheet1.xml" in parts
        assert rendered.path.read_bytes()[:2] == b"PK"
        assert b"<html" not in parts["xl/workbook.xml"][:200].lower()


def test_every_sheet_is_a_worksheet_part(xlsx_rendered):
    parts = package(xlsx_rendered.path)
    sheets = [name for name in parts if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
    assert len(sheets) == len(xlsx_rendered.document.workbook.sheets) == 5


def test_sheet_names_and_order_match_the_model(xlsx_rendered, feature_rendered):
    for rendered in (xlsx_rendered, feature_rendered):
        workbook = load_workbook(rendered.path)
        expected = [sheet.name for sheet in rendered.document.workbook.sheets]
        assert workbook.sheetnames == expected


def test_capabilities_are_honest():
    assert XLSX_CAPABILITIES.format == "xlsx"
    assert XLSX_CAPABILITIES.page_kind == "sheet"
    assert XLSX_CAPABILITIES.tables is True
    assert XLSX_CAPABILITIES.bbox is False
    assert XLSX_CAPABILITIES.footnotes is False
    assert XLSX_CAPABILITIES.headers_footers is False


# -- typed cells ------------------------------------------------------------
def test_cells_are_typed_not_formatted_strings(feature_rendered):
    sheet = load_workbook(feature_rendered.path)["Transaction ledger"]
    # header row 3, body rows 4..6
    assert sheet["A4"].value == dt.datetime(2024, 1, 15)
    assert isinstance(sheet["D4"].value, (int, float)) and sheet["D4"].value == 2_500_000
    assert sheet["B4"].value == "XW-01"
    holding = load_workbook(feature_rendered.path)["Shareholder register"]["D4"]
    assert isinstance(holding.value, float) and holding.value == pytest.approx(0.40)


def test_number_formats_are_real_excel_formats(feature_rendered):
    workbook = load_workbook(feature_rendered.path)
    money = workbook["Transaction ledger"]["D4"]
    percent = workbook["Shareholder register"]["D4"]
    date = workbook["Transaction ledger"]["A4"]
    assert money.number_format == '"£"#,##0.00'
    assert percent.number_format == "0.00%"
    assert date.number_format == "dd mmm yyyy"


def test_currency_is_presented_by_format_and_kept_out_of_the_value(feature_rendered):
    workbook = load_workbook(feature_rendered.path)
    cell = workbook["Transaction ledger"]["D4"]
    assert "£" in cell.number_format
    assert cell.value == 2_500_000          # the factor stays a number
    assert not isinstance(cell.value, str)


def test_mixed_and_homogeneous_columns_coexist(feature_rendered):
    sheet = load_workbook(feature_rendered.path, data_only=True)["Reconciliation"]
    types = [type(sheet.cell(row=4, column=index).value)
             for index in range(1, 5)]
    assert types[0] is str                      # Check
    assert types[1] in (int, float)             # Value
    assert types[2] in (int, float)             # Expected
    assert types[3] in (int, float)             # Difference


# -- formulas and cached values --------------------------------------------
def test_formulas_are_preserved_in_the_xml(feature_rendered):
    parts = package(feature_rendered.path)
    found = []
    for name in parts:
        if name.startswith("xl/worksheets/sheet"):
            found.extend(re.findall(rb"<f>([^<]{2,120})</f>", parts[name]))
    formulas = b" ".join(found).decode("utf-8")
    assert "SUM(C4:C7)" in formulas
    assert "SUM(D4:D7)" in formulas
    assert "'Financial summary'!B4" in formulas
    assert "0.065*90/365" in formulas


def test_every_formula_cell_has_a_non_empty_cached_value(feature_rendered):
    parts = package(feature_rendered.path)
    for name in parts:
        if not name.startswith("xl/worksheets/sheet"):
            continue
        xml = parts[name]
        for match in re.finditer(rb"<c [^>]*>(<f>[^<]*</f>)(<v>([^<]*)</v>)?", xml):
            assert match.group(3) not in (None, b""), (
                name, match.group(1))


def test_both_openpyxl_modes_read_the_same_cell(feature_rendered):
    formula_view = load_workbook(feature_rendered.path, data_only=False)
    value_view = load_workbook(feature_rendered.path, data_only=True)
    summary = formula_view["Financial summary"]["B4"]
    cached = value_view["Financial summary"]["B4"]
    assert summary.value == "='Transaction ledger'!D4+'Transaction ledger'!D5"
    assert cached.value == pytest.approx(4_000_000)


def test_a_formula_only_counts_when_its_cached_value_is_readable(feature_rendered):
    """A formula string alone proves nothing: the reader must see a value."""
    values = load_workbook(feature_rendered.path, data_only=True)
    for sheet_name, coordinate in (("Shareholder register", "D8"),
                                   ("Transaction ledger", "D7"),
                                   ("Ownership schedule", "C6"),
                                   ("Reconciliation", "D4")):
        assert values[sheet_name][coordinate].value is not None, (sheet_name, coordinate)


# -- workbook features ------------------------------------------------------
def test_frozen_headers_and_filters(feature_rendered):
    sheet = load_workbook(feature_rendered.path)["Transaction ledger"]
    assert sheet.freeze_panes == "A4"          # rows 1..3 frozen, header is row 3
    assert sheet.auto_filter.ref == "A3:G7"


def test_column_widths_are_set(feature_rendered):
    sheet = load_workbook(feature_rendered.path)["Transaction ledger"]
    widths = {key: value.width for key, value in sheet.column_dimensions.items()}
    assert widths and all(10 <= width <= 48 for width in widths.values())


def test_a_valid_defined_name_is_written(feature_rendered):
    workbook = load_workbook(feature_rendered.path)
    assert "Principal" in workbook.defined_names
    target = workbook.defined_names["Principal"].attr_text
    assert target.startswith("'Financial summary'!")


def test_defined_names_resolve_through_sheet_ids(xlsx_rendered, pilot_workbook):
    """A name written from a display name used to match nothing; a sheet id cannot."""
    workbook = load_workbook(xlsx_rendered.path)
    names = {name: workbook.defined_names[name].attr_text for name in workbook.defined_names}
    assert set(names) == {"Ledger", "ShareRegister"}
    assert names["Ledger"] == "'Transaction ledger'!E4:E15"
    assert names["ShareRegister"] == "'Share register'!E4:E7"

    sheet = workbook["Transaction ledger"]
    first, last = names["Ledger"].split("!")[1].split(":")
    assert sheet[first].value == 12_000_000
    assert sheet[last].value == 3_750_000
    assert sheet[first].number_format == '"£"#,##0.00'
    assert pilot_workbook.workbook.named_ranges["Ledger"].ref == "E4:E15"


def test_a_workbook_is_never_rendered_as_prose(tmp_path):
    from synthetic_data.tests.v3_txt import pilot_plan
    plan = pilot_plan()
    prose = plan.documents[plan.prose_ids[0]]
    with pytest.raises(ValueError, match="workbook"):
        XlsxRenderer().render(prose, None, tmp_path / "nope.xlsx")


# -- provenance -------------------------------------------------------------
def test_every_node_is_spanned_exactly_once(xlsx_rendered, feature_rendered):
    for rendered in (xlsx_rendered, feature_rendered):
        ids = [span.canonical_id for span in rendered.spans]
        assert len(ids) == len(set(ids))
        assert set(ids) == set(rendered.document.node_ids())
        assert_render_coverage(set(ids), RenderMap(document_id=rendered.document.document_id,
                                                   format="xlsx", spans=rendered.spans))


def test_every_span_is_pageless_and_names_its_sheet(xlsx_rendered, feature_rendered):
    for rendered in (xlsx_rendered, feature_rendered):
        names = {sheet.name for sheet in rendered.document.workbook.sheets}
        for span in rendered.spans:
            assert span.loc.format == "xlsx"
            assert span.loc.pageless is True
            assert span.loc.page is None
            assert span.loc.bbox is None
            assert span.loc.sheet_name in names


def test_cell_spans_point_at_the_cell_they_describe(xlsx_rendered):
    workbook = load_workbook(xlsx_rendered.path, data_only=True)
    checked = 0
    for span in xlsx_rendered.spans:
        if span.kind != "cell" or span.loc.cell_ref is None:
            continue
        cell = workbook[span.loc.sheet_name][span.loc.cell_ref]
        assert cell.coordinate == span.loc.cell_ref
        assert span.loc.row == cell.row and span.loc.column == cell.column
        checked += 1
    assert checked >= 170


def test_the_parser_page_count_never_becomes_provenance(xlsx_rendered):
    assert xlsx_rendered.result.capabilities.page_kind == "sheet"
    assert all(span.loc.page is None for span in xlsx_rendered.spans)


# -- determinism ------------------------------------------------------------
def test_two_renders_of_one_workbook_are_byte_identical(tmp_path_factory, pilot_workbook):
    out = tmp_path_factory.mktemp("xlsx_repeat")
    first = render_xlsx(pilot_workbook, out / "a.xlsx")
    second = render_xlsx(pilot_workbook, out / "b.xlsx")
    assert first.sha256 == second.sha256


def test_zip_members_are_sorted_and_timestamped_fixed(xlsx_rendered):
    with zipfile.ZipFile(xlsx_rendered.path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_document_properties_are_fixed(xlsx_rendered):
    core = package(xlsx_rendered.path)["docProps/core.xml"].decode("utf-8")
    assert "2000-01-01T00:00:00Z" in core
    assert "Synthetic Data Generator" in core
