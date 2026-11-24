"""TXT renderer (architecture §6.2, §7.2–7.4).

TXT is a *logical* format: ``\\f`` separates logical pages, one line is one block,
and a table row is a pipe-delimited line.  The renderer is a pure function of
``(CdmDocument, RenderProfile)``: no clock, no randomness, no model calls.

Layout rules (renderer decisions, documented because they change bytes):

* one logical page per top-level section, with the title block on the first page,
  so a page break never falls mid-section — the V2 defect §2.2 measured; an
  explicit ``page_break`` block always starts a new page;
* whole sentences are packed per line up to ``page_width_chars``; a sentence is
  never split across lines, because a line is a parser block and splitting one
  would destroy sentence addressability.  A sentence longer than the width gets
  its own line and a ``RendererWarning`` (report, not repair — §7.3 rule 7);
* headers and footers are rendered once, at the top and bottom of the document,
  so each carries exactly one span (invariant 8);
* workbooks are refused: a workbook is an XLSX document, never prose TXT (§7.7).
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from synthetic_data.canonical import (
    CanonicalDocument,
    CanonicalSentence,
    apply_statement_sentences,
    block_text,
    canonicalize,
    cell_text,
    heading_text,
    list_item_text,
    row_text,
)
from synthetic_data.renderers.context import RenderContext
from synthetic_data.model import (
    Block,
    CdmDocument,
    RenderLoc,
    RenderMap,
    RenderProfile,
    RenderResult,
    RenderSpan,
    RendererCapabilities,
    Section,
    Table,
    TableRow,
    assert_render_coverage,
)

__all__ = [
    "TxtRenderer",
    "DEFAULT_TXT_PROFILE",
    "TXT_CAPABILITIES",
    "layout_lines",
]

TXT_CAPABILITIES = RendererCapabilities(
    format="txt", paginates=True, page_kind="logical", tables=True, bbox=False,
    footnotes=True, headers_footers=True)

DEFAULT_TXT_PROFILE = RenderProfile(page_width_chars=120, page_height_lines=None,
                                    include_headers=True, include_footers=True,
                                    table_style="pipe")

_BULLET = "- "


@dataclass
class _Line:
    text: str
    page: int
    kind: str


@dataclass(frozen=True)
class _Unit:
    canonical_id: str
    kind: str
    page: int
    first_line: int
    last_line: int


class _Layout:
    """Lines per logical page, plus the render units and sentence positions."""

    def __init__(self) -> None:
        self.pages: list[list[_Line]] = [[]]
        self.page = 1
        self.units: list[_Unit] = []
        self.sentences: dict[str, tuple[int, int, int]] = {}   # page, line, offset
        self.cells: dict[str, tuple[int, int, int]] = {}        # page, line, offset
        self.warnings: list[str] = []

    # -- emission ----------------------------------------------------------
    def line(self, text: str, kind: str) -> int:
        self.pages[-1].append(_Line(text=text, page=self.page, kind=kind))
        return len(self.pages[-1]) - 1

    def blank(self) -> None:
        page = self.pages[-1]
        if page and page[-1].text != "":
            page.append(_Line(text="", page=self.page, kind="blank"))

    def new_page(self) -> None:
        page = self.pages[-1]
        while page and page[-1].kind == "blank":
            page.pop()
        self.pages.append([])
        self.page += 1

    def unit(self, canonical_id: str, kind: str, first: int, last: int) -> None:
        self.units.append(_Unit(canonical_id, kind, self.page, first, last))

    def text_for(self, page: int, first: int, last: int) -> str:
        return "\n".join(line.text for line in self.pages[page - 1][first:last + 1])

    # -- composition -------------------------------------------------------
    def compose(self) -> tuple[str, dict[tuple[int, int], int]]:
        while self.pages and self.pages[-1] and self.pages[-1][-1].text == "":
            self.pages[-1].pop()
        parts: list[str] = []
        starts: dict[tuple[int, int], int] = {}
        cursor = 0
        for page_index, page in enumerate(self.pages):
            if page_index:
                parts.append("\f")
                cursor += 1
            for line_index, line in enumerate(page):
                starts[(page_index + 1, line_index)] = cursor
                parts.append(line.text)
                cursor += len(line.text)
                if line_index != len(page) - 1:
                    parts.append("\n")
                    cursor += 1
        return "".join(parts), starts


class TxtRenderer:
    """Renderer protocol implementation for ``format == "txt"`` (§7.2)."""

    capabilities = TXT_CAPABILITIES

    def render(self, document: CdmDocument, profile: RenderProfile,
               out: str | Path,
               *, context: RenderContext | None = None) -> RenderResult:
        if document.doc_kind != "prose":
            raise ValueError(
                f"{document.document_id} is a {document.doc_kind} document; a "
                f"workbook is not rendered as prose TXT (§7.7)")
        width = profile.page_width_chars
        if width is None:
            raise ValueError("the TXT profile requires page_width_chars")

        canonical = canonicalize(document)
        document = apply_statement_sentences(document, canonical)
        layout = self._layout(document, canonical, profile, width)
        text, starts = layout.compose()
        text = unicodedata.normalize("NFC", text)

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

        spans = self._spans(document, canonical, layout, starts)
        fact_surfaces = _fact_surfaces(document, canonical, layout, context)
        render_map = RenderMap(document_id=document.document_id, format="txt",
                               spans=spans, fact_surfaces=fact_surfaces)
        expected = set(document.node_ids()) | set(canonical.sentence_ids)
        assert_render_coverage(expected, render_map)

        return RenderResult(
            format="txt", path=str(path),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            spans=spans, fact_surfaces=fact_surfaces,
            warnings=layout.warnings, capabilities=self.capabilities)

    # -- layout ------------------------------------------------------------
    def _layout(self, document: CdmDocument, canonical: CanonicalDocument,
                profile: RenderProfile, width: int) -> _Layout:
        layout = _Layout()
        if profile.include_headers:
            for header in document.headers:
                text = _runs_text(header.runs)
                if text:
                    layout.line(text, "header")
            layout.blank()
        self._title_block(layout, document, width)

        for index, section in enumerate(document.sections):
            if index and section.depth == 0:
                layout.new_page()
            self._section(layout, document, canonical, section, width)

        if document.footnotes:
            layout.blank()
            layout.line("Notes", "note_heading")
            for footnote in document.footnotes:
                body = f"[{footnote.marker}] " + _runs_text(footnote.runs)
                for line in _wrap_single(body, width, layout):
                    layout.line(line, "note_body")

        if profile.include_footers:
            for footer in document.footers:
                text = _runs_text(footer.runs)
                if text:
                    layout.blank()
                    layout.line(text, "footer")
        return layout

    def _title_block(self, layout: _Layout, document: CdmDocument,
                     width: int) -> None:
        metadata = document.metadata
        if metadata.title:
            for line in _wrap_single(metadata.title, width, layout):
                layout.line(line, "title")
        if metadata.subtitle:
            layout.line(metadata.subtitle, "subtitle")
        fields = (
            ("Reference", metadata.reference),
            ("Date", metadata.document_date.iso() if metadata.document_date else None),
            ("Jurisdiction", metadata.jurisdiction),
            ("Classification", metadata.classification),
            ("Version", metadata.version),
        )
        if metadata.thread is not None:
            fields += (("Thread", metadata.thread.thread_id),
                       ("Message", f"{metadata.thread.message_id} "
                                   f"({metadata.thread.sequence})"))
        for label, value in fields:
            if value:
                layout.line(f"{label}: {value}", "metadata")
        layout.blank()

    def _section(self, layout: _Layout, document: CdmDocument,
                 canonical: CanonicalDocument, section: Section, width: int) -> None:
        heading = heading_text(section)
        index = layout.line(heading, "heading")
        layout.unit(section.section_id, "section", index, index)
        for sentence in canonical.for_block(section.section_id):
            at = heading.find(sentence.text)
            layout.sentences[sentence.sentence_id] = (layout.page, index, max(at, 0))
        for block in section.blocks:
            self._block(layout, document, canonical, block, width)
        layout.blank()

    def _block(self, layout: _Layout, document: CdmDocument,
               canonical: CanonicalDocument, block: Block, width: int) -> None:
        first = len(layout.pages[-1])
        if block.kind == "page_break":
            layout.line("", "page_break")
            layout.unit(block.block_id, "block", first, first)
            layout.new_page()
            return

        if block.kind == "table" and block.table is not None:
            self._table(layout, block, canonical, width)
            return

        if block.kind == "list":
            sentences = [s for item in block.items or []
                         for s in canonical.for_block(item.item_id)]
            for position, item in enumerate(block.items or [], start=1):
                text = list_item_text([run.text or "" for run in item.runs])
                prefix = (f"({position}) " if block.ordered else _BULLET)
                line_index = layout.line((prefix + text).strip(), "list_item")
                if len(prefix + text) > width:
                    ids = ", ".join(s.sentence_id
                                    for s in canonical.for_block(item.item_id))
                    layout.warnings.append(
                        f"list item {item.item_id} ({ids}) exceeds the wrap width "
                        f"({len(prefix + text)} > {width}); it was kept whole")
                for sentence in canonical.for_block(item.item_id):
                    at = (prefix + text).find(sentence.text)
                    if at >= 0:
                        layout.sentences[sentence.sentence_id] = (
                            layout.page, line_index, at)
            last = len(layout.pages[-1]) - 1
            layout.unit(block.block_id, "block", first, last)
            layout.blank()
            return

        if block.kind in {"signature", "exhibit"}:
            texts = [run.text for run in block.runs or [] if run.text]
            for text in texts:
                for line in _wrap_single(text, width, layout):
                    line_index = layout.line(line, block.kind)
                    for sentence in canonical.for_block(block.block_id):
                        at = line.find(sentence.text)
                        if at >= 0:
                            layout.sentences[sentence.sentence_id] = (
                                layout.page, line_index, at)
            last = len(layout.pages[-1]) - 1
            layout.unit(block.block_id, "block", first, max(first, last))
            layout.blank()
            return

        text = block_text(block)
        if not text:
            layout.line("", block.kind)
            layout.unit(block.block_id, "block", first, first)
            return
        sentences = canonical.for_block(block.block_id)
        if sentences:
            self._emit_sentences(layout, sentences, width, block.kind)
        else:
            for line in _wrap_words(text, width):
                layout.line(line, block.kind)
        last = len(layout.pages[-1]) - 1
        layout.unit(block.block_id, "block", first, max(first, last))
        layout.blank()

    def _emit_sentences(self, layout: _Layout, sentences: list[CanonicalSentence],
                        width: int, kind: str) -> None:
        current: list[tuple[CanonicalSentence, int]] = []
        length = 0
        for sentence in sentences:
            text = sentence.text
            addition = len(text) + (1 if current else 0)
            if current and length + addition > width:
                self._emit_line(layout, current, kind)
                current, length = [], 0
                addition = len(text)
            if not current and len(text) > width:
                layout.warnings.append(
                    f"sentence {sentence.sentence_id} is {len(text)} chars and exceeds "
                    f"the wrap width {width}; it was kept whole")
            offset = length + (1 if current else 0)
            current.append((sentence, offset))
            length = offset + len(text)
        if current:
            self._emit_line(layout, current, kind)

    def _emit_line(self, layout: _Layout, pieces: list[tuple[CanonicalSentence, int]],
                   kind: str) -> None:
        text = " ".join(sentence.text for sentence, _ in pieces)
        index = layout.line(text, kind)
        for sentence, offset in pieces:
            layout.sentences[sentence.sentence_id] = (layout.page, index, offset)

    def _table(self, layout: _Layout, block: Block,
               canonical: CanonicalDocument, width: int) -> None:
        table: Table = block.table
        rows = [*table.header_rows, *table.body_rows, *table.total_rows]
        first_row_line: int | None = None
        for row in rows:
            line, cells = _render_row(row)
            index = layout.line(line, "table_row")
            if first_row_line is None:
                first_row_line = index
            if len(line) > width:
                ids = ", ".join(s.sentence_id for s in canonical.for_block(row.row_id))
                layout.warnings.append(
                    f"table row {row.row_id} ({ids}) exceeds the wrap width "
                    f"({len(line)} > {width}); it was kept whole")
            layout.unit(row.row_id, "row", index, index)
            for sentence in canonical.for_block(row.row_id):
                at = line.find(sentence.text)
                layout.sentences[sentence.sentence_id] = (layout.page, index, max(at, 0))
            for cell_id, start, end, text in cells:
                layout.units.append(_Unit(cell_id, "cell", layout.page, index, index))
                layout.cells[cell_id] = (layout.page, index, start)
        if first_row_line is not None:
            last_row_line = first_row_line + len(rows) - 1
            layout.units.append(_Unit(table.table_id, "table", layout.page,
                                      first_row_line, last_row_line))
            layout.units.append(_Unit(block.block_id, "block", layout.page,
                                      first_row_line, last_row_line))
        layout.blank()

    # -- spans -------------------------------------------------------------
    def _spans(self, document: CdmDocument, canonical: CanonicalDocument,
               layout: _Layout, starts: dict[tuple[int, int], int]) -> list[RenderSpan]:
        spans: list[RenderSpan] = []
        for unit in layout.units:
            text = layout.text_for(unit.page, unit.first_line, unit.last_line)
            start = starts[(unit.page, unit.first_line)]
            end = start + len(text)
            if unit.kind == "cell":
                page, line_index, offset = layout.cells[unit.canonical_id]
                line_text = layout.pages[page - 1][line_index].text
                cell_text_value = _cell_text_at(document, unit.canonical_id)
                start = starts[(page, line_index)] + offset
                end = start + len(cell_text_value)
                text = cell_text_value
            spans.append(RenderSpan(
                canonical_id=unit.canonical_id, kind=unit.kind, format="txt",
                loc=RenderLoc(format="txt", page=unit.page, pageless=False,
                              line_no=unit.first_line + 1,
                              char_start=start, char_end=end),
                text=text,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()))

        for sentence in canonical.sentences:
            located = layout.sentences.get(sentence.sentence_id)
            if located is None:
                continue
            page, line_index, offset = located
            start = starts[(page, line_index)] + offset
            end = start + len(sentence.text)
            spans.append(RenderSpan(
                canonical_id=sentence.sentence_id, kind="sentence", format="txt",
                loc=RenderLoc(format="txt", page=page, pageless=False,
                              line_no=line_index + 1, char_start=start, char_end=end),
                text=sentence.text,
                text_sha256=hashlib.sha256(sentence.text.encode("utf-8")).hexdigest()))
        return spans


# ---------------------------------------------------------------------------
def _runs_text(runs) -> str:
    return " ".join(run.text.strip() for run in runs or []
                    if run.text and run.text.strip())


def _wrap_single(text: str, width: int, layout: _Layout) -> list[str]:
    text = " ".join(text.split())
    if len(text) <= width:
        return [text] if text else []
    layout.warnings.append(
        f"line of {len(text)} chars exceeds the wrap width {width}; the unit was "
        f"kept intact rather than split mid-sentence")
    return [text]


def _wrap_words(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _render_row(row: TableRow) -> tuple[str, list[tuple[str, int, int, str]]]:
    """``canonical.row_text`` plus each cell's ``(cell_id, start, end, text)``."""
    line = row_text(row)
    cells: list[tuple[str, int, int, str]] = []
    cursor = 0
    for cell in row.cells:
        text = cell_text(cell)
        start = line.find(text, cursor) if text else cursor
        if start < 0:
            start = cursor
        cells.append((cell.cell_id, start, start + len(text), text))
        cursor = start + len(text) + 1
    return line, cells


def _cell_text_at(document: CdmDocument, cell_id: str) -> str:
    for section in document.sections:
        for block in section.blocks:
            if block.table is None:
                continue
            for row in [*block.table.header_rows, *block.table.body_rows,
                        *block.table.total_rows]:
                for cell in row.cells:
                    if cell.cell_id == cell_id:
                        return cell_text(cell)
    raise KeyError(cell_id)


def _fact_surfaces(document: CdmDocument, canonical: CanonicalDocument,
                   layout: _Layout,
                   context: RenderContext | None = None) -> dict[str, list[str]]:
    """Rendered surfaces per fact (architecture §7.3 rule 3).

    Value surfaces are exactly the strings this renderer wrote.  Entity *names* are
    not resolvable from a document alone, so they are added only when the caller
    supplies a :class:`RenderContext`; layout and bytes never depend on it.
    """
    by_sentence = {sentence.sentence_id: sentence for sentence in canonical.sentences}
    surfaces: dict[str, set[str]] = {}
    for statement in document.statements:
        rendered = [by_sentence[sid].text for sid in statement.canonical_sentence_ids
                    if sid in by_sentence]
        if not rendered:
            continue
        blob = " ".join(rendered)
        # only values (and names) count as surfaces; a sentence is not a value
        values = {slot for slot in statement.value_slots.values()
                  if slot and slot in blob}
        if context is not None:
            for mention in statement.mention_ids:
                name = context.entity_name(mention)
                if name and name in blob:
                    values.add(name)
                role = context.role(mention)
                if role and role in blob:
                    values.add(role)
        if values:
            surfaces.setdefault(statement.fact_id, set()).update(values)
    return {fact: sorted(values) for fact, values in surfaces.items()}


def layout_lines(document: CdmDocument,
                 profile: RenderProfile = DEFAULT_TXT_PROFILE) -> list[str]:
    """Rendered lines, for tests and fixture inspection."""
    renderer = TxtRenderer()
    canonical = canonicalize(document)
    document = apply_statement_sentences(document, canonical)
    layout = renderer._layout(document, canonical, profile, profile.page_width_chars or 100)
    return [line.text for page in layout.pages for line in page]
