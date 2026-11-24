"""Workbook arithmetic, cell classification and derived-value validation (DEC-12).

A spreadsheet is not a set of independent numbers: a total is computed from the
rows above it, a balance from the row before, an interest figure from a principal
and a rate.  The canonical model records each of those as a :class:`DerivedValue`
with a stable fact id and explicit input cells, and this module is the one place
that proves the three representations agree:

* the **formula** (what a spreadsheet engine would compute),
* the **cached value** the parser actually extracts,
* the **typed value** the semantic layer carries.

``evaluate`` is deliberately small — numbers, cell references, ranges, ``+ - * /``,
parentheses and ``SUM``/``MIN``/``MAX``/``COUNT``/``AVERAGE``.  A formula outside
that grammar is reported as unsupported rather than approximated: a validation
that cannot compute the result must not claim the result is right.

Cell coordinates come from the :class:`~synthetic_data.model.RenderMap`, which is
the renderer's own record of where each canonical cell landed.  Nothing here
derives a cell reference from table geometry, because the parser does not supply
coordinates and the renderer does.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from synthetic_data.model import (
    CellFormula,
    IsoDate,
    Money,
    Percentage,
    RenderMap,
    TableCell,
    Workbook,
    cell_role,
    workbook_cells,
)

__all__ = [
    "WorkbookError",
    "SheetIndex",
    "sheet_index",
    "numeric_value",
    "evaluate",
    "formula_inputs",
    "validate_derived_values",
    "classify_cells",
    "derived_cells",
]

_TOLERANCE = 1e-6
_EXCEL_EPOCH = date(1899, 12, 30)

_TOKEN = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<ref>(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_. ]*)!\$?[A-Z]{1,3}\$?\d{1,7}"
    r"|\$?[A-Z]{1,3}\$?\d{1,7})"
    r"|(?P<function>[A-Z][A-Z0-9.]*)"
    r"|(?P<range>:)"
    r"|(?P<op>[-+*/(),]))")
_A1 = re.compile(r"^(?P<sheet>.*?)(?P<col>\$?[A-Z]{1,3})(?P<row>\$?\d{1,7})$")
_FUNCTIONS = {"SUM", "MIN", "MAX", "COUNT", "AVERAGE"}


class WorkbookError(RuntimeError):
    """A derived value disagrees with its formula, its inputs or its cell."""


@dataclass(frozen=True)
class SheetIndex:
    """Canonical cell ids addressable by ``A1`` reference, per sheet."""

    by_ref: dict[str, dict[str, str]]          # sheet name -> A1 -> canonical id
    by_id: dict[str, tuple[str, str]]          # canonical id -> (sheet name, A1)

    def resolve(self, sheet: str, ref: str) -> str | None:
        return self.by_ref.get(sheet, {}).get(ref.replace("$", ""))

    def sheet_of(self, cell_id: str) -> str | None:
        found = self.by_id.get(cell_id)
        return found[0] if found else None

    def ref_of(self, cell_id: str) -> str | None:
        found = self.by_id.get(cell_id)
        return found[1] if found else None


def sheet_index(render_map: RenderMap | object) -> SheetIndex:
    """Index every rendered cell by sheet and ``A1`` reference.

    Accepts a ``RenderMap``, a ``RenderResult`` or a plain span list: they carry the
    same information and a caller should not have to unwrap it.
    """
    by_ref: dict[str, dict[str, str]] = {}
    by_id: dict[str, tuple[str, str]] = {}
    spans = getattr(render_map, "spans", render_map)
    for span in spans:
        loc = span.loc
        if span.kind != "cell" or not loc.sheet_name or not loc.cell_ref:
            continue
        by_ref.setdefault(loc.sheet_name, {})[loc.cell_ref] = span.canonical_id
        by_id[span.canonical_id] = (loc.sheet_name, loc.cell_ref)
    return SheetIndex(by_ref=by_ref, by_id=by_id)


def numeric_value(cell: TableCell) -> float:
    """The cell's value as the number Excel stores, or an error.

    Money is major units, percentage is its fraction (``40%`` -> ``0.4``) and a
    date is its 1900-system serial.  Text is *not* coerced: an arithmetic cell
    that holds a label is a modelling error, not a zero.
    """
    value = cell.value
    if isinstance(value, Money):
        return value.amount_minor / 100
    if isinstance(value, Percentage):
        return value.basis_points / 10000
    if isinstance(value, IsoDate):
        return float((value.value - _EXCEL_EPOCH).days)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raise WorkbookError(
        f"cell {cell.cell_id} holds {type(value).__name__} text, which cannot be "
        f"used in arithmetic")


@dataclass
class _Parser:
    tokens: list[tuple[str, str]]
    position: int
    formula: str

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise WorkbookError(f"unexpected end of formula {self.formula!r}")
        self.position += 1
        return token


def _tokenise(formula: str) -> list[tuple[str, str]]:
    body = formula[1:] if formula.startswith("=") else formula
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(body):
        match = _TOKEN.match(body, position)
        if match is None or match.end() == position:
            raise WorkbookError(f"unsupported formula syntax at {body[position:]!r}")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _split_ref(ref: str) -> tuple[str | None, str]:
    """``'Sheet name'!B4`` -> ``("Sheet name", "B4")``; ``B4`` -> ``(None, "B4")``."""
    if "!" not in ref:
        return None, ref.replace("$", "")
    sheet, _, cell = ref.rpartition("!")
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    else:
        sheet = sheet.strip()
    return sheet, cell.replace("$", "")


def _peek_is(parser: "_Parser", text: str) -> bool:
    token = parser.peek()
    return token is not None and token[1] == text


class _Evaluator:
    def __init__(self, sheet: str, index: SheetIndex, workbook: Workbook) -> None:
        self.sheet = sheet
        self.index = index
        self.cells = {cell.cell_id: cell for cell in workbook_cells(workbook)}

    # -- cells -------------------------------------------------------------
    def value_of(self, sheet: str | None, ref: str) -> float:
        cell_id = self.index.resolve(sheet or self.sheet, ref)
        if cell_id is None:
            raise WorkbookError(
                f"formula on sheet {self.sheet!r} references {sheet and sheet + '!' or ''}{ref}, "
                f"which is not a rendered cell")
        return numeric_value(self.cells[cell_id])

    def range_values(self, sheet: str | None, first: str, last: str) -> list[float]:
        start, end = _rect(first, last)
        sheet = sheet or self.sheet
        values: list[float] = []
        for ref in _refs_in(start, end):
            cell_id = self.index.resolve(sheet, ref)
            if cell_id is None:
                continue                     # a blank cell contributes nothing
            cell = self.cells[cell_id]
            if cell.value is None:
                continue
            values.append(numeric_value(cell))
        return values

    # -- grammar -----------------------------------------------------------
    def parse(self, parser: _Parser) -> float:
        value = self.term(parser)
        while True:
            token = parser.peek()
            if token is None or token[1] not in {"+", "-"}:
                return value
            parser.take()
            right = self.term(parser)
            value = value + right if token[1] == "+" else value - right

    def term(self, parser: _Parser) -> float:
        value = self.unary(parser)
        while True:
            token = parser.peek()
            if token is None or token[1] not in {"*", "/"}:
                return value
            parser.take()
            right = self.unary(parser)
            if token[1] == "*":
                value *= right
            else:
                if right == 0:
                    raise WorkbookError("division by zero")
                value /= right

    def unary(self, parser: _Parser) -> float:
        token = parser.peek()
        if token is not None and token[1] in {"+", "-"}:
            parser.take()
            value = self.unary(parser)
            return -value if token[1] == "-" else value
        return self.primary(parser)

    def primary(self, parser: _Parser) -> float:
        kind, text = parser.take()
        if kind == "number":
            return float(text)
        if kind == "ref":
            sheet, ref = _split_ref(text)
            if _peek_is(parser, ":"):
                parser.take()
                last_kind, last_text = parser.take()
                if last_kind != "ref":
                    raise WorkbookError(f"bad range end {last_text!r}")
                raise WorkbookError(f"range {text}:{last_text} used outside a function")
            return self.value_of(sheet, ref)
        if kind == "function":
            return self.call(text, parser)
        if text == "(":
            value = self.parse(parser)
            if parser.take()[1] != ")":
                raise WorkbookError("unbalanced parentheses")
            return value
        raise WorkbookError(f"unexpected token {text!r}")

    def call(self, name: str, parser: _Parser) -> float:
        name = name.upper()
        if name not in _FUNCTIONS:
            raise WorkbookError(f"unsupported function {name}()")
        if parser.take()[1] != "(":
            raise WorkbookError(f"{name} requires an argument list")
        values: list[float] = []
        while True:
            if _peek_is(parser, ")"):
                parser.take()
                break
            args = self.argument(parser)
            values.extend(args)
            token = parser.take()
            if token[1] == ")":
                break
            if token[1] != ",":
                raise WorkbookError(f"{name} argument list is malformed")
        if name == "COUNT":
            return float(len(values))
        if not values:
            return 0.0
        if name == "SUM":
            return sum(values)
        if name == "MIN":
            return min(values)
        if name == "MAX":
            return max(values)
        return sum(values) / len(values)

    def argument(self, parser: _Parser) -> list[float]:
        token = parser.peek()
        if token is not None and token[0] == "ref":
            sheet, ref = _split_ref(token[1])
            parser.take()
            if _peek_is(parser, ":"):
                parser.take()
                last_kind, last_text = parser.take()
                if last_kind != "ref":
                    raise WorkbookError(f"bad range end {last_text!r}")
                last_sheet, last_ref = _split_ref(last_text)
                if last_sheet is not None and last_sheet != sheet:
                    raise WorkbookError("a range must stay on one sheet")
                return self.range_values(sheet, ref, last_ref)
            return [self.value_of(sheet, ref)]
        return [self.parse(parser)]


def _column_number(letters: str) -> int:
    total = 0
    for char in letters:
        total = total * 26 + (ord(char) - 64)
    return total


def _column_letters(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _rect(first: str, last: str) -> tuple[tuple[int, int], tuple[int, int]]:
    start, end = _A1.match(first), _A1.match(last)
    if start is None or end is None:
        raise WorkbookError(f"bad range {first}:{last}")
    c1, r1 = _column_number(start.group("col")), int(start.group("row"))
    c2, r2 = _column_number(end.group("col")), int(end.group("row"))
    return (min(c1, c2), min(r1, r2)), (max(c1, c2), max(r1, r2))


def _refs_in(start: tuple[int, int], end: tuple[int, int]) -> list[str]:
    (c1, r1), (c2, r2) = start, end
    return [f"{_column_letters(column)}{row}"
            for row in range(r1, r2 + 1)
            for column in range(c1, c2 + 1)]


def evaluate(formula: str | CellFormula, *, sheet: str, index: SheetIndex,
             workbook: Workbook) -> float:
    """Compute ``formula`` in the numeric space Excel stores."""
    text = formula.formula if isinstance(formula, CellFormula) else formula
    parser = _Parser(tokens=_tokenise(text), position=0, formula=text)
    value = _Evaluator(sheet, index, workbook).parse(parser)
    if parser.peek() is not None:
        raise WorkbookError(f"trailing tokens in formula {text!r}")
    return value


def formula_inputs(formula: str | CellFormula, *, sheet: str, index: SheetIndex
                   ) -> list[str]:
    """Canonical cell ids the formula reads, in first-reference order.

    A range contributes every rendered cell inside it, not just its two corners:
    ``SUM(E2:E5)`` reads four cells, and a derivation that names fewer inputs than
    it actually consumes is rejected.
    """
    text = formula.formula if isinstance(formula, CellFormula) else formula
    tokens = _tokenise(text)
    out: list[str] = []

    def add(sheet_name: str, ref: str) -> None:
        cell_id = index.resolve(sheet_name, ref)
        if cell_id is not None and cell_id not in out:
            out.append(cell_id)

    position = 0
    while position < len(tokens):
        kind, token = tokens[position]
        if kind != "ref":
            position += 1
            continue
        ref_sheet, ref = _split_ref(token)
        sheet_name = ref_sheet or sheet
        if (position + 2 < len(tokens) and tokens[position + 1][1] == ":"
                and tokens[position + 2][0] == "ref"):
            last_sheet, last_ref = _split_ref(tokens[position + 2][1])
            if last_sheet is not None and last_sheet != sheet_name:
                raise WorkbookError("a range must stay on one sheet")
            for a1 in _refs_in(*_rect(ref, last_ref)):
                add(sheet_name, a1)
            position += 3
            continue
        add(sheet_name, ref)
        position += 1
    return out


def _expected_number(cell: TableCell) -> float:
    return numeric_value(cell)


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= _TOLERANCE * max(1.0, abs(right))


def validate_derived_values(workbook: Workbook, render_map: RenderMap) -> None:
    """Prove every derived value against its formula, its inputs and its value.

    Three separate claims are checked, because each can fail alone: the formula
    must reproduce the cached value, the declared inputs must be exactly the cells
    the formula reads, and the cached value must equal the typed value.
    """
    index = sheet_index(render_map)
    cells = {cell.cell_id: cell for cell in workbook_cells(workbook)}
    problems: list[str] = []
    for value in workbook.derived_values:
        cell = cells[value.cell_id]
        formula = cell.formula
        sheet = index.sheet_of(value.cell_id)
        if formula is None or sheet is None:
            problems.append(f"{value.fact_id}: cell {value.cell_id} has no formula or "
                            f"no rendered location")
            continue
        try:
            computed = evaluate(formula, sheet=sheet, index=index, workbook=workbook)
        except WorkbookError as exc:
            problems.append(f"{value.fact_id}: {exc}")
            continue
        typed = _expected_number(cell)
        if not _close(computed, typed):
            problems.append(
                f"{value.fact_id}: formula computes {computed!r} but the cell value "
                f"is {typed!r}")
        if formula.cached_value != cell.value:
            problems.append(f"{value.fact_id}: cached value disagrees with typed value")
        try:
            referenced = formula_inputs(formula, sheet=sheet, index=index)
        except WorkbookError as exc:
            problems.append(f"{value.fact_id}: {exc}")
            continue
        if set(referenced) != set(value.inputs):
            problems.append(
                f"{value.fact_id}: declared inputs {sorted(value.inputs)} are not the "
                f"cells the formula reads {sorted(referenced)}")
    if problems:
        raise WorkbookError("\n".join(problems))


def classify_cells(workbook: Workbook) -> dict[str, str]:
    """cell id -> role, for every cell of the workbook."""
    return {cell.cell_id: cell_role(cell) for cell in workbook_cells(workbook)}


def derived_cells(workbook: Workbook) -> dict[str, TableCell]:
    """cell id -> cell, for cells whose formula declares a derived fact."""
    return {cell.cell_id: cell for cell in workbook_cells(workbook)
            if cell_role(cell) == "derived"}
