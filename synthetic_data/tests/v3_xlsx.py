"""Shared fixtures for the XLSX tests (Phase F).

Rendering happens once per session; ingestion goes through the real parser.  Two
documents are used:

``pilot``
    the Phase B workbook that ships with the pilot corpus (Cover, Share register,
    Transaction ledger, Financial summary, Security schedule) — the real thing.
``workbook_feature()``
    a hand-built workbook that adds the sheet types and spreadsheet features the
    pilot does not exercise: an ownership schedule, a reconciliation sheet with a
    cross-sheet reference and an interest calculation, a *valid* defined name, and
    homogeneous and mixed columns side by side in the same table.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from openpyxl import load_workbook

from synth.config import load_config
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.model import (
    Block,
    CdmWorkbook,
    DerivedValue,
    NamedRange,
    ColumnSpec,
    DocumentMetadata,
    InlineRun,
    IsoDate,
    Money,
    Percentage,
    Sheet,
    Table,
    TableCell,
    TableRow,
    Workbook,
)
from synthetic_data.renderers import DEFAULT_XLSX_PROFILE, XlsxRenderer

from synthetic_data.tests.v3_txt import pilot_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_HASHES = Path(__file__).with_name("v3_xlsx_fixtures.json")


@dataclass(frozen=True)
class XlsxRendered:
    doc_id: str
    path: Path
    result: object
    document: object

    @property
    def spans(self):
        return self.result.spans

    @property
    def sha256(self) -> str:
        return self.result.sha256

    def sheet(self, name: str, *, data_only: bool = True):
        return load_workbook(self.path, data_only=data_only)[name]


@dataclass(frozen=True)
class XlsxIngested:
    rendered: XlsxRendered
    view: DocView
    summary: dict


def render_xlsx(document: CdmWorkbook, path: Path) -> XlsxRendered:
    result = XlsxRenderer().render(document, DEFAULT_XLSX_PROFILE, path)
    return XlsxRendered(doc_id=document.document_id, path=Path(result.path),
                        result=result, document=document)


@pytest.fixture(scope="session")
def pilot_workbook():
    plan = pilot_plan()
    return plan.documents[plan.workbook_ids[0]]


@pytest.fixture(scope="session")
def xlsx_rendered(tmp_path_factory, pilot_workbook):
    return render_xlsx(pilot_workbook, tmp_path_factory.mktemp("xlsx") / "pilot.xlsx")


@pytest.fixture(scope="session")
def feature_rendered(tmp_path_factory):
    return render_xlsx(workbook_feature(), tmp_path_factory.mktemp("xlsx") / "feature.xlsx")


@pytest.fixture(scope="session")
def xlsx_store():
    instance = make_test_store()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture(scope="session")
def xlsx_config():
    return load_config()


@pytest.fixture(scope="session")
def feature_ingested(feature_rendered, xlsx_store, xlsx_config):
    records = ingest_path(xlsx_store, xlsx_config, feature_rendered.path)
    assert records, "no ingest record"
    summary = records[0]
    with xlsx_store.reader() as reader:
        view = DocView.load(reader, summary["doc_id"], xlsx_config)
    return XlsxIngested(rendered=feature_rendered, view=view, summary=summary)


@pytest.fixture(scope="session")
def xlsx_ingested(xlsx_rendered, xlsx_store, xlsx_config):
    records = ingest_path(xlsx_store, xlsx_config, xlsx_rendered.path)
    assert records, "no ingest record"
    summary = records[0]
    with xlsx_store.reader() as reader:
        view = DocView.load(reader, summary["doc_id"], xlsx_config)
    return XlsxIngested(rendered=xlsx_rendered, view=view, summary=summary)


# ---------------------------------------------------------------------------
def _cell(cell_id: str, *, text: str | None = None, value=None, display: str = "",
          number_format: str | None = None, fact_ref: str | None = None,
          formula: str | None = None, cached=None,
          derived: str | None = None) -> TableCell:
    from synthetic_data.model import CellFormula

    return TableCell(cell_id=cell_id, text=text, value=value, display=display,
                     number_format=number_format, fact_ref=fact_ref,
                     formula=CellFormula(formula=formula, cached_value=cached,
                                         derived_fact_id=derived)
                     if formula else None)


def _row(row_id: str, cells: list[TableCell]) -> TableRow:
    return TableRow(row_id=row_id, cells=cells)


def _table(table_id: str, columns: list[str], *, header: list[str],
           body: list[list[TableCell]], totals: list[list[TableCell]] | None = None,
           types: list[str | None] | None = None) -> Table:
    header_rows = [_row(f"{table_id}/r00",
                        [_cell(f"{table_id}/r00c{index:02d}", text=text)
                         for index, text in enumerate(header)])]
    body_rows = [_row(f"{table_id}/r{index + 1:02d}", cells)
                 for index, cells in enumerate(body)]
    total_rows = [_row(f"{table_id}/r{len(body) + 1:02d}", cells)
                  for cells in (totals or [])]
    cell_refs: dict[str, str] = {}
    for row_index, table_row in enumerate([*header_rows, *body_rows, *total_rows]):
        for column_index, cell in enumerate(table_row.cells):
            if cell.fact_ref:
                cell_refs[f"r{row_index:02d}c{column_index:02d}"] = cell.fact_ref
    return Table(
        table_id=table_id,
        columns=[ColumnSpec(name=name, type=(types or [None] * len(columns))[index])
                 for index, name in enumerate(columns)],
        header_rows=header_rows, body_rows=body_rows, total_rows=total_rows,
        cell_refs=cell_refs)


PRINCIPAL = Money(amount_minor=400_000_000, currency="GBP")     # £4,000,000.00
RATE_BP = 650                                                   # 6.50 %
DAYS = 90
INTEREST_MINOR = round(PRINCIPAL.amount_minor * (RATE_BP / 10_000) * DAYS / 365)


def workbook_feature() -> CdmWorkbook:
    """Five sheets: register, ledger, summary, ownership schedule, reconciliation."""
    register = _table(
        "tbl:xw/register.t01", ["Shareholder", "Class", "Shares", "Holding"],
        types=["text", "text", "int", "percent"],
        header=["Shareholder", "Class", "Shares", "Holding"],
        body=[
            [_cell("tbl:xw/register.t01/r01c00", text="Nordwind Holdings AG"),
             _cell("tbl:xw/register.t01/r01c01", text="Ordinary"),
             _cell("tbl:xw/register.t01/r01c02", value=2_500_000, display="2,500,000",
                   number_format="#,##0"),
             _cell("tbl:xw/register.t01/r01c03", value=Percentage(basis_points=4000),
                   display="40.00%", number_format="0.00%", fact_ref="fact:xw:01")],
            [_cell("tbl:xw/register.t01/r02c00", text="Copperwick Resources plc"),
             _cell("tbl:xw/register.t01/r02c01", text="Ordinary"),
             _cell("tbl:xw/register.t01/r02c02", value=1_875_000, display="1,875,000",
                   number_format="#,##0"),
             _cell("tbl:xw/register.t01/r02c03", value=Percentage(basis_points=3000),
                   display="30.00%", number_format="0.00%", fact_ref="fact:xw:02")],
            [_cell("tbl:xw/register.t01/r03c00", text="Quarryreach Partners LLC"),
             _cell("tbl:xw/register.t01/r03c01", text="Ordinary"),
             _cell("tbl:xw/register.t01/r03c02", value=1_250_000, display="1,250,000",
                   number_format="#,##0"),
             _cell("tbl:xw/register.t01/r03c03", value=Percentage(basis_points=2000),
                   display="20.00%", number_format="0.00%", fact_ref="fact:xw:03")],
            [_cell("tbl:xw/register.t01/r04c00", text="Hollowgate Capital LLC"),
             _cell("tbl:xw/register.t01/r04c01", text="Ordinary"),
             _cell("tbl:xw/register.t01/r04c02", value=625_000, display="625,000",
                   number_format="#,##0"),
             _cell("tbl:xw/register.t01/r04c03", value=Percentage(basis_points=1000),
                   display="10.00%", number_format="0.00%", fact_ref="fact:xw:04")],
        ],
        totals=[
            [_cell("tbl:xw/register.t01/r05c00", text="Total"),
             _cell("tbl:xw/register.t01/r05c01"),
             _cell("tbl:xw/register.t01/r05c02", value=6_250_000, display="6,250,000",
                   number_format="#,##0", formula="=SUM(C4:C7)",
                   cached=6_250_000, derived="fact:derived:xw:01"),
             _cell("tbl:xw/register.t01/r05c03", value=Percentage(basis_points=10000),
                   display="100.00%", number_format="0.00%",
                   formula="=SUM(D4:D7)", cached=Percentage(basis_points=10000),
                   derived="fact:derived:xw:02")],
        ])

    ledger = _table(
        "tbl:xw/ledger.t01",
        ["Date", "Reference", "Type", "Amount", "Running balance", "Status",
         "Currency"],
        types=["date", "text", "text", "money", "money", "text", "text"],
        header=["Date", "Reference", "Type", "Amount", "Running balance", "Status",
                "Currency"],
        body=[
            [_cell("tbl:xw/ledger.t01/r01c00", value=IsoDate(value=dt.date(2024, 1, 15)),
                   display="15 Jan 2024", number_format="dd mmm yyyy"),
             _cell("tbl:xw/ledger.t01/r01c01", text="XW-01"),
             _cell("tbl:xw/ledger.t01/r01c02", text="Drawdown"),
             _cell("tbl:xw/ledger.t01/r01c03",
                   value=Money(amount_minor=250_000_000, currency="GBP"),
                   display="£2,500,000.00", number_format="#,##0.00",
                   fact_ref="fact:xw:10"),
             _cell("tbl:xw/ledger.t01/r01c04",
                   value=Money(amount_minor=250_000_000, currency="GBP"),
                   display="£2,500,000.00", number_format="#,##0.00"),
             _cell("tbl:xw/ledger.t01/r01c05", text="Settled"),
             _cell("tbl:xw/ledger.t01/r01c06", text="GBP")],
            [_cell("tbl:xw/ledger.t01/r02c00", value=IsoDate(value=dt.date(2024, 2, 15)),
                   display="15 Feb 2024", number_format="dd mmm yyyy"),
             _cell("tbl:xw/ledger.t01/r02c01", text="XW-02"),
             _cell("tbl:xw/ledger.t01/r02c02", text="Drawdown"),
             _cell("tbl:xw/ledger.t01/r02c03",
                   value=Money(amount_minor=150_000_000, currency="GBP"),
                   display="£1,500,000.00", number_format="#,##0.00",
                   fact_ref="fact:xw:11"),
             _cell("tbl:xw/ledger.t01/r02c04",
                   value=Money(amount_minor=400_000_000, currency="GBP"),
                   display="£4,000,000.00", number_format="#,##0.00"),
             _cell("tbl:xw/ledger.t01/r02c05", text="Settled"),
             _cell("tbl:xw/ledger.t01/r02c06", text="GBP")],
            [_cell("tbl:xw/ledger.t01/r03c00", value=IsoDate(value=dt.date(2024, 4, 15)),
                   display="15 Apr 2024", number_format="dd mmm yyyy"),
             _cell("tbl:xw/ledger.t01/r03c01", text="XW-03"),
             _cell("tbl:xw/ledger.t01/r03c02", text="Repayment"),
             _cell("tbl:xw/ledger.t01/r03c03",
                   value=Money(amount_minor=-100_000_000, currency="GBP"),
                   display="-£1,000,000.00", number_format="#,##0.00",
                   fact_ref="fact:xw:12"),
             _cell("tbl:xw/ledger.t01/r03c04",
                   value=Money(amount_minor=300_000_000, currency="GBP"),
                   display="£3,000,000.00", number_format="#,##0.00"),
             _cell("tbl:xw/ledger.t01/r03c05", text="Settled"),
             _cell("tbl:xw/ledger.t01/r03c06", text="GBP")],
        ],
        totals=[
            [_cell("tbl:xw/ledger.t01/r04c00", text="Total repaid"),
             _cell("tbl:xw/ledger.t01/r04c01"),
             _cell("tbl:xw/ledger.t01/r04c02"),
             _cell("tbl:xw/ledger.t01/r04c03",
                   value=Money(amount_minor=100_000_000, currency="GBP"),
                   display="£1,000,000.00", number_format="#,##0.00",
                   formula="=-D6", cached=Money(amount_minor=100_000_000,
                                                       currency="GBP"),
                   derived="fact:derived:xw:03"),
             _cell("tbl:xw/ledger.t01/r04c04"),
             _cell("tbl:xw/ledger.t01/r04c05"),
             _cell("tbl:xw/ledger.t01/r04c06", text="GBP")],
        ])

    summary = _table(
        "tbl:xw/summary.t01", ["Measure", "Amount"],
        types=["text", "money"],
        header=["Measure", "Amount"],
        body=[
            [_cell("tbl:xw/summary.t01/r01c00", text="Principal drawn"),
             _cell("tbl:xw/summary.t01/r01c01", value=PRINCIPAL, display="£4,000,000.00",
                   number_format="#,##0.00",
                   formula="='Transaction ledger'!D4+'Transaction ledger'!D5",
                   cached=PRINCIPAL, derived="fact:derived:xw:04")],
            [_cell("tbl:xw/summary.t01/r02c00", text="Repaid in the period"),
             _cell("tbl:xw/summary.t01/r02c01",
                   value=Money(amount_minor=100_000_000, currency="GBP"),
                   display="£1,000,000.00", number_format="#,##0.00")],
            [_cell("tbl:xw/summary.t01/r03c00", text="Outstanding"),
             _cell("tbl:xw/summary.t01/r03c01",
                   value=Money(amount_minor=300_000_000, currency="GBP"),
                   display="£3,000,000.00", number_format="#,##0.00")],
        ])

    ownership = _table(
        "tbl:xw/ownership.t01", ["Entity", "Parent", "Effective share", "Basis"],
        types=["text", "text", "percent", "text"],
        header=["Entity", "Parent", "Effective share", "Basis"],
        body=[
            [_cell("tbl:xw/ownership.t01/r01c00", text="Dungate Industries GmbH"),
             _cell("tbl:xw/ownership.t01/r01c01", text="Nordwind Holdings AG"),
             _cell("tbl:xw/ownership.t01/r01c02", value=Percentage(basis_points=4000),
                   display="40.00%", number_format="0.00%", fact_ref="fact:xw:01"),
             _cell("tbl:xw/ownership.t01/r01c03", text="Direct")],
            [_cell("tbl:xw/ownership.t01/r02c00", text="Dungate Trading Limited"),
             _cell("tbl:xw/ownership.t01/r02c01", text="Dungate Industries GmbH"),
             _cell("tbl:xw/ownership.t01/r02c02", value=Percentage(basis_points=10000),
                   display="100.00%", number_format="0.00%"),
             _cell("tbl:xw/ownership.t01/r02c03", text="Direct")],
            [_cell("tbl:xw/ownership.t01/r03c00", text="Effective interest in Dungate Trading"),
             _cell("tbl:xw/ownership.t01/r03c01", text="Nordwind Holdings AG"),
             _cell("tbl:xw/ownership.t01/r03c02", value=Percentage(basis_points=4000),
                   display="40.00%", number_format="0.00%",
                   formula="=C4*C5", cached=Percentage(basis_points=4000),
                   derived="fact:derived:xw:05"),
             _cell("tbl:xw/ownership.t01/r03c03", text="Multiplied")],
        ])

    reconciliation = _table(
        "tbl:xw/recon.t01", ["Check", "Value", "Expected", "Difference"],
        types=["text", "money", "money", "mixed"],
        header=["Check", "Value", "Expected", "Difference"],
        body=[
            [_cell("tbl:xw/recon.t01/r01c00", text="Principal drawn"),
             _cell("tbl:xw/recon.t01/r01c01", value=PRINCIPAL, display="£4,000,000.00",
                   number_format="#,##0.00",
                   formula="='Financial summary'!B4", cached=PRINCIPAL),
             _cell("tbl:xw/recon.t01/r01c02", value=PRINCIPAL, display="£4,000,000.00",
                   number_format="#,##0.00"),
             _cell("tbl:xw/recon.t01/r01c03", value=Money(amount_minor=0, currency="GBP"),
                   display="£0.00", number_format="#,##0.00",
                   formula="=B4-C4", cached=Money(amount_minor=0, currency="GBP"),
                   derived="fact:derived:xw:06")],
            [_cell("tbl:xw/recon.t01/r02c00", text="Interest for the period"),
             _cell("tbl:xw/recon.t01/r02c01",
                   value=Money(amount_minor=INTEREST_MINOR, currency="GBP"),
                   display=f"£{INTEREST_MINOR / 100:,.2f}", number_format="#,##0.00",
                   formula="=B4*0.065*90/365",
                   cached=Money(amount_minor=INTEREST_MINOR, currency="GBP"),
                   derived="fact:derived:xw:07"),
             _cell("tbl:xw/recon.t01/r02c02",
                   value=Money(amount_minor=INTEREST_MINOR, currency="GBP"),
                   display=f"£{INTEREST_MINOR / 100:,.2f}", number_format="#,##0.00"),
             _cell("tbl:xw/recon.t01/r02c03", value=Money(amount_minor=0, currency="GBP"),
                   display="£0.00", number_format="#,##0.00",
                   formula="=B5-C5", cached=Money(amount_minor=0, currency="GBP"),
                   derived="fact:derived:xw:08")],
        ])

    sheets = [
        Sheet(sheet_id="sht:xw/01", name="Shareholder register", blocks=[], tables=[register]),
        Sheet(sheet_id="sht:xw/02", name="Transaction ledger", blocks=[], tables=[ledger]),
        Sheet(sheet_id="sht:xw/03", name="Financial summary", blocks=[], tables=[summary]),
        Sheet(sheet_id="sht:xw/04", name="Ownership schedule", blocks=[], tables=[ownership]),
        Sheet(sheet_id="sht:xw/05", name="Reconciliation", blocks=[], tables=[reconciliation]),
    ]
    for sheet, note in zip(sheets, [
            "Issued share capital and holdings",
            "Drawdowns and repayments",
            "Amounts used for the covenant test",
            "Effective ownership of the group",
            "Reconciliation of principal and interest"]):
        sheet.blocks.append(Block(block_id=f"blk:xw/{sheet.sheet_id[-2:]}.01", kind="paragraph",
                                  runs=[InlineRun(text=note)]))

    return CdmWorkbook(
        document_id="doc:93001", doc_kind="workbook",
        doc_type="workbook:ownership_schedule", structure_id="workbook/ownership",
        metadata=DocumentMetadata(title="Ownership Schedule", subtitle="Facility XW-2024",
                                  reference="WB-XW-2024",
                                  document_date=IsoDate(value=dt.date(2024, 1, 15)),
                                  jurisdiction="England and Wales",
                                  classification="Internal", version="1.0"),
        sections=[], statements=[],
        plan_fingerprint="fixture:xlsx-feature",
        workbook=Workbook(
            workbook_id="wbk:xw/wb", sheets=sheets,
            named_ranges={"Principal": NamedRange(
                name="Principal", sheet_id="sht:xw/03", ref="B4")},
            derived_values=[
                DerivedValue(
                    fact_id="fact:derived:xw:01",
                    cell_id="tbl:xw/register.t01/r05c02",
                    label="Total shares recorded in the register",
                    inputs=[f"tbl:xw/register.t01/r0{index}c02" for index in range(1, 5)]),
                DerivedValue(
                    fact_id="fact:derived:xw:02",
                    cell_id="tbl:xw/register.t01/r05c03",
                    label="Total holding recorded in the register",
                    inputs=[f"tbl:xw/register.t01/r0{index}c03" for index in range(1, 5)]),
                DerivedValue(
                    fact_id="fact:derived:xw:03",
                    cell_id="tbl:xw/ledger.t01/r04c03",
                    label="Total repaid under the facility",
                    inputs=["tbl:xw/ledger.t01/r03c03"]),
                DerivedValue(
                    fact_id="fact:derived:xw:04",
                    cell_id="tbl:xw/summary.t01/r01c01",
                    label="Principal drawn under the facility",
                    inputs=["tbl:xw/ledger.t01/r01c03", "tbl:xw/ledger.t01/r02c03"]),
                DerivedValue(
                    fact_id="fact:derived:xw:05",
                    cell_id="tbl:xw/ownership.t01/r03c02",
                    label="Effective interest in Dungate Trading",
                    inputs=["tbl:xw/ownership.t01/r01c02", "tbl:xw/ownership.t01/r02c02"]),
                DerivedValue(
                    fact_id="fact:derived:xw:06",
                    cell_id="tbl:xw/recon.t01/r01c03",
                    label="Reconciliation difference on principal",
                    inputs=["tbl:xw/recon.t01/r01c01", "tbl:xw/recon.t01/r01c02"]),
                DerivedValue(
                    fact_id="fact:derived:xw:07",
                    cell_id="tbl:xw/recon.t01/r02c01",
                    label="Interest for the period",
                    inputs=["tbl:xw/recon.t01/r01c01"]),
                DerivedValue(
                    fact_id="fact:derived:xw:08",
                    cell_id="tbl:xw/recon.t01/r02c03",
                    label="Reconciliation difference on interest",
                    inputs=["tbl:xw/recon.t01/r02c01", "tbl:xw/recon.t01/r02c02"]),
            ]))


# ---------------------------------------------------------------------------
def fixture_payload() -> dict:
    return json.loads(FIXTURE_HASHES.read_text(encoding="utf-8"))


_SUBPROCESS = """
import json, tempfile
from pathlib import Path
from synthetic_data.renderers import DEFAULT_XLSX_PROFILE, XlsxRenderer
from synthetic_data.tests.v3_txt import pilot_plan
from synthetic_data.tests.v3_xlsx import workbook_feature

plan = pilot_plan()
out = Path(tempfile.mkdtemp())
digests = {}
document = plan.documents[plan.workbook_ids[0]]
digests[document.document_id] = XlsxRenderer().render(
    document, DEFAULT_XLSX_PROFILE, out / "pilot.xlsx").sha256
feature = workbook_feature()
digests[feature.document_id] = XlsxRenderer().render(
    feature, DEFAULT_XLSX_PROFILE, out / "feature.xlsx").sha256
print(json.dumps(digests, sort_keys=True))
"""


def subprocess_digests(hashseed: str = "3") -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = hashseed
    completed = subprocess.run([sys.executable, "-c", _SUBPROCESS], capture_output=True,
                               text=True, env=environment, timeout=600)
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
