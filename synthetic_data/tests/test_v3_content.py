"""Phase A — Layer 2 content model and the specialised workbook."""
from __future__ import annotations

import copy
import datetime as dt

import pytest
from pydantic import ValidationError

from synthetic_data.model import (
    Block,
    CdmDocument,
    CdmWorkbook,
    CellFormula,
    ColumnSpec,
    DocumentMetadata,
    InlineRun,
    IsoDate,
    Money,
    NamedRange,
    Section,
    Sheet,
    Table,
    TableCell,
    TableRow,
    Workbook,
)
from synthetic_data.tests.v3_builders import build_fixture

FIX = build_fixture()
DOC1 = FIX.corpus.documents[FIX.id("doc:00001")]


def revalidate(doc: CdmDocument, mutate) -> dict:
    data = copy.deepcopy(doc.model_dump())
    mutate(data)
    return data


def test_valid_document_hierarchy():
    doc = DOC1
    assert [s.heading for s in doc.sections] == ["Purpose", "Resolutions"]
    assert doc.node_ids()
    assert len(doc.statements) == 4


def test_section_depth_must_follow_parent():
    data = revalidate(DOC1, lambda d: d["sections"].__setitem__(1, {
        **d["sections"][1], "parent_id": d["sections"][0]["section_id"], "depth": 3}))
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(data)


def test_section_cycle_is_rejected():
    def mutate(d):
        a, b = d["sections"][0], d["sections"][1]
        b = {**b, "parent_id": a["section_id"], "depth": 1}
        a = {**a, "parent_id": b["section_id"], "depth": 1}
        d["sections"] = [a, b]
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(revalidate(DOC1, mutate))


def test_paragraph_requires_runs():
    def mutate(d):
        d["sections"][0]["blocks"][0]["runs"] = []
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(revalidate(DOC1, mutate))


def test_table_block_requires_table():
    def mutate(d):
        d["sections"][1]["blocks"][0]["table"] = None
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(revalidate(DOC1, mutate))


def test_page_break_must_not_carry_payload():
    with pytest.raises(ValidationError):
        Block(block_id="blk:00001:99.01:aaaa", kind="page_break",
              runs=[InlineRun(text="not allowed")])


def test_list_block_requires_items_and_ordered():
    with pytest.raises(ValidationError):
        Block(block_id="blk:00001:99.02:aaaa", kind="list")
    with pytest.raises(ValidationError):
        Block(block_id="blk:00001:99.02:aaaa", kind="list",
              items=[{"item_id": "li:00001:01:aaaa", "runs": [{"text": "x"}]}])


def test_table_dimensions_must_match_columns():
    table = DOC1.sections[1].blocks[0].table
    data = table.model_dump()
    data["body_rows"][0]["cells"] = data["body_rows"][0]["cells"][:1]   # drop a cell
    with pytest.raises(ValidationError):
        Table.model_validate(data)


def test_table_cell_spans_are_counted():
    table = DOC1.sections[1].blocks[0].table.model_dump()
    table["body_rows"][0]["cells"][0]["colspan"] = 2                  # 2+1 = 3 != 2
    with pytest.raises(ValidationError):
        Table.model_validate(table)


def test_duplicate_cell_ids_are_rejected():
    table = DOC1.sections[1].blocks[0].table.model_dump()
    first = table["body_rows"][0]["cells"][0]["cell_id"]
    table["body_rows"][1]["cells"][0]["cell_id"] = first
    with pytest.raises(ValidationError):
        Table.model_validate(table)


def test_cell_refs_must_address_a_cell_and_agree():
    table = DOC1.sections[1].blocks[0].table.model_dump()
    table["cell_refs"] = {"r09c09": table["cell_refs"]["r01c01"]}
    with pytest.raises(ValidationError):
        Table.model_validate(table)


def test_typed_cell_requires_a_display_string():
    with pytest.raises(ValidationError):
        TableCell(cell_id="tbl:00001:02.t01/r01c01:aaaa",
                  value=Money(amount_minor=1, currency="GBP"))


def test_blank_cell_is_allowed_but_blank_text_is_not():
    assert TableCell(cell_id="tbl:00001:02.t01/r01c01:aaaa").display == ""
    with pytest.raises(ValidationError):
        TableCell(cell_id="tbl:00001:02.t01/r01c01:aaaa", text="   ")


def test_footnote_references_must_resolve():
    def mutate(d):
        d["sections"][0]["blocks"][0]["runs"][0]["footnote_id"] = "ftn:00009:01:zzzz"
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(revalidate(DOC1, mutate))


def test_statement_must_address_an_existing_node():
    def mutate(d):
        d["statements"][0]["node_id"] = "blk:00009:99.99:zzzz"
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(revalidate(DOC1, mutate))


def test_duplicate_node_ids_are_rejected():
    def mutate(d):
        d["sections"][0]["blocks"][1]["block_id"] = d["sections"][0]["blocks"][0]["block_id"]
    with pytest.raises(ValidationError):
        CdmDocument.model_validate(revalidate(DOC1, mutate))


# --------------------------------------------------------------------------
# workbook
# --------------------------------------------------------------------------
def _sheet(table: Table) -> Sheet:
    return Sheet(sheet_id="sht:00077:01:aaaa", name="Transactions", tables=[table])


def _workbook_table() -> Table:
    amount = Money(amount_minor=400_000_000, currency="GBP")
    return Table(
        table_id="tbl:00077:01:bbbb",
        columns=[ColumnSpec(name="Party", type="text"),
                 ColumnSpec(name="Amount", type="money")],
        header_rows=[TableRow(row_id="tbl:00077:01/r00:aaaa",
                              cells=[TableCell(cell_id="tbl:00077:01/r00c00:aaaa", text="Party"),
                                     TableCell(cell_id="tbl:00077:01/r00c01:aaaa", text="Amount")])],
        body_rows=[TableRow(row_id="tbl:00077:01/r01:aaaa",
                            cells=[TableCell(cell_id="tbl:00077:01/r01c00:aaaa", text="Oakhaven"),
                                   TableCell(cell_id="tbl:00077:01/r01c01:aaaa", value=amount,
                                             display="GBP 4,000,000.00",
                                             formula=CellFormula(
                                                 formula="=SUM(B2:B2)",
                                                 cached_value=amount))])],
    )


def _workbook_doc(table: Table, sheets=None, **kw) -> CdmWorkbook:
    sheets = sheets or [_sheet(table)]
    return CdmWorkbook(
        document_id="doc:00077:aaaa", doc_type="schedule", structure_id="workbook/v1",
        workbook=Workbook(workbook_id="wb:00077:aaaa", sheets=sheets,
                          named_ranges={"Drawdowns": NamedRange(
                              name="Drawdowns", sheet_id="sht:00077:01:aaaa",
                              ref="A2:B2")}),
        plan_fingerprint="fp-workbook-001", **kw)


def test_valid_workbook_document():
    doc = _workbook_doc(_workbook_table())
    assert doc.doc_kind == "workbook"
    assert doc.sections == []
    assert any(node_id.startswith("tbl:") for node_id in doc.node_ids())


def test_workbook_requires_a_sheet():
    with pytest.raises(ValidationError):
        Workbook(workbook_id="wb:00077:aaaa", sheets=[])


def test_sheet_must_have_content_and_unique_names():
    with pytest.raises(ValidationError):
        Sheet(sheet_id="sht:00077:01:aaaa", name="Empty")
    table = _workbook_table()
    with pytest.raises(ValidationError):
        Workbook(workbook_id="wb:00077:aaaa",
                 sheets=[_sheet(table), Sheet(sheet_id="sht:00077:02:bbbb",
                                              name="transactions", tables=[table])])


def test_duplicate_table_ids_across_sheets_are_rejected():
    table = _workbook_table()
    sheets = [_sheet(table), Sheet(sheet_id="sht:00077:02:bbbb", name="Notes",
                                   tables=[table.model_copy()])]
    with pytest.raises(ValidationError):
        Workbook(workbook_id="wb:00077:aaaa", sheets=sheets)


def test_formula_requires_cached_value_and_equals_sign():
    with pytest.raises(ValidationError):
        CellFormula(formula="SUM(A1:A2)", cached_value=1)
    with pytest.raises(ValidationError):
        CellFormula(formula="=", cached_value=1)


def test_workbook_document_kind_is_fixed():
    table = _workbook_table()
    with pytest.raises(ValidationError):
        CdmWorkbook(document_id="doc:00077:aaaa", doc_kind="prose", doc_type="schedule",
                    structure_id="workbook/v1", plan_fingerprint="fp",
                    workbook=Workbook(workbook_id="wb:00077:aaaa", sheets=[_sheet(table)]))


def test_workbook_can_carry_a_title_section():
    table = _workbook_table()
    title = Section(section_id="sec:00009:01:aaaa", depth=0, heading="Cover",
                    blocks=[Block(block_id="blk:00009:01.01:aaaa", kind="paragraph",
                                  runs=[InlineRun(text="Facility schedule")])])
    doc = _workbook_doc(table, sections=[title])
    assert doc.sections[0].heading == "Cover"


def test_prose_document_requires_sections():
    with pytest.raises(ValidationError):
        CdmDocument(document_id="doc:00009:aaaa", doc_type="memorandum",
                    structure_id="memorandum/v1", plan_fingerprint="fp", sections=[])
