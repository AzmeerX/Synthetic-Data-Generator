"""Workbook readiness: named ranges, derived values, cell roles (DEC-12).

These are the three issues Phase G had to close before any gold could be built on a
workbook: a defined name that pointed at a display string, a formula cell with no
semantic identity, and a currency that existed only inside an Excel number format.
"""
from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from synthetic_data.model import (
    CellFormula,
    ColumnSpec,
    DerivedValue,
    IsoDate,
    Money,
    NamedRange,
    Percentage,
    Sheet,
    Table,
    TableCell,
    TableRow,
    Workbook,
    cell_role,
    workbook_cells,
)
from synthetic_data.tests.v3_gold import gold_workbook, pilot_plan
from synthetic_data.workbook import (
    WorkbookError,
    classify_cells,
    evaluate,
    formula_inputs,
    numeric_value,
    sheet_index,
    validate_derived_values,
)


def _cell(cell_id: str, *, text: str | None = None, value=None, display: str = "",
          number_format: str | None = None, formula: str | None = None,
          cached=None, derived: str | None = None, fact_ref: str | None = None
          ) -> TableCell:
    return TableCell(
        cell_id=cell_id, text=text, value=value, display=display,
        fact_ref=fact_ref, number_format=number_format,
        formula=CellFormula(formula=formula, cached_value=cached,
                            derived_fact_id=derived) if formula else None)


def _table(table_id: str = "tbl:x/01", *, derived: bool = True) -> Table:
    return Table(
        table_id=table_id,
        columns=[ColumnSpec(name="Label"), ColumnSpec(name="Amount"), ColumnSpec(name="Currency")],
        header_rows=[TableRow(row_id=f"{table_id}/r00", cells=[
            _cell(f"{table_id}/r00c00", text="Label"),
            _cell(f"{table_id}/r00c01", text="Amount"),
            _cell(f"{table_id}/r00c02", text="Currency")])],
        body_rows=[
            TableRow(row_id=f"{table_id}/r01", cells=[
                _cell(f"{table_id}/r01c00", text="First"),
                _cell(f"{table_id}/r01c01", value=Money(amount_minor=100_000, currency="GBP"),
                      display="GBP 1,000.00", number_format='#,##0.00'),
                _cell(f"{table_id}/r01c02", text="GBP")]),
            TableRow(row_id=f"{table_id}/r02", cells=[
                _cell(f"{table_id}/r02c00", text="Second"),
                _cell(f"{table_id}/r02c01", value=Money(amount_minor=250_000, currency="GBP"),
                      display="GBP 2,500.00", number_format='#,##0.00'),
                _cell(f"{table_id}/r02c02", text="GBP")]),
        ],
        total_rows=[TableRow(row_id=f"{table_id}/r03", cells=[
            _cell(f"{table_id}/r03c00", text="Total"),
            _cell(f"{table_id}/r03c01", value=Money(amount_minor=350_000, currency="GBP"),
                  display="GBP 3,500.00", formula="=SUM(B2:B3)",
                  cached=Money(amount_minor=350_000, currency="GBP"),
                  derived="fact:derived:total" if derived else None),
            _cell(f"{table_id}/r03c02", text="GBP")])])


def _workbook(**kw) -> Workbook:
    sheet = Sheet(sheet_id="sht:x/01", name="Sheet one", tables=[kw.pop("table", _table())])
    return Workbook(workbook_id="wbk:x/01", sheets=[sheet], **kw)


# -- named ranges ----------------------------------------------------------
def test_a_named_range_resolves_through_the_sheet_id():
    target = NamedRange(name="Totals", sheet_id="sht:x/01", ref="B2:B3")
    workbook = _workbook(table=_table(derived=False), named_ranges={"Totals": target})
    assert workbook.named_ranges["Totals"].sheet_id == "sht:x/01"
    assert workbook.named_ranges["Totals"].ref == "B2:B3"


def test_a_named_range_for_an_unknown_sheet_is_rejected():
    with pytest.raises(ValidationError, match="not in this workbook"):
        _workbook(table=_table(derived=False), named_ranges={"Totals": NamedRange(
            name="Totals", sheet_id="sht:x/99", ref="B2:B3")})


def test_a_named_range_with_a_display_name_target_is_rejected():
    """``Transaction_ledger!E2`` in a ``ref`` is the bug this decision removes."""
    with pytest.raises(ValidationError, match="sheet-relative A1"):
        NamedRange(name="Ledger", sheet_id="sht:x/01", ref="Transaction_ledger!E2")


def test_a_named_range_key_must_match_its_name():
    with pytest.raises(ValidationError, match="does not match its name"):
        _workbook(table=_table(derived=False), named_ranges={"Other": NamedRange(
            name="Totals", sheet_id="sht:x/01", ref="B2:B3")})


def test_an_invalid_defined_name_is_rejected():
    with pytest.raises(ValidationError, match="invalid defined name"):
        NamedRange(name="1 bad name", sheet_id="sht:x/01", ref="B2")


def test_pilot_named_ranges_point_at_the_data_rows():
    plan = pilot_plan()
    workbook = plan.documents[plan.workbook_ids[0]].workbook
    sheet_ids = {sheet.sheet_id: sheet.name for sheet in workbook.sheets}
    ledger = workbook.named_ranges["Ledger"]
    register = workbook.named_ranges["ShareRegister"]
    assert sheet_ids[ledger.sheet_id] == "Transaction ledger"
    assert sheet_ids[register.sheet_id] == "Share register"
    assert ledger.ref == "E4:E15"       # title row, blank row, header row 3, body 4..15
    assert register.ref == "E4:E7"


# -- derived values --------------------------------------------------------
def test_a_derived_value_declares_its_identity_and_inputs():
    table = _table()
    workbook = _workbook(table=table, derived_values=[DerivedValue(
        fact_id="fact:derived:total", cell_id="tbl:x/01/r03c01",
        label="Total of the two amounts",
        inputs=["tbl:x/01/r01c01", "tbl:x/01/r02c01"])])
    assert workbook.derived_fact_ids() == ["fact:derived:total"]


def test_a_formula_without_an_identity_is_decorative():
    table = _table()
    table.total_rows[0].cells[1] = _cell(
        "tbl:x/01/r03c01", value=Money(amount_minor=350_000, currency="GBP"),
        display="GBP 3,500.00", formula="=SUM(B2:B3)",
        cached=Money(amount_minor=350_000, currency="GBP"))
    workbook = _workbook(table=table)
    roles = classify_cells(workbook)
    assert roles["tbl:x/01/r03c01"] == "decorative"
    assert workbook.derived_values == []


def test_an_asserted_cell_is_neither_derived_nor_decorative():
    table = _table(derived=False)
    table.body_rows[0].cells[1] = _cell(
        "tbl:x/01/r01c01", value=Money(amount_minor=100_000, currency="GBP"),
        display="GBP 1,000.00", fact_ref="fact:edge:x:01")
    table.cell_refs["r01c01"] = "fact:edge:x:01"
    workbook = _workbook(table=table)
    assert cell_role(workbook.sheets[0].tables[0].body_rows[0].cells[1]) == "asserted"


def test_a_derived_value_without_its_cell_identity_is_rejected():
    with pytest.raises(ValidationError, match="does not match its cell"):
        _workbook(table=_table(derived=False), derived_values=[DerivedValue(
            fact_id="fact:derived:total", cell_id="tbl:x/01/r03c01",
            label="Total", inputs=["tbl:x/01/r01c01"])])


def test_a_derived_value_naming_an_unknown_input_is_rejected():
    with pytest.raises(ValidationError, match="unknown input"):
        _workbook(table=_table(), derived_values=[DerivedValue(
            fact_id="fact:derived:total", cell_id="tbl:x/01/r03c01",
            label="Total", inputs=["tbl:x/01/r99c01"])])


def test_a_derived_value_cannot_feed_itself():
    with pytest.raises(ValidationError, match="its own input"):
        DerivedValue(fact_id="fact:derived:total", cell_id="tbl:x/01/r03c01",
                     label="Total", inputs=["tbl:x/01/r03c01"])


def test_cyclic_derivations_are_rejected():
    table = _table()
    table.body_rows[0].cells[1] = _cell(
        "tbl:x/01/r01c01", value=Money(amount_minor=100_000, currency="GBP"),
        display="GBP 1,000.00", formula="=B4", cached=Money(amount_minor=100_000,
                                                           currency="GBP"),
        derived="fact:derived:first")
    table.total_rows[0].cells[1] = _cell(
        "tbl:x/01/r03c01", value=Money(amount_minor=100_000, currency="GBP"),
        display="GBP 1,000.00", formula="=B2", cached=Money(amount_minor=100_000,
                                                            currency="GBP"),
        derived="fact:derived:total")
    # r01 is spreadsheet B2 and r03 is B4: each feeds the other
    with pytest.raises(ValidationError, match="cyclic derivation"):
        _workbook(table=table, derived_values=[
            DerivedValue(fact_id="fact:derived:first", cell_id="tbl:x/01/r01c01",
                         label="First", inputs=["tbl:x/01/r03c01"]),
            DerivedValue(fact_id="fact:derived:total", cell_id="tbl:x/01/r03c01",
                         label="Total", inputs=["tbl:x/01/r01c01"])])


# -- formula arithmetic ----------------------------------------------------
def _index_from(workbook, spans):
    from synthetic_data.model import RenderLoc, RenderMap, RenderSpan

    layout = {}
    for sheet in workbook.sheets:
        for table in sheet.tables:
            for offset, row in enumerate([*table.header_rows, *table.body_rows,
                                          *table.total_rows], start=1):
                for column, cell in enumerate(row.cells, start=1):
                    layout[cell.cell_id] = (sheet.name, offset, column)

    render_spans = []
    for cell_id, (name, row, column) in layout.items():
        from openpyxl.utils import get_column_letter
        render_spans.append(RenderSpan(
            canonical_id=cell_id, kind="cell", format="xlsx",
            loc=RenderLoc(format="xlsx", pageless=True, sheet_name=name, row=row,
                          column=column, cell_ref=f"{get_column_letter(column)}{row}"),
            text="", text_sha256="0" * 64))
    return sheet_index(RenderMap(document_id="doc:x", format="xlsx", spans=render_spans))


def test_sum_over_a_range_and_arithmetic_chain_evaluate():
    table = _table(derived=False)
    table.body_rows[1].cells[1] = _cell(
        "tbl:x/01/r02c01", value=Money(amount_minor=250_000, currency="GBP"),
        display="GBP 2,500.00", formula="=B2+B2", cached=Money(amount_minor=250_000,
                                                              currency="GBP"))
    workbook = _workbook(table=table)
    index = _index_from(workbook, None)
    assert evaluate("=B2+B3", sheet="Sheet one", index=index, workbook=workbook) == 3500.0
    assert evaluate("=SUM(B2:B3)", sheet="Sheet one", index=index,
                    workbook=workbook) == 3500.0
    assert evaluate("=-B2", sheet="Sheet one", index=index, workbook=workbook) == -1000.0


def test_percentages_are_evaluated_as_their_fraction():
    assert numeric_value(TableCell(cell_id="tbl:x/01/r01c01",
                                   value=Percentage(basis_points=4000),
                                   display="40.00%")) == 0.4
    assert numeric_value(TableCell(cell_id="tbl:x/01/r01c02",
                                   value=Money(amount_minor=250, currency="GBP"),
                                   display="GBP 2.50")) == 2.5


def test_dates_are_evaluated_as_their_excel_serial():
    cell = TableCell(cell_id="tbl:x/01/r01c03",
                     value=IsoDate(value=dt.date(2024, 1, 15)), display="15 Jan 2024")
    assert numeric_value(cell) == (dt.date(2024, 1, 15) - dt.date(1899, 12, 30)).days


def test_an_unsupported_function_is_reported_not_approximated():
    workbook = _workbook(table=_table(derived=False))
    index = _index_from(workbook, None)
    with pytest.raises(WorkbookError, match="unsupported function"):
        evaluate("=VLOOKUP(B2,B2:B3,1)", sheet="Sheet one", index=index,
                 workbook=workbook)


def test_a_text_cell_in_arithmetic_is_an_error_not_a_zero():
    workbook = _workbook(table=_table(derived=False))
    index = _index_from(workbook, None)
    with pytest.raises(WorkbookError, match="cannot be used in arithmetic"):
        evaluate("=A2+1", sheet="Sheet one", index=index, workbook=workbook)


def test_formula_inputs_expands_a_range():
    workbook = _workbook(table=_table(derived=False))
    index = _index_from(workbook, None)
    assert formula_inputs("=SUM(B2:B3)", sheet="Sheet one",
                          index=index) == ["tbl:x/01/r01c01", "tbl:x/01/r02c01"]


# -- validation against the rendered workbook ------------------------------
def test_pilot_derived_values_match_their_formulas(gold_workbook):
    workbook = gold_workbook.document.workbook
    validate_derived_values(workbook, gold_workbook.render)
    assert len(workbook.derived_values) == 14


def test_a_tampered_cached_value_is_rejected(gold_workbook):
    workbook = gold_workbook.document.workbook
    sheet = workbook.sheets[1]
    table = sheet.tables[0]
    total = table.total_rows[0]
    wrong = Money(amount_minor=1, currency="GBP")
    total.cells[4] = total.cells[4].model_copy(update={"value": wrong, "display": "GBP 0.01"})
    with pytest.raises(WorkbookError, match="formula computes"):
        validate_derived_values(workbook, gold_workbook.render)


def test_every_pilot_cell_has_exactly_one_role(gold_workbook):
    roles = classify_cells(gold_workbook.document.workbook)
    cells = list(workbook_cells(gold_workbook.document.workbook))
    assert len(roles) == len(cells)
    assert set(roles.values()) == {"asserted", "derived", "decorative"}
    assert sum(1 for role in roles.values() if role == "derived") == 14
