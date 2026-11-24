"""Phase B — canonical workbook planning (§7.5, DEC-4).

The workbook is its own semantic document.  These tests pin the parts that make
it a workbook rather than prose in cells: typed cells, number formats, formulas
with cached values, and the same transaction ids the prose documents use.
"""
from __future__ import annotations

import re

from collections import Counter

import pytest

from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.model import (
    CdmWorkbook,
    CellFormula,
    IsoDate,
    Money,
    Percentage,
    Table,
)

from synthetic_data.tests.v3_generation import checkpoint_plan, pilot_plan

@pytest.fixture(scope="module")
def v3_pilot_plan():
    return pilot_plan()


@pytest.fixture(scope="module")
def v3_checkpoint_plan():
    return checkpoint_plan()



@pytest.fixture(scope="module")
def plan(v3_pilot_plan):
    return v3_pilot_plan


@pytest.fixture(scope="module")
def workbooks(plan):
    return [plan.documents[doc_id] for doc_id in plan.workbook_ids]


def _tables(workbook):
    for sheet in workbook.workbook.sheets:
        yield from sheet.tables
        for block in sheet.blocks:
            if block.table is not None:
                yield block.table


def test_pilot_has_a_workbook_document(plan, workbooks):
    assert len(workbooks) == 1
    workbook = workbooks[0]
    assert isinstance(workbook, CdmWorkbook)
    assert workbook.doc_kind == "workbook"
    assert workbook.doc_type.startswith("workbook:")
    assert workbook.metadata.title
    assert workbook.sections == []          # content lives in sheets, not prose


def test_workbook_has_named_sheets_with_content(workbooks):
    workbook = workbooks[0]
    sheets = workbook.workbook.sheets
    assert len(sheets) >= 5
    names = [sheet.name for sheet in sheets]
    assert len(set(name.casefold() for name in names)) == len(names)
    for sheet in sheets:
        assert sheet.blocks or sheet.tables
        assert sheet.frozen_header is True


def test_every_sheet_carries_the_expected_table(workbooks):
    sheet_names = {sheet.name for sheet in workbooks[0].workbook.sheets}
    assert {"Cover", "Share register", "Transaction ledger",
            "Financial summary", "Security schedule"} <= sheet_names


def test_cells_are_typed_with_number_formats(workbooks):
    kinds = Counter()
    formats = Counter()
    for table in _tables(workbooks[0]):
        for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
            for cell in row.cells:
                if isinstance(cell.value, Money):
                    kinds["money"] += 1
                elif isinstance(cell.value, Percentage):
                    kinds["percent"] += 1
                elif isinstance(cell.value, IsoDate):
                    kinds["date"] += 1
                elif isinstance(cell.value, int):
                    kinds["int"] += 1
                if cell.number_format:
                    formats[cell.number_format] += 1
    assert {"money", "percent", "date", "int"} <= set(kinds)
    assert {"#,##0.00", "0.00%", "dd mmm yyyy", "#,##0"} <= set(formats)


def test_formulas_have_cached_values_that_agree(workbooks):
    formulas = [cell for table in _tables(workbooks[0])
                for row in [*table.header_rows, *table.body_rows, *table.total_rows]
                for cell in row.cells if cell.formula is not None]
    assert formulas, "a workbook total must be a formula, not a constant"
    for cell in formulas:
        assert isinstance(cell.formula, CellFormula)
        assert cell.formula.formula.startswith("=")
        assert cell.formula.cached_value == cell.value
        assert cell.formula.recalc_on_load is False


def test_ledger_states_the_same_facts_as_the_agreement(plan, workbooks):
    workbook = workbooks[0]
    workbook_facts = {statement.fact_id for statement in workbook.statements}
    agreement = next(document for document in plan.documents.values()
                     if document.doc_type == "financial_agreement")
    agreement_facts = {statement.fact_id for statement in agreement.statements}
    transaction_facts = {txn.fact_id for txn in plan.world.transactions.values()}
    shared = workbook_facts & agreement_facts & transaction_facts
    assert shared, "the ledger and the agreement must share transaction facts"
    # and the workflow cell really points at that fact
    ledger = next(sheet for sheet in workbook.workbook.sheets
                  if sheet.name == "Transaction ledger")
    table = ledger.tables[0]
    assert set(table.cell_refs.values()) & shared


def test_share_register_percentages_reconcile(plan, workbooks):
    workbook = workbooks[0]
    register = next(sheet for sheet in workbook.workbook.sheets
                    if sheet.name == "Share register")
    table = register.tables[0]
    holding_column = next(index for index, column in enumerate(table.columns)
                          if column.type == "percent")
    total = 0
    for row in table.body_rows:
        value = row.cells[holding_column].value
        assert isinstance(value, Percentage)
        total += value.basis_points
    assert total == 10_000
    # the same percentages the semantic layer assigned
    expected = sum(share.percent.basis_points
                   for share in plan.matters[0].shareholdings)
    assert total == expected


def test_named_ranges_resolve_through_sheet_ids(workbooks):
    """A defined name names a sheet *id*; the display name is never the reference."""
    workbook = workbooks[0].workbook
    sheet_ids = {sheet.sheet_id for sheet in workbook.sheets}
    assert set(workbook.named_ranges) == {"Ledger", "ShareRegister"}
    for name, target in workbook.named_ranges.items():
        assert target.name == name
        assert target.sheet_id in sheet_ids
        assert "!" not in target.ref, "a ref is sheet-relative, not a display name"
        assert re.fullmatch(r"[A-Z]{1,3}\d+:[A-Z]{1,3}\d+", target.ref)


def test_workbook_statements_resolve_to_the_world(plan, workbooks):
    for workbook in workbooks:
        node_ids = workbook.node_ids()
        for statement in workbook.statements:
            assert statement.fact_id in plan.world.facts
            assert statement.node_id in node_ids
            assert statement.statement_id in plan.world.facts[statement.fact_id].statement_ids


def test_workbooks_are_not_prose_and_carry_no_page_numbers(workbooks):
    for workbook in workbooks:
        assert workbook.sections == []
        for table in _tables(workbook):
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                for cell in row.cells:
                    assert cell.rowspan == 1 and cell.colspan == 1


def test_workbook_families_vary_across_a_larger_corpus(v3_checkpoint_plan):
    families = Counter()
    for doc_id in v3_checkpoint_plan.workbook_ids:
        families[v3_checkpoint_plan.documents[doc_id].doc_type] += 1
    assert len(families) >= 2, "a 70-document corpus must not ship one workbook family"
    assert sum(families.values()) == 10
