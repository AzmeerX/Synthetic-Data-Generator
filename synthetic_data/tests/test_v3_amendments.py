"""Phase A amendments — DEC-9 (sentence refs), DEC-10 (nested tables),
DEC-11 (mixed-type columns and typed XLSX cells)."""
from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from synthetic_data.model import (
    CellFormula,
    ColumnSpec,
    FactStatement,
    IsoDate,
    Money,
    Percentage,
    Table,
    TableCell,
    TableRow,
)

SENT_A = "snt:00001:01.01.s01:aaaa"
SENT_B = "snt:00001:01.01.s02:bbbb"


def statement(**kw) -> FactStatement:
    data = dict(
        statement_id="stmt:00001/01.a:aaaa",
        fact_id="fact:edge:00001:01:bbbb",
        node_id="blk:00001/01.01:cccc",
        surface="Oakhaven Holdings Limited owns Juniper Data Centre.",
    )
    data.update(kw)
    return FactStatement(**data)


def table(columns, rows, **kw) -> Table:
    return Table(
        table_id="tbl:00001/02.t01:aaaa",
        columns=columns,
        body_rows=rows,
        **kw,
    )


def row(index: int, cells) -> TableRow:
    return TableRow(row_id=f"tbl:00001/02.t01/r{index:02d}:aaaa", cells=cells)


def cell(col: int, *, row: int = 1, **kw) -> TableCell:
    return TableCell(cell_id=f"tbl:00001/02.t01/r{row:02d}c{col:02d}:aaaa", **kw)


# --------------------------------------------------------------------------
# DEC-9 — sentence-level references are optional and canonical-only
# --------------------------------------------------------------------------
def test_statement_without_sentence_refs_is_valid():
    s = statement()
    assert s.canonical_sentence_ids == []
    assert s.node_id


def test_statement_with_canonical_sentence_refs_is_valid():
    s = statement(canonical_sentence_ids=[SENT_A, SENT_B])
    assert s.canonical_sentence_ids == [SENT_A, SENT_B]
    assert s.node_id, "the structural anchor is retained alongside sentence refs"


def test_parser_sentence_ids_are_not_canonical():
    with pytest.raises(ValidationError):
        statement(canonical_sentence_ids=["s19"])
    with pytest.raises(ValidationError):
        statement(canonical_sentence_ids=["sen_01HXYZ"])


def test_duplicate_sentence_refs_are_rejected():
    with pytest.raises(ValidationError):
        statement(canonical_sentence_ids=[SENT_A, SENT_A])


def test_table_and_cell_statements_need_no_sentence_refs():
    s = statement(node_id="tbl:00001/02.t01/r01c01:aaaa")
    assert s.canonical_sentence_ids == []


# --------------------------------------------------------------------------
# DEC-10 — nested tables are rejected, separate tables are the alternative
# --------------------------------------------------------------------------
def test_nested_table_payload_in_a_cell_is_rejected():
    with pytest.raises(ValidationError, match="nested tables"):
        TableCell(cell_id="tbl:00001/02.t01/r01c00:aaaa", table={"table_id": "x"})
    with pytest.raises(ValidationError, match="nested tables"):
        TableCell(cell_id="tbl:00001/02.t01/r01c00:aaaa", nested_table="x")
    with pytest.raises(ValidationError, match="nested tables"):
        TableCell(cell_id="tbl:00001/02.t01/r01c00:aaaa", rows=[])


def test_separate_tables_are_the_supported_alternative():
    t1 = table([ColumnSpec(name="Party", type="text")],
               [row(1, [cell(0, text="Oakhaven")])])
    t2 = Table(
        table_id="tbl:00001/02.t02:bbbb",
        columns=[ColumnSpec(name="Amount", type="money")],
        body_rows=[TableRow(
            row_id="tbl:00001/02.t02/r01:cccc",
            cells=[TableCell(cell_id="tbl:00001/02.t02/r01c00:dddd",
                             value=Money(amount_minor=100, currency="GBP"),
                             display="GBP 1.00")])],
    )
    assert t1.table_id != t2.table_id
    assert t1.body_rows[0].cells[0].cell_id != t2.body_rows[0].cells[0].cell_id


# --------------------------------------------------------------------------
# DEC-11 — column types are expectations, cell types are authoritative
# --------------------------------------------------------------------------
def test_column_type_is_optional():
    spec = ColumnSpec(name="Notes")
    assert spec.type is None
    t = table([spec], [row(1, [cell(0, text="free text")])])
    assert t.columns[0].type is None


def test_mixed_column_declaration_accepts_mixed_values():
    t = table(
        [ColumnSpec(name="Value", type="mixed")],
        [row(1, [cell(0, row=1, value=Money(amount_minor=100, currency="GBP"),
                       display="GBP 1.00")]),
         row(2, [cell(0, row=2, value=Percentage(basis_points=350), display="3.50%")]),
         row(3, [cell(0, row=3, value=IsoDate(value=dt.date(2024, 3, 3)),
                       display="3 March 2024")]),
         row(4, [cell(0, row=4, value=7, display="7")])],
    )
    values = [r.cells[0].value for r in t.body_rows]
    assert isinstance(values[0], Money)
    assert isinstance(values[1], Percentage)
    assert isinstance(values[2], IsoDate)
    assert isinstance(values[3], int)


def test_homogeneous_column_rejects_a_typed_mismatch_without_coercion():
    with pytest.raises(ValidationError, match="no coercion"):
        table([ColumnSpec(name="Amount", type="money")],
              [row(1, [cell(0, value=7, display="7")])])


def test_mixed_column_is_the_explicit_escape_hatch():
    t = table([ColumnSpec(name="Amount", type="mixed")],
              [row(1, [cell(0, value=7, display="7")])])
    assert isinstance(t.body_rows[0].cells[0].value, int)


def test_text_placeholder_does_not_violate_a_typed_column():
    t = table([ColumnSpec(name="Amount", type="money")],
              [row(1, [cell(0, text="—")])])
    assert t.body_rows[0].cells[0].value is None


def test_display_value_format_and_formula_stay_distinguishable():
    amount = Money(amount_minor=400_000_000, currency="GBP")
    c = cell(0, value=amount, display="£4,000,000.00", number_format="#,##0.00",
             formula=CellFormula(formula="=SUM(B2:B2)", cached_value=amount))
    assert c.value == amount
    assert c.display == "£4,000,000.00"
    assert c.number_format == "#,##0.00"
    assert c.formula is not None and c.formula.cached_value == amount
    assert c.display != str(c.value.amount_minor)


def test_formula_cached_value_must_match_the_cell_value():
    amount = Money(amount_minor=100, currency="GBP")
    with pytest.raises(ValidationError, match="cached_value"):
        cell(0, value=amount, display="GBP 1.00",
             formula=CellFormula(formula="=1+1",
                                 cached_value=Money(amount_minor=200, currency="GBP")))


def test_round_trip_preserves_types_and_formats():
    t = table([ColumnSpec(name="Value", type="mixed")],
              [row(1, [cell(0, value=Percentage(basis_points=350), display="3.50%",
                            number_format="0.00%")])])
    again = Table.model_validate(t.model_dump())
    c = again.body_rows[0].cells[0]
    assert isinstance(c.value, Percentage) and c.value.basis_points == 350
    assert c.display == "3.50%" and c.number_format == "0.00%"
