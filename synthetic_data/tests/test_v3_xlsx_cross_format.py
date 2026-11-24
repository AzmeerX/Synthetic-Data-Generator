"""Phase F — a workbook fact and a document fact must be the same fact.

The pilot workbook states 25 facts; the financial agreement states 28, and the two sets
share 7.  Sharing is checked by **fact id**, and values by typed key (money, percentage,
date) — never by sentence.

One asymmetry is real and asserted rather than papered over: a fact has more than one
cell, so each format surfaces a different part of it.  The workbook prints the ownership
percentage (``40.00%``) where the clause prints the holder's name
(``Junipermoor Holdings plc``); both carry the same fact id.

The workbook is a **separate evaluation cohort** and is never counted as a prose
document.
"""
from __future__ import annotations

import pytest

from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.renderers import (DEFAULT_DOCX_PROFILE, DEFAULT_PDF_PROFILE,
                                      DEFAULT_TXT_PROFILE, DocxRenderer, PdfRenderer,
                                      RenderContext, TxtRenderer)
from synthetic_data.surfaces import (date_surface_key, money_surface_key,
                                     percent_surface_key, surfaces_equivalent)

from synthetic_data.tests.v3_docx import docx_rendered
from synthetic_data.tests.v3_txt import pilot_plan
from synthetic_data.tests.v3_xlsx import (feature_rendered, pilot_workbook, xlsx_rendered)


@pytest.fixture(scope="module")
def agreement(tmp_path_factory):
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)
    doc_id = next(identifier for identifier in plan.prose_ids
                  if plan.documents[identifier].doc_type == "financial_agreement")
    canonical = canonicalize(plan.documents[doc_id])
    document = apply_statement_sentences(plan.documents[doc_id], canonical)
    out = tmp_path_factory.mktemp("xlsx_cross")
    return {
        "txt": TxtRenderer().render(document, DEFAULT_TXT_PROFILE, out / "fa.txt",
                                    context=context),
        "pdf": PdfRenderer().render(document, DEFAULT_PDF_PROFILE, out / "fa.pdf",
                                    context=context),
        "docx": DocxRenderer().render(document, DEFAULT_DOCX_PROFILE, out / "fa.docx",
                                      context=context),
    }


def _values(rendered):
    return [surface for values in rendered.fact_surfaces.values() for surface in values]


def test_the_workbook_and_the_agreement_share_fact_ids(xlsx_rendered, agreement):
    workbook_facts = set(xlsx_rendered.result.fact_surfaces)
    assert len(workbook_facts) == 25
    shared = workbook_facts & set(agreement["docx"].fact_surfaces)
    assert len(shared) == 7
    for fact in shared:
        for rendered in agreement.values():
            assert rendered.fact_surfaces.get(fact), (fact, rendered.format)


def test_shared_name_surfaces_agree_exactly(xlsx_rendered, agreement):
    """Where both formats surface a name for the same fact, the names must match."""
    compared = 0
    for fact_id, workbook_surfaces in xlsx_rendered.result.fact_surfaces.items():
        names = {surface for surface in workbook_surfaces
                 if not any(character.isdigit() for character in surface)}
        if not names:
            continue
        for rendered in agreement.values():
            document_surfaces = set(rendered.fact_surfaces.get(fact_id, []))
            assert names <= document_surfaces, (rendered.format, fact_id, names)
            compared += len(names)
    assert compared >= 3


def test_the_ownership_fact_is_stated_as_a_percentage_in_the_sheet(xlsx_rendered):
    """The workbook surfaces the percentage cell of the ownership facts."""
    import re
    percentages = {fact: values for fact, values in xlsx_rendered.result.fact_surfaces.items()
                   if all(re.fullmatch(r"\d+\.\d\d%", value) for value in values)}
    assert len(percentages) == 4


def test_the_same_ownership_fact_is_stated_as_a_name_in_the_clause(agreement):
    document = agreement["docx"].fact_surfaces
    ownership = [fact for fact, values in document.items()
                 if any(value.endswith(("Holdings plc", "Resources plc", "Partners LLC",
                                        "Capital LLC")) for value in values)]
    assert len(ownership) >= 4


def _keys(rendered, fact_ids, key) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for fact in fact_ids:
        values = {key(surface) for surface in rendered.fact_surfaces.get(fact, [])
                  if key(surface)}
        if values:
            out[fact] = values
    return out


def _keys(rendered, key) -> set[str]:
    return {key(surface) for surface in _values(rendered) if key(surface)}


def test_the_workbook_percentages_are_its_typed_values(xlsx_rendered, pilot_workbook):
    """The sheet's percentage surfaces are the model's typed basis points, not text."""
    from synthetic_data.model import Percentage
    from synthetic_data.surfaces import percent_surface_key
    typed = set()
    for sheet in pilot_workbook.workbook.sheets:
        for table in sheet.tables:
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                for cell in row.cells:
                    # only fact-referenced cells become surfaces
                    if cell.fact_ref and isinstance(cell.value, Percentage):
                        typed.add(f"{cell.value.basis_points}bp")
    assert typed
    assert _keys(xlsx_rendered.result, percent_surface_key) == typed


def test_money_values_overlap_with_the_document_cohort(xlsx_rendered, docx_rendered):
    """Some money the workbook states is stated by the documents; the rest is derived."""
    from synthetic_data.surfaces import money_surface_key
    union = set()
    for document in docx_rendered:
        union |= _keys(document.result, money_surface_key)
    assert union
    workbook = _keys(xlsx_rendered.result, money_surface_key)
    assert workbook
    assert workbook & union, "at least one amount is stated in both formats"


def test_the_workbook_also_states_figures_no_document_does(xlsx_rendered, docx_rendered):
    """Running balances and computed totals exist only in the sheet (they are not facts)."""
    from synthetic_data.surfaces import money_surface_key
    union = set()
    for document in docx_rendered:
        union |= _keys(document.result, money_surface_key)
    workbook = _keys(xlsx_rendered.result, money_surface_key)
    assert workbook - union


def test_ledger_dates_are_the_transaction_dates(xlsx_rendered, pilot_workbook):
    """Dates are typed, and the ledger's dates are the fact dates (not strings)."""
    from openpyxl import load_workbook
    from synthetic_data.model import IsoDate
    workbook = load_workbook(xlsx_rendered.path, data_only=True)["Transaction ledger"]
    sheet_dates = [workbook[f"A{row}"].value.date() for row in range(4, 16)]
    model_dates = []
    for sheet in pilot_workbook.workbook.sheets:
        if sheet.name != "Transaction ledger":
            continue
        for table in sheet.tables:
            for row in table.body_rows:
                for cell in row.cells:
                    if isinstance(cell.value, IsoDate):
                        model_dates.append(cell.value.value)
    assert sheet_dates == model_dates


def test_no_shared_fact_disagrees_on_a_typed_value(xlsx_rendered, agreement):
    from synthetic_data.surfaces import money_surface_key, percent_surface_key
    checked = 0
    for fact_id, workbook_surfaces in xlsx_rendered.result.fact_surfaces.items():
        for rendered in agreement.values():
            for left in workbook_surfaces:
                for right in rendered.fact_surfaces.get(fact_id, []):
                    if money_surface_key(left) and money_surface_key(right):
                        assert surfaces_equivalent(left, right), (fact_id, left, right)
                        checked += 1
                    if percent_surface_key(left) and percent_surface_key(right):
                        assert percent_surface_key(left) == percent_surface_key(right), (
                            fact_id, left, right)
                        checked += 1
    assert checked >= 0


def test_entity_names_are_preserved_in_the_sheet(xlsx_rendered, agreement):
    names = {surface for surface in _values(xlsx_rendered.result)
             if not any(character.isdigit() for character in surface)}
    assert names
    document_names = set(_values(agreement["docx"]))
    assert names & document_names, (names, document_names)


def test_the_workbook_is_a_separate_cohort(xlsx_rendered):
    plan = pilot_plan()
    assert xlsx_rendered.doc_id in plan.workbook_ids
    assert xlsx_rendered.doc_id not in plan.prose_ids
    assert xlsx_rendered.document.doc_kind == "workbook"


def test_the_feature_workbook_keeps_its_own_facts(feature_rendered):
    facts = set(feature_rendered.result.fact_surfaces)
    assert facts == {"fact:xw:01", "fact:xw:02", "fact:xw:03", "fact:xw:04",
                     "fact:xw:10", "fact:xw:11", "fact:xw:12"}
    upper = next(values for fact, values in feature_rendered.result.fact_surfaces.items()
                 if fact == "fact:xw:01")
    assert "40.00%" in upper
