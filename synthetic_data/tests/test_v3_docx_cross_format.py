"""Phase E — cross-format consistency with DOCX in the set.

One canonical document, three renderings (TXT, PDF, DOCX).  The comparison is **per
fact and per typed value**, never per sentence: the formats word the same fact
differently on purpose, so sentence equality would be the wrong test and would also
pass trivially for identical renderers.
"""
from __future__ import annotations

import pytest

from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.renderers import (DEFAULT_DOCX_PROFILE, DEFAULT_PDF_PROFILE,
                                      DEFAULT_TXT_PROFILE, DocxRenderer, PdfRenderer,
                                      RenderContext, TxtRenderer)
from synthetic_data.surfaces import (date_surface_key, money_surface_key,
                                     percent_surface_key, surfaces_equivalent)

from synthetic_data.tests.v3_txt import pilot_plan


@pytest.fixture(scope="module")
def triplets(tmp_path_factory):
    out = tmp_path_factory.mktemp("docx_cross_format")
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)
    rendered = {}
    for index, doc_id in enumerate(plan.prose_ids):
        canonical = canonicalize(plan.documents[doc_id])
        document = apply_statement_sentences(plan.documents[doc_id], canonical)
        txt = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                   out / f"{index:02d}.txt", context=context)
        pdf = PdfRenderer().render(document, DEFAULT_PDF_PROFILE,
                                   out / f"{index:02d}.pdf", context=context)
        docx = DocxRenderer().render(document, DEFAULT_DOCX_PROFILE,
                                     out / f"{index:02d}.docx", context=context)
        rendered[doc_id] = (document, txt, pdf, docx)
    return rendered


def test_three_formats_render_the_same_document_set(triplets):
    assert len(triplets) == 7
    for document, txt, pdf, docx in triplets.values():
        assert (txt.format, pdf.format, docx.format) == ("txt", "pdf", "docx")
        assert txt.sha256 and pdf.sha256 and docx.sha256


def test_fact_identity_is_identical_in_all_three_formats(triplets):
    for doc_id, (document, txt, pdf, docx) in triplets.items():
        assert set(txt.fact_surfaces) == set(pdf.fact_surfaces) == set(docx.fact_surfaces)
        assert all(statement.canonical_sentence_ids for statement in document.statements)


def test_every_fact_surface_agrees_between_pdf_and_docx(triplets):
    compared = 0
    for doc_id, (document, txt, pdf, docx) in triplets.items():
        for fact_id, docx_surfaces in docx.fact_surfaces.items():
            pdf_values = [s for s in pdf.fact_surfaces[fact_id] if any(c.isdigit() for c in s)]
            docx_values = [s for s in docx_surfaces if any(c.isdigit() for c in s)]
            for left in docx_values:
                assert any(surfaces_equivalent(left, right) for right in pdf_values), (
                    doc_id, fact_id, left, pdf_values)
                compared += 1
    assert compared > 50


def test_amounts_and_currencies_agree_in_all_three_formats(triplets):
    compared = 0
    for doc_id, (document, txt, pdf, docx) in triplets.items():
        keys = []
        for rendered in (txt, pdf, docx):
            surfaces = [s for values in rendered.fact_surfaces.values() for s in values]
            keys.append({money_surface_key(s) for s in surfaces if money_surface_key(s)})
        assert keys[0] == keys[1] == keys[2], (doc_id, keys[0] ^ keys[2])
        compared += len(keys[0])
    assert compared > 10


def test_dates_agree_in_all_three_formats(triplets):
    compared = 0
    for doc_id, (document, txt, pdf, docx) in triplets.items():
        keys = []
        for rendered in (txt, pdf, docx):
            surfaces = [s for values in rendered.fact_surfaces.values() for s in values]
            keys.append({date_surface_key(s) for s in surfaces if date_surface_key(s)})
        assert keys[0] == keys[1] == keys[2], (doc_id, keys[0] ^ keys[2])
        compared += len(keys[0])
    assert compared > 10


def test_percentages_agree_in_all_three_formats(triplets):
    compared = 0
    for doc_id, (document, txt, pdf, docx) in triplets.items():
        keys = []
        for rendered in (txt, pdf, docx):
            surfaces = [s for values in rendered.fact_surfaces.values() for s in values]
            keys.append({percent_surface_key(s) for s in surfaces if percent_surface_key(s)})
        assert keys[0] == keys[1] == keys[2], (doc_id, keys[0] ^ keys[2])
        compared += len(keys[0])
    assert compared > 5


def test_entity_names_are_preserved_in_all_three_formats(triplets):
    compared = 0
    for doc_id, (document, txt, pdf, docx) in triplets.items():
        for fact_id, txt_surfaces in txt.fact_surfaces.items():
            names = {s for s in txt_surfaces if not any(c.isdigit() for c in s)}
            if not names:
                continue
            assert names <= set(pdf.fact_surfaces[fact_id])
            assert names <= set(docx.fact_surfaces[fact_id]), (doc_id, fact_id, names)
            compared += len(names)
    assert compared > 10


def test_the_formats_differ_in_layout_not_in_content(triplets):
    """Same canonical sentences, different pagination semantics — so the value checks
    above compare real renderings rather than one document copied three times."""
    for document, txt, pdf, docx in triplets.values():
        sentences = {rendered.format: {span.canonical_id for span in rendered.spans
                                       if span.kind == "sentence"}
                     for rendered in (txt, pdf, docx)}
        assert sentences["txt"] == sentences["pdf"] == sentences["docx"]
        assert all(span.loc.page for span in pdf.spans)
        assert all(span.loc.page for span in txt.spans)
        assert all(span.loc.page is None for span in docx.spans)
        assert all(span.loc.pageless for span in docx.spans)
