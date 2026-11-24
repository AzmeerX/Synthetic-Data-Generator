"""Phase D — actual Docling ingestion of the PDF fixtures (D7 in the open)."""
from __future__ import annotations

import pytest

from synthetic_data.alignment import AlignmentError, require_fact_evidence
from synthetic_data.canonical import canonicalize
from synthetic_data.surfaces import money_surface_key, money_surfaces

from synthetic_data.tests.v3_pdf import (PROSE_TYPES, pdf_config, pdf_ingested,
                                         pdf_rendered, pdf_store)


@pytest.fixture(scope="module")
def documents(pdf_ingested):
    return pdf_ingested


def test_every_pdf_is_segmented(documents):
    for ingested in documents.values():
        assert ingested.summary["status"] == "SEGMENTED", ingested.doc_id
        assert ingested.summary["sentences"] > 0
        assert ingested.view.sentences
        assert ingested.summary["pages"] == ingested.rendered.page_count


def test_physical_page_count_survives_ingestion(documents):
    for ingested in documents.values():
        assert ingested.view.doc.get("pages") == ingested.rendered.page_count
        assert ingested.view.doc.get("pages") == len(ingested.rendered.page_texts)
        pages = {span.loc.page for span in ingested.rendered.spans}
        assert max(pages) == ingested.view.doc.get("pages")


def test_no_canonical_sentence_is_lost(documents):
    for ingested in documents.values():
        kinds = {}
        for alignment in ingested.alignments:
            kinds[alignment.kind] = kinds.get(alignment.kind, 0) + 1
        assert kinds.get("missing", 0) == 0, (ingested.doc_id, kinds)
        assert kinds.get("partial", 0) == 0, (ingested.doc_id, kinds)
        assert kinds.get("ambiguous", 0) == 0, (ingested.doc_id, kinds)
        assert sum(kinds.values()) == len(canonicalize(ingested.rendered.document).sentences)


def test_alignment_ids_are_parser_ids_and_spans_are_substrings(documents):
    for ingested in documents.values():
        text = ingested.view.text
        for alignment in ingested.alignments:
            if alignment.kind in {"missing", "ambiguous"}:
                assert alignment.char_start is None
                assert alignment.text == ""
                continue
            assert set(alignment.sentence_ids) <= set(ingested.view.by_id)
            assert text[alignment.char_start:alignment.char_end] == alignment.text
            for sentence_id, start, end in alignment.pieces:
                assert text[start:end]


def test_every_fact_is_grounded(documents):
    for ingested in documents.values():
        require_fact_evidence(list(ingested.evidence))
        assert ingested.evidence
        for item in ingested.evidence:
            assert item.quote
            assert ingested.view.text[item.char_start:item.char_end] == item.quote


def test_table_rows_are_flattened_and_recorded_as_such(documents):
    """D7: the parser returns cells, not tables; the alignment must say so."""
    total_rows = 0
    docs_with_tables = 0
    for ingested in documents.values():
        rows = [alignment for alignment in ingested.alignments
                if alignment.missing_row_structure]
        canonical = canonicalize(ingested.rendered.document)
        expected = [s for s in canonical.sentences if s.kind == "table_row"]
        if not expected:
            assert rows == []
            continue
        docs_with_tables += 1
        # every row is present in the parser output, but a flattener may return a
        # whole row as one sentence; then the row is exact and needs no flag
        for alignment in rows:
            assert alignment.kind in {"split", "exact"}
            assert alignment.pieces
        aligned_rows = [a for a in ingested.alignments
                        if a.canonical_id in {s.sentence_id for s in expected}]
        assert len(aligned_rows) == len(expected), ingested.doc_id
        total_rows += len(rows)
    assert docs_with_tables >= 6
    assert total_rows > 40


def test_the_parser_reports_no_table_structure_for_pdf(documents):
    """The negative half of D7: there are no ``table_row`` lines at all."""
    for ingested in documents.values():
        table_like = [row for row in ingested.view.sentences
                      if ingested.view.sentence_text(row).lstrip().startswith("|")]
        assert table_like == [], ingested.doc_id


def test_money_values_survive_the_round_trip(documents):
    checked = 0
    for ingested in documents.values():
        text = ingested.view.text
        surface_keys = {money_surface_key(surface)
                        for surfaces in ingested.rendered.result.fact_surfaces.values()
                        for surface in surfaces
                        if money_surface_key(surface)}
        present = {money_surface_key(candidate) for candidate in money_surfaces(text)}
        assert surface_keys <= present, (ingested.doc_id, surface_keys - present)
        checked += len(surface_keys)
    assert checked > 10


def test_entity_names_survive_the_round_trip(documents):
    """Every entity name the renderer drew is present in the parser's text."""
    from synthetic_data.tests.v3_txt import pilot_plan

    world = pilot_plan().world
    names = sorted({entity.canonical_name for entity in world.entities.values()
                    if len(entity.canonical_name) > 8})
    checked = 0
    for ingested in documents.values():
        rendered_text = "\n".join(ingested.rendered.page_texts)
        parser_text = ingested.view.text
        for name in names:
            if name not in rendered_text:
                continue
            assert name in parser_text, (ingested.doc_id, name)
            checked += 1
    assert checked > 20


def test_filtered_sentences_are_only_page_furniture(documents):
    for ingested in documents.values():
        filtered = [ingested.view.sentence_text(row) for row in ingested.view.sentences
                    if row.get("filtered")]
        reference = ingested.rendered.document.metadata.reference or ""
        label = ingested.rendered.document.doc_type.replace("_", " ").title()
        for text in filtered:
            furniture = ("Page " in text or " of " in text
                         or ingested.rendered.document.metadata.title[:30] in text
                         or label in text
                         or (reference and reference in text))
            assert furniture, (ingested.doc_id, text)


def test_cell_spans_point_at_a_page_that_holds_the_cell(documents):
    for ingested in documents.values():
        pages = ingested.rendered.page_texts
        for span in ingested.rendered.spans:
            if span.kind != "cell" or not span.text:
                continue
            page_text = " ".join(pages[span.loc.page - 1].split())
            assert " ".join(span.text.split()) in page_text, (
                ingested.doc_id, span.canonical_id)


def test_a_flattened_row_quote_covers_its_cells(documents):
    checked = 0
    for ingested in documents.values():
        by_sentence = {sentence.sentence_id: sentence
                       for sentence in canonicalize(ingested.rendered.document).sentences}
        for alignment in ingested.alignments:
            if not alignment.missing_row_structure:
                continue
            sentence = by_sentence[alignment.canonical_id]
            cells = [cell.strip() for cell in sentence.text.strip("|").split("|")]
            for cell in cells:
                if not cell or not any(char.isalnum() for char in cell):
                    continue
                assert " ".join(cell.split()) in " ".join(alignment.text.split()), (
                    ingested.doc_id, cell[:40])
            checked += 1
    assert checked > 40
