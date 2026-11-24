"""Canonical workbook planning (architecture §7.5, decision DEC-4).

A workbook is its own semantic document, not prose placed into cells.  It shares
the corpus identity space: the ledger states the *same* transaction facts the
facility agreement states, so one repayment has one id across a prose document
and a spreadsheet.

No XLSX writer is implemented here (Phase F); this module produces
:class:`CdmWorkbook` values only.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from synthetic_data.model import (
    Block,
    DerivedValue,
    NamedRange,
    CellFormula,
    CdmWorkbook,
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

from .config import CorpusConfig
from .ids import IdAllocator
from .surface import (
    date_number_format,
    fmt_date,
    fmt_money,
    fmt_percent,
    money_number_format,
    percent_number_format,
)
from .world import Matter

__all__ = ["plan_workbook", "WORKBOOK_FAMILIES"]

WORKBOOK_FAMILIES = ("shareholder_register", "transaction_ledger",
                     "financial_summary", "ownership_schedule")


@dataclass
class _SheetDraft:
    sheet_id: str
    name: str
    blocks: list[Block] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)


class WorkbookBuilder:
    def __init__(self, matter: Matter, doc_id: str, alloc: IdAllocator,
                 family: str) -> None:
        self.matter = matter
        self.doc_id = doc_id
        self.alloc = alloc
        self.family = family
        self.scope = doc_id.split(":")[1]
        self.drafts: list[_SheetDraft] = []
        self.tables: list[Table] = []
        self.statements = []
        self.row_statements = 0
        self.derived: list[DerivedValue] = []
        self._block_count: dict[str, int] = {}
        self._table_start: dict[str, int] = {}

    def sheet(self, index: int, name: str) -> _SheetDraft:
        sheet_id = self.alloc.alloc("sht", f"{self.scope}/{index:02d}")
        draft = _SheetDraft(sheet_id=sheet_id, name=name[:31])
        self.drafts.append(draft)
        return draft

    def finish(self) -> list[Sheet]:
        return [Sheet(sheet_id=d.sheet_id, name=d.name, blocks=d.blocks,
                      tables=d.tables) for d in self.drafts]

    def ref(self, table: Table, row: int, column: int) -> str:
        """``A1`` reference for a 0-based row/column of ``table`` as it is written."""
        from openpyxl.utils import get_column_letter
        return f"{get_column_letter(column + 1)}{self._table_start[table.table_id] + row}"

    def derive(self, table: Table, *, row: int, column: int, formula: str,
               label: str, inputs: list[tuple[int, int]]) -> DerivedValue:
        """Give a computed cell a stable identity and explicit inputs (DEC-12).

        The cell already carries the typed value it should compute; this makes the
        formula authoritative for that value and records the cells it reads, so a
        derived total is a fact with a derivation rather than an anonymous number.
        """
        rows = [*table.header_rows, *table.body_rows, *table.total_rows]
        cell = rows[row].cells[column]
        if cell.value is None:
            raise AssertionError("a derived value needs a typed value")
        scope = table.table_id.split(":", 1)[1]
        fact_id = self.alloc.alloc(
            "fact:derived", f"{scope}/r{row:02d}c{column:02d}")
        rows[row].cells[column] = cell.model_copy(update={
            "formula": CellFormula(formula=formula, cached_value=cell.value,
                                   derived_fact_id=fact_id)})
        value = DerivedValue(
            fact_id=fact_id, cell_id=cell.cell_id, label=label,
            inputs=[rows[r].cells[c].cell_id for r, c in inputs])
        self.derived.append(value)
        return value

    def title_block(self, sheet: _SheetDraft, text: str) -> None:
        index = self._block_count.get(sheet.sheet_id, 0) + 1
        self._block_count[sheet.sheet_id] = index
        block_id = self.alloc.alloc("blk", f"{self.scope}/{_slug(sheet.name)}.{index:02d}")
        sheet.blocks.append(Block(block_id=block_id, kind="paragraph",
                                  runs=[InlineRun(text=text)]))

    def table(self, sheet: _SheetDraft, *, name: str, columns: list[ColumnSpec],
              header: list[list[TableCell]], body: list[list[TableCell]],
              totals: list[list[TableCell]] | None = None,
              fact_cells: dict[tuple[int, int], str] | None = None) -> Table:
        from synthetic_data.model import FactStatement
        # the renderer writes one row per title block, then one blank row before
        # the sheet's tables; formulas are built from the same geometry
        start_row = 1 + len(sheet.blocks) + (1 if sheet.blocks else 0)
        index = len(sheet.tables) + 1
        base = f"{self.scope}/{_slug(sheet.name)}.t{index:02d}"
        table_id = self.alloc.alloc("tbl", base)
        width = len(columns)
        rows_spec = list(header) + list(body) + list(totals or [])
        rows: list[TableRow] = []
        cell_refs: dict[str, str] = {}
        row_facts: list[tuple[str, str, list[TableCell]]] = []
        for ri, specs in enumerate(rows_spec):
            if sum(cell.colspan for cell in specs) != width:
                raise AssertionError(f"sheet {sheet.name!r} row {ri} has the wrong width")
            row_id = self.alloc.alloc("tbl", f"{base}/r{ri:02d}")
            cells: list[TableCell] = []
            column = 0
            for cell in specs:
                cell_id = self.alloc.alloc("tbl", f"{base}/r{ri:02d}c{column:02d}")
                cells.append(TableCell(
                    cell_id=cell_id, text=cell.text, value=cell.value,
                    display=cell.display or (cell.text or ""),
                    number_format=cell.number_format, formula=cell.formula,
                    entity_ref=cell.entity_ref))
                column += cell.colspan
            # attach fact references after ids exist
            if fact_cells:
                for (row_index, column_index), fact_id in fact_cells.items():
                    if row_index != ri:
                        continue
                    target = cells[column_index]
                    cells[column_index] = target.model_copy(update={"fact_ref": fact_id})
                    cell_refs[f"r{ri:02d}c{column_index:02d}"] = fact_id
            rows.append(TableRow(row_id=row_id, cells=cells))
            if ri >= len(header):
                referenced = sorted({cell.fact_ref for cell in cells if cell.fact_ref})
                for fact_id in referenced:
                    row_facts.append((fact_id, row_id,
                                      [cell for cell in cells if cell.fact_ref == fact_id]))
        self._table_start[table_id] = start_row
        table = Table(table_id=table_id, caption=name, columns=columns,
                      header_rows=rows[:len(header)],
                      body_rows=rows[len(header):len(header) + len(body)],
                      total_rows=rows[len(header) + len(body):],
                      cell_refs=cell_refs)
        sheet.tables.append(table)
        self.tables.append(table)
        for fact_id, row_id, cells in row_facts:
            self.row_statements += 1
            statement_id = self.alloc.alloc(
                "stmt", f"{self.scope}/{_slug(sheet.name)}.s{self.row_statements:03d}")
            surface = " | ".join(cell.display or cell.text or "" for cell in cells)
            self.statements.append(FactStatement(
                statement_id=statement_id, fact_id=fact_id,
                node_id=row_id, surface=surface or name,
                mention_ids=sorted({cell.entity_ref.entity_id for cell in cells
                                    if cell.entity_ref})))
        return table


def _slug(name: str) -> str:
    return "".join(ch for ch in name.upper() if ch.isalnum())[:10] or "SHEET"


def _cell(text: str | None = None, value=None, display: str = "",
          number_format: str | None = None, entity_ref: str | None = None,
          formula: str | None = None, cached=None) -> TableCell:
    from synthetic_data.model import EntityRef
    cell_formula = None
    if formula is not None:
        cell_formula = CellFormula(formula=formula,
                                   cached_value=cached if cached is not None else value)
    return TableCell(cell_id="cell:pending",
                     text=None if (text is not None and not text.strip()
                                   and value is None) else text,
                     value=value, display=display,
                     number_format=number_format, formula=cell_formula,
                     entity_ref=EntityRef(entity_id=entity_ref) if entity_ref else None)


def plan_workbook(matter: Matter, doc_id: str, alloc: IdAllocator,
                  config: CorpusConfig, family: str) -> CdmWorkbook:
    builder = WorkbookBuilder(matter, doc_id, alloc, family)
    m = matter
    principal: Money = m.typed["principal"]          # type: ignore[assignment]
    outstanding: Money = m.typed["outstanding"]      # type: ignore[assignment]

    # ---- cover sheet -------------------------------------------------------
    cover = builder.sheet(1, "Cover")
    workbook_date = m.dates.get(doc_id) or IsoDate(value=m.typed["start"])
    builder.title_block(cover, f"{m.ctx['holdco']} — {_family_title(family)}")
    builder.title_block(cover, f"Facility {m.ctx['facility_ref']} — prepared {fmt_date(workbook_date, 0)}")
    builder.title_block(cover, f"All amounts in {principal.currency}; figures are stated at their typed value.")

    # ---- share register ----------------------------------------------------
    register = builder.sheet(2, "Share register")
    builder.title_block(register, f"Issued share capital of {m.ctx['holdco']} and its subsidiaries")
    shares = m.shareholdings
    body: list[list[TableCell]] = []
    fact_cells: dict[tuple[int, int], str] = {}
    share_count = 10_000_000
    for offset, share in enumerate(shares):
        child = m.entities[share.child_id]
        holding = Percentage(basis_points=share.percent.basis_points)
        issued = share_count // 4
        body.append([
            _cell(text=m.entities[m.typed["holdco"]].canonical_name,
                  entity_ref=m.typed["holdco"]),
            _cell(text=child.canonical_name, entity_ref=child.entity_id),
            _cell(text="Ordinary"),
            _cell(value=issued, display=f"{issued:,}", number_format="#,##0"),
            _cell(value=holding, display=holding.display(),
                  number_format=percent_number_format()),
            _cell(text=child.jurisdiction or m.ctx["jurisdiction"]),
        ])
        fact_cells[(offset + 1, 4)] = share.fact_id
    total_holding = sum(share.percent.basis_points for share in shares)
    total_issued = (share_count // 4) * len(shares)
    totals = [[
        _cell(text="Total"), _cell(text=""), _cell(text=""),
        _cell(value=total_issued, display=f"{total_issued:,}", number_format="#,##0"),
        _cell(value=Percentage(basis_points=total_holding),
              display=Percentage(basis_points=total_holding).display(),
              number_format=percent_number_format(),
              formula="=SUM(E2:E5)",
              cached=Percentage(basis_points=total_holding)),
        _cell(text=""),
    ]]
    header = [[_cell(text="Shareholder"), _cell(text="Company"), _cell(text="Class"),
               _cell(text="Shares"), _cell(text="Holding"), _cell(text="Jurisdiction")]]
    register_table = builder.table(register, name="Share register",
                  columns=[ColumnSpec(name="Shareholder", type="text"),
                           ColumnSpec(name="Company", type="text"),
                           ColumnSpec(name="Class", type="text"),
                           ColumnSpec(name="Shares", type="int", align="right"),
                           ColumnSpec(name="Holding", type="percent", align="right"),
                           ColumnSpec(name="Jurisdiction", type="text")],
                  header=header, body=body, totals=totals, fact_cells=fact_cells)
    if shares:
        builder.derive(register_table, row=len(shares) + 1, column=4,
                       formula=f"=SUM({builder.ref(register_table, 1, 4)}:"
                               f"{builder.ref(register_table, len(shares), 4)})",
                       label="Total holding recorded in the share register",
                       inputs=[(offset + 1, 4) for offset in range(len(shares))])

    # ---- transaction ledger -----------------------------------------------
    ledger = builder.sheet(3, "Transaction ledger")
    builder.title_block(ledger, "Drawdowns and repayments under the facility")
    txns = sorted(m.transactions.values(), key=lambda t: (t.date.value, t.transaction_id))
    ledger_body: list[list[TableCell]] = []
    ledger_facts: dict[tuple[int, int], str] = {}
    running = 0
    for offset, txn in enumerate(txns):
        if txn.kind == "drawdown":
            running += txn.amount.amount_minor
        else:
            running -= txn.amount.amount_minor
        ledger_body.append([
            _cell(value=txn.date, display=fmt_date(txn.date, 0),
                  number_format=date_number_format()),
            _cell(text=txn.transaction_id.split(":")[1]),
            _cell(text=m.entities[txn.to_entity.entity_id].canonical_name,
                  entity_ref=txn.to_entity.entity_id),
            _cell(text=txn.kind.replace("_", " ").capitalize()),
            _cell(value=txn.amount, display=fmt_money(txn.amount, 1),
                  number_format=money_number_format()),
            _cell(value=Money(amount_minor=running, currency=txn.amount.currency),
                  display=fmt_money(Money(amount_minor=running, currency=txn.amount.currency), 1),
                  number_format=money_number_format()),
            _cell(text=txn.status.capitalize()),
            # Currency as *text*: the parser drops a currency number format, so the
            # row has to state the code itself or the currency is never extracted (D6)
            _cell(text=txn.amount.currency),
        ])
        ledger_facts[(offset + 1, 4)] = txn.fact_id

    repayments = [offset for offset, txn in enumerate(txns) if txn.kind == "repayment"]
    ledger_total = sum(txns[offset].amount.amount_minor for offset in repayments)
    ledger_totals = [[
        _cell(text="Total repaid"), _cell(text=""), _cell(text=""), _cell(text=""),
        _cell(value=Money(amount_minor=ledger_total, currency=principal.currency),
              display=fmt_money(Money(amount_minor=ledger_total, currency=principal.currency), 1),
              number_format=money_number_format()),
        _cell(text=""), _cell(text=""), _cell(text=principal.currency),
    ]]
    ledger_table = builder.table(
        ledger, name="Transaction ledger",
        columns=[ColumnSpec(name="Date", type="date"),
                 ColumnSpec(name="Reference", type="text"),
                 ColumnSpec(name="Counterparty", type="text"),
                 ColumnSpec(name="Type", type="text"),
                 ColumnSpec(name="Amount", type="money", align="right"),
                 ColumnSpec(name="Running balance", type="money", align="right"),
                 ColumnSpec(name="Status", type="text"),
                 ColumnSpec(name="Currency", type="text")],
        header=[[_cell(text="Date"), _cell(text="Reference"), _cell(text="Counterparty"),
                 _cell(text="Type"), _cell(text="Amount"),
                 _cell(text="Running balance"), _cell(text="Status"),
                 _cell(text="Currency")]],
        body=ledger_body, totals=ledger_totals, fact_cells=ledger_facts)

    # A running balance is derived, not copied: each row is the row before it plus
    # this row's amount, and the derivation is recorded so the value can be rechecked
    for offset, txn in enumerate(txns):
        row = offset + 1
        label = f"Running balance after {txn.transaction_id.split(':')[1]}"
        if offset == 0:
            builder.derive(ledger_table, row=row, column=5,
                           formula=f"={builder.ref(ledger_table, row, 4)}",
                           label=label, inputs=[(row, 4)])
        else:
            sign = "+" if txn.kind == "drawdown" else "-"
            builder.derive(
                ledger_table, row=row, column=5,
                formula=(f"={builder.ref(ledger_table, row - 1, 5)}{sign}"
                         f"{builder.ref(ledger_table, row, 4)}"),
                label=label, inputs=[(row - 1, 5), (row, 4)])
    if repayments:
        builder.derive(ledger_table, row=len(txns) + 1, column=4,
                       formula=f"=SUM({builder.ref(ledger_table, repayments[0] + 1, 4)}:"
                               f"{builder.ref(ledger_table, repayments[-1] + 1, 4)})",
                       label="Total repaid under the facility",
                       inputs=[(offset + 1, 4) for offset in repayments])

    # ---- financial summary -------------------------------------------------
    summary = builder.sheet(4, "Financial summary")
    builder.title_block(summary, "Financial information relied on for the covenant calculation")
    financial = [m.facts[fid] for fid in m.typed["financial_facts"]]
    summary_body: list[list[TableCell]] = []
    summary_facts: dict[tuple[int, int], str] = {}
    for offset, fact in enumerate(financial):
        year = fact.effective.start.value.year if fact.effective and fact.effective.start else 0
        label = ("Revenue" if "revenue" in (fact.text or "") else
                 "EBITDA" if "EBITDA" in (fact.text or "") else "Net debt")
        summary_body.append([
            _cell(value=year, display=str(year), number_format="0"),
            _cell(text=label),
            _cell(value=fact.amount, display=fmt_money(fact.amount, 1),
                  number_format=money_number_format()),
            _cell(text=fact.amount.currency),
        ])
        summary_facts[(offset + 1, 2)] = fact.fact_id
    summary_totals = [[
        _cell(text="Net debt at year end"), _cell(text=""),
        _cell(value=outstanding, display=fmt_money(outstanding, 1),
              number_format=money_number_format()),
        _cell(text=outstanding.currency),
    ]]
    builder.table(
        summary, name="Financial summary",
        columns=[ColumnSpec(name="Year", type="int", align="right"),
                 ColumnSpec(name="Measure", type="text"),
                 ColumnSpec(name="Amount", type="money", align="right"),
                 ColumnSpec(name="Currency", type="text")],
        header=[[_cell(text="Year"), _cell(text="Measure"), _cell(text="Amount"),
                 _cell(text="Currency")]],
        body=summary_body, totals=summary_totals, fact_cells=summary_facts)

    # ---- security schedule -------------------------------------------------
    security = builder.sheet(5, "Security schedule")
    builder.title_block(security, "Transaction Security and guarantees")
    security_facts = [m.facts[fid] for fid in m.facts
                      if m.fact_family[fid] == "security"]
    security_body: list[list[TableCell]] = []
    security_cells: dict[tuple[int, int], str] = {}
    for offset, fact in enumerate(security_facts):
        asset = m.entities[fact.object.entity_id]
        security_body.append([
            _cell(text=asset.canonical_name, entity_ref=asset.entity_id),
            _cell(text="Fixed charge"),
            _cell(text="First ranking"),
            _cell(value=Percentage(basis_points=0), display="n/a",
                  number_format=percent_number_format()),
        ])
        security_cells[(offset + 1, 0)] = fact.fact_id
    builder.table(
        security, name="Security schedule",
        columns=[ColumnSpec(name="Asset", type="text"),
                 ColumnSpec(name="Charge", type="text"),
                 ColumnSpec(name="Ranking", type="text"),
                 ColumnSpec(name="Cover ratio", type="mixed", align="right")],
        header=[[_cell(text="Asset"), _cell(text="Charge"), _cell(text="Ranking"),
                 _cell(text="Cover ratio")]],
        body=security_body, fact_cells=security_cells)

    worksheets = builder.finish()
    workbook = Workbook(
        workbook_id=builder.alloc.alloc("wbk", f"{builder.scope}/wb"),
        sheets=worksheets,
        named_ranges={
            "Ledger": NamedRange(
                name="Ledger", sheet_id=ledger.sheet_id,
                ref=f"{builder.ref(ledger_table, 1, 4)}:"
                    f"{builder.ref(ledger_table, len(txns), 4)}"),
            "ShareRegister": NamedRange(
                name="ShareRegister", sheet_id=register.sheet_id,
                ref=f"{builder.ref(register_table, 1, 4)}:"
                    f"{builder.ref(register_table, len(shares), 4)}"),
        },
        derived_values=builder.derived)

    return CdmWorkbook(
        document_id=doc_id, doc_kind="workbook",
        doc_type=f"workbook:{family}", structure_id=f"workbook/{family}",
        metadata=DocumentMetadata(
            title=_family_title(family), subtitle=m.ctx["facility_ref"],
            document_date=workbook_date,
            jurisdiction=m.ctx["jurisdiction"], reference=f"WB-{builder.scope}",
            classification="Internal", formality="neutral"),
        sections=[], statements=builder.statements,
        plan_fingerprint=f"{family}|{m.ctx['facility_ref']}|{len(builder.tables)}",
        workbook=workbook)


def _family_title(family: str) -> str:
    return {
        "shareholder_register": "Shareholder Register",
        "transaction_ledger": "Transaction Ledger",
        "financial_summary": "Financial Summary",
        "ownership_schedule": "Ownership Schedule",
    }.get(family, "Workbook")
