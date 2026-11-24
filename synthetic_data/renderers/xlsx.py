"""XLSX workbook renderer (architecture §7.5, Phase F, decision DEC-4).

A workbook is a first-class document kind: sheets of typed tables, not prose in cells.
The renderer writes a real OOXML spreadsheet with ``openpyxl`` — one worksheet per
model sheet, typed numeric/date/percentage cells, currency-bearing number formats,
frozen headers, filters, column widths, defined names and formulas **with cached
results** — and never converts a workbook into a document.

Pagination (§7.5, D11)
    A sheet is an addressing unit, not a page.  ``page_kind`` is ``"sheet"`` because
    that is the unit a location is expressed in, and every ``RenderLoc`` is still
    ``pageless=True`` with ``page=None`` and ``bbox=None``.  The parser reports one
    synthetic page per workbook; that number stays in the parser record and is never
    copied into the render map.

Formulas and cached values
    ``openpyxl`` writes ``<f>SUM(...)</f><v></v>``: the formula, and an *empty* cached
    value.  A reader that opens the file without recalculating (which is exactly what a
    parser does) therefore sees nothing.  After saving, the renderer rewrites each
    worksheet part to put the model's ``CellFormula.cached_value`` into ``<v>`` (and
    ``t="str"`` for text results), which is the same thing Excel itself stores.  The
    formula text is preserved, so ``data_only=True`` and ``data_only=False`` both work.
    This is verified in the tests by reading the XML and by both openpyxl modes.

Determinism
    ``openpyxl`` stamps ``docProps/core.xml`` with the current time and writes ZIP
    members in hash order with local timestamps.  The renderer fixes the document
    properties and repacks the archive (sorted members, fixed timestamp, fixed
    attributes, fixed compression); part contents are copied byte for byte apart from
    the cached-value injection.

Known ingestion limitations, measured rather than assumed (D6)
    * currency symbols and thousands separators are lost: the parser returns plain
      numbers (``4000000``, not ``£4,000,000.00``);
    * a formula's text is not extracted — the cached value is what arrives;
    * blank rows and columns are compacted, so parser row indices are not sheet rows.
      The renderer therefore never writes blank rows inside a table.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lxml import etree
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

from synthetic_data.canonical import cell_text, row_text
from synthetic_data.model import (
    Block,
    CdmDocument,
    IsoDate,
    Money,
    Percentage,
    RenderLoc,
    RenderMap,
    RenderProfile,
    RenderResult,
    RenderSpan,
    RendererCapabilities,
    Table,
    TableRow,
    assert_render_coverage,
)
from synthetic_data.renderers.context import RenderContext

__all__ = ["XlsxRenderer", "DEFAULT_XLSX_PROFILE", "XLSX_CAPABILITIES",
           "currency_number_format"]

XLSX_CAPABILITIES = RendererCapabilities(
    format="xlsx", paginates=True, page_kind="sheet", tables=True, bbox=False,
    footnotes=False, headers_footers=False)

DEFAULT_XLSX_PROFILE = RenderProfile(page_size=None, font_family="Calibri",
                                     table_style="Excel default")

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_FIXED_MOMENT = datetime(2000, 1, 1, 0, 0, 0)

_CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥", "CHF": "CHF "}
_HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
_TOTAL_FILL = PatternFill("solid", fgColor="F2F2F2")
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(bottom=_THIN)


def currency_number_format(currency: str) -> str:
    """A real Excel currency format for the typed amount's own currency."""
    symbol = _CURRENCY_SYMBOL.get(currency, f"{currency} ")
    return f'"{symbol}"#,##0.00' if not symbol.endswith(" ") else f'"{symbol}"#,##0.00'


@dataclass
class _Unit:
    canonical_id: str
    kind: str
    text: str
    sheet_name: str
    section_index: int | None = None
    table_index: int | None = None
    row: int | None = None
    column: int | None = None
    cell_ref: str | None = None


class XlsxRenderer:
    """Renderer protocol implementation for ``format == "xlsx"``."""

    capabilities = XLSX_CAPABILITIES

    def render(self, document: CdmDocument, profile: RenderProfile,
               out: str | Path,
               *, context: RenderContext | None = None) -> RenderResult:
        if document.doc_kind != "workbook":
            raise ValueError(
                f"{document.document_id} is a {document.doc_kind} document; only a "
                f"workbook is rendered as XLSX (§7.7)")
        workbook = getattr(document, "workbook", None)
        if workbook is None:
            raise ValueError(f"{document.document_id} carries no workbook model")

        builder = _XlsxBuilder(document, profile)
        builder.build()

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = path.with_name(path.name + ".raw")
        builder.save(raw, path)

        spans = builder.spans()
        facts = builder.fact_surfaces()
        render_map = RenderMap(document_id=document.document_id, format="xlsx",
                               spans=spans, fact_surfaces=facts)
        assert_render_coverage(set(document.node_ids()), render_map)

        payload = path.read_bytes()
        return RenderResult(
            format="xlsx", path=str(path), sha256=hashlib.sha256(payload).hexdigest(),
            spans=spans, fact_surfaces=facts, warnings=builder.warnings,
            capabilities=self.capabilities)


# ---------------------------------------------------------------------------
class _XlsxBuilder:
    def __init__(self, document: CdmDocument, profile: RenderProfile) -> None:
        self.document = document
        self.profile = profile
        self.workbook = document.workbook
        self.book = Workbook()
        self.book.remove(self.book.active)
        self.units: list[_Unit] = []
        self.warnings: list[str] = []
        self.table_index = 0
        self.cached: dict[str, dict[str, object]] = {}   # sheet -> cell ref -> value
        self._first_table_row: dict[str, int] = {}
        self._last_table_row: dict[str, int] = {}

    # -- build -------------------------------------------------------------
    def build(self) -> None:
        self._properties()
        for index, sheet in enumerate(self.workbook.sheets):
            word = self.book.create_sheet(title=sheet.name)
            self.cached[sheet.name] = {}
            row = 1
            self.units.append(_Unit(sheet.sheet_id, "section", sheet.name, sheet.name,
                                    section_index=index))
            for block in sheet.blocks:
                if block.table is not None:              # a table inside a block
                    row = self._table(word, block.table, sheet.name, row)
                    continue
                row = self._block(word, block, sheet.name, row)
            if sheet.blocks and sheet.tables:
                row += 1                                 # one blank row, as in a real sheet
            for table in sheet.tables:
                row = self._table(word, table, sheet.name, row)
            self._finish_sheet(word, sheet)
        self._named_ranges()

    def _properties(self) -> None:
        metadata = self.document.metadata
        properties = self.book.properties
        properties.creator = "Synthetic Data Generator"
        properties.lastModifiedBy = "Synthetic Data Generator"
        properties.title = metadata.title or self.document.title
        properties.subject = self.document.doc_type
        properties.description = "Typed workbook generated from the canonical model"
        properties.created = _FIXED_MOMENT
        properties.modified = _FIXED_MOMENT
        properties.lastPrinted = _FIXED_MOMENT
        properties.identifier = self.document.document_id
        properties.keywords = metadata.classification or ""
        properties.category = self.document.doc_type
        properties.version = "1.0"

    def _block(self, word, block: Block, sheet_name: str, row: int) -> int:
        text = " ".join(run.text.strip() for run in block.runs or []
                        if run.text and run.text.strip())
        if not text:
            return row
        cell = word.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, size=11)
        self.units.append(_Unit(block.block_id, "block", text, sheet_name,
                                section_index=self._sheet_index(sheet_name),
                                row=row, column=1, cell_ref=f"A{row}"))
        return row + 1

    def _table(self, word, table: Table, sheet_name: str, row: int) -> int:
        index = self.table_index
        self.table_index += 1
        rows = [*table.header_rows, *table.body_rows, *table.total_rows]
        width = len(table.columns)
        self._first_table_row.setdefault(sheet_name, row)
        self.units.append(_Unit(table.table_id, "table", row_text(rows[0]) if rows else "",
                                sheet_name, section_index=self._sheet_index(sheet_name),
                                table_index=index))

        header_count = len(table.header_rows)
        for offset, table_row in enumerate(rows):
            is_header = offset < header_count
            is_total = offset >= header_count + len(table.body_rows)
            self._write_row(word, table_row, row + offset, index, sheet_name,
                            is_header=is_header, is_total=is_total, width=width)
        last = row + len(rows) - 1
        self._last_table_row[sheet_name] = last
        return last + 1

    def _write_row(self, word, table_row: TableRow, row: int, table_index: int,
                   sheet_name: str, *, is_header: bool, is_total: bool,
                   width: int) -> None:
        for column, table_cell in enumerate(table_row.cells, start=1):
            if column > width:
                break
            ref = f"{get_column_letter(column)}{row}"
            target = word.cell(row=row, column=column)
            value = self._typed(table_cell)
            if table_cell.formula is not None:
                target.value = table_cell.formula.formula
                self.cached[sheet_name][ref] = self._cached(table_cell.formula.cached_value)
            elif value is not None:
                target.value = value
            number_format = table_cell.number_format
            if isinstance(table_cell.value, Money) and not _has_currency(number_format):
                # presentation only: the amount stays a number, Excel shows the currency
                number_format = currency_number_format(table_cell.value.currency)
            if number_format:
                target.number_format = number_format
            if is_header:
                target.font = Font(bold=True)
                target.fill = _HEADER_FILL
                target.alignment = Alignment(horizontal="center", vertical="center")
            elif is_total:
                target.font = Font(bold=True)
                target.fill = _TOTAL_FILL
                target.border = _BORDER
            if target.number_format in {"0.00%", "#,##0"}:
                target.alignment = Alignment(horizontal="right")

            self.units.append(_Unit(table_cell.cell_id, "cell", cell_text(table_cell),
                                    sheet_name,
                                    section_index=self._sheet_index(sheet_name),
                                    table_index=table_index, row=row, column=column,
                                    cell_ref=ref))
        self.units.append(_Unit(table_row.row_id, "row", row_text(table_row), sheet_name,
                                section_index=self._sheet_index(sheet_name),
                                table_index=table_index, row=row))

    def _finish_sheet(self, word, sheet) -> None:
        if sheet.frozen_header and sheet.name in self._first_table_row:
            word.freeze_panes = f"A{self._first_table_row[sheet.name] + 1}"
        first = self._first_table_row.get(sheet.name)
        last = self._last_table_row.get(sheet.name)
        if sheet.tables and first is not None and last is not None and last > first:
            width = len(sheet.tables[0].columns) if sheet.tables else 1
            word.auto_filter.ref = f"A{first}:{get_column_letter(width)}{last}"
        for column in range(1, max(len(word.column_dimensions), 1) + 1):
            letter = get_column_letter(column)
            widest = 0
            for cell in word[letter]:
                if cell.value is None:
                    continue
                widest = max(widest, len(str(cell.value)))
            if widest:
                word.column_dimensions[letter].width = min(max(widest + 2, 10), 48)

    def _named_ranges(self) -> None:
        """Resolve each defined name through its sheet id, or refuse to write it.

        A name is never dropped silently and never repaired by guessing: the model
        has already proved that the sheet id exists and the reference is A1, so a
        failure here means the workbook and the renderer disagree, which is a bug
        worth stopping for.
        """
        sheets = {sheet.sheet_id: sheet.name for sheet in self.workbook.sheets}
        for name, target in self.workbook.named_ranges.items():
            sheet_name = sheets.get(target.sheet_id)
            if sheet_name is None:
                raise ValueError(
                    f"named range {name!r} targets sheet id {target.sheet_id!r}, "
                    f"which is not in the workbook")
            self.book.defined_names[name] = DefinedName(
                name, attr_text=f"{_quote_sheet(sheet_name)}!{target.ref}")

    # -- helpers -----------------------------------------------------------
    def _sheet_index(self, sheet_name: str) -> int:
        for index, sheet in enumerate(self.workbook.sheets):
            if sheet.name == sheet_name:
                return index
        return 0

    @staticmethod
    def _typed(table_cell) -> object:
        if table_cell.value is not None:
            return _plain(table_cell.value)
        # a text cell carries its string in ``text``; an empty one carries nothing
        return table_cell.text

    @staticmethod
    def _cached(value) -> object:
        return _plain(value)

    # -- output ------------------------------------------------------------
    def save(self, raw: Path, target: Path) -> None:
        self.book.save(str(raw))
        with zipfile.ZipFile(raw) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        if "docProps/core.xml" in parts:
            # openpyxl stamps ``dcterms:modified`` with the save time, whatever the
            # workbook's properties say; the fixed value is written back here
            parts["docProps/core.xml"] = _fix_modified(parts["docProps/core.xml"])
        for name in list(parts):
            if not name.startswith("xl/worksheets/sheet"):
                continue
            sheet_name = self._sheet_for_part(name)
            if sheet_name and self.cached.get(sheet_name):
                parts[name] = _inject_cached(parts[name], self.cached[sheet_name])
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
            for name in sorted(parts):
                info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0o600 << 16
                out.writestr(info, parts[name])
        raw.unlink(missing_ok=True)

    def _sheet_for_part(self, part: str) -> str | None:
        stamp = part.rsplit("/", 1)[-1].replace("sheet", "").replace(".xml", "")
        if not stamp.isdigit():
            return None
        position = int(stamp) - 1
        sheets = self.workbook.sheets
        return sheets[position].name if 0 <= position < len(sheets) else None

    # -- provenance --------------------------------------------------------
    def spans(self) -> list[RenderSpan]:
        spans: list[RenderSpan] = []
        seen: set[str] = set()
        for unit in self.units:
            if unit.canonical_id in seen:
                continue
            seen.add(unit.canonical_id)
            text = unit.text or ""
            spans.append(RenderSpan(
                canonical_id=unit.canonical_id, kind=unit.kind, format="xlsx",
                loc=RenderLoc(format="xlsx", pageless=True, page=None, bbox=None,
                              sheet_name=unit.sheet_name,
                              section_index=unit.section_index,
                              table_index=unit.table_index,
                              row=unit.row, column=unit.column,
                              cell_ref=unit.cell_ref),
                text=text, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()))
        return spans

    def fact_surfaces(self) -> dict[str, list[str]]:
        """A fact's surface in this format is the text its cells actually display."""
        surfaces: dict[str, set[str]] = {}
        for sheet in self.workbook.sheets:
            for table in [*(b.table for b in sheet.blocks if b.table is not None),
                          *sheet.tables]:
                for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                    for cell in row.cells:
                        if not cell.fact_ref:
                            continue
                        text = cell_text(cell)
                        if text:
                            surfaces.setdefault(cell.fact_ref, set()).add(text)
        return {fact: sorted(values) for fact, values in surfaces.items()}


# ---------------------------------------------------------------------------
_SIMPLE_SHEET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_sheet(name: str) -> str:
    """Excel sheet reference: bare when the name is a simple identifier."""
    if _SIMPLE_SHEET.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _has_currency(number_format: str | None) -> bool:
    if not number_format:
        return False
    return any(marker in number_format for marker in ('"', "£", "$", "€", "¥"))         or "CHF" in number_format


def _plain(value):
    """A model value as the number/date/string an Excel cell stores."""
    if isinstance(value, Money):
        return value.amount_minor / 100
    if isinstance(value, Percentage):
        return value.basis_points / 10000
    if isinstance(value, IsoDate):
        return value.value
    if isinstance(value, bool):
        return bool(value)
    return value


def _fix_modified(part: bytes) -> bytes:
    """Pin ``dcterms:modified`` to the document's fixed moment."""
    root = etree.fromstring(part)
    for element in root.iter("{http://purl.org/dc/terms/}modified"):
        element.text = _FIXED_MOMENT.strftime("%Y-%m-%dT%H:%M:%SZ")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _inject_cached(part: bytes, cached: dict[str, object]) -> bytes:
    """Put a cached result into every formula cell that has one.

    ``openpyxl`` writes ``<f>…</f><v></v>``; a reader that does not recalculate sees an
    empty value.  The cached result is written exactly where Excel writes it, and the
    formula text is left untouched.
    """
    root = etree.fromstring(part)
    for cell in root.iter(f"{{{_NS}}}c"):
        if cell.find(f"{{{_NS}}}f") is None:
            continue
        ref = cell.get("r")
        if ref not in cached:
            continue
        value = cached[ref]
        for old in cell.findall(f"{{{_NS}}}v"):
            cell.remove(old)
        node = etree.SubElement(cell, f"{{{_NS}}}v")
        if isinstance(value, str):
            cell.set("t", "str")
            node.text = value
        elif isinstance(value, bool):
            cell.set("t", "b")
            node.text = "1" if value else "0"
        elif hasattr(value, "isoformat"):
            cell.attrib.pop("t", None)
            node.text = str(_excel_serial(value))
        else:
            cell.attrib.pop("t", None)
            node.text = repr(float(value)) if isinstance(value, float) else str(value)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _excel_serial(value) -> int:
    """Excel's 1900 date system, origin 1899-12-30 (the value openpyxl itself writes)."""
    epoch = datetime(1899, 12, 30)
    moment = datetime(value.year, value.month, value.day)
    return (moment - epoch).days
