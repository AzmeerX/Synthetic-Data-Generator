"""Gold alignment for prose: exactness, defects, and cross-document identity.

Every test here works on real parser output.  The defect tests render a document,
then feed the parser a *changed* file and prove the gold build refuses it — a
detector that only runs on hand-made data would not show that the pipeline catches
a real drift.
"""
from __future__ import annotations

import pytest

from synth.eval.gold import GoldEvidence, GoldSet, load_gold_file

from synthetic_data.accounting import account_parser_content, require_full_accounting
from synthetic_data.alignment import AlignmentError, FactEvidence, align_document, \
    surface_in_quote
from synthetic_data.gold import GoldBuildError, GoldInputs, build_gold, validate_alignment, \
    validate_pair, validate_v1, validate_v2, write_gold_pair
from synthetic_data.gold.defects import detect_defects
from synthetic_data.model import Money
from synthetic_data.surfaces import (money_surface_key, money_surfaces,
                                     surfaces_equivalent)

from synthetic_data.tests.v3_gold import (gold_store, gold_txt, gold_pdf, gold_docx,
                                          gold_workbook, gold_pilot, mutate_text,
                                          pilot_plan)

MUTATION_TARGETS = {"txt", "pdf", "docx"}


# -- exactness -------------------------------------------------------------
def test_txt_gold_pair_validates(tmp_path, gold_store, gold_txt):
    v1, v2, alignment = write_gold_pair(gold_txt.pair, tmp_path / "gold",
                                        gold_txt.document_id, gold_txt.document.doc_type,
                                        "txt")
    report = validate_pair(v1, v2, alignment, store=gold_store, view=gold_txt.view,
                           render=gold_txt.render, canonical=gold_txt.canonical,
                           document=gold_txt.document, world=pilot_plan().world)
    assert report.ok, report.errors[:5]
    assert report["items"] > 0


@pytest.mark.parametrize("fixture", ["gold_pdf", "gold_docx"])
def test_every_format_produces_a_valid_pair(fixture, tmp_path, gold_store, request):
    golded = request.getfixturevalue(fixture)
    v1, v2, alignment = write_gold_pair(golded.pair, tmp_path / fixture,
                                        golded.document_id,
                                        golded.document.doc_type, golded.format)
    report = validate_pair(v1, v2, alignment, store=gold_store, view=golded.view,
                           render=golded.render, canonical=golded.canonical,
                           document=golded.document, world=pilot_plan().world)
    assert report.ok, report.errors[:5]


@pytest.mark.parametrize("fixture", ["gold_txt", "gold_pdf", "gold_docx"])
def test_every_quote_is_an_exact_parser_substring(fixture, request):
    golded = request.getfixturevalue(fixture)
    text = golded.view.text
    items = 0
    for edge in golded.pair.v2.edges:
        for item in edge.evidence:
            assert item.quote in text, (edge.id, item.quote)
            items += 1
    for prop in golded.pair.v2.propositions:
        for item in prop.evidence:
            assert item.quote in text, (prop.id, item.quote)
            items += 1
    assert items > 0


def test_pdf_alignments_include_a_split_or_merged_case(gold_pdf):
    kinds = gold_pdf.pair.alignment.counts
    assert kinds.get("split", 0) + kinds.get("merged", 0) > 0
    for unit in gold_pdf.pair.alignment.units:
        if unit.kind in {"split", "merged"}:
            assert unit.parser_sentence_ids
            assert unit.char_start is not None and unit.char_end is not None


def test_pdf_records_a_physical_page_and_txt_a_logical_one(gold_pdf, gold_txt):
    pdf_pages = {item.render_loc["page"]
                 for edge in gold_pdf.pair.v2.edges for item in edge.evidence}
    assert pdf_pages, "a PDF must record a page for every evidence item"
    assert all(isinstance(page, int) and page >= 1 for page in pdf_pages)
    assert max(pdf_pages) <= gold_pdf.pair.v2.doc.pages
    assert gold_pdf.pair.v2.doc.pageless is False

    rendered = gold_txt.path.read_text(encoding="utf-8")
    rendered_pages = len(rendered.split("\f"))
    txt_pages = {item.render_loc["page"]
                 for edge in gold_txt.pair.v2.edges for item in edge.evidence}
    assert txt_pages, "a TXT document must record a logical page too"
    assert all(isinstance(page, int) and 1 <= page <= rendered_pages
               for page in txt_pages)
    assert gold_txt.pair.v2.doc.pages == len(
        {row["page"] for row in gold_txt.view.sentences if row["page"] is not None})


def test_docx_is_pageless_by_contract(gold_docx):
    assert gold_docx.pair.v2.doc.pageless is True
    assert gold_docx.pair.v2.doc.pages is None
    for edge in gold_docx.pair.v2.edges:
        for item in edge.evidence:
            assert item.render_loc["page"] is None
            assert item.render_loc["pageless"] is True


def test_txt_page_numbers_are_logical_not_invented(gold_txt):
    pages = {row["page"] for row in gold_txt.view.sentences}
    assert pages and all(page is not None for page in pages)
    # the logical page count matches the renderer's form feeds, not a guess
    assert len(pages) == gold_txt.pair.v2.doc.pages


# -- defects ---------------------------------------------------------------
def _money_evidence(golded):
    """The first evidence item whose fact has a typed money value."""
    for edge in golded.pair.v2.edges:
        for item in edge.evidence:
            if item.currency and money_surfaces(item.quote):
                return item
    raise AssertionError("no money evidence in this document")


def _different_surface(surface: str) -> str:
    """The same shape, a different amount: formatting preserved, value changed."""
    import re

    match = re.search(r"[0-9][0-9,]*(?:\.\d+)?", surface)
    assert match is not None
    digits = match.group(0)
    whole, _, decimals = digits.partition(".")
    amount = int(whole.replace(",", ""))
    changed = f"{amount + 1:,}"
    if decimals:
        changed += "." + decimals
    return surface[:match.start()] + changed + surface[match.end():]


def test_an_altered_amount_fails_the_build(tmp_path, gold_txt):
    item = _money_evidence(gold_txt)
    surface = money_surfaces(item.quote)[0]
    replacement = _different_surface(surface)
    assert money_surface_key(surface) != money_surface_key(replacement)

    original = gold_txt.path.read_text(encoding="utf-8")
    assert surface in original, "the test must change a surface the file really contains"

    def change(text: str) -> str:
        return text.replace(surface, replacement, 1)

    path, view, doc_id = mutate_text(gold_txt, change, tmp_path)
    changed = path.read_text(encoding="utf-8")
    assert changed != original and replacement in changed

    with pytest.raises(GoldBuildError,
                       match="altered|extra|unexplained|cannot be grounded|not in"):
        build_gold(GoldInputs(document=gold_txt.document, canonical=gold_txt.canonical,
                              render=gold_txt.render, view=view, parse_doc_id=doc_id,
                              filename=path.name, world=pilot_plan().world, seed=7))


def _fact_bearing_row(golded):
    """A canonical row that states at least one fact — the row whose loss matters."""
    for sentence in golded.canonical.sentences:
        if sentence.kind == "table_row" and sentence.fact_ids:
            return sentence
    raise AssertionError("no fact-bearing table row in this document")


def test_a_missing_table_row_fails_the_build(tmp_path, gold_txt):
    row = _fact_bearing_row(gold_txt)
    original = gold_txt.path.read_text(encoding="utf-8")
    assert row.text in original, "the selected row must be in the rendered file"
    assert row.text in original.split("\n")

    def drop_row(text: str) -> str:
        lines = text.split("\n")
        return "\n".join(line for line in lines if line != row.text)

    path, view, doc_id = mutate_text(gold_txt, drop_row, tmp_path)
    assert row.text not in path.read_text(encoding="utf-8")

    with pytest.raises(GoldBuildError,
                       match="missing|not found|not in|unexplained|cannot be grounded"):
        build_gold(GoldInputs(document=gold_txt.document, canonical=gold_txt.canonical,
                              render=gold_txt.render, view=view, parse_doc_id=doc_id,
                              filename=path.name, world=pilot_plan().world, seed=7))


def _last_fact_row(golded):
    """The last fact-bearing row: duplicating it cannot shift any later row."""
    rows = [sentence for sentence in golded.canonical.sentences
            if sentence.kind == "table_row" and sentence.fact_ids]
    assert rows, "no fact-bearing table row in this document"
    return rows[-1]


def _duplicate_line(golded, line_text: str, tmp_path):
    original = golded.path.read_text(encoding="utf-8")
    lines = original.split("\n")
    positions = [index for index, line in enumerate(lines) if line == line_text]
    assert positions, "the selected row must be a line of the rendered file"
    lines.insert(positions[-1] + 1, line_text)
    out = tmp_path
    out.mkdir(parents=True, exist_ok=True)
    path = out / "duplicated.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    from synthetic_data.tests.v3_gold import ingest_text
    view, doc_id = ingest_text(path)
    return path, view, doc_id, original


def test_a_duplicated_matched_sentence_is_unexplained(tmp_path, gold_txt):
    """The sentence is already matched once; its second copy has no declaration."""
    row = _last_fact_row(gold_txt)
    path, view, doc_id, original = _duplicate_line(gold_txt, row.text, tmp_path)
    changed = path.read_text(encoding="utf-8")
    assert changed != original
    assert changed.count(row.text) == original.count(row.text) + 1

    alignments = align_document(gold_txt.canonical, view)
    report = account_parser_content(gold_txt.document, gold_txt.canonical, view,
                                    alignments, fmt="txt")
    assert not report.ok, "a duplicated sentence must leave an unexplained unit"
    assert any(gap.text.strip() == row.text.strip() for gap in report.gaps)
    with pytest.raises(AlignmentError, match="unexplained"):
        require_full_accounting(report)

    with pytest.raises(GoldBuildError, match="unexplained"):
        build_gold(GoldInputs(document=gold_txt.document, canonical=gold_txt.canonical,
                              render=gold_txt.render, view=view, parse_doc_id=doc_id,
                              filename=path.name, world=pilot_plan().world, seed=7))


def test_an_inserted_fact_sentence_is_unexplained(tmp_path, gold_txt):
    """Content the model never declared cannot be explained by a declaration."""
    fabricated = ("The parties recorded that the Borrower had paid GBP 987,654.32 "
                  "in settlement of all outstanding claims.")
    original = gold_txt.path.read_text(encoding="utf-8")
    assert fabricated not in original
    path, view, doc_id = mutate_text(
        gold_txt, lambda text: text + "\n" + fabricated + "\n", tmp_path)
    alignments = align_document(gold_txt.canonical, view)
    report = account_parser_content(gold_txt.document, gold_txt.canonical, view,
                                    alignments, fmt="txt")
    assert any("987,654.32" in gap.text for gap in report.gaps), report.describe()
    with pytest.raises(GoldBuildError, match="unexplained"):
        build_gold(GoldInputs(document=gold_txt.document, canonical=gold_txt.canonical,
                              render=gold_txt.render, view=view, parse_doc_id=doc_id,
                              filename=path.name, world=pilot_plan().world, seed=7))


def test_every_pilot_artifact_is_fully_accounted(gold_pilot):
    for golded in gold_pilot:
        report = golded.pair.accounting
        assert report is not None
        assert report.ok, report.describe()
        assert report.total == len(golded.view.sentences)


def _evidence(kind: str, quote: str, *, node_id: str = "blk:x/01") -> FactEvidence:
    return FactEvidence(fact_id="fact:edge:x:01", statement_id="stmt:x/01",
                        sentence_ids=("s1",), quote=quote, kinds=(kind,),
                        acceptable=kind in {"exact", "normalised"}, node_id=node_id)


def test_an_unsupported_alignment_is_a_defect():
    defects = detect_defects([_evidence("partial", "something approximately right")], {})
    assert [defect.kind for defect in defects] == ["unsupported"]


def test_an_ambiguous_alignment_is_a_defect():
    defects = detect_defects([_evidence("ambiguous", "same text, two places")], {})
    assert [defect.kind for defect in defects] == ["ambiguous"]


def test_an_unaccounted_money_value_in_a_sentence_is_extra():
    evidence = [_evidence("exact", "The Lender advanced GBP 4,000,000 to the Borrower.")]
    typed = {"fact:edge:x:01": Money(amount_minor=400_000_000, currency="GBP")}
    defects = detect_defects(evidence, typed, known_money={"GBP:400000000"})
    assert not defects
    defects = detect_defects([_evidence("exact",
                                        "The Lender advanced GBP 4,000,000 and GBP 250,000.")],
                             typed, known_money={"GBP:400000000"})
    assert {defect.kind for defect in defects} == {"extra"}


def test_a_row_states_several_amounts_without_being_extra():
    evidence = [_evidence("exact",
                          "| Drawdown | GBP 4,000,000 | GBP 4,000,000 | 6.50% |",
                          node_id="tbl:x/01/r01")]
    typed = {"fact:edge:x:01": Money(amount_minor=400_000_000, currency="GBP")}
    assert detect_defects(evidence, typed, known_money={"GBP:400000000"}) == []


# -- monetary formatting ---------------------------------------------------
def test_money_is_compared_by_value_not_spelling():
    assert surfaces_equivalent("£4,000,000", "GBP 4,000,000.00")
    assert not surfaces_equivalent("£4,000,000", "£4,500,000")
    assert surface_in_quote("GBP 4,000,000.00", "advanced £4,000,000 today")
    assert not surface_in_quote("GBP 4,000,000.00", "advanced 4,000,000 today")


# -- cross-document identity ----------------------------------------------
def test_one_fact_keeps_its_identity_across_documents_and_formats(gold_pilot):
    seen: dict[str, set[str]] = {}
    for golded in gold_pilot:
        for edge in golded.pair.v1.edges:
            seen.setdefault(edge.id, set()).add(golded.document_id)
    shared = {fact: docs for fact, docs in seen.items() if len(docs) > 1}
    assert shared, "no fact is stated in more than one document"
    fact_id, documents = next(iter(shared.items()))
    assert len(documents) >= 2
    assert fact_id.startswith("fact:")
    # the same id is cited by each document's own gold file, never a per-file copy
    stating = [golded for golded in gold_pilot
               if fact_id in {edge.id for edge in golded.pair.v1.edges}]
    assert {golded.document_id for golded in stating} == documents


def test_the_pilot_contains_a_grounded_contradiction(gold_pilot):
    with_flags = [golded for golded in gold_pilot if golded.pair.v2.contradictions]
    assert with_flags, "the pilot must contain at least one contradiction"
    record = with_flags[0].pair.v2.contradictions[0]
    assert record.fact_a != record.fact_b
    assert len(record.documents) >= 2
    assert record.stated_here


def test_entity_mentions_are_grounded_in_parser_text(gold_txt):
    text = gold_txt.view.text
    mentions = 0
    for entity in gold_txt.pair.v2.entities:
        for mention in entity.mentions:
            assert mention.quote in text
            assert mention.quote in entity.name or entity.name in mention.quote
            mentions += 1
    assert mentions > 0


def test_v1_file_has_no_v2_fields(tmp_path, gold_txt):
    v1, _, _ = write_gold_pair(gold_txt.pair, tmp_path / "gold", gold_txt.document_id,
                               gold_txt.document.doc_type, "txt")
    payload = load_gold_file(v1)
    assert isinstance(payload, GoldSet)
    raw = v1.read_text(encoding="utf-8")
    assert "canonical_node_id" not in raw
    assert "alignment_kind" not in raw


def test_evidence_without_a_canonical_sentence_is_refused(gold_txt):
    """A statement that carries no canonical sentence id cannot be grounded."""
    from synthetic_data.alignment import AlignmentError, require_fact_evidence

    with pytest.raises(AlignmentError):
        require_fact_evidence([FactEvidence(
            fact_id="fact:edge:x:01", statement_id="stmt:x/01", sentence_ids=(),
            quote="", kinds=(), acceptable=False,
            problems=("statement carries no canonical sentence id",))])
