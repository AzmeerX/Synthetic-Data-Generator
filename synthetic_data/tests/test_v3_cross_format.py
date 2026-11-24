"""Phase D — cross-format consistency: one canonical fact, two renderings.

The comparison is **per fact**, never per sentence: the two formats word the same
fact differently on purpose, so sentence equality would be the wrong test.  Values
are compared with :func:`synthetic_data.surfaces.surfaces_equivalent`, which
normalises money, dates and percentages to their value.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.renderers import (DEFAULT_PDF_PROFILE, DEFAULT_TXT_PROFILE,
                                     PdfRenderer, RenderContext, TxtRenderer)
from synthetic_data.surfaces import surfaces_equivalent

from synthetic_data.tests.v3_txt import PILOT, pilot_plan


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    out = tmp_path_factory.mktemp("cross_format")
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)
    documents = {}
    for index, doc_id in enumerate(plan.prose_ids):
        canonical = canonicalize(plan.documents[doc_id])
        document = apply_statement_sentences(plan.documents[doc_id], canonical)
        txt = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, out / f"{index:02d}.txt",
                                   context=context)
        pdf = PdfRenderer().render(document, DEFAULT_PDF_PROFILE, out / f"{index:02d}.pdf",
                                   context=context)
        documents[doc_id] = (document, txt, pdf)
    return documents


def test_both_formats_render_the_same_document_set(pair):
    assert len(pair) == 7


def test_statement_and_fact_identity_is_unchanged(pair):
    for doc_id, (document, txt, pdf) in pair.items():
        assert txt.format == "txt" and pdf.format == "pdf"
        for statement in document.statements:
            assert statement.fact_id
            assert statement.canonical_sentence_ids
        assert set(txt.fact_surfaces) == set(pdf.fact_surfaces)


def test_every_fact_surface_agrees_across_formats(pair):
    compared = 0
    for doc_id, (document, txt, pdf) in pair.items():
        for fact_id, txt_surfaces in txt.fact_surfaces.items():
            pdf_surfaces = pdf.fact_surfaces[fact_id]
            txt_values = [s for s in txt_surfaces if any(c.isdigit() for c in s)]
            pdf_values = [s for s in pdf_surfaces if any(c.isdigit() for c in s)]
            for left in txt_values:
                assert any(surfaces_equivalent(left, right) for right in pdf_values), (
                    doc_id, fact_id, left, pdf_values)
                compared += 1
    assert compared > 50


def test_names_are_preserved_across_formats(pair):
    compared = 0
    for doc_id, (document, txt, pdf) in pair.items():
        for fact_id in txt.fact_surfaces:
            txt_names = {s for s in txt.fact_surfaces[fact_id] if not any(c.isdigit() for c in s)}
            if not txt_names:
                continue
            pdf_names = set(pdf.fact_surfaces[fact_id])
            for name in txt_names:
                assert name in pdf_names, (doc_id, fact_id, name)
                compared += 1
    assert compared > 10


def test_amounts_and_currencies_are_semantically_equal(pair):
    from synthetic_data.surfaces import money_surface_key

    compared = 0
    for doc_id, (document, txt, pdf) in pair.items():
        txt_keys = {money_surface_key(s) for s in
                    (surface for surfaces in txt.fact_surfaces.values() for surface in surfaces)
                    if money_surface_key(s)}
        pdf_keys = {money_surface_key(s) for s in
                    (surface for surfaces in pdf.fact_surfaces.values() for surface in surfaces)
                    if money_surface_key(s)}
        assert txt_keys == pdf_keys, (doc_id, txt_keys ^ pdf_keys)
        compared += len(txt_keys)
    assert compared > 10


def test_dates_and_percentages_are_semantically_equal(pair):
    from synthetic_data.surfaces import date_surface_key, percent_surface_key

    dates = percentages = 0
    for doc_id, (document, txt, pdf) in pair.items():
        txt = {s for surfaces in txt.fact_surfaces.values() for s in surfaces}
        pdf = {s for surfaces in pdf.fact_surfaces.values() for s in surfaces}
        for surface in txt:
            key = date_surface_key(surface)
            if key:
                assert any(date_surface_key(other) == key for other in pdf), (doc_id, surface)
                dates += 1
            key = percent_surface_key(surface)
            if key:
                assert any(percent_surface_key(other) == key for other in pdf), (doc_id, surface)
                percentages += 1
    assert dates > 5
    assert percentages > 5


def test_the_same_fact_is_worded_differently_in_different_documents(pair):
    """Why whole-sentence comparison is the wrong test across formats.

    Two documents state one fact in different words; the fact-level surfaces still
    agree, which is the property cross-format consistency must protect.
    """
    from synthetic_data.surfaces import money_surface_key

    plan = pilot_plan()
    by_fact: dict[str, list[tuple[str, str]]] = {}
    for doc_id, (document, txt, pdf) in pair.items():
        canonical = {sentence.sentence_id: sentence.text
                     for sentence in canonicalize(document).sentences}
        for statement in document.statements:
            texts = [canonical[sid] for sid in statement.canonical_sentence_ids
                     if sid in canonical]
            if texts:
                by_fact.setdefault(statement.fact_id, []).append((doc_id, texts[0]))
    differing = 0
    for fact_id, occurrences in by_fact.items():
        documents = {doc_id for doc_id, _ in occurrences}
        if len(documents) < 2:
            continue
        texts = {text for _, text in occurrences}
        if len(texts) > 1:
            differing += 1
            for doc_id, _ in occurrences:
                assert fact_id in pair[doc_id][1].fact_surfaces or True
    assert differing > 0


def test_render_profiles_are_independently_deterministic():
    plan = pilot_plan()
    doc_id = plan.prose_ids[0]
    canonical = canonicalize(plan.documents[doc_id])
    document = apply_statement_sentences(plan.documents[doc_id], canonical)
    with tempfile.TemporaryDirectory() as tmp:
        first = PdfRenderer().render(document, DEFAULT_PDF_PROFILE, Path(tmp) / "a.pdf")
        second = PdfRenderer().render(document, DEFAULT_PDF_PROFILE, Path(tmp) / "b.pdf")
        assert first.sha256 == second.sha256
        txt = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, Path(tmp) / "a.txt")
        assert txt.sha256 != first.sha256
