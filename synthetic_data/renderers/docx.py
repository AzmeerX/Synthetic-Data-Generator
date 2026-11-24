"""DOCX renderer (architecture §7.4, D4/D11, Phase E).

A real OOXML package written with ``python-docx``: a ``Title``/``Heading N`` style
hierarchy, ordinary paragraphs, Word list styles, ``Table Grid`` tables with genuine
``gridSpan``/``vMerge`` merges, a header and footer, a notes section and fixed core
properties.  No HTML and no text file renamed to ``.docx``.

Pagination (D11, §7.4)
    DOCX has no physical pagination and the tested ingestion path does not supply
    one, so every ``RenderLoc`` is ``pageless=True`` with ``page=None`` and
    ``bbox=None``.  Location is expressed structurally instead: ``section_index``,
    ``paragraph_index`` (the index in ``Document.paragraphs``), ``table_index``,
    ``row``/``column``/``cell_ref``.  The parser will report one synthetic page for
    the file; that number comes from the parser and is never copied into the map.

Determinism
    ``python-docx`` stamps ZIP members with the current local time and fills
    ``docProps/core.xml`` with ``datetime.now()``, so two saves of the same
    document differ in bytes.  The renderer writes fixed core properties and then
    repackages the archive deterministically (sorted members, fixed timestamp,
    fixed attributes, fixed compression).  The normaliser only rewrites container
    metadata — every XML part is copied byte for byte, so the OOXML is intact.

Known ingestion limitations, encoded rather than hidden
    * D4: the parser may split ``Label: value`` into two items, so a field-level
      check must not assume one parser block per field.
    * A vertically merged cell is reported once, at its starting row, so the rows it
      covers arrive with fewer cells than the model declares.
    * Footnote markers are text (``[1]``) plus a notes section, not real
      ``footnotes.xml`` entries; the renderer declares ``footnotes=False``.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from synthetic_data.canonical import (
    CanonicalDocument,
    apply_statement_sentences,
    block_text,
    canonicalize,
    cell_text,
    heading_text,
    list_item_text,
    row_text,
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
    assert_render_coverage,
)
from synthetic_data.renderers.context import RenderContext
from synthetic_data.renderers.factsurfaces import fact_surfaces

__all__ = ["DocxRenderer", "DEFAULT_DOCX_PROFILE", "DOCX_CAPABILITIES", "normalise_package"]

DOCX_CAPABILITIES = RendererCapabilities(
    format="docx", paginates=False, page_kind="none", tables=True, bbox=False,
    footnotes=False, headers_footers=True)

DEFAULT_DOCX_PROFILE = RenderProfile(
    page_size="A4", margins_mm=(25.4, 25.4, 25.4, 25.4), font_family="Calibri",
    include_headers=True, include_footers=True, table_style="Table Grid")

_PAGE_CM = {"A4": (21.0, 29.7), "Letter": (21.59, 27.94), "Legal": (21.59, 35.56)}
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_FIXED_MOMENT = datetime(2000, 1, 1, 0, 0, 0)


@dataclass
class _Unit:
    canonical_id: str
    kind: str
    text: str
    section_index: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row: int | None = None
    column: int | None = None
    cell_ref: str | None = None


class DocxRenderer:
    """Renderer protocol implementation for ``format == "docx"``."""

    capabilities = DOCX_CAPABILITIES

    def render(self, document: CdmDocument, profile: RenderProfile,
               out: str | Path,
               *, context: RenderContext | None = None) -> RenderResult:
        if document.doc_kind != "prose":
            raise ValueError(
                f"{document.document_id} is a {document.doc_kind} document; a workbook "
                f"is not rendered as prose DOCX (§7.7)")

        canonical = canonicalize(document)
        document = apply_statement_sentences(document, canonical)

        builder = _DocxBuilder(document, canonical, profile)
        builder.build()

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = path.with_suffix(path.suffix + ".raw")
        builder.doc.save(str(raw))
        normalise_package(raw, path)
        raw.unlink(missing_ok=True)

        spans = builder.spans()
        facts = fact_surfaces(document, canonical, context)
        render_map = RenderMap(document_id=document.document_id, format="docx",
                               spans=spans, fact_surfaces=facts)
        assert_render_coverage(set(document.node_ids()) | set(canonical.sentence_ids),
                               render_map)

        payload = path.read_bytes()
        return RenderResult(
            format="docx", path=str(path), sha256=hashlib.sha256(payload).hexdigest(),
            spans=spans, fact_surfaces=facts, warnings=builder.warnings,
            capabilities=self.capabilities)


# ---------------------------------------------------------------------------
class _DocxBuilder:
    def __init__(self, document: CdmDocument, canonical: CanonicalDocument,
                 profile: RenderProfile) -> None:
        self.document = document
        self.canonical = canonical
        self.profile = profile
        self.doc = Document()
        self.units: list[_Unit] = []
        self.warnings: list[str] = []
        self.section_index = -1
        self.table_index = 0
        self.footnote_marks = {footnote.footnote_id: footnote.marker
                               for footnote in document.footnotes}

    # -- setup -------------------------------------------------------------
    def _setup(self) -> None:
        font = self.profile.font_family or "Calibri"
        width, height = _PAGE_CM[self.profile.page_size or "A4"]
        left, top, right, bottom = (value / 10.0 for value in
                                    (self.profile.margins_mm or (25.4, 25.4, 25.4, 25.4)))
        section = self.doc.sections[0]
        section.page_width = Cm(width)
        section.page_height = Cm(height)
        section.left_margin, section.top_margin = Cm(left), Cm(top)
        section.right_margin, section.bottom_margin = Cm(right), Cm(bottom)
        section.header_distance, section.footer_distance = Cm(1.25), Cm(1.25)

        normal = self.doc.styles["Normal"]
        normal.font.name = font
        normal.font.size = Pt(10.5)
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), font)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.15

        for name, size in (("Title", 22), ("Heading 1", 15), ("Heading 2", 12.5),
                           ("Heading 3", 11)):
            style = self.doc.styles[name]
            style.font.name = font
            style.font.size = Pt(size)
            style.font.bold = True
            style.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
            style.paragraph_format.space_after = Pt(6)

        properties = self.doc.core_properties
        metadata = self.document.metadata
        properties.title = metadata.title or self.document.title
        properties.subject = (f"{self.document.doc_type.replace('_', ' ').title()} "
                              f"{metadata.reference or ''}").strip()
        properties.author = "Synthetic Data Generator"
        properties.last_modified_by = "Synthetic Data Generator"
        properties.comments = ""
        properties.keywords = metadata.classification or ""
        properties.category = self.document.doc_type
        properties.identifier = self.document.document_id
        properties.revision = 1
        properties.version = metadata.version or "1.0"
        properties.language = "en-GB"
        properties.created = _FIXED_MOMENT
        properties.modified = _FIXED_MOMENT
        properties.last_printed = _FIXED_MOMENT

    def _header_footer(self) -> None:
        metadata = self.document.metadata
        label = self.document.doc_type.replace("_", " ").title()
        header_label = (f"{label} — {metadata.classification}" if metadata.classification
                        else label)
        section = self.doc.sections[0]
        if self.profile.include_headers:
            paragraph = section.header.paragraphs[0]
            paragraph.text = header_label[:110]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for run in paragraph.runs:
                run.font.size = Pt(8)
        if self.profile.include_footers and metadata.reference:
            paragraph = section.footer.paragraphs[0]
            # no page numbers: the format has no pagination and numbers would be invented
            paragraph.text = metadata.reference
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in paragraph.runs:
                run.font.size = Pt(8)

    # -- content -----------------------------------------------------------
    def build(self) -> None:
        self._setup()
        self._header_footer()
        self._title_block()
        for index, section in enumerate(self.document.sections):
            self.section_index = index
            self._section(section)
        self._notes()

    def _paragraph(self, text: str, *, style: str | None = None) -> int:
        """Append a paragraph and return its index in ``Document.paragraphs``."""
        index = len(self.doc.paragraphs)
        paragraph = self.doc.add_paragraph(style=style) if style else self.doc.add_paragraph()
        paragraph.add_run(text)
        return index

    def _paragraph_with_runs(self, block: Block) -> int:
        """Append a paragraph run by run, keeping emphasis.

        ``block_text`` joins the runs with a space, so the rendered text is exactly the
        canonical projection, and a bold ``Label:`` stays a separate run — the shape
        that makes Docling emit the D4 two-item split.
        """
        index = len(self.doc.paragraphs)
        paragraph = self.doc.add_paragraph()
        parts = [(run.text.strip(), run.emphasis) for run in block.runs or []
                 if run.text and run.text.strip()]
        for position, (text, emphasis) in enumerate(parts):
            if position:
                paragraph.add_run(" ")
            run = paragraph.add_run(text)
            if emphasis == "bold":
                run.bold = True
            elif emphasis in {"italic", "italic_bold"}:
                run.italic = True
                run.bold = emphasis == "italic_bold"
        return index

    def _title_block(self) -> None:
        metadata = self.document.metadata
        body = " ".join(
            [heading_text(section) for section in self.document.sections]
            + [block_text(block) for section in self.document.sections
               for block in section.blocks if block.kind == "paragraph"])
        title_echoed = bool(metadata.title) and metadata.title in body
        if metadata.title and not title_echoed:
            heading = self.doc.add_heading(metadata.title, level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif title_echoed:
            # a second copy of the same sentence would make the parsed copy ambiguous
            self.warnings.append(
                "document title is not drawn separately: the first paragraph states it")
        if metadata.subtitle:
            paragraph = self.doc.add_paragraph()
            run = paragraph.add_run(metadata.subtitle)
            run.italic = True
            run.font.size = Pt(10.5)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

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
        # metadata is not an addressable node: it is drawn, not spanned (§7.3)
        for label, value in fields:
            if not value:
                continue
            paragraph = self.doc.add_paragraph()
            bold = paragraph.add_run(f"{label}: ")
            bold.bold = True
            paragraph.add_run(str(value))

    def _section(self, section: Section) -> None:
        level = min(section.depth + 1, 3)
        heading = heading_text(section)
        index = len(self.doc.paragraphs)
        self.doc.add_heading(heading, level=level)
        self.units.append(_Unit(section.section_id, "section", heading,
                                section_index=self.section_index, paragraph_index=index))
        for sentence in self.canonical.for_block(section.section_id):
            self.units.append(_Unit(sentence.sentence_id, "sentence", sentence.text,
                                    section_index=self.section_index,
                                    paragraph_index=index))
        for block in section.blocks:
            self._block(block)

    def _block(self, block: Block) -> None:
        if block.kind == "page_break":
            # DOCX has no pages to break; the node still has to appear exactly once
            self.units.append(_Unit(block.block_id, "block", "",
                                    section_index=self.section_index))
            return
        if block.kind == "table" and block.table is not None:
            self._table(block)
            return
        if block.kind == "list":
            self._list(block)
            return
        if block.kind in {"signature", "exhibit"}:
            self._signature_or_exhibit(block)
            return

        text = block_text(block)
        if not text:
            return
        index = self._paragraph_with_runs(block)
        self._footnote_markers(index, block)
        self.units.append(_Unit(block.block_id, "block", text,
                                section_index=self.section_index, paragraph_index=index))
        for sentence in self.canonical.for_block(block.block_id):
            self.units.append(_Unit(sentence.sentence_id, "sentence", sentence.text,
                                    section_index=self.section_index,
                                    paragraph_index=index))

    def _footnote_markers(self, index: int, block: Block) -> None:
        if not block.footnote_refs:
            return
        marks = [self.footnote_marks[ref] for ref in block.footnote_refs
                 if ref in self.footnote_marks]
        if marks:
            self.doc.paragraphs[index].add_run(" " + " ".join(f"[{m}]" for m in marks))

    def _list(self, block: Block) -> None:
        style = "List Number" if block.ordered else "List Bullet"
        texts: list[str] = []
        first = len(self.doc.paragraphs)
        for position, item in enumerate(block.items or [], start=1):
            text = list_item_text([run.text or "" for run in item.runs])
            if not text:
                continue
            texts.append(text)
            try:
                self._paragraph(text, style=style)
            except KeyError:                   # a template without that list style
                self._paragraph(f"{position}. {text}")
        if not texts:
            return
        # a list block is one addressable node; its items are not
        self.units.append(_Unit(block.block_id, "block", " ".join(texts),
                                section_index=self.section_index, paragraph_index=first))
        for item in block.items or []:
            for sentence in self.canonical.for_block(item.item_id):
                self.units.append(_Unit(sentence.sentence_id, "sentence", sentence.text,
                                        section_index=self.section_index,
                                        paragraph_index=first))

    def _signature_or_exhibit(self, block: Block) -> None:
        text = block_text(block)
        if not text:
            return
        index = self._paragraph_with_runs(block)
        if block.kind == "exhibit":
            for run in self.doc.paragraphs[index].runs:
                run.italic = True
        else:
            line = self._paragraph("_" * 46)
            self.doc.paragraphs[line].paragraph_format.space_before = Pt(18)
        self.units.append(_Unit(block.block_id, "block", text,
                                section_index=self.section_index, paragraph_index=index))
        for sentence in self.canonical.for_block(block.block_id):
            self.units.append(_Unit(sentence.sentence_id, "sentence", sentence.text,
                                    section_index=self.section_index,
                                    paragraph_index=index))

    def _table(self, block: Block) -> None:
        table = block.table
        if table is None:
            return
        rows = [*table.header_rows, *table.body_rows, *table.total_rows]
        ncols = len(table.columns)
        if not rows or ncols <= 0:
            return
        word = self.doc.add_table(rows=len(rows), cols=ncols)
        word.style = self.profile.table_style or "Table Grid"
        word.alignment = WD_TABLE_ALIGNMENT.CENTER

        index = self.table_index
        self.table_index += 1
        self.units.append(_Unit(block.block_id, "block", block_text(block),
                                section_index=self.section_index, table_index=index))
        self.units.append(_Unit(table.table_id, "table", block_text(block),
                                section_index=self.section_index, table_index=index))

        # place the text first; a merge concatenates the text of the cells it joins
        for r, row in enumerate(rows):
            column = 0
            for cell in row.cells:
                if column >= ncols:
                    break
                value = cell_text(cell)
                if value:
                    word.cell(r, column).paragraphs[0].add_run(value)
                self.units.append(_Unit(cell.cell_id, "cell", value,
                                        section_index=self.section_index,
                                        table_index=index, row=r + 1, column=column + 1,
                                        cell_ref=f"r{r + 1:02d}c{column + 1:02d}"))
                column += cell.colspan or 1
            self.units.append(_Unit(row.row_id, "row", row_text(row),
                                    section_index=self.section_index,
                                    table_index=index, row=r + 1))
            for sentence in self.canonical.for_block(row.row_id):
                self.units.append(_Unit(sentence.sentence_id, "sentence", sentence.text,
                                        section_index=self.section_index,
                                        table_index=index, row=r + 1))

        for r, row in enumerate(rows):
            column = 0
            for cell in row.cells:
                if column >= ncols:
                    break
                span, heights = cell.colspan or 1, cell.rowspan or 1
                if span > 1 or heights > 1:
                    anchor = word.cell(r, column)
                    target = word.cell(min(r + heights - 1, len(rows) - 1),
                                       min(column + span - 1, ncols - 1))
                    if target is not anchor:
                        merged = anchor.merge(target)
                        merged.text = ""
                        merged.paragraphs[0].add_run(cell_text(cell))
                column += span

        # a table needs a following paragraph in Word; it is spacing, not content
        self._paragraph("")

    def _notes(self) -> None:
        if not self.document.footnotes:
            return
        self.doc.add_heading("Notes", level=1)
        # a footnote is not an addressable node: it is drawn, not spanned
        for footnote in self.document.footnotes:
            body = f"[{footnote.marker}] " + " ".join(
                run.text for run in footnote.runs if run.text)
            paragraph = self.doc.add_paragraph()
            paragraph.add_run(body).font.size = Pt(8.5)

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
                canonical_id=unit.canonical_id, kind=unit.kind, format="docx",
                loc=RenderLoc(format="docx", pageless=True, page=None, bbox=None,
                              section_index=unit.section_index,
                              paragraph_index=unit.paragraph_index,
                              table_index=unit.table_index,
                              row=unit.row, column=unit.column,
                              cell_ref=unit.cell_ref),
                text=text, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()))
        return spans


# ---------------------------------------------------------------------------
def normalise_package(source: str | Path, target: str | Path) -> None:
    """Repackage a DOCX deterministically without touching its XML parts.

    Members are written in sorted order with a fixed timestamp, fixed external
    attributes and fixed compression, which removes every source of byte
    variation ``zipfile``/``python-docx`` introduce (mtime, directory order,
    permission bits).  The part contents are copied verbatim.
    """
    with zipfile.ZipFile(source) as archive:
        names = sorted(archive.namelist())
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0o600 << 16
                out.writestr(info, archive.read(name))
