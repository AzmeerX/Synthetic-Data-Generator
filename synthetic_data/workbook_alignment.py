"""Canonical-to-parser alignment for workbooks (architecture §6.4, §8.2, D6/D7).

A spreadsheet is the format where the parser gives the least away: Docling returns
one ``table_row`` string per sheet row, with **no sheet name, no coordinates and no
cell identity**.  Everything that locates a value has to come from the renderer:

* the :class:`~synthetic_data.model.RenderMap` is authoritative for the sheet, row,
  column and ``A1`` reference of every cell;
* the parser supplies the sentence id and the exact text, which is what a gold
  quote must be;
* a row is only mapped to cells when the row's own values line up one-to-one with
  the cells the renderer wrote there.  When they do not, the row is recorded as
  missing and no cell is grounded.

The quote for a cell is sliced out of the parser text by offset, so it is still an
exact parser substring even though the cell identity came from the render map.
Nothing here infers a coordinate from a parser sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from synthetic_data.alignment import FactEvidence, normalise
from synthetic_data.model import (
    CdmWorkbook,
    IsoDate,
    Money,
    Percentage,
    RenderMap,
    TableCell,
    TableRow,
)
from synthetic_data.surfaces import date_surface_key, percent_surface_key

__all__ = [
    "CellAlignment",
    "RowAlignment",
    "WorkbookAlignment",
    "align_workbook",
    "workbook_fact_evidence",
    "workbook_derived_evidence",
    "numeric_surface_in",
    "currency_in_quote",
    "value_kind",
]

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_PERCENT = re.compile(r"-?\d+(?:\.\d+)?\s?%")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE = re.compile(r"\d{1,2} [A-Z][a-z]{2} \d{4}")
_SPLIT = "|"


@dataclass(frozen=True)
class CellAlignment:
    """One canonical cell, its parser quote and the render location it came from."""

    cell_id: str
    sheet_id: str
    sheet_name: str | None
    row_id: str | None
    column: int | None
    cell_ref: str | None
    sentence_id: str | None
    quote: str
    char_start: int | None
    char_end: int | None
    row_quote: str
    exact_cell_quote: bool
    kind: str


@dataclass(frozen=True)
class RowAlignment:
    row_id: str
    sheet_id: str
    sheet_name: str | None
    row_number: int | None
    sentence_id: str | None
    quote: str
    char_start: int | None
    char_end: int | None
    kind: str
    #: ``(start, end)`` of each cell token in the parser text, by column index
    tokens: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class WorkbookAlignment:
    document_id: str
    rows: tuple[RowAlignment, ...] = ()
    cells: tuple[CellAlignment, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row.kind] = out.get(row.kind, 0) + 1
        return out

    def row(self, row_id: str) -> RowAlignment | None:
        return next((row for row in self.rows if row.row_id == row_id), None)

    def cell(self, cell_id: str) -> CellAlignment | None:
        return next((cell for cell in self.cells if cell.cell_id == cell_id), None)


# ---------------------------------------------------------------------------
def _typed_key(value) -> str:
    if isinstance(value, Money):
        return f"n:{value.amount_minor / 100:g}"
    if isinstance(value, Percentage):
        return f"p:{value.basis_points}"
    if isinstance(value, IsoDate):
        return f"d:{value.value.isoformat()}"
    if isinstance(value, bool):
        return f"b:{int(value)}"
    if isinstance(value, (int, float)):
        return f"n:{float(value):g}"
    return ""


def _cell_key(cell: TableCell) -> str:
    key = _typed_key(cell.value)
    if key:
        return key
    text = (cell.text or cell.display or "").strip()
    return f"t:{normalise(text)}"


def _token_key(token: str) -> str:
    text = normalise(token)
    if not text:
        return "t:"
    percent = _PERCENT.fullmatch(text)
    if percent:
        return f"p:{round(float(percent.group(0).rstrip('%').strip()) * 100)}"
    date = date_surface_key(text)
    if date:
        return f"d:{date}"
    number = _NUMBER.fullmatch(text.replace(",", ""))
    if number:
        return f"n:{float(number.group(0)):g}"
    return f"t:{text}"


def _row_key(cells: list[TableCell]) -> tuple[str, ...]:
    return tuple(_cell_key(cell) for cell in cells)


def _tokens(text: str, base: int) -> tuple[tuple[str, int, int], ...]:
    """Split one parser row on ``|`` and keep each token's absolute span."""
    out: list[tuple[str, int, int]] = []
    position = 0
    for part in text.split(_SPLIT):
        leading = len(part) - len(part.lstrip())
        trailing = len(part) - len(part.rstrip())
        start = base + position + leading
        end = base + position + len(part) - trailing
        out.append((part.strip(), start, max(start, end)))
        position += len(part) + len(_SPLIT)
    return tuple(out)


def _row_signature(tokens: tuple[tuple[str, int, int], ...]) -> tuple[str, ...]:
    keys = [_token_key(token[0]) for token in tokens]
    while keys and keys[-1] in {"t:"}:
        keys.pop()
    return tuple(keys)


def _sheet_tables(sheet):
    for block in sheet.blocks:
        if block.table is not None:
            yield block.table
    yield from sheet.tables


def align_workbook(document: CdmWorkbook, render_map: RenderMap | object, view
                   ) -> WorkbookAlignment:
    """Align every workbook row and cell to the parser's own sentences."""
    workbook = document.workbook
    spans = list(getattr(render_map, "spans", render_map))
    span_by_id = {span.canonical_id: span for span in spans}
    rows_by_location: dict[tuple[str, int], str] = {}
    for span in spans:
        if span.kind == "row" and span.loc.sheet_name and span.loc.row:
            rows_by_location[(span.loc.sheet_name, span.loc.row)] = span.canonical_id

    parser_rows = []
    for row in view.sentences:
        if row.get("kind") != "table_row":
            continue
        start, end = row["char_start"], row["char_end"]
        # the raw slice, not the display text: every offset below is absolute in
        # ``view.text`` so a quote stays reproducible by slicing the store
        parser_rows.append((row["sentence_id"], view.text[start:end], start, end))

    problems: list[str] = []
    alignments: list[RowAlignment] = []
    cells: list[CellAlignment] = []
    cursor = 0

    for sheet in workbook.sheets:
        for table in _sheet_tables(sheet):
            for table_row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                span = span_by_id.get(table_row.row_id)
                location = (span.loc.sheet_name, span.loc.row) if span else (None, None)
                expected = _row_key(table_row.cells)
                match = None
                for index in range(cursor, len(parser_rows)):
                    sentence_id, text, start, end = parser_rows[index]
                    tokens = _tokens(text, start)
                    if _row_signature(tokens) == _stripped(expected):
                        match = (index, sentence_id, text, start, end, tokens)
                        break
                if match is None:
                    problems.append(
                        f"row {table_row.row_id} ({sheet.name}) was not found in the "
                        f"parser output")
                    alignments.append(RowAlignment(
                        row_id=table_row.row_id, sheet_id=sheet.sheet_id,
                        sheet_name=location[0], row_number=location[1],
                        sentence_id=None, quote="", char_start=None, char_end=None,
                        kind="missing"))
                    continue
                index, sentence_id, text, start, end, tokens = match
                cursor = index + 1
                kind = "exact" if _row_signature(tokens) == expected else "normalised"
                alignments.append(RowAlignment(
                    row_id=table_row.row_id, sheet_id=sheet.sheet_id,
                    sheet_name=location[0], row_number=location[1],
                    sentence_id=sentence_id, quote=text, char_start=start,
                    char_end=end, kind=kind,
                    tokens=tuple((token[1], token[2]) for token in tokens)))

                exact_cells = len(tokens) == len(table_row.cells)
                if not exact_cells:
                    problems.append(
                        f"row {table_row.row_id} has {len(table_row.cells)} cells but "
                        f"the parser returned {len(tokens)} tokens; cells are not "
                        f"mapped individually")
                for column, cell in enumerate(table_row.cells):
                    cell_span = span_by_id.get(cell.cell_id)
                    cell_ref = cell_span.loc.cell_ref if cell_span is not None else None
                    if exact_cells:
                        token_start, token_end = tokens[column][1], tokens[column][2]
                        quote = view.text[token_start:token_end]
                    else:
                        token_start = token_end = None
                        quote = text
                    cells.append(CellAlignment(
                        cell_id=cell.cell_id, sheet_id=sheet.sheet_id,
                        sheet_name=location[0], row_id=table_row.row_id,
                        column=column, cell_ref=cell_ref,
                        sentence_id=sentence_id, quote=quote,
                        char_start=token_start, char_end=token_end,
                        row_quote=text, exact_cell_quote=exact_cells, kind=kind))

    return WorkbookAlignment(document_id=document.document_id,
                             rows=tuple(alignments), cells=tuple(cells),
                             problems=tuple(problems))


def _stripped(keys: tuple[str, ...]) -> tuple[str, ...]:
    out = list(keys)
    while out and out[-1] == "t:":
        out.pop()
    return tuple(out)


# ---------------------------------------------------------------------------
def value_kind(cell: TableCell) -> str:
    value = cell.value
    if isinstance(value, Money):
        return "money"
    if isinstance(value, Percentage):
        return "percent"
    if isinstance(value, IsoDate):
        return "date"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def numeric_surface_in(cell: TableCell, quote: str) -> bool:
    """Does ``quote`` state this cell's typed value, formatting aside?

    A spreadsheet parser loses currency symbols and thousands separators and shows
    a date as ``23 Jan 2023``; the value it *does* extract is the number.  Money is
    therefore compared by amount, never by surface string.
    """
    folded = normalise(quote)
    value = cell.value
    if isinstance(value, Money):
        amount = value.amount_minor / 100
        return any(abs(float(number.group(0).replace(",", "")) - amount) < 1e-6
                   for number in _NUMBER.finditer(folded))
    if isinstance(value, Percentage):
        for candidate in _PERCENT.finditer(folded):
            key = percent_surface_key(candidate.group(0))
            if key == f"{value.basis_points}bp":
                return True
        return False
    if isinstance(value, IsoDate):
        for candidate in _DATE.finditer(folded):
            if date_surface_key(candidate.group(0)) == value.value.isoformat():
                return True
        return _ISO.fullmatch(folded) is not None and folded == value.value.isoformat()
    if isinstance(value, (int, float)):
        return any(abs(float(number.group(0).replace(",", "")) - float(value)) < 1e-6
                   for number in _NUMBER.finditer(folded))
    text = (cell.text or cell.display or "").strip()
    return normalise(text) in folded


def currency_in_quote(currency: str, quote: str) -> bool:
    """Is the currency *stated in the parser text*?  A number format never is."""
    folded = normalise(quote).upper()
    return re.search(rf"\b{re.escape(currency.upper())}\b", folded) is not None


def workbook_fact_evidence(document: CdmWorkbook, alignment: WorkbookAlignment
                           ) -> list[FactEvidence]:
    """One evidence item per asserted fact, quoted from its own row and cells."""
    by_id = {cell.cell_id: cell for cell in alignment.cells}
    spans = {cell.cell_id: cell for cell in alignment.cells}
    evidence: list[FactEvidence] = []
    for statement in document.statements:
        row = alignment.row(statement.node_id)
        if row is None or row.sentence_id is None:
            evidence.append(FactEvidence(
                fact_id=statement.fact_id, statement_id=statement.statement_id,
                sentence_ids=(), quote="", kinds=(), acceptable=False,
                problems=("statement is not anchored to an aligned row",),
                node_id=statement.node_id))
            continue
        table = _table_of(document, statement.node_id)
        targets = [cell for cell in table.cells if cell.fact_ref == statement.fact_id] \
            if table is not None else []
        problems: list[str] = []
        if not targets:
            problems.append("no cell in the statement's row carries its fact")
        pieces = []
        for cell in targets:
            found = by_id.get(cell.cell_id)
            if found is None or found.quote == "":
                problems.append(f"cell {cell.cell_id} is not aligned")
                continue
            pieces.append(found)
            if not numeric_surface_in(cell, found.quote):
                problems.append(f"cell {cell.cell_id} value is not in its quote "
                                f"{found.quote!r}")
        if not pieces:
            evidence.append(FactEvidence(
                fact_id=statement.fact_id, statement_id=statement.statement_id,
                sentence_ids=(row.sentence_id,), quote=row.quote, kinds=(row.kind,),
                acceptable=False, problems=tuple(problems),
                node_id=statement.node_id))
            continue
        start = min(piece.char_start for piece in pieces if piece.char_start is not None) \
            if all(piece.char_start is not None for piece in pieces) else row.char_start
        end = max(piece.char_end for piece in pieces if piece.char_end is not None) \
            if all(piece.char_end is not None for piece in pieces) else row.char_end
        quote = _slice(row, start, end)
        evidence.append(FactEvidence(
            fact_id=statement.fact_id, statement_id=statement.statement_id,
            sentence_ids=(row.sentence_id,), quote=quote, kinds=(row.kind,),
            acceptable=not problems and bool(quote), problems=tuple(problems),
            char_start=start, char_end=end, node_id=statement.node_id))
    return evidence


def workbook_derived_evidence(document: CdmWorkbook, alignment: WorkbookAlignment
                              ) -> list[FactEvidence]:
    """One evidence item per derived value, quoted from the cell that computes it."""
    evidence: list[FactEvidence] = []
    for value in document.workbook.derived_values:
        cell = _cell_of(document, value.cell_id)
        found = alignment.cell(value.cell_id)
        problems: list[str] = []
        if cell is None:
            problems.append(f"derived value {value.fact_id} names an unknown cell")
        if found is None or found.sentence_id is None:
            problems.append(f"cell {value.cell_id} is not aligned to a parser row")
        quote = found.quote if found is not None else ""
        if cell is not None and quote and not numeric_surface_in(cell, quote):
            problems.append("the computed value is not in the parser text")
        evidence.append(FactEvidence(
            fact_id=value.fact_id, statement_id=f"derived:{value.cell_id}",
            sentence_ids=(found.sentence_id,) if found and found.sentence_id else (),
            quote=quote, kinds=(found.kind,) if found else (),
            acceptable=not problems and bool(quote), problems=tuple(problems),
            char_start=found.char_start if found else None,
            char_end=found.char_end if found else None, node_id=value.cell_id))
    return evidence


def _slice(row: RowAlignment, start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return row.quote
    # the row quote is contiguous in the parser text; the caller slices the original
    return row.quote[start - (row.char_start or 0):end - (row.char_start or 0)]


def _table_of(document: CdmWorkbook, row_id: str):
    for sheet in document.workbook.sheets:
        for table in _sheet_tables(sheet):
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                if row.row_id == row_id:
                    return _RowTable(table_row=row, cells=list(row.cells))
    return None


@dataclass
class _RowTable:
    table_row: TableRow
    cells: list[TableCell]


def _cell_of(document: CdmWorkbook, cell_id: str) -> TableCell | None:
    for sheet in document.workbook.sheets:
        for table in _sheet_tables(sheet):
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                for cell in row.cells:
                    if cell.cell_id == cell_id:
                        return cell
    return None
