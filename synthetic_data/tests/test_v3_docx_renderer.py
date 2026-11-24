"""Phase E — the DOCX renderer writes real OOXML, not prose in a .docx coat.

Every assertion about structure is made against the *package* (``word/document.xml``),
not against the renderer's own object model, so a renderer that believed it wrote a
table but emitted paragraphs would fail here.
"""
from __future__ import annotations

import re

import pytest
import zipfile
from pathlib import Path

from synthetic_data.canonical import canonicalize, heading_text
from synthetic_data.model import RenderMap, assert_render_coverage
from synthetic_data.renderers import DOCX_CAPABILITIES, DocxRenderer

from synthetic_data.tests.v3_docx import (PROSE_TYPES, docx_by_type, docx_package,
                                          docx_rendered, digest, label_value_document,
                                          render_docx)


# -- package shape ----------------------------------------------------------
def test_the_output_is_a_zip_package_with_ooxml_parts(docx_rendered):
    for rendered in docx_rendered:
        package = docx_package(rendered.path)
        assert "[Content_Types].xml" in package
        assert "word/document.xml" in package
        assert "word/styles.xml" in package
        assert "docProps/core.xml" in package
        assert rendered.path.read_bytes()[:2] == b"PK"


def test_document_xml_declares_a_wordprocessing_document(docx_rendered):
    xml = docx_rendered[0].part("word/document.xml")
    assert "w:document" in xml and "wordprocessingml" in xml
    assert "w:body" in xml


def test_it_is_not_html_or_plain_text_with_a_docx_extension(docx_rendered):
    for rendered in docx_rendered:
        head = rendered.path.read_bytes()[:400].lower()
        assert b"<html" not in head and b"<!doctype html" not in head
        assert b"<w:document" in rendered.package()["word/document.xml"]


# -- content ----------------------------------------------------------------
def test_all_seven_prose_types_render(docx_rendered):
    assert {item.doc_type for item in docx_rendered} == PROSE_TYPES
    assert len(docx_rendered) == 7


def test_capabilities_are_honest(docx_rendered):
    assert DOCX_CAPABILITIES.paginates is False
    assert DOCX_CAPABILITIES.page_kind == "none"
    assert DOCX_CAPABILITIES.tables is True
    assert DOCX_CAPABILITIES.bbox is False
    assert DOCX_CAPABILITIES.footnotes is False      # notes section, not real footnotes
    assert DOCX_CAPABILITIES.headers_footers is True
    for rendered in docx_rendered:
        assert rendered.result.capabilities == DOCX_CAPABILITIES


def test_headings_are_word_heading_styles(docx_rendered):
    for rendered in docx_rendered:
        xml = rendered.part("word/document.xml")
        styles = set(re.findall(r'w:pStyle w:val="(Heading\d|Title)"', xml))
        assert rendered.document.sections, "a prose document has sections"
        assert any(style.startswith("Heading") for style in styles), (
            f"{rendered.doc_type} has no heading-styled paragraph")


def test_section_count_matches_the_model(docx_rendered):
    for rendered in docx_rendered:
        xml = rendered.part("word/document.xml")
        headings = len(re.findall(r'w:pStyle w:val="Heading\d"', xml))
        # the notes section is a heading too, but it is not a canonical section
        notes = 1 if rendered.document.footnotes else 0
        assert headings == len(rendered.document.sections) + notes


def test_each_section_is_spanned_with_its_paragraph_index(docx_rendered):
    for rendered in docx_rendered:
        canonical = canonicalize(rendered.document)
        sections = {span.canonical_id: span for span in rendered.spans
                    if span.kind == "section"}
        assert len(sections) == len(rendered.document.sections)
        for section in rendered.document.sections:
            span = sections[section.section_id]
            assert span.text == heading_text(section)
            assert span.loc.section_index is not None
            assert span.loc.paragraph_index is not None
            assert canonical.for_block(section.section_id)


def test_paragraphs_are_single_word_paragraphs(docx_rendered):
    for rendered in docx_rendered:
        blocks = [span for span in rendered.spans if span.kind == "block"]
        assert blocks, f"{rendered.doc_type} has no block spans"
        for span in blocks:
            if span.loc.table_index is not None:
                continue                     # a table or table cell
            assert span.loc.paragraph_index is not None, span.canonical_id


def test_sentences_are_addressable_and_ordered(docx_rendered):
    for rendered in docx_rendered:
        canonical = canonicalize(rendered.document)
        sentence_spans = [span for span in rendered.spans if span.kind == "sentence"]
        assert [span.canonical_id for span in sentence_spans] == canonical.sentence_ids
        for span in sentence_spans:
            assert span.loc.paragraph_index is not None or span.loc.table_index is not None


def test_lists_use_word_list_styles(docx_rendered):
    seen = {"ListNumber": 0, "ListBullet": 0}
    for rendered in docx_rendered:
        xml = rendered.part("word/document.xml")
        ordered = sum(len(block.items or []) for section in rendered.document.sections
                      for block in section.blocks
                      if block.kind == "list" and block.ordered)
        bulleted = sum(len(block.items or []) for section in rendered.document.sections
                       for block in section.blocks
                       if block.kind == "list" and not block.ordered)
        assert len(re.findall(r'w:pStyle w:val="ListNumber"', xml)) == ordered
        assert len(re.findall(r'w:pStyle w:val="ListBullet"', xml)) == bulleted
        seen["ListNumber"] += ordered
        seen["ListBullet"] += bulleted
    assert seen["ListNumber"] and seen["ListBullet"], "the corpus exercises both list styles"


def test_tables_are_real_word_tables(docx_rendered):
    for rendered in docx_rendered:
        xml = rendered.part("word/document.xml")
        modelled = [block.table for section in rendered.document.sections
                    for block in section.blocks if block.table is not None]
        assert xml.count("<w:tbl>") == len(modelled)
        assert len(re.findall(r"<w:gridCol ", xml)) == sum(
            len(table.columns) for table in modelled)
        assert len(re.findall(r"<w:tr>", xml)) == sum(
            len([*table.header_rows, *table.body_rows, *table.total_rows])
            for table in modelled)


def test_table_rows_and_cells_are_spanned_with_indices(docx_rendered):
    for rendered in docx_rendered:
        rows = [span for span in rendered.spans if span.kind == "row"]
        cells = [span for span in rendered.spans if span.kind == "cell"]
        modelled_rows = [row for section in rendered.document.sections
                         for block in section.blocks if block.table is not None
                         for row in [*block.table.header_rows, *block.table.body_rows,
                                     *block.table.total_rows]]
        assert len(rows) == len(modelled_rows)
        assert len(cells) == sum(len(row.cells) for row in modelled_rows)
        for span in rows:
            assert span.loc.table_index is not None and span.loc.row
        for span in cells:
            assert span.loc.table_index is not None
            assert span.loc.row and span.loc.column
            assert span.loc.cell_ref == f"r{span.loc.row:02d}c{span.loc.column:02d}"


# -- spans, notes, signatures, metadata -------------------------------------
def test_colspan_and_rowspan_are_written_as_word_merges(tmp_path_factory):
    from synthetic_data.tests.v3_pdf import pdf_feature_document
    rendered = render_docx(pdf_feature_document(), tmp_path_factory.mktemp("docx_spans")
                           / "feature.docx")
    xml = rendered.part("word/document.xml")
    assert 'w:gridSpan w:val="3"' in xml        # the spanning header row
    assert 'w:gridSpan w:val="2"' in xml        # the total row
    assert "w:vMerge" in xml                    # the two-row amount cell


def test_footnotes_and_signatures_are_rendered(tmp_path_factory):
    from synthetic_data.tests.v3_pdf import pdf_feature_document
    document = pdf_feature_document()
    rendered = render_docx(document, tmp_path_factory.mktemp("docx_notes") / "feature.docx")
    body = rendered.part("word/document.xml")
    assert "Notes" in body
    assert "[1]" in body
    assert "Clause references are to clauses of this agreement." in body
    assert "Signed for and on behalf of Amina Kerr, director." in body
    # a footnote is drawn, not spanned: it is not an addressable node
    assert all(span.canonical_id in document.node_ids() | set()
               for span in rendered.spans if span.kind != "sentence")


def test_metadata_and_correspondence_are_carried(docx_by_type):
    core = docx_by_type["financial_agreement"].part("docProps/core.xml")
    assert "2000-01-01T00:00:00Z" in core
    assert "Synthetic Data Generator" in core
    correspondence = docx_by_type["correspondence"]
    body = correspondence.part("word/document.xml")
    metadata = correspondence.document.metadata
    assert metadata.thread is not None
    assert metadata.thread.thread_id in body
    assert metadata.thread.message_id in body


def test_headers_and_footers_carry_no_page_numbers(docx_by_type):
    rendered = docx_by_type["financial_agreement"]
    package = rendered.package()
    header_parts = [name for name in package if name.startswith("word/header")]
    footer_parts = [name for name in package if name.startswith("word/footer")]
    assert header_parts and footer_parts
    for name in header_parts + footer_parts:
        text = package[name].decode("utf-8")
        assert "PAGE" not in text.upper().replace("PAGES", "")
        assert not re.search(r"\bPage \d+ of \d+\b", text)


# -- provenance -------------------------------------------------------------
def test_every_node_is_rendered_exactly_once(docx_rendered):
    for rendered in docx_rendered:
        canonical = canonicalize(rendered.document)
        span_ids = [span.canonical_id for span in rendered.spans]
        assert len(span_ids) == len(set(span_ids))
        assert set(span_ids) == set(rendered.document.node_ids()) | set(canonical.sentence_ids)
        # the renderer asserts this itself; here it is checked independently
        assert_render_coverage(
            set(span_ids),
            RenderMap(document_id=rendered.document.document_id, format="docx",
                      spans=rendered.spans))


def test_every_span_is_pageless_and_structural(docx_rendered):
    for rendered in docx_rendered:
        for span in rendered.spans:
            loc = span.loc
            assert loc.format == "docx"
            assert loc.pageless is True
            assert loc.page is None
            assert loc.bbox is None
            assert any(value is not None for value in
                       (loc.section_index, loc.paragraph_index, loc.table_index)), span


def test_no_page_number_leaks_into_the_map_or_the_text(docx_rendered):
    for rendered in docx_rendered:
        assert not any("pagination" in warning for warning in rendered.result.warnings)
        body = rendered.part("word/document.xml")
        assert "w:br w:type=\"page\"" not in body


def test_workbooks_are_refused(tmp_path):
    from synthetic_data.renderers import DEFAULT_DOCX_PROFILE
    from synthetic_data.tests.v3_txt import pilot_plan
    plan = pilot_plan()
    workbook = plan.documents[plan.workbook_ids[0]]
    with pytest.raises(ValueError, match="workbook"):
        DocxRenderer().render(workbook, DEFAULT_DOCX_PROFILE, tmp_path / "unused.docx")


# -- determinism ------------------------------------------------------------
def test_two_renders_of_one_document_are_byte_identical(tmp_path_factory):
    from synthetic_data.tests.v3_txt import pilot_plan
    plan = pilot_plan()
    doc_id = plan.prose_ids[0]
    out = tmp_path_factory.mktemp("docx_repeat")
    first = render_docx(plan.documents[doc_id], out / "a.docx")
    second = render_docx(plan.documents[doc_id], out / "b.docx")
    assert first.sha256 == second.sha256
    assert digest(first.path) == digest(second.path)


def test_zip_members_are_ordered_and_timestamped_fixed(docx_by_type):
    rendered = docx_by_type["case_notes"]
    with zipfile.ZipFile(rendered.path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_core_properties_are_fixed(docx_by_type):
    for rendered in docx_by_type.values():
        core = rendered.part("docProps/core.xml")
        assert core.count("2000-01-01T00:00:00Z") >= 2
        assert "<dcterms:created" in core and "<dcterms:modified" in core
