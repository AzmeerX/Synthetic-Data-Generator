"""Phase D — the PDF renderer: layout, provenance, determinism, page parity."""
from __future__ import annotations

import re
from collections import Counter

import pytest

from synthetic_data.canonical import canonicalize
from synthetic_data.model import RenderProfile
from synthetic_data.renderers import DEFAULT_PDF_PROFILE, PDF_CAPABILITIES, PdfRenderer

from synthetic_data.tests.v3_pdf import (PROSE_TYPES, pdf_feature_document, pdf_pages,
                                         pdf_rendered, render_pdf, subprocess_digests,
                                         subprocess_pages)


@pytest.fixture(scope="module")
def documents(pdf_rendered):
    return {item.doc_id: item for item in pdf_rendered}


def test_capabilities_are_honest():
    assert PDF_CAPABILITIES.format == "pdf"
    assert PDF_CAPABILITIES.page_kind == "physical"
    assert PDF_CAPABILITIES.paginates is True
    assert PDF_CAPABILITIES.bbox is True
    assert PDF_CAPABILITIES.tables is True


def test_every_canonical_node_is_rendered_exactly_once(documents):
    for rendered in documents.values():
        canonical = canonicalize(rendered.document)
        expected = rendered.document.node_ids() | set(canonical.sentence_ids)
        counted = Counter(span.canonical_id for span in rendered.spans)
        assert not (expected - set(counted)), rendered.doc_id
        assert not (set(counted) - expected), rendered.doc_id
        assert {cid for cid, n in counted.items() if n > 1} == set()


def test_page_count_matches_pypdfium2(documents):
    """B1: the renderer's own pages equal the independent reader's count."""
    for rendered in documents.values():
        pages = {span.loc.page for span in rendered.spans}
        assert max(pages) == rendered.page_count, rendered.doc_id
        assert min(pages) == 1
        assert rendered.page_count == len(rendered.page_texts)


def test_page_count_is_stable_across_processes(documents):
    local = {doc_id: item.page_count for doc_id, item in documents.items()
             if doc_id.startswith("doc:000")}
    assert subprocess_pages() == local


def test_bytes_are_identical_across_processes(documents):
    local = {doc_id: item.result.sha256 for doc_id, item in documents.items()
             if doc_id.startswith("doc:000")}
    assert subprocess_digests() == local
    assert subprocess_digests("11") == local


def test_render_is_deterministic_within_a_process(documents, tmp_path):
    for rendered in documents.values():
        again = render_pdf(rendered.document, tmp_path / f"{rendered.doc_id.split(':')[1]}.pdf")
        assert again.result.sha256 == rendered.result.sha256
        assert [span.canonical_id for span in again.spans] == \
               [span.canonical_id for span in rendered.spans]


def test_all_seven_prose_types_are_rendered(documents):
    types = {item.doc_type for item in documents.values()
             if item.doc_id.startswith("doc:000")}
    assert types == PROSE_TYPES


def test_bbox_geometry_is_real_and_inside_the_page(documents):
    width, height = 595.2756, 841.8898            # A4 points
    for rendered in documents.values():
        for span in rendered.spans:
            box = span.loc.bbox
            if box is None:
                continue
            x0, y0, x1, y1 = box
            assert x1 > x0 and y1 > y0, span.canonical_id
            assert 0.0 <= x0 < width and 0.0 <= x1 <= width + 0.01
            assert 0.0 <= y0 < height and 0.0 <= y1 <= height + 0.01


def test_character_spans_reproduce_the_drawn_text(documents):
    """The documented invariant: page_buffer[start:end] == span.text."""
    for rendered in documents.values():
        for span in rendered.spans:
            if span.loc.char_start is None:
                assert span.text == ""
                continue
            buffer = rendered.page_buffers[span.loc.page - 1]
            assert span.loc.char_end <= len(buffer), (
                rendered.doc_id, span.kind, span.canonical_id, span.loc.page,
                span.loc.char_start, span.loc.char_end, len(buffer))
            assert buffer[span.loc.char_start:span.loc.char_end] == span.text, (
                rendered.doc_id, span.kind, span.canonical_id, span.loc.page)
            assert span.loc.char_end > span.loc.char_start


def test_section_spans_follow_document_order(documents):
    for rendered in documents.values():
        section_spans = [span for span in rendered.spans if span.kind == "section"]
        assert len(section_spans) == len(rendered.document.sections)
        pages = [span.loc.page for span in section_spans]
        assert pages == sorted(pages)
        for span, section in zip(section_spans, rendered.document.sections):
            assert span.canonical_id == section.section_id
            assert span.text.startswith((section.number or "", section.heading))


def test_every_top_level_section_starts_a_page(documents):
    for rendered in documents.values():
        roots = [section for section in rendered.document.sections if section.depth == 0]
        first_pages = []
        for section in roots:
            span = next(span for span in rendered.spans
                        if span.canonical_id == section.section_id)
            first_pages.append(span.loc.page)
        assert first_pages == sorted(set(first_pages))


def test_explicit_page_break_moves_to_the_next_page(documents):
    rendered = documents["doc:91001"]
    break_span = next(span for span in rendered.spans
                      if span.canonical_id == "blk:91001/02.01")
    after = next(span for span in rendered.spans
                 if span.canonical_id == "blk:91001/02.02")
    assert after.loc.page == break_span.loc.page + 1


def test_table_rows_and_cells_carry_row_and_column_indices(documents):
    checked = 0
    for rendered in documents.values():
        cells = [span for span in rendered.spans if span.kind == "cell"]
        rows = [span for span in rendered.spans if span.kind == "row"]
        assert len(rows) >= len(cells) / 12
        for span in cells:
            assert span.loc.row is not None and span.loc.column is not None
            assert span.loc.cell_ref and re.fullmatch(r"r\d{2}c\d{2}", span.loc.cell_ref)
            assert span.loc.table_index is not None
            checked += 1
    assert checked > 40


def test_colspan_and_rowspan_are_drawn_as_merged_cells(documents):
    rendered = documents["doc:91001"]
    table = next(block.table for section in rendered.document.sections
                 for block in section.blocks if block.table is not None)
    cells = {span.canonical_id: span for span in rendered.spans if span.kind == "cell"}

    def width(span):
        return span.loc.bbox[2] - span.loc.bbox[0]

    def height(span):
        return span.loc.bbox[3] - span.loc.bbox[1]

    header = cells[table.header_rows[0].cells[0].cell_id]
    singles = [cells[cell.cell_id] for row in table.body_rows for cell in row.cells
               if (cell.colspan or 1) == 1 and cell.text]
    assert width(header) > 1.5 * max(width(span) for span in singles)
    assert width(header) > sum(width(span) for span in singles[:2]) * 0.9

    spanning = [(row_index, cell) for row_index, row in enumerate(table.body_rows)
                for cell in row.cells if (cell.rowspan or 1) > 1]
    assert spanning, "the fixture must contain a rowspan cell"
    row_index, cell = spanning[0]
    origin = cells[cell.cell_id]
    plain = next(cells[other.cell_id] for other_row in table.body_rows
                 for other in other_row.cells
                 if (other.rowspan or 1) == 1 and other.colspan == 1 and other.text)
    assert height(origin) > 1.5 * height(plain)
    # the covered placeholder keeps its own band as a location
    placeholder = next(cells[other.cell_id] for other in table.body_rows[row_index + 1].cells
                       if not other.text)
    assert placeholder.loc.bbox is not None
    assert width(placeholder) == width(origin)


def test_headers_and_footers_appear_on_every_page(documents):
    for rendered in documents.values():
        for page, text in enumerate(rendered.page_texts, start=1):
            assert f"Page {page} of {rendered.page_count}" in text, (rendered.doc_id, page)
            label = rendered.document.doc_type.replace("_", " ").title()
            assert label in text, (rendered.doc_id, page)
            if rendered.document.metadata.reference:
                assert rendered.document.metadata.reference[:30] in text


def test_footnote_and_signature_are_rendered(documents):
    rendered = documents["doc:91001"]
    text = "\n".join(rendered.page_texts)
    assert "Notes" in text
    assert "[1] Clause references are to clauses of this agreement." in text
    assert "Signed for and on behalf of Amina Kerr, director." in text
    assert "Signature" in text


def test_render_map_is_complete_and_typed(documents):
    from synthetic_data.model import RenderMap
    for rendered in documents.values():
        render_map = RenderMap(document_id=rendered.doc_id, format="pdf",
                               spans=rendered.spans,
                               fact_surfaces=rendered.result.fact_surfaces)
        assert len(render_map.spans) == len(rendered.spans)
        for span in render_map.spans:
            assert span.format == "pdf"
            assert span.loc.page is not None
            assert span.loc.pageless is False


def test_page_size_and_margins_come_from_the_profile(tmp_path):
    document = pdf_feature_document()
    letter = RenderProfile(page_size="Letter", margins_mm=(25.0, 25.0, 25.0, 25.0),
                           font_family="Helvetica")
    a4 = PdfRenderer().render(document, DEFAULT_PDF_PROFILE, tmp_path / "a4.pdf")
    us = PdfRenderer().render(document, letter, tmp_path / "us.pdf")
    import pypdfium2 as pdfium
    a4_page = pdfium.PdfDocument(a4.path)[0]
    letter_page = pdfium.PdfDocument(us.path)[0]
    assert round(a4_page.get_width(), 1) == 595.3
    assert round(letter_page.get_width(), 1) == 612.0
    assert a4.sha256 != us.sha256


def test_workbook_documents_are_refused(tmp_path):
    plan_pilot = __import__("synthetic_data.tests.v3_txt", fromlist=["pilot_plan"]).pilot_plan()
    workbook = plan_pilot.documents[plan_pilot.workbook_ids[0]]
    with pytest.raises(ValueError, match="workbook"):
        PdfRenderer().render(workbook, DEFAULT_PDF_PROFILE, tmp_path / "wb.pdf")


def test_no_filler_pages(documents):
    """Every page carries at least one canonical span (no padding to a page count)."""
    for rendered in documents.values():
        pages = {span.loc.page for span in rendered.spans}
        assert pages == set(range(1, rendered.page_count + 1)), rendered.doc_id
        assert not [w for w in rendered.result.warnings if "carry no canonical span" in w]
