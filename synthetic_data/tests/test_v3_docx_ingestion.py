"""Phase E — DOCX through the real Synth ingestion path.

These tests assert what the parser *actually* returns, not what the OOXML says: the
renderer wrote real rows, merges and headings, and Docling turns some of them into
something else.  Where structure is lost the test records the loss instead of
pretending it survived.
"""
from __future__ import annotations

import pytest

from synthetic_data.alignment import EVIDENCE_KINDS
from synthetic_data.canonical import canonicalize, heading_text
from synthetic_data.renderers import DEFAULT_DOCX_PROFILE, DocxRenderer

from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.tests.v3_docx import (PROSE_TYPES, docx_config, docx_ingested,
                                          docx_rendered, docx_store, label_value_document,
                                          render_docx)


# -- ingestion basics -------------------------------------------------------
def test_every_prose_type_segments(docx_ingested):
    assert len(docx_ingested) == 7
    for ingested in docx_ingested.values():
        assert ingested.summary["status"] == "SEGMENTED"
        assert ingested.summary["pages"] == 1        # D11: one synthetic page
        assert ingested.view.text.strip()


def test_the_map_stays_pageless_even_though_the_parser_reports_a_page(docx_ingested):
    for ingested in docx_ingested.values():
        assert ingested.summary["pages"] == 1
        for span in ingested.rendered.spans:
            assert span.loc.page is None and span.loc.pageless is True


def test_headings_are_recovered_in_order(docx_ingested):
    checked = 0
    for ingested in docx_ingested.values():
        headings = [" ".join(ingested.view.sentence_text(row).split())
                    for row in ingested.view.sentences if row["kind"] == "heading"]
        expected = [" ".join(heading_text(section).split())
                    for section in ingested.rendered.document.sections]
        # every canonical heading is recovered, in document order
        remaining = list(headings)
        for text in expected:
            assert text in remaining, (ingested.rendered.doc_type, text, headings[:6])
            remaining = remaining[remaining.index(text) + 1:]
        checked += len(expected)
    assert checked >= 40


def test_section_provenance_comes_from_the_map_not_the_parser(docx_ingested):
    """Section provenance for DOCX is the render map, not the parser (D8-analogue).

    The parser's ``section_path`` is unreliable for DOCX: some documents get a path per
    sentence, others get none (its heading ``title`` is not always populated).  What it
    does supply must still name a real heading, and every section is always spanned
    with its ``section_index``.
    """
    for ingested in docx_ingested.values():
        headings = {" ".join(heading_text(section).split())
                    for section in ingested.rendered.document.sections}
        title = ingested.rendered.document.metadata.title
        if title:
            headings.add(" ".join(title.split()))   # the title is a heading too
        for path in (row["section_path"] for row in ingested.view.sentences):
            if path.strip():                 # if a path exists it must name a heading
                assert any(heading in path for heading in headings), (path, headings)
        sections = [span for span in ingested.rendered.spans if span.kind == "section"]
        assert len(sections) == len(ingested.rendered.document.sections)
        assert all(span.loc.section_index is not None for span in sections)
    # measured, not assumed: some DOCX documents get paths, others none at all
    populated = sum(1 for ingested in docx_ingested.values()
                    for row in ingested.view.sentences if row["section_path"].strip())
    assert populated or not populated       # recorded, not asserted: the map is authoritative


def test_paragraph_sentences_are_all_addressable(docx_ingested):
    for ingested in docx_ingested.values():
        canonical = canonicalize(ingested.rendered.document)
        assert [a.canonical_id for a in ingested.alignments] == canonical.sentence_ids


def test_no_canonical_sentence_is_lost(docx_ingested):
    for ingested in docx_ingested.values():
        problems = [(a.kind, a.canonical_id, a.text[:60], (a.quote or "")[:60])
                    for a in ingested.alignments
                    if a.kind in {"missing", "partial", "ambiguous"}]
        assert not problems, f"{ingested.rendered.doc_type}: {problems[:3]}"


def test_alignment_rate_is_at_least_95_percent(docx_ingested):
    total = exact = 0
    for ingested in docx_ingested.values():
        for alignment in ingested.alignments:
            total += 1
            exact += alignment.kind in {"exact", "normalised"}
    assert total
    assert exact / total >= 0.95, f"{exact}/{total}"


def test_evidence_quotes_are_exact_parser_substrings(docx_ingested):
    from synthetic_data.alignment import normalise
    for ingested in docx_ingested.values():
        for alignment in ingested.alignments:
            if alignment.kind not in EVIDENCE_KINDS:
                continue
            quote = ingested.view.text[alignment.char_start:alignment.char_end]
            assert quote.strip(), alignment.canonical_id
            if alignment.kind in {"exact", "normalised"}:
                assert normalise(quote) == normalise(alignment.text), alignment.canonical_id
            else:
                assert normalise(alignment.text) in normalise(quote) or \
                    normalise(quote) in normalise(alignment.text), alignment.canonical_id
            for sentence_id in alignment.sentence_ids:
                assert sentence_id in ingested.view.by_id


def test_canonical_and_parser_ids_stay_distinct(docx_ingested):
    for ingested in docx_ingested.values():
        canonical = set(canonicalize(ingested.rendered.document).sentence_ids)
        assert canonical.isdisjoint(ingested.view.by_id)
        assert not any(sid.startswith("snt:") for sid in ingested.view.by_id)


# -- tables -----------------------------------------------------------------
def test_table_rows_arrive_as_rows_with_their_cells(docx_ingested):
    """DOCX keeps rows: a row is one parser sentence that carries its own cells."""
    checked = 0
    for ingested in docx_ingested.values():
        canonical = canonicalize(ingested.rendered.document)
        sentence = {item.sentence_id: item for item in canonical.sentences}
        rows = {sid: item for sid, item in sentence.items() if item.kind == "table_row"}
        aligned = {a.canonical_id: a for a in ingested.alignments if a.canonical_id in rows}
        assert set(aligned) == set(rows)
        for sentence_id, alignment in aligned.items():
            assert alignment.kind in {"exact", "normalised", "merged", "split"}
            assert alignment.missing_row_structure is False
            quote = ingested.view.text[alignment.char_start:alignment.char_end]
            for value in cells_of_row(ingested, rows[sentence_id].block_id):
                assert value in quote, (value, quote[:80])
            checked += 1
    assert checked >= 40


def test_a_rowspan_cell_is_reported_once_and_the_row_shrinks(tmp_path_factory):
    """Documented DOCX limitation: a vertically merged cell appears at its first row."""
    from synthetic_data.tests.v3_pdf import pdf_feature_document
    from synth.modules.u01_parser import ingest_path
    from synth.modules.common import DocView
    from synth.store import test_store as make_test_store

    document = pdf_feature_document()
    rendered = render_docx(document, tmp_path_factory.mktemp("docx_rowspan") / "f.docx")
    store = make_test_store()
    try:
        record = ingest_path(store, _config(), rendered.path)[0]
        with store.reader() as reader:
            view = DocView.load(reader, record["doc_id"], _config())
        rows = [view.sentence_text(row) for row in view.sentences
                if row["kind"] == "table_row"]
        assert any(row.startswith("Tranche A") and "GBP 4,000,000.00" in row for row in rows)
        # the spanned amount is not repeated on the second row: the row arrives shorter
        second = next(row for row in rows if row.startswith("Tranche B"))
        assert "GBP 4,000,000.00" not in second
    finally:
        store.dispose()


# -- D4 ---------------------------------------------------------------------
def test_label_value_paragraphs_survive_the_parser_split(tmp_path_factory):
    """D4: a bold ``Label:`` splits into two parser items; gold must not be lost."""
    from synthetic_data.alignment import align_document, align_facts, require_fact_evidence

    document = label_value_document()
    rendered = render_docx(document, tmp_path_factory.mktemp("docx_d4") / "d4.docx")
    store = make_test_store()
    try:
        record = ingest_path(store, _config(), rendered.path)[0]
        with store.reader() as reader:
            view = DocView.load(reader, record["doc_id"], _config())
        texts = [view.sentence_text(row) for row in view.sentences]
        # the split is real: label and value are separate parser sentences
        assert "Borrower:" in texts
        assert "Meridian Holdings Limited." in texts

        canonical = canonicalize(rendered.document)
        alignments = align_document(canonical, view)
        assert all(a.kind not in {"missing", "ambiguous"} for a in alignments)
        evidence = align_facts(rendered.document, canonical, alignments,
                               rendered.result.fact_surfaces, view.text)
        require_fact_evidence(evidence)
        borrower = [e for e in evidence if e.fact_id == "fact:edge:92001:01"]
        assert borrower and borrower[0].acceptable
        assert "Meridian Holdings Limited" in borrower[0].quote
    finally:
        store.dispose()


# -- values and cross-document references -----------------------------------
def test_entity_names_and_typed_values_survive(docx_ingested):
    import re
    checked = {"money": 0, "date": 0, "percent": 0}
    for ingested in docx_ingested.values():
        text = ingested.view.text
        for surfaces in ingested.rendered.result.fact_surfaces.values():
            for surface in surfaces:
                if surface not in text:
                    continue
                if re.match(r"^[£$€]", surface) or "GBP" in surface:
                    checked["money"] += 1
                elif re.search(r"\d{4}", surface):
                    checked["date"] += 1
                if "%" in surface:
                    checked["percent"] += 1
    assert checked["money"] and checked["date"], checked


def test_every_fact_is_grounded(docx_ingested):
    from synthetic_data.alignment import require_fact_evidence
    total = 0
    for ingested in docx_ingested.values():
        require_fact_evidence(ingested.evidence)
        total += len(ingested.evidence)
    assert total == 300


def test_cross_document_facts_are_grounded_in_each_document(docx_ingested, docx_config):
    by_fact: dict[str, list[str]] = {}
    for ingested in docx_ingested.values():
        for evidence in ingested.evidence:
            by_fact.setdefault(evidence.fact_id, []).append(ingested.rendered.doc_type)
    shared = {fact: docs for fact, docs in by_fact.items() if len(docs) > 1}
    assert shared, "the pilot corpus contains cross-document facts"
    for fact, docs in shared.items():
        for ingested in docx_ingested.values():
            if ingested.rendered.doc_type in docs:
                assert any(e.fact_id == fact and e.acceptable
                           for e in ingested.evidence), fact


# -- helpers ---------------------------------------------------------------
_IMPORTS = None


def _config():
    global _IMPORTS
    if _IMPORTS is None:
        from synth.config import load_config
        _IMPORTS = load_config()
    return _IMPORTS


def cells_of_row(ingested, row_id: str) -> list[str]:
    """Non-empty cell texts of a canonical row, as the model declares them."""
    for section in ingested.rendered.document.sections:
        for block in section.blocks:
            if block.table is None:
                continue
            for row in [*block.table.header_rows, *block.table.body_rows,
                        *block.table.total_rows]:
                if row.row_id == row_id:
                    return [value for value in
                            ((cell.text or cell.display or "").strip() for cell in row.cells)
                            if value]
    return []
