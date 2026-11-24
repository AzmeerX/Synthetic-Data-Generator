"""Layer 2 — content model (architecture §3.1, §4.2).

A document is a hierarchy of sections, blocks, tables, rows and cells, plus the
statements that link those nodes to semantic facts.  No renderer, parser or
page coordinate appears here: layout belongs to Layer 3.
"""
from __future__ import annotations

import re
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .values import (
    CanonicalId,
    CellValue,
    DocumentRef,
    Emphasis,
    EntityRef,
    IsoDate,
    Money,
    Percentage,
    Register,
)

__all__ = [
    "MessageMeta",
    "DocumentMetadata",
    "CdmDocument",
    "Section",
    "Block",
    "BlockKind",
    "InlineRun",
    "ListItem",
    "Table",
    "TableRow",
    "TableCell",
    "CellFormula",
    "ColumnSpec",
    "ColumnType",
    "FactStatement",
    "HeaderFooter",
    "Exhibit",
    "SignatureRecord",
    "Footnote",
    "DocKind",
]

BlockKind = Literal[
    "paragraph", "list", "table", "note", "page_break",
    "signature", "exhibit", "header", "footer",
]
#: ``mixed`` is an explicit declaration; ``None`` means "not declared".
ColumnType = Literal["text", "money", "date", "percent", "int", "mixed"]
DocKind = Literal["prose", "workbook"]

_ROW_KEY_RE = re.compile(r"^r\d{2,3}c\d{2,3}$")


class _Content(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MessageMeta(_Content):
    """Per-message thread linkage (decision DEC-7); the thread itself is Layer 1."""

    thread_id: CanonicalId
    message_id: CanonicalId
    sequence: StrictInt = Field(ge=1)
    in_reply_to: CanonicalId | None = None
    references: list[CanonicalId] = Field(default_factory=list)
    attachments: list[CanonicalId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reply(self) -> "MessageMeta":
        if self.sequence == 1 and self.in_reply_to is not None:
            raise ValueError("the first message in a thread cannot reply to anything")
        if self.in_reply_to == self.message_id:
            raise ValueError("a message cannot reply to itself")
        if len(set(self.references)) != len(self.references):
            raise ValueError("duplicate reference id on message")
        return self


class DocumentMetadata(_Content):
    # ``BaseModel.register`` exists, so the field is named ``formality`` with the
    # architecture's ``register`` as its wire alias.
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    title: str | None = Field(default=None, max_length=300)
    subtitle: str | None = Field(default=None, max_length=300)
    author: EntityRef | None = None
    issuer: EntityRef | None = None
    addressee: EntityRef | None = None
    jurisdiction: str | None = Field(default=None, max_length=64)
    document_date: IsoDate | None = None
    reference: str | None = Field(default=None, max_length=120)
    version: str | None = Field(default=None, max_length=32)
    classification: str | None = Field(default=None, max_length=64)
    formality: Register = Field(default="formal", alias="register",
                                serialization_alias="register")
    thread: MessageMeta | None = None


class InlineRun(_Content):
    """A run of text, optionally anchored to an entity, document or typed value."""

    text: str | None = Field(default=None, max_length=4000)
    entity_ref: EntityRef | None = None
    document_ref: DocumentRef | None = None
    value: CellValue | None = None
    emphasis: Emphasis = "none"
    footnote_id: CanonicalId | None = None
    #: fact this run expresses; required to check invariant 3 (value equality)
    fact_ref: CanonicalId | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> "InlineRun":
        if not any([self.text, self.entity_ref, self.document_ref,
                    self.value is not None, self.footnote_id]):
            raise ValueError("inline run carries no content")
        if self.text is not None and not self.text.strip() and not any(
                [self.entity_ref, self.document_ref, self.value is not None,
                 self.footnote_id]):
            raise ValueError("blank text run must carry another payload")
        return self


class ListItem(_Content):
    item_id: CanonicalId
    runs: list[InlineRun] = Field(min_length=1)
    level: StrictInt = Field(default=1, ge=1, le=6)


class ColumnSpec(_Content):
    """Column-level type is an *expectation*, never a coercion (DEC-11).

    Cell values are authoritative.  A homogeneous column may declare its type;
    a heterogeneous column declares ``mixed``; leaving it ``None`` also means no
    expectation.  Typed cells that disagree with a concrete declaration are
    rejected rather than converted.
    """

    name: str = Field(min_length=1, max_length=120)
    type: ColumnType | None = None
    width_hint: float | None = Field(default=None, gt=0)
    align: Literal["left", "right", "center"] = "left"


class CellFormula(_Content):
    """Spreadsheet formula with its cached result.

    The cached value is what a reader sees without recalculation; it must agree
    with the cell's typed ``value`` so a formula can never disagree with the
    semantic fact the cell references.

    A formula that computes a *meaningful* value declares ``derived_fact_id``, the
    stable identity of that derived value (DEC-12).  The matching
    :class:`~synthetic_data.model.workbook.DerivedValue` names the cell ids the
    result is computed from, so a derived number is never an anonymous cell and is
    never promoted to a semantic fact on the strength of "it has a formula".
    """

    formula: str = Field(min_length=2, max_length=400, pattern=r"^=")
    cached_value: CellValue
    recalc_on_load: bool = False
    #: canonical id of the derived semantic fact this cell computes (DEC-12)
    derived_fact_id: CanonicalId | None = None

    @model_validator(mode="after")
    def _body(self) -> "CellFormula":
        if self.formula.strip() == "=":
            raise ValueError("formula must contain an expression")
        return self


class TableCell(_Content):
    cell_id: CanonicalId
    text: str | None = Field(default=None, max_length=2000)
    value: CellValue | None = None
    #: renderer-independent display string; renderers may reformat but not change value
    display: str = Field(default="", max_length=200)
    rowspan: StrictInt = Field(default=1, ge=1)
    colspan: StrictInt = Field(default=1, ge=1)
    entity_ref: EntityRef | None = None
    fact_ref: CanonicalId | None = None
    #: workbook display format, e.g. "#,##0.00" or "dd mmm yyyy" (DEC-11)
    number_format: str | None = Field(default=None, max_length=64)
    #: workbook cells only; ``cached_value`` must equal ``value``
    formula: "CellFormula | None" = None

    @model_validator(mode="before")
    @classmethod
    def _no_nested_table(cls, data):
        if isinstance(data, dict):
            for key in ("table", "tables", "nested_table", "rows"):
                if key in data:
                    raise ValueError(
                        "nested tables inside cells are not supported (DEC-10); "
                        "use a separate table in the document hierarchy or a separate worksheet")
        return data

    @model_validator(mode="after")
    def _payload(self) -> "TableCell":
        if self.formula is not None:
            if self.value is None:
                raise ValueError("a formula cell requires a typed value")
            if self.formula.cached_value != self.value:
                raise ValueError("formula cached_value disagrees with the cell value")
        if self.text is None and self.value is None and self.display == "":
            return self                      # deliberately blank cell
        if self.value is not None and not self.display:
            raise ValueError("cell with a typed value requires a display string")
        if self.text is not None and self.text.strip() == "" and self.value is None:
            raise ValueError("blank text cell must carry a value or be fully empty")
        return self


class TableRow(_Content):
    row_id: CanonicalId
    cells: list[TableCell] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_cells(self) -> "TableRow":
        ids = [c.cell_id for c in self.cells]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate cell id in row")
        return self


def _typed_kind(value) -> str | None:
    """Map a typed cell value to its column type; ``None`` for text/blank cells."""
    if value is None:
        return None
    if isinstance(value, Money):
        return "money"
    if isinstance(value, Percentage):
        return "percent"
    if isinstance(value, IsoDate):
        return "date"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    return None


class Table(_Content):
    table_id: CanonicalId
    caption: str | None = Field(default=None, max_length=300)
    header_rows: list[TableRow] = Field(default_factory=list)
    body_rows: list[TableRow] = Field(min_length=1)
    total_rows: list[TableRow] = Field(default_factory=list)
    columns: list[ColumnSpec] = Field(min_length=1)
    cell_refs: dict[str, CanonicalId] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _dimensions(self) -> "Table":
        ncols = len(self.columns)
        rows = [*self.header_rows, *self.body_rows, *self.total_rows]
        row_ids = [r.row_id for r in rows]
        if len(set(row_ids)) != len(row_ids):
            raise ValueError("duplicate row id in table")
        cell_ids: set[str] = set()
        key_to_cell: dict[str, TableCell] = {}
        for ri, row in enumerate(rows):
            width = sum(c.colspan for c in row.cells)
            if width != ncols:
                raise ValueError(
                    f"row {row.row_id} spans {width} columns, table declares {ncols}")
            ci = 0
            for cell in row.cells:
                if cell.cell_id in cell_ids:
                    raise ValueError(f"duplicate cell id {cell.cell_id} in table")
                cell_ids.add(cell.cell_id)
                key_to_cell[f"r{ri:02d}c{ci:02d}"] = cell
                for column_index in range(ci, ci + cell.colspan):
                    expected = self.columns[column_index].type
                    actual = _typed_kind(cell.value)
                    if expected in (None, "mixed") or actual is None:
                        continue
                    if actual != expected:
                        raise ValueError(
                            f"cell {cell.cell_id} holds a {actual} value but column "
                            f"{column_index} is declared {expected}; declare the column "
                            f"'mixed' or fix the cell (no coercion)")
                ci += cell.colspan
            if ri < len(self.header_rows) and any(c.rowspan > 1 for c in row.cells):
                raise ValueError("header cells must not span rows")
            for cell in row.cells:
                if ri + cell.rowspan > len(rows):
                    raise ValueError(f"cell {cell.cell_id} rowspan overflows the table")
        for key, cell in key_to_cell.items():
            if cell.fact_ref and key not in self.cell_refs:
                raise ValueError(
                    f"cell {cell.cell_id} has fact_ref but no cell_refs entry ({key})")
        for key, fact_id in self.cell_refs.items():
            if not _ROW_KEY_RE.match(key):
                raise ValueError(f"cell_refs key {key!r} is not rNNcNN")
            if key not in key_to_cell:
                raise ValueError(f"cell_refs key {key!r} does not address a cell")
            cell = key_to_cell[key]
            if cell.fact_ref and cell.fact_ref != fact_id:
                raise ValueError(
                    f"cell_refs[{key!r}]={fact_id} contradicts cell.fact_ref={cell.fact_ref}")
        return self


class Block(_Content):
    """Discriminated by ``kind``; validators enforce exactly one payload shape."""

    block_id: CanonicalId
    kind: BlockKind
    runs: list[InlineRun] | None = None
    items: list[ListItem] | None = None
    ordered: bool | None = None
    table: Table | None = None
    footnote_refs: list[CanonicalId] | None = None

    @model_validator(mode="after")
    def _payload(self) -> "Block":
        kind = self.kind
        if kind == "paragraph":
            if not self.runs:
                raise ValueError("paragraph block requires runs")
        elif kind == "list":
            if not self.items:
                raise ValueError("list block requires items")
            if self.ordered is None:
                raise ValueError("list block requires ordered")
        elif kind == "table":
            if self.table is None:
                raise ValueError("table block requires a table")
        elif kind in {"note", "header", "footer"}:
            if not self.runs:
                raise ValueError(f"{kind} block requires runs")
        elif kind == "page_break":
            if any([self.runs, self.items, self.table]):
                raise ValueError("page_break block must not carry a payload")
        elif kind in {"signature", "exhibit"}:
            pass                              # records live on the document; runs optional
        if kind != "table" and self.table is not None:
            raise ValueError(f"{kind} block must not carry a table")
        if kind != "list" and (self.items is not None or self.ordered is not None):
            raise ValueError(f"{kind} block must not carry list fields")
        if kind in {"paragraph", "note", "header", "footer"} and self.items is not None:
            raise ValueError(f"{kind} block must not carry items")
        return self


class Section(_Content):
    section_id: CanonicalId
    parent_id: CanonicalId | None = None
    depth: StrictInt = Field(ge=0, le=8)
    number: str | None = Field(default=None, max_length=32)
    heading: str = Field(min_length=1, max_length=300)
    blocks: list[Block] = Field(default_factory=list)

    @model_validator(mode="after")
    def _depth(self) -> "Section":
        if self.depth == 0 and self.parent_id is not None:
            raise ValueError("root section must not have a parent")
        if self.depth > 0 and self.parent_id is None:
            raise ValueError("non-root section requires parent_id")
        return self


class FactStatement(_Content):
    statement_id: CanonicalId
    fact_id: CanonicalId
    #: structural anchor: the block, row or cell that states the fact
    node_id: CanonicalId
    #: intended wording; checked against alignment, never used as gold evidence
    surface: str = Field(min_length=1, max_length=4000)
    mention_ids: list[CanonicalId] = Field(default_factory=list)
    value_slots: dict[str, str] = Field(default_factory=dict)
    #: canonical sentence ids (DEC-9).  Optional until the Phase C canonicalizer
    #: allocates them; a table-row or cell statement may never carry any.
    canonical_sentence_ids: list[CanonicalId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "FactStatement":
        if len(set(self.mention_ids)) != len(self.mention_ids):
            raise ValueError("duplicate mention id on statement")
        if len(set(self.canonical_sentence_ids)) != len(self.canonical_sentence_ids):
            raise ValueError("duplicate canonical sentence id on statement")
        return self


class HeaderFooter(_Content):
    header_footer_id: CanonicalId
    position: Literal["header", "footer"]
    runs: list[InlineRun] = Field(min_length=1)
    applies_to: Literal["all", "first", "last"] = "all"


class Exhibit(_Content):
    exhibit_id: CanonicalId
    label: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=300)
    document_ref: DocumentRef | None = None


class SignatureRecord(_Content):
    signature_id: CanonicalId
    signatory: EntityRef
    capacity: str = Field(min_length=1, max_length=120)
    signed_on: IsoDate
    witness: EntityRef | None = None
    digital: bool = False


class Footnote(_Content):
    footnote_id: CanonicalId
    marker: str = Field(min_length=1, max_length=8)
    runs: list[InlineRun] = Field(min_length=1)


class CdmDocument(_Content):
    document_id: CanonicalId
    doc_kind: DocKind = "prose"
    doc_type: str = Field(min_length=1, max_length=64)
    structure_id: str = Field(min_length=1, max_length=120)
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    sections: list[Section] = Field(default_factory=list)
    statements: list[FactStatement] = Field(default_factory=list)
    headers: list[HeaderFooter] = Field(default_factory=list)
    footers: list[HeaderFooter] = Field(default_factory=list)
    exhibits: list[Exhibit] = Field(default_factory=list)
    signatures: list[SignatureRecord] = Field(default_factory=list)
    footnotes: list[Footnote] = Field(default_factory=list)
    plan_fingerprint: str = Field(min_length=1, max_length=128)

    # -- traversal ----------------------------------------------------------
    def iter_content_blocks(self) -> Iterator[Block]:
        """Blocks that carry prose content; workbooks extend this to sheet blocks."""
        for section in self.sections:
            yield from section.blocks

    def iter_blocks(self) -> Iterator[Block]:
        yield from self.iter_content_blocks()

    def iter_runs(self) -> Iterator[InlineRun]:
        for block in self.iter_content_blocks():
            yield from _block_runs(block)
        for header_footer in [*self.headers, *self.footers]:
            yield from header_footer.runs

    def iter_nodes(self) -> Iterator[tuple[str, object]]:
        """Yield ``(node_id, node)`` for every addressable content node."""
        for section in self.sections:
            yield section.section_id, section
            for block in section.blocks:
                yield block.block_id, block
                if block.table is not None:
                    yield block.table.table_id, block.table
                    for row in [*block.table.header_rows, *block.table.body_rows,
                                *block.table.total_rows]:
                        yield row.row_id, row
                        for cell in row.cells:
                            yield cell.cell_id, cell

    def node_ids(self) -> set[str]:
        return {node_id for node_id, _ in self.iter_nodes()}

    # -- validation ---------------------------------------------------------
    @model_validator(mode="after")
    def _structure(self) -> "CdmDocument":
        _validate_sections(self.sections)
        node_ids = self.node_ids()
        if len(node_ids) != sum(1 for _ in self.iter_nodes()):
            raise ValueError("duplicate node id in document")

        statement_ids = [s.statement_id for s in self.statements]
        if len(set(statement_ids)) != len(statement_ids):
            raise ValueError("duplicate statement id in document")
        for statement in self.statements:
            if statement.node_id not in node_ids:
                raise ValueError(
                    f"statement {statement.statement_id} cites unknown node {statement.node_id}")

        exhibit_ids = [e.exhibit_id for e in self.exhibits]
        if len(set(exhibit_ids)) != len(exhibit_ids):
            raise ValueError("duplicate exhibit id in document")
        signature_ids = [s.signature_id for s in self.signatures]
        if len(set(signature_ids)) != len(signature_ids):
            raise ValueError("duplicate signature id in document")

        footnote_ids = [f.footnote_id for f in self.footnotes]
        if len(set(footnote_ids)) != len(footnote_ids):
            raise ValueError("duplicate footnote id in document")
        known_footnotes = set(footnote_ids)
        for block in self.iter_content_blocks():
            for ref in block.footnote_refs or []:
                if ref not in known_footnotes:
                    raise ValueError(f"block {block.block_id} cites unknown footnote {ref}")
            for run in _block_runs(block):
                if run.footnote_id and run.footnote_id not in known_footnotes:
                    raise ValueError(
                        f"run in block {block.block_id} cites unknown footnote {run.footnote_id}")

        if self.doc_kind == "prose" and not self.sections:
            raise ValueError("prose document requires at least one section")
        return self


def _block_runs(block: Block) -> Iterator[InlineRun]:
    for run in block.runs or []:
        yield run
    for item in block.items or []:
        yield from item.runs


def _validate_sections(sections: list[Section]) -> None:
    if not sections:
        return
    by_id = {s.section_id: s for s in sections}
    if len(by_id) != len(sections):
        raise ValueError("duplicate section id in document")
    if sections[0].depth != 0:
        raise ValueError("first section must be a root (depth 0)")
    for section in sections:
        if section.parent_id is None:
            if section.depth != 0:
                raise ValueError(f"section {section.section_id} has depth but no parent")
            continue
        parent = by_id.get(section.parent_id)
        if parent is None:
            raise ValueError(f"section {section.section_id} cites unknown parent {section.parent_id}")
        if parent.depth + 1 != section.depth:
            raise ValueError(
                f"section {section.section_id} depth {section.depth} does not follow "
                f"parent depth {parent.depth}")
        # cycle guard
        seen = {section.section_id}
        cursor = parent
        while cursor.parent_id is not None:
            if cursor.section_id in seen:
                raise ValueError(f"section cycle at {cursor.section_id}")
            seen.add(cursor.section_id)
            nxt = by_id.get(cursor.parent_id)
            if nxt is None:
                break
            cursor = nxt
