"""PDF renderer (architecture §7.4, DEC-1, D7/D8).

Physical pagination with ReportLab: A4/Letter/Legal, millimetre margins, Helvetica,
drawn grid tables (with colspan/rowspan), headers and footers with ``Page n of m``,
and a ``RenderLoc`` per occurrence carrying a physical page, a real bounding box and
a character span into the page's own text buffer.

Coordinate convention (a contract, not an accident):

* PDF user space, points, origin at the **bottom-left**, y upwards — the convention
  Docling reports (``coord_origin: BOTTOMLEFT``), so renderer and parser boxes are
  directly comparable;
* ``bbox = (x0, y0, x1, y1)`` (the model's corner convention) in PDF points, always
  inside the page and always with ``x1 > x0`` and ``y1 > y0``; an occurrence with no
  drawn area (an explicit page break) carries ``bbox = null`` rather than a
  degenerate box;
* ``char_start/char_end`` index the **page text buffer**: the exact strings drawn on
  that page, in draw order, joined with ``"\\n"``.  ``page_text(page)[start:end] ==
  span.text``, so a span is reproducible rather than estimated.

Determinism: no clock, no randomness, no ``id()``-derived values; ReportLab runs with
``invariant=1`` (fixed document id and dates), standard Type-1 fonts are not
embedded, and drawing order is fixed.

Known limitation, encoded rather than hidden: Docling's native PDF pipeline produces
**no table items** (D7) — every cell is its own text passage — so row structure is
lost on ingestion even though the grid is genuinely drawn here.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib.pagesizes import A4, LEGAL, LETTER
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from synthetic_data.canonical import (
    CanonicalDocument,
    CanonicalSentence,
    apply_statement_sentences,
    block_text,
    canonicalize,
    cell_text,
    heading_text,
    list_item_text,
)
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
from synthetic_data.renderers.context import RenderContext

__all__ = ["PdfRenderer", "DEFAULT_PDF_PROFILE", "PDF_CAPABILITIES"]

PDF_CAPABILITIES = RendererCapabilities(
    format="pdf", paginates=True, page_kind="physical", tables=True, bbox=True,
    footnotes=True, headers_footers=True)

DEFAULT_PDF_PROFILE = RenderProfile(
    page_size="A4", margins_mm=(22.0, 22.0, 20.0, 20.0), font_family="Helvetica",
    include_headers=True, include_footers=True, table_style="grid")

_PAGE_SIZES = {"A4": A4, "Letter": LETTER, "Legal": LEGAL}

_BODY = 9.9
_LEADING = 12.6
_H1, _H2, _H3 = 14.0, 11.6, 10.4
_SMALL = 7.6
_TABLE = 8.2
_TABLE_LEADING = 9.8
_HEADER_BAND = 16.0 * mm
_FOOTER_BAND = 12.0 * mm

_HEAD = "Helvetica-Bold"
_BODY_FONT = "Helvetica"
_ITALIC = "Helvetica-Oblique"


# ---------------------------------------------------------------------------
@dataclass
class _Line:
    text: str
    x: float
    y: float
    font: str
    size: float


@dataclass
class _Page:
    number: int
    width: float
    height: float
    lines: list[_Line] = field(default_factory=list)
    rules: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    rects: list[tuple[float, float, float, float, tuple | None, bool]] = field(
        default_factory=list)
    texts: list[str] = field(default_factory=list)
    offsets: list[tuple[int, int]] = field(default_factory=list)
    cursor_y: float = 0.0

    def add_text(self, text: str, x: float, y: float, font: str, size: float) -> tuple[int, int]:
        start = self.offsets[-1][1] + 1 if self.offsets else 0
        self.texts.append(text)
        end = start + len(text)
        self.offsets.append((start, end))
        self.lines.append(_Line(text=text, x=x, y=y, font=font, size=size))
        return start, end

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


@dataclass(frozen=True)
class _Unit:
    canonical_id: str
    kind: str
    page: int
    bbox: tuple[float, float, float, float]
    start: int | None = None
    end: int | None = None
    extra: dict = field(default_factory=dict)


class PdfRenderer:
    """Renderer protocol implementation for ``format == "pdf"``."""

    capabilities = PDF_CAPABILITIES

    def render(self, document: CdmDocument, profile: RenderProfile,
               out: str | Path,
               *, context: RenderContext | None = None) -> RenderResult:
        result, _ = self.render_with_layout(document, profile, out, context=context)
        return result

    def render_with_layout(self, document: CdmDocument, profile: RenderProfile,
                           out: str | Path,
                           *, context: RenderContext | None = None
                           ) -> tuple[RenderResult, "_PdfLayout"]:
        """Like :meth:`render`, but also returns the layout (page text buffers).

        Tests and diagnostics need the page buffers to check that a character span
        really reproduces the drawn text; production code only needs the result.
        """
        if document.doc_kind != "prose":
            raise ValueError(
                f"{document.document_id} is a {document.doc_kind} document; a "
                f"workbook is not rendered as prose PDF (§7.7)")
        page_size = _PAGE_SIZES[(profile.page_size or "A4")]
        margins = profile.margins_mm or (22.0, 22.0, 20.0, 20.0)

        canonical = canonicalize(document)
        document = apply_statement_sentences(document, canonical)

        layout = _PdfLayout(page_size, margins, profile)
        layout.build(document, canonical)

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        layout.emit(path)

        spans = layout.spans(document, canonical)
        covered = max((span.loc.page or 1) for span in spans)
        if covered != len(layout.pages):
            layout.warnings.append(
                f"{len(layout.pages) - covered} page(s) carry no canonical span")
        fact_surfaces = _fact_surfaces(document, canonical, context)
        render_map = RenderMap(document_id=document.document_id, format="pdf",
                               spans=spans, fact_surfaces=fact_surfaces)
        assert_render_coverage(set(document.node_ids()) | set(canonical.sentence_ids),
                               render_map)

        payload = path.read_bytes()
        result = RenderResult(
            format="pdf", path=str(path), sha256=hashlib.sha256(payload).hexdigest(),
            spans=spans, fact_surfaces=fact_surfaces, warnings=layout.warnings,
            capabilities=self.capabilities)
        return result, layout


# ---------------------------------------------------------------------------
class _PdfLayout:
    def __init__(self, page_size, margins_mm, profile: RenderProfile) -> None:
        self.page_w, self.page_h = page_size
        left, top, right, bottom = (value * mm for value in margins_mm)
        self.margins = (left, top, right, bottom)
        self.left = left
        self.right = self.page_w - right
        self.top = self.page_h - top - (_HEADER_BAND if profile.include_headers else 0.0)
        self.bottom = bottom + (_FOOTER_BAND if profile.include_footers else 0.0)
        self.profile = profile
        self.pages: list[_Page] = []
        self.units: list[_Unit] = []
        self.sentence_slots: dict[str, tuple[int, int, int]] = {}
        self.warnings: list[str] = []
        self.counters = {"section": 0, "table": 0}
        self.title = ""
        self.reference = ""
        self.classification = ""
        self.header_label = ""
        self._new_page()

    # -- pages -------------------------------------------------------------
    def _new_page(self) -> None:
        page = _Page(number=len(self.pages) + 1, width=self.page_w, height=self.page_h)
        page.cursor_y = self.top
        self.pages.append(page)

    @property
    def page(self) -> _Page:
        return self.pages[-1]

    def _space(self, height: float) -> None:
        if self.page.cursor_y - height < self.bottom and self.page.lines:
            self._new_page()

    def _break(self) -> None:
        if self.page.lines:
            self._new_page()

    # -- text --------------------------------------------------------------
    def _wrap(self, text: str, width: float, font: str, size: float) -> list[str]:
        words = text.split()
        if not words:
            return []
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or page_text_size(candidate, font, size) <= width:
                if not current and page_text_size(word, font, size) > width:
                    self.warnings.append(
                        f"token {word[:24]!r} is wider than its column; split across lines")
                    chunk = ""
                    for char in word:
                        if chunk and page_text_size(chunk + char, font, size) > width:
                            lines.append(chunk)
                            chunk = char
                        else:
                            chunk += char
                    current = chunk
                    continue
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def _draw_lines(self, text: str, font: str, size: float, leading: float,
                    indent: float = 0.0,
                    sentences: list[CanonicalSentence] | None = None
                    ) -> tuple[float, float, int | None, int | None]:
        """Draw a wrapped block, paginating line by line, and map its sentences.

        A block taller than one page used to be drawn past the bottom margin: the
        PDF still held the bytes but the page did not, so the parser never saw the
        tail and the accounting called it unexplained.  Lines now flow onto the next
        page, the way a typesetter would set them, and a sentence that straddles the
        break keeps a span on the page where it starts (the parser sees the two
        fragments and the aligner records them as a split).
        """
        width = self.right - self.left - indent
        lines = self._wrap(text, width, font, size)
        if not lines:
            return self.page.cursor_y, 0.0, None, None
        source_starts: list[int] = []
        cursor = 0
        for line in lines:
            found = text.find(line, cursor)
            source_starts.append(found if found >= 0 else cursor)
            cursor = source_starts[-1] + len(line)

        capacity = self.top - self.bottom
        self._space(min(leading * len(lines), capacity))
        start_page = self.page.number
        start_cursor = self.page.cursor_y
        drawn: list[tuple[int, int, int]] = []      # page, buffer start, buffer end
        for line in lines:
            if self.page.cursor_y - leading < self.bottom and self.page.lines:
                self._new_page()
            page = self.page
            y = page.cursor_y - leading + size * 0.30
            start, end = page.add_text(line, self.left + indent, y, font, size)
            page.cursor_y -= leading
            drawn.append((page.number, start, end))

        first_page_lines = [index for index, item in enumerate(drawn)
                            if item[0] == start_page]
        first_start = drawn[first_page_lines[0]][1] if first_page_lines else None
        first_end = drawn[first_page_lines[-1]][2] if first_page_lines else None
        height = start_cursor - self.pages[start_page - 1].cursor_y
        self._last_block_page = drawn[0][0] if drawn else start_page

        if sentences:
            for sentence in sentences:
                first = _line_for(source_starts, lines, sentence.char_start)
                last = _line_for(source_starts, lines,
                                 max(sentence.char_start, sentence.char_end - 1))
                if first is None or last is None:
                    self.warnings.append(
                        f"sentence {sentence.sentence_id} could not be mapped to "
                        f"drawn lines")
                    continue
                page_no = drawn[first][0]
                on_page = [index for index in range(first, last + 1)
                           if drawn[index][0] == page_no]
                if not on_page:
                    continue
                start = drawn[first][1] + max(0, sentence.char_start
                                              - source_starts[first])
                end = drawn[first][1] + max(0, sentence.char_end
                                            - source_starts[first])
                end = min(end, drawn[on_page[-1]][2])
                start = max(drawn[first][1], min(start, end - 1))
                self.sentence_slots[sentence.sentence_id] = (page_no, start, end)
        return start_cursor, height, first_start, first_end

    # -- document ----------------------------------------------------------
    def build(self, document: CdmDocument, canonical: CanonicalDocument) -> None:
        self.title = document.metadata.title or document.doc_type.replace("_", " ").title()
        self.reference = document.metadata.reference or document.document_id
        self.classification = document.metadata.classification or ""
        label = document.doc_type.replace("_", " ").title()
        self.header_label = (f"{label} — {self.classification}" if self.classification
                             else label)
        self._footnote_marks = {footnote.footnote_id: footnote.marker
                                for footnote in document.footnotes}
        self._title_block(document)
        for index, section in enumerate(document.sections):
            if index and section.depth == 0:
                self._break()
            self._section(canonical, section)
        if document.footnotes:
            # notes follow the last section in the flow: a page that holds only notes
            # would carry no canonical span, and a page nobody can point at is not a page
            self._space(40.0)
            top = self.page.cursor_y
            self._draw_lines("Notes", _HEAD, _H2, _H2 * 1.4)
            self.units.append(_Unit("notes", "block", self.page.number,
                                    (self.left, self.page.cursor_y, self.right - self.left,
                                     top - self.page.cursor_y)))
            for footnote in document.footnotes:
                body = f"[{footnote.marker}] " + " ".join(
                    run.text for run in footnote.runs if run.text)
                # a footnote is not an addressable node: it is drawn, not spanned
                self._draw_lines(body, _BODY_FONT, _SMALL, _SMALL * 1.5, indent=8.0)
        self._headers_and_footers()

    def _title_block(self, document: CdmDocument) -> None:
        metadata = document.metadata
        page = self.page
        page.cursor_y -= 6.0
        body = " ".join(
            [heading_text(section) for section in document.sections]
            + [block_text(block) for section in document.sections
               for block in section.blocks if block.kind == "paragraph"])
        title_echoed = bool(metadata.title) and metadata.title in body
        if metadata.title and not title_echoed:
            for line in self._wrap(metadata.title, self.right - self.left, _HEAD, 17.0):
                page.add_text(line, self.left, page.cursor_y - 15.0, _HEAD, 17.0)
                page.cursor_y -= 23.0
        elif title_echoed:
            # printing the same sentence twice on page 1 would make the parser's copy
            # ambiguous as evidence; the body states it, so the banner does not
            self.warnings.append(
                "document title is not drawn separately: the first paragraph states it")
        if metadata.subtitle:
            for line in self._wrap(metadata.subtitle, self.right - self.left, _ITALIC, 10.5):
                page.add_text(line, self.left, page.cursor_y - 10.0, _ITALIC, 10.5)
                page.cursor_y -= 14.0
        fields = (
            ("Reference", metadata.reference),
            ("Date", metadata.document_date.iso() if metadata.document_date else None),
            ("Jurisdiction", metadata.jurisdiction),
            ("Classification", metadata.classification),
            ("Version", metadata.version),
        )
        if metadata.thread is not None:
            fields += (("Thread", metadata.thread.thread_id),
                       ("Message", f"{metadata.thread.message_id} ({metadata.thread.sequence})"))
        page.cursor_y -= 8.0
        for label, value in fields:
            if not value:
                continue
            page.add_text(f"{label}: {value}", self.left, page.cursor_y - 9.0, _BODY_FONT, 9.0)
            page.cursor_y -= 12.4
        page.cursor_y -= 14.0

    def _section(self, canonical: CanonicalDocument, section: Section) -> None:
        size = {0: _H1, 1: _H2}.get(min(section.depth, 2), _H3)
        heading = heading_text(section)
        lines = self._wrap(heading, self.right - self.left, _HEAD, size)
        self._space(len(lines) * size * 1.4 + 12.0)
        page_no = self.page.number
        top = self.page.cursor_y
        first_index = len(self.page.texts)
        y = top - size
        for line in lines:
            self.page.add_text(line, self.left, y, _HEAD, size)
            y -= size * 1.4
        start = self.page.offsets[first_index][0]
        end = self.page.offsets[-1][1]
        index = self.counters["section"]
        self.counters["section"] += 1
        self.page.cursor_y = y - 7.0
        self.units.append(_Unit(section.section_id, "section", page_no,
                                (self.left, y + size * 0.3, self.right - self.left,
                                 top - (y + size * 0.3)), start, end,
                                {"section_index": index}))
        for sentence in canonical.for_block(section.section_id):
            column = heading.find(sentence.text)
            self.sentence_slots[sentence.sentence_id] = (
                page_no, start + max(column, 0), start + max(column, 0) + len(sentence.text))
        for block in section.blocks:
            self._block(canonical, block)

    def _block(self, canonical: CanonicalDocument, block: Block) -> None:
        if block.kind == "page_break":
            self.units.append(_Unit(block.block_id, "block", self.page.number,
                                    (self.left, self.page.cursor_y, 0.0, 0.0)))
            self._break()
            return
        if block.kind == "table" and block.table is not None:
            self._table(canonical, block)
            return
        if block.kind == "list":
            self._list(canonical, block)
            return
        if block.kind in {"signature", "exhibit"}:
            self._signature_or_exhibit(canonical, block)
            return
        text = block_text(block)
        if not text:
            return
        sentences = canonical.for_block(block.block_id)
        indent = 8.0 if block.kind == "note" else 0.0
        page_no = self.page.number
        top, height, start, end = self._draw_lines(text, _BODY_FONT, _BODY, _LEADING,
                                                   indent=indent, sentences=sentences)
        self._append_footnote_markers(block, getattr(self, "_last_block_page", page_no))
        self.page.cursor_y -= 5.0
        self.units.append(_Unit(block.block_id, "block", page_no,
                                (self.left, top - height, self.right - self.left, height),
                                start, end))

    def _append_footnote_markers(self, block: Block, page_no: int) -> None:
        marks = getattr(self, "_footnote_marks", None)
        if not block.footnote_refs or not marks:
            return
        suffix = " ".join(f"[{marks[ref]}]" for ref in block.footnote_refs if ref in marks)
        page = self.pages[page_no - 1]
        if suffix and page.texts:
            page.texts[-1] = f"{page.texts[-1]} {suffix}"
            page.lines[-1].text = page.texts[-1]
            page.offsets[-1] = (page.offsets[-1][0],
                                page.offsets[-1][0] + len(page.texts[-1]))

    def _list(self, canonical: CanonicalDocument, block: Block) -> None:
        block_top = self.page.cursor_y
        block_page = self.page.number
        block_start: int | None = None
        # canonical list-item sentence offsets are block-relative (canonical.py
        # projects the whole list into one text); the renderer works per item, so
        # each item's base offset has to be subtracted.  Without this a long item
        # that wraps onto a second line mapped past the end of its own text and its
        # sentence was silently dropped from the render map.
        item_base = 0
        for position, item in enumerate(block.items or [], start=1):
            text = list_item_text([run.text or "" for run in item.runs])
            prefix = f"({position})" if block.ordered else "•"
            lines = self._wrap(text, self.right - self.left - 22.0, _BODY_FONT, _BODY)
            self._space(len(lines) * _LEADING + 2.0)
            page_no = self.page.number
            top = self.page.cursor_y
            y = top - _LEADING + _BODY * 0.30
            self.page.add_text(prefix, self.left + 2.0, y, _BODY_FONT, _BODY)
            first_index = len(self.page.texts)
            source_starts: list[int] = []
            cursor = 0
            for line in lines:
                found = text.find(line, cursor)
                source_starts.append(found if found >= 0 else cursor)
                cursor = source_starts[-1] + len(line)
                self.page.add_text(line, self.left + 20.0, y, _BODY_FONT, _BODY)
                y -= _LEADING
            self.page.cursor_y = top - len(lines) * _LEADING - 2.0
            buffer_starts = [self.page.offsets[first_index + index][0]
                             for index in range(len(lines))]
            for sentence in canonical.for_block(item.item_id):
                relative_start = min(max(sentence.char_start - item_base, 0), len(text))
                relative_end = min(max(sentence.char_end - item_base, relative_start),
                                   len(text))
                first = _line_for(source_starts, lines, relative_start)
                last = _line_for(source_starts, lines,
                                 max(relative_start, relative_end - 1))
                if first is None or last is None:
                    continue
                start = buffer_starts[first] + (relative_start - source_starts[first])
                end = buffer_starts[last] + (relative_end - source_starts[last])
                start = max(0, min(start, len(self.page.text)))
                end = min(max(end, start), len(self.page.text))
                if end <= start:
                    continue
                self.sentence_slots[sentence.sentence_id] = (page_no, start, end)
            if block_start is None:
                block_start = self.page.offsets[first_index][0]
            item_base += len(text) + 1
        self.units.append(_Unit(block.block_id, "block", block_page,
                                (self.left, self.page.cursor_y, self.right - self.left,
                                 block_top - self.page.cursor_y),
                                block_start, self.page.offsets[-1][1]))

    def _signature_or_exhibit(self, canonical: CanonicalDocument, block: Block) -> None:
        texts = [run.text for run in block.runs or [] if run.text]
        if not texts:
            return
        self._space(64.0 if block.kind == "signature" else 22.0)
        page_no = self.page.number
        top = self.page.cursor_y
        first_index = len(self.page.texts)
        y = top - _LEADING
        if block.kind == "signature":
            self.page.add_text(texts[0], self.left, y, _ITALIC, _BODY)
            y -= 26.0
            self.page.rules.append((self.left, y, self.left + 190.0, y, 0.7))
            y -= 11.0
            self.page.add_text("Signature", self.left, y, _BODY_FONT, _SMALL)
            y -= 13.0
            for extra in texts[1:]:
                self.page.add_text(extra, self.left, y, _BODY_FONT, _SMALL)
                y -= 11.0
        else:
            for text in texts:
                for line in self._wrap(text, self.right - self.left, _ITALIC, _BODY):
                    self.page.add_text(line, self.left, y, _ITALIC, _BODY)
                    y -= _LEADING
        self.page.cursor_y = y - 12.0
        start = self.page.offsets[first_index][0]
        end = self.page.offsets[-1][1]
        self.units.append(_Unit(block.block_id, "block", page_no,
                                (self.left, y, self.right - self.left, top - y), start, end))
        for sentence in canonical.for_block(block.block_id):
            found = _locate_in_page(self.pages[page_no - 1], sentence.text)
            if found is not None:
                self.sentence_slots[sentence.sentence_id] = (page_no, *found)

    # -- tables ------------------------------------------------------------
    def _table(self, canonical: CanonicalDocument, block: Block) -> None:
        table: Table = block.table
        rows = [*table.header_rows, *table.body_rows, *table.total_rows]
        ncols = len(table.columns)
        self._table_size = _fit_table_size(rows, ncols, self.right - self.left)
        widths = self._column_widths(table, rows, ncols)
        heights = [self._row_height(row, widths) for row in rows]
        header_count = len(table.header_rows)
        index = self.counters["table"]
        self.counters["table"] += 1

        if table.caption:
            self._space(16.0)
            for line in self._wrap(table.caption, self.right - self.left, _ITALIC, 8.6):
                self.page.add_text(line, self.left, self.page.cursor_y - 9.0, _ITALIC, 8.6)
                self.page.cursor_y -= 11.0
            self.page.cursor_y -= 3.0

        if sum(heights) > self.page.cursor_y - self.bottom:
            self._break()
        if sum(heights) > self.page.cursor_y - self.bottom:
            self.warnings.append(
                f"table {table.table_id} is taller than one page; split across pages")

        xs = [self.left]
        for width in widths:
            xs.append(xs[-1] + width)

        r = 0
        first_segment = True
        while r < len(rows):
            space = self.page.cursor_y - self.bottom
            fit = r
            used = 0.0
            while fit < len(rows) and used + heights[fit] <= space:
                used += heights[fit]
                fit += 1
            if fit == r:
                if self.page.lines:
                    self._break()
                    continue
                fit = r + 1
                self.warnings.append(f"row {rows[r].row_id} is taller than the page")
            page_no = self.page.number
            top = self.page.cursor_y
            first_index = len(self.page.texts)
            if not first_segment:
                self._draw_row_group(rows[:header_count], heights, xs, page_no,
                                     record=False, offset=0)
            self._draw_row_group(rows[r:fit], heights, xs, page_no, record=True,
                                 offset=r, canonical=canonical)
            bottom = self.page.cursor_y
            if first_segment:
                box = (self.left, bottom, self.right - self.left, top - bottom)
                self.units.append(_Unit(block.block_id, "block", page_no, box,
                                        self.page.offsets[first_index][0],
                                        self.page.offsets[-1][1], {"table_index": index}))
                self.units.append(_Unit(table.table_id, "table", page_no, box,
                                        self.page.offsets[first_index][0],
                                        self.page.offsets[-1][1], {"table_index": index}))
                first_segment = False
            self.page.cursor_y -= 8.0
            r = fit
            if r < len(rows):
                self._break()

    def _draw_row_group(self, rows: list[TableRow], heights: list[float], xs: list[float],
                        page_no: int, *, record: bool, offset: int,
                        canonical: CanonicalDocument | None = None) -> None:
        covered = self._rowspan_coverage(rows, offset)
        for position, row in enumerate(rows, start=offset):
            height = heights[position]
            y = self.page.cursor_y - height
            col = 0
            first_cell: int | None = None
            last_end: int | None = None
            for cell in row.cells:
                colspan = cell.colspan or 1
                rowspan = cell.rowspan or 1
                x0 = xs[col]
                x1 = xs[min(col + colspan, len(xs) - 1)]
                span = rowspan
                cell_height = sum(heights[position:position + span]) if span > 1 else height
                covered_here = covered.get((position, col))
                if covered_here is not None and covered_here[0] != position:
                    # a placeholder under another cell's rowspan: give it its own band
                    # as a location, but do not draw a second box or duplicate text
                    if record:
                        self.units.append(_Unit(
                            cell.cell_id, "cell", page_no,
                            (x0, y, x1 - x0, height), None, None,
                            {"row": position + 1, "column": col + 1,
                             "table_index": self.counters["table"] - 1,
                             "cell_ref": f"r{position:02d}c{col:02d}"}))
                    col += colspan
                    continue
                text = cell_text(cell)
                size = getattr(self, "_table_size", _TABLE)
                lines = self._wrap(text, max(x1 - x0 - 6.0, 18.0), _BODY_FONT, size)
                first_index = len(self.page.texts)
                line_y = y + cell_height - size - 2.4
                for line in lines:
                    self.page.add_text(line, x0 + 3.0, line_y, _BODY_FONT, size)
                    line_y -= _TABLE_LEADING
                start = (self.page.offsets[first_index][0]
                         if lines and first_index < len(self.page.offsets) else None)
                end = self.page.offsets[-1][1] if lines else None
                if start is not None and first_cell is None:
                    first_cell = start
                if end is not None:
                    last_end = end
                if record:
                    self.units.append(_Unit(
                        cell.cell_id, "cell", page_no, (x0, y, x1 - x0, cell_height),
                        start, end,
                        {"row": position + 1, "column": col + 1, "table_index": self.counters["table"] - 1,
                         "cell_ref": f"r{position:02d}c{col:02d}"}))
                self.page.rects.append((x0, y, x1 - x0, cell_height,
                                        (0.93, 0.93, 0.93) if position < 1 else None, True))
                col += colspan
            if record:
                self.units.append(_Unit(row.row_id, "row", page_no,
                                        (self.left, y, self.right - self.left, height),
                                        first_cell, last_end,
                                        {"row": position + 1,
                                         "table_index": self.counters["table"] - 1}))
                if canonical is not None and first_cell is not None and last_end is not None:
                    for sentence in canonical.for_block(row.row_id):
                        self.sentence_slots[sentence.sentence_id] = (
                            page_no, first_cell, last_end)
            self.page.cursor_y = y

    @staticmethod
    def _rowspan_coverage(rows: list[TableRow], offset: int) -> dict[tuple[int, int], tuple[int, int]]:
        """``(row_index, column) -> (origin_row, origin_column)`` for rowspans."""
        covered: dict[tuple[int, int], tuple[int, int]] = {}
        for index, row in enumerate(rows, start=offset):
            col = 0
            for cell in row.cells:
                colspan = cell.colspan or 1
                rowspan = cell.rowspan or 1
                if rowspan > 1:
                    for extra in range(1, rowspan):
                        for span_col in range(col, col + colspan):
                            covered[(index + extra, span_col)] = (index, col)
                col += colspan
        return covered

    def _column_widths(self, table: Table, rows: list[TableRow], ncols: int) -> list[float]:
        """Column widths that never split a word unless the page cannot hold it.

        Each column gets at least its widest *word* (so "Outcome" is never drawn as
        "Outco me"), then the spare width is shared in proportion to how much more
        each column would like.
        """
        available = self.right - self.left
        size = getattr(self, "_table_size", _TABLE)
        minimum = [10.0] * ncols
        desired = [10.0] * ncols
        for row in rows:
            col = 0
            for cell in row.cells:
                span = cell.colspan or 1
                text = cell_text(cell)
                if span == 1 and text:
                    widest = max((page_text_size(word, _BODY_FONT, size)
                                  for word in text.split()), default=0.0)
                    minimum[col] = max(minimum[col], widest + 9.0)
                    desired[col] = max(desired[col], page_text_size(text, _BODY_FONT, size) + 10.0)
                col += span
        desired = [max(desired[i], minimum[i]) for i in range(ncols)]
        spare = available - sum(minimum)
        if spare <= 0:
            total = sum(minimum)
            return [width * available / total for width in minimum]
        wants = [desired[i] - minimum[i] for i in range(ncols)]
        total_wants = sum(wants)
        if total_wants <= 0:
            return [width + spare / ncols for width in minimum]
        return [minimum[i] + spare * wants[i] / total_wants for i in range(ncols)]

    def _row_height(self, row: TableRow, widths: list[float]) -> float:
        size = getattr(self, "_table_size", _TABLE)
        height = _TABLE_LEADING + 6.0
        col = 0
        for cell in row.cells:
            span = cell.colspan or 1
            width = sum(widths[col:col + span]) - 6.0
            lines = self._wrap(cell_text(cell), max(width, 18.0), _BODY_FONT, size)
            height = max(height, len(lines) * _TABLE_LEADING + 6.0)
            col += span
        return height

    # -- headers and footers ----------------------------------------------
    def _headers_and_footers(self) -> None:
        total = len(self.pages)
        for page in self.pages:
            if self.profile.include_headers and self.title:
                # the running head names the document *kind*, not its title: a header
                # that repeats a body sentence makes that sentence ambiguous evidence
                # the moment the parser keeps both copies
                header = self.header_label
                page.add_text(header[:110], self.left, self.top + _HEADER_BAND * 0.55,
                              _BODY_FONT, 7.6)
                page.rules.append((self.left, self.top + _HEADER_BAND * 0.34,
                                   self.right, self.top + _HEADER_BAND * 0.34, 0.4))
            if self.profile.include_footers:
                label = f"Page {page.number} of {total}"
                page.add_text(label, self.left, self.margins[3] * 0.60, _BODY_FONT, 7.6)
                if self.reference:
                    clipped = self.reference[:90]
                    page.add_text(clipped,
                                  self.right - page_text_size(clipped, _BODY_FONT, 7.6),
                                  self.margins[3] * 0.60, _BODY_FONT, 7.6)
                page.rules.append((self.left, self.margins[3] * 0.60 + 10.0, self.right,
                                   self.margins[3] * 0.60 + 10.0, 0.4))

    # -- emit --------------------------------------------------------------
    def emit(self, path: Path) -> None:
        c = canvas.Canvas(str(path), pagesize=(self.page_w, self.page_h), invariant=1)
        c.setTitle(self.title)
        c.setAuthor("Synthetic Data Generator")
        c.setCreator("synthetic_data.renderers.pdf")
        c.setProducer("Synthetic Data Generator")
        c.setSubject(self.reference)
        for page in self.pages:
            for x0, y0, w, h, fill, stroke in page.rects:
                if fill:
                    c.setFillColorRGB(*fill)
                    c.rect(x0, y0, w, h, stroke=1, fill=1)
                    c.setFillColorRGB(0, 0, 0)
                else:
                    c.rect(x0, y0, w, h, stroke=1, fill=0)
            for x0, y0, x1, y1, width in page.rules:
                c.setLineWidth(width)
                c.line(x0, y0, x1, y1)
            for line in page.lines:
                c.setFont(line.font, line.size)
                c.drawString(line.x, line.y, line.text)
            c.showPage()
        c.save()

    # -- provenance --------------------------------------------------------
    def spans(self, document: CdmDocument, canonical: CanonicalDocument) -> list[RenderSpan]:
        spans: list[RenderSpan] = []
        for unit in self.units:
            if not unit.canonical_id or unit.canonical_id == "notes":
                continue
            buffer = self.pages[unit.page - 1].text
            start = None if unit.start is None else max(0, min(unit.start, len(buffer)))
            end = None if unit.end is None else max(0, min(unit.end, len(buffer)))
            has_text = start is not None and end is not None and end > start
            text = buffer[start:end] if has_text else ""
            spans.append(RenderSpan(
                canonical_id=unit.canonical_id, kind=unit.kind, format="pdf",
                loc=RenderLoc(format="pdf", page=unit.page, pageless=False,
                              bbox=_clean_box(unit.bbox),
                              char_start=start if has_text else None,
                              char_end=end if has_text else None,
                              section_index=unit.extra.get("section_index"),
                              table_index=unit.extra.get("table_index"),
                              row=unit.extra.get("row"), column=unit.extra.get("column"),
                              cell_ref=unit.extra.get("cell_ref")),
                text=text, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()))
        for sentence in canonical.sentences:
            slot = self.sentence_slots.get(sentence.sentence_id)
            if slot is None:
                continue
            page, start, end = slot
            text = self.pages[page - 1].text[start:end] if end > start else ""
            spans.append(RenderSpan(
                canonical_id=sentence.sentence_id, kind="sentence", format="pdf",
                loc=RenderLoc(format="pdf", page=page, pageless=False,
                              char_start=start if end > start else None,
                              char_end=end if end > start else None),
                text=text, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()))
        return spans


# ---------------------------------------------------------------------------
def _fit_table_size(rows: list[TableRow], ncols: int, available: float) -> float:
    """Largest table font whose widest-word minimum still fits the measure."""
    for size in (_TABLE, _TABLE - 0.6, _TABLE - 1.2, _TABLE - 1.8, 6.0):
        minimum = [10.0] * ncols
        for row in rows:
            col = 0
            for cell in row.cells:
                span = cell.colspan or 1
                text = cell_text(cell)
                if span == 1 and text:
                    widest = max((page_text_size(word, _BODY_FONT, size)
                                  for word in text.split()), default=0.0)
                    minimum[col] = max(minimum[col], widest + 9.0)
                col += span
        if sum(minimum) <= available:
            return size
    return 6.0


def page_text_size(text: str, font: str = _BODY_FONT, size: float = _BODY) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def _line_for(starts: list[int], lines: list[str], offset: int) -> int | None:
    for index, start in enumerate(starts):
        if start <= offset < start + len(lines[index]):
            return index
    if starts and offset >= starts[-1]:
        return len(starts) - 1
    return None


def _locate_in_page(page: _Page, needle: str) -> tuple[int, int] | None:
    for start, end in reversed(page.offsets):
        if len(needle) <= end - start and page.text[start:end + 1].startswith(needle):
            return start, start + len(needle)
    needle = " ".join(needle.split())
    for start, end in reversed(page.offsets):
        column = page.text[start:end].find(needle)
        if column >= 0:
            return start + column, start + column + len(needle)
    return None


def _clean_box(box: tuple[float, float, float, float]
               ) -> tuple[float, float, float, float] | None:
    """Convert an internal ``(x, y, w, h)`` rectangle to the model's corners.

    The model stores ``(x0, y0, x1, y1)``; a degenerate rectangle is ``None``
    rather than a zero-area box that pretends to be a location.
    """
    x, y, w, h = box
    if w <= 0.0 or h <= 0.0:
        return None
    return (round(x, 3), round(y, 3), round(x + w, 3), round(y + h, 3))


def _fact_surfaces(document: CdmDocument, canonical: CanonicalDocument,
                   context: RenderContext | None) -> dict[str, list[str]]:
    by_sentence = {sentence.sentence_id: sentence for sentence in canonical.sentences}
    surfaces: dict[str, set[str]] = {}
    for statement in document.statements:
        rendered = [by_sentence[sid].text for sid in statement.canonical_sentence_ids
                    if sid in by_sentence]
        if not rendered:
            continue
        blob = " ".join(rendered)
        values = {slot for slot in statement.value_slots.values() if slot and slot in blob}
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
