"""Phase F — XLSX through the real Synth ingestion path.

The parser does not return a spreadsheet: it returns a stack of two-dimensional text
rows, one "page" per sheet, with numbers, dates and percentages formatted and currency
symbols and formulas gone.  These tests assert exactly that, so a later reader cannot
mistake "the file ingested" for "the sheet was understood".
"""
from __future__ import annotations

import re

import pytest

from synthetic_data.tests.v3_xlsx import (feature_ingested, feature_rendered,
                                          pilot_workbook, workbook_feature, xlsx_config,
                                          xlsx_ingested, xlsx_rendered, xlsx_store)


def rows_of(ingested, page: int | None = None) -> list[str]:
    return [" ".join(ingested.view.sentence_text(row).split())
            for row in ingested.view.sentences
            if page is None or row["page"] == page]


# -- segmentation -----------------------------------------------------------
def test_both_workbooks_segment(xlsx_ingested, feature_ingested):
    for ingested in (xlsx_ingested, feature_ingested):
        assert ingested.summary["status"] == "SEGMENTED"
        assert ingested.view.text.strip()


def test_every_sheet_becomes_one_parser_page(xlsx_ingested, feature_ingested):
    for ingested in (xlsx_ingested, feature_ingested):
        sheets = len(ingested.rendered.document.workbook.sheets)
        assert ingested.summary["pages"] == sheets == 5


def test_the_parser_page_is_not_a_physical_page(xlsx_ingested):
    assert xlsx_ingested.summary["pages"] == 5
    assert all(span.loc.page is None for span in xlsx_ingested.rendered.spans)


def test_sheet_order_is_preserved(xlsx_ingested, feature_ingested):
    for ingested in (xlsx_ingested, feature_ingested):
        names = [sheet.name for sheet in ingested.rendered.document.workbook.sheets]
        for page, name in enumerate(names, start=1):
            first = rows_of(ingested, page=page)[0]
            assert first  # a title row
            assert first in ingested.rendered.document.workbook.sheets[page - 1].blocks[0].runs[0].text


# -- rows and cells ---------------------------------------------------------
def test_row_count_matches_the_declared_table_plus_its_title(feature_ingested):
    workbook = feature_ingested.rendered.document.workbook
    for page, sheet in enumerate(workbook.sheets, start=1):
        declared = sum(len([*table.header_rows, *table.body_rows, *table.total_rows])
                       for table in sheet.tables)
        rows = rows_of(feature_ingested, page=page)
        assert len(rows) == declared + 1, (page, sheet.name, rows)   # +1 title row


def test_each_row_keeps_its_cell_order(feature_ingested):
    rows = rows_of(feature_ingested, page=2)
    header = next(row for row in rows if row.startswith("Date |"))
    assert header.split(" | ") == ["Date", "Reference", "Type", "Amount",
                                   "Running balance", "Status", "Currency"]
    body = [row for row in rows if row.startswith("15 ")]
    assert len(body) == 3
    for row in body:
        parts = row.split(" | ")
        assert len(parts) == 7
        assert parts[1].startswith("XW-0")
        assert parts[2] in {"Drawdown", "Repayment"}
        assert parts[5] == "Settled"
        assert parts[6] == "GBP"


def test_numeric_cells_arrive_as_numbers_not_formatted_strings(feature_ingested):
    rows = rows_of(feature_ingested, page=2)
    amount = next(row for row in rows if row.startswith("15 Jan 2024")).split(" | ")[3]
    assert re.fullmatch(r"-?\d+(\.\d+)?", amount), amount
    assert amount == "2500000"


def test_amount_is_extracted_but_a_currency_format_is_not(feature_ingested):
    """D6, measured: the amount survives as a number, the £ format does not.

    The currency is only identified because the sheet states it as its own text
    cell; nothing here claims the parser read a currency out of a number format.
    """
    text = feature_ingested.view.text
    for symbol in ("£", "€", "$"):
        assert symbol not in text, f"{symbol} unexpectedly survived a number format"
    assert "2,500,000" not in text          # no thousands separators either
    rows = rows_of(feature_ingested, page=2)
    parts = next(row for row in rows if row.startswith("15 Jan 2024")).split(" | ")
    assert parts[3] == "2500000", "the amount must arrive as an unformatted number"
    assert parts[6] == "GBP", "the currency column is the only currency evidence"


def test_percentages_and_dates_do_survive_their_formats(feature_ingested):
    rows = rows_of(feature_ingested, page=1)
    assert any(row.endswith("40.00%") for row in rows)
    ledger = rows_of(feature_ingested, page=2)
    assert any(row.startswith("15 Jan 2024") for row in ledger)


def test_formula_text_is_not_extracted_but_the_cached_value_is(feature_ingested):
    text = feature_ingested.view.text
    assert "SUM(" not in text and "=" not in text
    rows = rows_of(feature_ingested, page=2)
    total = next(row for row in rows if row.startswith("Total repaid"))
    assert "1000000" in total


def test_structural_information_the_parser_loses(feature_ingested):
    """Recorded, not assumed: what a workbook reader must not expect from ingestion."""
    text = feature_ingested.view.text
    assert "Reconciliation" in text                      # the title survives
    assert "B4" not in text                              # no cell coordinates
    assert "0.00%" != text and "0.065" not in text       # no formulas
    for row in feature_ingested.view.sentences:
        assert row["kind"] == "table_row"                # everything is a table row
        assert not row["section_path"]                   # no section paths at all


def test_duplicate_sheet_names_are_not_needed_to_disambiguate(feature_ingested):
    names = [sheet.name for sheet in feature_ingested.rendered.document.workbook.sheets]
    assert len(set(names)) == len(names) == 5
    assert all(len(name) <= 31 for name in names)


def test_cross_sheet_values_agree_after_parsing(feature_ingested):
    summary = rows_of(feature_ingested, page=3)
    principal = next(row for row in summary if row.startswith("Principal drawn"))
    recon = next(row for row in rows_of(feature_ingested, page=5)
                 if row.startswith("Principal drawn"))
    assert "4000000" in principal
    assert recon.split(" | ")[1] in {"4000000.0", "4000000"}


def test_the_pilot_workbook_survives_with_all_its_rows(xlsx_ingested):
    rows = rows_of(xlsx_ingested)
    assert any(row.startswith("Shareholder | Company") for row in rows)
    assert any(row.startswith("Date | Reference | Counterparty") for row in rows)
    assert any(row.startswith("Year | Measure | Amount") for row in rows)
    assert any(row.startswith("Asset | Charge | Ranking") for row in rows)
    assert any(row.startswith("Total repaid") for row in rows)
