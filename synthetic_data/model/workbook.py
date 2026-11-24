"""Specialised workbook model (architecture §7.5, decisions DEC-4, DEC-12).

A workbook is a first-class document kind, not prose placed into cells.  Cells
carry typed values, an independent display string and an optional formula with a
cached result, so a reader never has to recalculate and gold never depends on a
renderer's formatting.

Three things this module makes representable that a bare grid cannot:

``NamedRange``
    A defined name points at a **sheet id**, not at a display name typed into a
    string.  ``Transaction_ledger!E2:E13`` was a reference that silently matched
    nothing once the sheet was called ``Transaction ledger``; the sheet id is
    stable, the display name is not, and a reference that cannot resolve is
    rejected at construction rather than dropped at write time.

``DerivedValue``
    A formula cell that computes something meaningful has a stable fact id, the
    cell it lives in and the cells it is computed from.  A formula cell without one
    is decorative: it is still written, but it is never offered as a fact.

``cell_role``
    The single classification of a content-bearing cell: ``derived`` (formula plus
    identity), ``asserted`` (``fact_ref``), ``decorative`` (labels, headers, blanks
    and anonymous calculations).
"""
from __future__ import annotations

import re
from typing import Iterator, Literal

from pydantic import Field, model_validator

from .content import Block, CdmDocument, CellFormula, Table, TableCell, _Content
from .values import CanonicalId

__all__ = ["Workbook", "Sheet", "CellFormula", "CdmWorkbook", "NamedRange",
           "DerivedValue", "CellRole", "cell_role", "workbook_cells"]

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: sheet-relative A1 reference: a cell (``B4``) or a rectangular range (``E2:E13``)
_A1_RE = re.compile(r"^\$?[A-Z]{1,3}\$?\d{1,7}(?::\$?[A-Z]{1,3}\$?\d{1,7})?$")

CellRole = Literal["asserted", "derived", "decorative"]


class NamedRange(_Content):
    """A defined name that resolves through a stable sheet id (DEC-12).

    ``ref`` is sheet-relative A1 notation.  The display name is *not* part of the
    reference: the renderer resolves ``sheet_id`` to whatever the sheet is called
    in this artifact, so renaming a sheet can never silently break a name.
    """

    name: str = Field(min_length=1, max_length=64)
    sheet_id: CanonicalId
    ref: str = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def _shape(self) -> "NamedRange":
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid defined name {self.name!r}")
        if not _A1_RE.match(self.ref):
            raise ValueError(
                f"defined name {self.name!r} has {self.ref!r}; a sheet-relative A1 "
                f"cell or range such as 'B4' or 'E2:E13' is required")
        return self


class DerivedValue(_Content):
    """A computed value with a stable identity and explicit inputs (DEC-12)."""

    fact_id: CanonicalId
    cell_id: CanonicalId
    label: str = Field(min_length=1, max_length=200)
    #: canonical cell ids this result is computed from, in formula order
    inputs: list[CanonicalId] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "DerivedValue":
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError(f"derived value {self.fact_id} repeats an input cell")
        if self.cell_id in self.inputs:
            raise ValueError(f"derived value {self.fact_id} is its own input")
        return self


class Sheet(_Content):
    sheet_id: CanonicalId
    name: str = Field(min_length=1, max_length=31)   # Excel sheet-name limit
    blocks: list[Block] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    frozen_header: bool = True

    @model_validator(mode="after")
    def _has_content(self) -> "Sheet":
        if not self.blocks and not self.tables:
            raise ValueError(f"sheet {self.name!r} has no content")
        return self


class Workbook(_Content):
    workbook_id: CanonicalId
    sheets: list[Sheet] = Field(min_length=1)
    #: defined name -> NamedRange, keyed by the name for lookup convenience
    named_ranges: dict[str, NamedRange] = Field(default_factory=dict)
    derived_values: list[DerivedValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _structure(self) -> "Workbook":
        sheet_ids = [s.sheet_id for s in self.sheets]
        if len(set(sheet_ids)) != len(sheet_ids):
            raise ValueError("duplicate sheet id in workbook")
        names = [s.name.casefold() for s in self.sheets]
        if len(set(names)) != len(names):
            raise ValueError("duplicate sheet name in workbook")

        table_ids: list[str] = []
        node_ids: list[str] = []
        for sheet in self.sheets:
            for block in sheet.blocks:
                node_ids.append(block.block_id)
                if block.table is not None:
                    node_ids.append(block.table.table_id)
                    table_ids.append(block.table.table_id)
                    for row in [*block.table.header_rows, *block.table.body_rows,
                                *block.table.total_rows]:
                        node_ids.extend([row.row_id, *(c.cell_id for c in row.cells)])
            for table in sheet.tables:
                node_ids.append(table.table_id)
                table_ids.append(table.table_id)
                for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                    node_ids.extend([row.row_id, *(c.cell_id for c in row.cells)])
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node id in workbook")
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("duplicate table id in workbook")

        for key, target in self.named_ranges.items():
            if key != target.name:
                raise ValueError(
                    f"named range key {key!r} does not match its name {target.name!r}")
            if target.sheet_id not in sheet_ids:
                raise ValueError(
                    f"named range {target.name!r} targets sheet id "
                    f"{target.sheet_id!r}, which is not in this workbook")

        self._derivations()
        return self

    def _derivations(self) -> None:
        """Every derived fact has one definition, one cell and resolvable inputs."""
        cells = {cell.cell_id: cell for cell in workbook_cells(self)}
        by_fact: dict[str, DerivedValue] = {}
        declared_cell: set[str] = set()
        for value in self.derived_values:
            if value.fact_id in by_fact:
                raise ValueError(f"duplicate derived fact id {value.fact_id}")
            by_fact[value.fact_id] = value
            cell = cells.get(value.cell_id)
            if cell is None:
                raise ValueError(
                    f"derived value {value.fact_id} names unknown cell {value.cell_id}")
            if cell.formula is None or cell.formula.derived_fact_id != value.fact_id:
                raise ValueError(
                    f"derived value {value.fact_id} does not match its cell's "
                    f"formula derived_fact_id")
            if value.cell_id in declared_cell:
                raise ValueError(f"cell {value.cell_id} declares two derived values")
            declared_cell.add(value.cell_id)
            for input_id in value.inputs:
                if input_id not in cells:
                    raise ValueError(
                        f"derived value {value.fact_id} names unknown input {input_id}")

        for cell in cells.values():
            if cell.formula is not None and cell.formula.derived_fact_id:
                if cell.formula.derived_fact_id not in by_fact:
                    raise ValueError(
                        f"cell {cell.cell_id} declares derived fact "
                        f"{cell.formula.derived_fact_id} with no DerivedValue")

        # derivation inputs must be acyclic: a value cannot feed itself, directly
        # or through other derived values
        def derived_of(cell_id: str) -> str | None:
            formula = cells[cell_id].formula
            return formula.derived_fact_id if formula is not None else None

        inputs: dict[str, list[str]] = {}
        for value in self.derived_values:
            parents = [derived_of(input_id) for input_id in value.inputs]
            inputs[value.fact_id] = [parent for parent in parents if parent]
        state: dict[str, int] = {}

        def visit(node: str) -> None:
            if state.get(node) == 1:
                raise ValueError(f"cyclic derivation through derived fact {node}")
            if state.get(node) == 2:
                return
            state[node] = 1
            for parent in inputs.get(node, []):
                visit(parent)
            state[node] = 2

        for fact_id in inputs:
            visit(fact_id)

    def sheet(self, sheet_id: str) -> Sheet:
        for sheet in self.sheets:
            if sheet.sheet_id == sheet_id:
                return sheet
        raise KeyError(f"unknown sheet id {sheet_id!r}")

    def derived_fact_ids(self) -> list[str]:
        return [value.fact_id for value in self.derived_values]


def workbook_cells(workbook: Workbook) -> Iterator[TableCell]:
    """Every cell of every table of every sheet, in sheet/row/column order."""
    for sheet in workbook.sheets:
        for table in _sheet_tables(sheet):
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                yield from row.cells


def _sheet_tables(sheet: Sheet):
    for block in sheet.blocks:
        if block.table is not None:
            yield block.table
    yield from sheet.tables


def cell_role(cell: TableCell) -> CellRole:
    """Classify a cell: derived results first, then asserted facts, then decoration.

    A derived cell is one whose formula declares its identity.  A formula cell with
    no identity is decorative — it is still a real spreadsheet cell, it is simply
    not a fact, and this function is where that distinction is made instead of a
    caller guessing from "it has a formula".
    """
    if cell.formula is not None and cell.formula.derived_fact_id:
        return "derived"
    if cell.fact_ref:
        return "asserted"
    return "decorative"


class CdmWorkbook(CdmDocument):
    """A document whose content lives in worksheets rather than prose sections."""

    doc_kind: Literal["workbook"] = "workbook"  # type: ignore[assignment]
    workbook: Workbook

    def iter_nodes(self) -> Iterator[tuple[str, object]]:
        yield from super().iter_nodes()
        for sheet in self.workbook.sheets:
            yield sheet.sheet_id, sheet
            for block in sheet.blocks:
                yield block.block_id, block
                if block.table is not None:
                    yield from _table_nodes(block.table)
            for table in sheet.tables:
                yield from _table_nodes(table)

    @model_validator(mode="after")
    def _workbook_structure(self) -> "CdmWorkbook":
        if self.doc_kind != "workbook":       # pragma: no cover - literal-typed
            raise ValueError("CdmWorkbook requires doc_kind == 'workbook'")
        if not self.workbook.sheets:
            raise ValueError("workbook requires at least one sheet")
        # Prose sections are allowed only as a title/notes block, never as the
        # primary content: a workbook with no sheets is prose in disguise.
        return self


def _table_nodes(table: Table) -> Iterator[tuple[str, object]]:
    yield table.table_id, table
    for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
        yield row.row_id, row
        for cell in row.cells:
            yield cell.cell_id, cell
