"""Gold for workbooks: cell mapping, derived values, and currency identification.

The XLSX parser returns row text with no sheet name and no coordinates, so these
tests are as much about *not* inventing provenance as about producing gold:

* a cell's location comes from the render map, never from a parser sentence;
* a row maps to cells only when the token counts agree;
* the amount is extracted, the currency is only identified because the row says so.
"""
from __future__ import annotations

import pytest

from synthetic_data.gold import GoldInputs, build_gold, validate_pair, validate_v2, write_gold_pair
from synthetic_data.model import Money, Percentage, cell_role, workbook_cells
from synthetic_data.workbook import classify_cells, validate_derived_values
from synthetic_data.workbook_alignment import (
    align_workbook, currency_in_quote, numeric_surface_in, workbook_derived_evidence,
    workbook_fact_evidence,
)

from synthetic_data.tests.v3_gold import gold_store, gold_workbook, pilot_plan


def test_every_cell_has_a_sheet_and_a_reference(gold_workbook):
    alignment = gold_workbook.pair.workbook_alignment
    assert alignment.rows and alignment.cells
    for cell in alignment.cells:
        assert cell.sheet_name, "the render map must name the sheet"
        assert cell.cell_ref, "the render map must name the cell"
        assert cell.sentence_id, "each cell must sit in an aligned parser row"
    # the parser never supplies coordinates: every reference comes from the renderer
    assert {cell.cell_ref for cell in alignment.cells} >= {"E4", "A3"}


def test_rows_map_only_when_their_values_line_up(gold_workbook):
    alignment = gold_workbook.pair.workbook_alignment
    assert not alignment.problems
    for row in alignment.rows:
        assert row.kind in {"exact", "normalised"}
        assert row.quote and row.char_start is not None


def test_a_cells_quote_is_the_parser_token_not_the_row(gold_workbook):
    alignment = gold_workbook.pair.workbook_alignment
    ledger = next(cell for cell in alignment.cells
                  if cell.sheet_name == "Transaction ledger" and cell.column == 4)
    assert ledger.quote and "|" not in ledger.quote
    assert ledger.quote in ledger.row_quote
    assert ledger.exact_cell_quote is True


def test_the_amount_is_extracted_but_the_currency_is_not_in_the_cell(gold_workbook):
    """The difference the brief asks for: amount extraction vs currency identification."""
    view = gold_workbook.view
    assert "£" not in view.text
    alignment = gold_workbook.pair.workbook_alignment
    amount = next(cell for cell in alignment.cells
                  if cell.sheet_name == "Transaction ledger" and cell.column == 4
                  and cell.quote.replace(".", "").isdigit())
    currency_cell = next(cell for cell in alignment.cells
                         if cell.sheet_name == "Transaction ledger" and cell.column == 7
                         and cell.quote == "GBP")
    assert not currency_in_quote("GBP", amount.quote)
    assert currency_in_quote("GBP", amount.row_quote)


def test_money_evidence_records_where_the_currency_was_stated(gold_workbook):
    money_items = [item for edge in gold_workbook.pair.v2.edges for item in edge.evidence
                   if item.value_kind == "money"]
    assert money_items
    for item in money_items:
        assert item.currency == "GBP"
        # the cell quote never carries the code; the row does
        assert item.currency_in_quote is False
        assert item.currency_in_row is True


def test_derived_values_become_propositions_with_their_inputs(gold_workbook):
    workbook = gold_workbook.document.workbook
    derived = [prop for prop in gold_workbook.pair.v2.propositions if prop.derived_from]
    assert len(derived) == len(workbook.derived_values)
    for prop in derived:
        assert prop.canonical_fact_id
        assert prop.derived_from
        assert prop.evidence and prop.evidence[0].quote
        assert prop.evidence[0].value_kind in {"money", "percent", "number", "date"}


def test_a_derived_total_states_its_computed_value(gold_workbook):
    totals = {prop.id: prop for prop in gold_workbook.pair.v2.propositions
              if prop.derived_from}
    repaid = next(prop for prop in totals.values()
                  if "Total repaid" in prop.text)
    assert "30,000,000.00" in repaid.text
    assert repaid.evidence[0].typed_value == "GBP 30,000,000.00"


def test_derived_values_are_validated_against_their_formulas(gold_workbook):
    validate_derived_values(gold_workbook.document.workbook, gold_workbook.render)


def test_workbook_gold_pair_validates(tmp_path, gold_store, gold_workbook):
    v1, v2, alignment = write_gold_pair(
        gold_workbook.pair, tmp_path / "gold", gold_workbook.document_id,
        gold_workbook.document.doc_type, "xlsx")
    report = validate_pair(v1, v2, alignment, store=gold_store,
                           view=gold_workbook.view, render=gold_workbook.render,
                           document=gold_workbook.document, world=pilot_plan().world,
                           row_quotes=gold_workbook.row_quotes)
    assert report.ok, report.errors[:5]


def test_workbook_evidence_cites_parser_rows(gold_workbook):
    sentence_ids = {row["sentence_id"] for row in gold_workbook.view.sentences}
    for item in [item for edge in gold_workbook.pair.v2.edges for item in edge.evidence]:
        assert item.sentence_id in sentence_ids
        assert item.quote in gold_workbook.view.text
        assert item.render_loc["sheet_name"]
        assert item.render_loc["page"] is None
        assert item.render_loc["pageless"] is True


def _without_locations(alignment, keep: int):
    """The same alignment with only ``keep`` cells left carrying a location."""
    cells = []
    for index, cell in enumerate(alignment.cells):
        if index < keep:
            cells.append(cell)
            continue
        cells.append(cell.__class__(
            cell_id=cell.cell_id, sheet_id=cell.sheet_id, sheet_name=None,
            row_id=cell.row_id, column=cell.column, cell_ref=None,
            sentence_id=None, quote="", char_start=None, char_end=None,
            row_quote=cell.row_quote, exact_cell_quote=False, kind="missing"))
    return alignment.__class__(document_id=alignment.document_id,
                               rows=alignment.rows, cells=tuple(cells),
                               problems=())


def test_cells_without_a_rendered_location_are_not_grounded(gold_workbook):
    """A cell the render map does not place cannot become evidence."""
    alignment = gold_workbook.pair.workbook_alignment
    trimmed = _without_locations(alignment, keep=3)
    evidence = workbook_fact_evidence(gold_workbook.document, trimmed)
    refused = [item for item in evidence if not item.acceptable]
    assert refused, "a cell without a location must be refused"


def test_derived_evidence_is_refused_when_the_value_is_not_in_the_row(gold_workbook):
    """A quote that does not state the computed value is not evidence for it."""
    alignment = gold_workbook.pair.workbook_alignment
    document = gold_workbook.document
    cell = document.workbook.derived_values[0].cell_id
    broken = alignment.__class__(
        document_id=alignment.document_id, rows=alignment.rows,
        cells=tuple(
            (c.__class__(cell_id=c.cell_id, sheet_id=c.sheet_id,
                         sheet_name=c.sheet_name, row_id=c.row_id, column=c.column,
                         cell_ref=c.cell_ref, sentence_id=c.sentence_id,
                         quote="unrelated text", char_start=c.char_start,
                         char_end=c.char_end, row_quote=c.row_quote,
                         exact_cell_quote=c.exact_cell_quote, kind=c.kind)
             if c.cell_id == cell else c)
            for c in alignment.cells),
        problems=())
    evidence = workbook_derived_evidence(document, broken)
    item = next(item for item in evidence if item.statement_id == f"derived:{cell}")
    assert not item.acceptable
    assert "not in the parser text" in " ".join(item.problems)


def test_numeric_comparison_ignores_formatting_and_rejects_other_amounts():
    from synthetic_data.model import IsoDate, TableCell
    import datetime as dt

    money = TableCell(cell_id="tbl:x/01/r01c01",
                      value=Money(amount_minor=1_200_000_000, currency="GBP"),
                      display="GBP 12,000,000.00")
    assert numeric_surface_in(money, "12000000")
    assert numeric_surface_in(money, "12000000.00")
    assert not numeric_surface_in(money, "12500000")
    percent = TableCell(cell_id="tbl:x/01/r01c02",
                        value=Percentage(basis_points=4000), display="40.00%")
    assert numeric_surface_in(percent, "40.00%")
    assert not numeric_surface_in(percent, "45.00%")
    date = TableCell(cell_id="tbl:x/01/r01c03",
                     value=IsoDate(value=dt.date(2024, 1, 15)), display="15 Jan 2024")
    assert numeric_surface_in(date, "15 Jan 2024")
    assert not numeric_surface_in(date, "16 Jan 2024")


def test_role_counts_on_the_pilot_workbook(gold_workbook):
    roles = classify_cells(gold_workbook.document.workbook)
    assert sum(1 for role in roles.values() if role == "asserted") == 25
    assert sum(1 for role in roles.values() if role == "derived") == 14
    assert sum(1 for role in roles.values() if role == "decorative") > 100
