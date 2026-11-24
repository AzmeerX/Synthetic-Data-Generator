"""Phase C — actual Synth ingestion of the rendered TXT fixtures (§8, acceptance).

Every assertion here runs against the vendored ``u01_parser.ingest_path`` output
held in a fresh in-memory SQLite store.  Nothing is reconstructed from model text:
quotes are sliced out of ``DocView.text`` and compared to the store's own sentence
rows.
"""
from __future__ import annotations

import pytest

from synth.modules.common import DocView

from synthetic_data.alignment import (AlignmentError, align_document, align_facts,
                                      require_fact_evidence, EVIDENCE_KINDS)
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.model import (Block, CdmDocument, FactStatement, InlineRun,
                                  Section)

from synthetic_data.tests.v3_txt import pilot_plan, txt_config, txt_ingested, txt_rendered, txt_store

PILOT_TYPES = {"case_notes", "due_diligence", "financial_agreement", "memorandum",
               "board_minutes", "press_release", "correspondence"}


# ---------------------------------------------------------------------------
# helpers: doctored views, to prove the checks refuse rather than repair
# ---------------------------------------------------------------------------
def _rebuild(view, rows) -> DocView:
    """A DocView whose text is exactly the concatenation of ``rows``."""
    texts = [view.sentence_text(row) for row in rows]
    parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for index, text in enumerate(texts):
        if index:
            parts.append("\n")
            cursor += 1
        offsets.append((cursor, cursor + len(text)))
        parts.append(text)
        cursor += len(text)
    rebuilt = []
    for row, (start, end) in zip(rows, offsets):
        rebuilt.append({**row, "char_start": start, "char_end": end})
    return DocView(doc=view.doc, text="".join(parts), text_version=view.text_version,
                   sentences=rebuilt, config=view.config)


def _view_missing(view, needle: str) -> DocView:
    rows = [row for row in view.sentences if needle not in view.sentence_text(row)]
    assert len(rows) < len(view.sentences), "needle was not present"
    return _rebuild(view, rows)


def _view_duplicated(view, needle: str) -> DocView:
    rows = list(view.sentences)
    for row in view.sentences:
        if needle in view.sentence_text(row):
            rows.insert(rows.index(row) + 1,
                        {**row, "sentence_id": f"{row['sentence_id']}_dup",
                         "idx": row["idx"] + 10000})
            break
    else:                                   # pragma: no cover - guarded by the test
        raise AssertionError("needle was not present")
    return _rebuild(view, rows)


# ---------------------------------------------------------------------------
def test_every_document_ingests_and_segments(txt_ingested, txt_rendered):
    assert set(txt_ingested) == {rendered.doc_id for rendered in txt_rendered}
    for doc_id, ingested in txt_ingested.items():
        assert ingested.view.sentences, doc_id
        assert ingested.summary["doc_id"] == ingested.view.doc_id
        assert len(ingested.view.text) == len(ingested.rendered.text)


def test_all_seven_prose_types_reach_the_store(txt_ingested):
    types = {ingested.rendered.doc_type for ingested in txt_ingested.values()
             if ingested.rendered.doc_id.startswith("doc:000")}
    assert types == PILOT_TYPES


def test_parser_text_is_the_rendered_text(txt_ingested):
    """Provenance is only meaningful if the store keeps the bytes we wrote."""
    for ingested in txt_ingested.values():
        assert ingested.view.text == ingested.rendered.text


def test_logical_page_counts_are_correct(txt_ingested):
    for ingested in txt_ingested.values():
        rendered_pages = ingested.rendered.pages
        stored_pages = ingested.view.doc.get("pages")
        assert stored_pages == rendered_pages, ingested.doc_id
        assert len(ingested.view.text.split("\f")) == stored_pages
        assert max(span.loc.page for span in ingested.rendered.result.spans) == stored_pages


def test_nothing_rendered_is_silently_missing(txt_ingested):
    for ingested in txt_ingested.values():
        for line in ingested.rendered.lines:
            if line.strip():
                assert line in ingested.view.text, (ingested.doc_id, line[:60])
        kinds = {alignment.kind for alignment in ingested.alignments}
        assert kinds <= EVIDENCE_KINDS, (ingested.doc_id, kinds)
        assert not [a for a in ingested.alignments if a.kind not in EVIDENCE_KINDS]


def test_alignment_ids_exist_and_kinds_are_valid(txt_ingested):
    for ingested in txt_ingested.values():
        canonical_ids = canonicalize(ingested.rendered.document).sentence_ids
        assert [a.canonical_id for a in ingested.alignments] == canonical_ids
        for alignment in ingested.alignments:
            if alignment.kind in EVIDENCE_KINDS:
                assert alignment.sentence_ids
                assert set(alignment.sentence_ids) <= set(ingested.view.by_id)
                assert alignment.text
                assert ingested.view.text[alignment.char_start:alignment.char_end] \
                    == alignment.text
            if alignment.kind == "split":
                assert len(alignment.sentence_ids) > 1


def test_canonical_and_parser_ids_stay_distinct(txt_ingested):
    for ingested in txt_ingested.values():
        canonical_ids = canonicalize(ingested.rendered.document).sentence_ids
        assert set(canonical_ids).isdisjoint(ingested.view.by_id)
        for alignment in ingested.alignments:
            for sentence_id in alignment.sentence_ids:
                assert sentence_id in ingested.view.by_id
                assert not sentence_id.startswith("snt:")


def test_every_fact_is_grounded(txt_ingested):
    for ingested in txt_ingested.values():
        require_fact_evidence(list(ingested.evidence))
        assert ingested.evidence, ingested.doc_id
        for item in ingested.evidence:
            assert item.acceptable, (item.statement_id, item.problems)


def test_quotes_are_exact_parser_substrings(txt_ingested):
    for ingested in txt_ingested.values():
        for item in ingested.evidence:
            assert item.quote
            assert ingested.view.text[item.char_start:item.char_end] == item.quote
            assert item.char_start < item.char_end


def test_fact_surfaces_are_recoverable_from_parser_text(txt_ingested):
    for ingested in txt_ingested.values():
        quotes: dict[str, list[str]] = {}
        for item in ingested.evidence:
            quotes.setdefault(item.fact_id, []).append(item.quote)
        for fact_id, surfaces in ingested.rendered.result.fact_surfaces.items():
            blob = " ".join(quotes.get(fact_id, []))
            for surface in surfaces:
                assert surface in blob, (ingested.doc_id, fact_id, surface)


def test_provenance_spans_point_at_the_right_source_location(txt_ingested):
    for ingested in txt_ingested.values():
        by_id = {alignment.canonical_id: alignment
                 for alignment in ingested.alignments}
        checked = 0
        for span in ingested.rendered.result.spans:
            if span.kind != "sentence":
                continue
            alignment = by_id.get(span.canonical_id)
            assert alignment is not None, span.canonical_id
            assert ingested.view.text[span.loc.char_start:span.loc.char_end] \
                == span.text
            if alignment.kind in {"exact", "normalised"}:
                # the store kept the bytes, so render and parse offsets agree
                assert alignment.char_start == span.loc.char_start
                assert alignment.char_end == span.loc.char_end
            else:                       # merged/split: the parser span is wider
                assert alignment.char_start <= span.loc.char_start
                assert alignment.char_end >= span.loc.char_end
            checked += 1
        assert checked


def test_sentence_spans_land_in_the_page_they_claim(txt_ingested):
    for ingested in txt_ingested.values():
        starts = [0]
        for index, char in enumerate(ingested.view.text):
            if char == "\f":
                starts.append(index + 1)
        for span in ingested.rendered.result.spans:
            if span.kind != "sentence":
                continue
            page = max(number for number, start in enumerate(starts, start=1)
                       if span.loc.char_start >= start)
            assert page == span.loc.page


def test_cross_document_facts_are_grounded_in_every_document(txt_ingested):
    plan = pilot_plan()
    seen: dict[str, list] = {}
    for doc_id, ingested in txt_ingested.items():
        if doc_id not in plan.prose_ids:
            continue
        for item in ingested.evidence:
            seen.setdefault(item.fact_id, []).append((doc_id, item))
    shared = {fact: items for fact, items in seen.items()
              if len({doc_id for doc_id, _ in items}) > 1}
    assert shared, "the pilot must state some fact in more than one document"
    for fact_id, items in shared.items():
        for doc_id, item in items:
            assert item.acceptable, (fact_id, doc_id, item.problems)
            assert item.quote in txt_ingested[doc_id].view.text


def test_contradiction_sides_are_both_grounded(txt_ingested):
    world = pilot_plan().world
    contradictions = list(world.contradictions.values())
    assert contradictions, "the pilot must contain a contradiction"
    plan = pilot_plan()
    for contradiction in contradictions:
        sides = []
        for doc_id in contradiction.documents:
            if doc_id not in txt_ingested:
                continue
            statements = {statement.fact_id: statement
                          for statement in plan.documents[doc_id].statements}
            evidence = {item.fact_id: item
                        for item in txt_ingested[doc_id].evidence}
            for fact_id in (contradiction.fact_a, contradiction.fact_b):
                if fact_id in statements and fact_id in evidence:
                    item = evidence[fact_id]
                    assert item.acceptable, (doc_id, fact_id, item.problems)
                    sides.append((doc_id, item.quote))
        assert len(sides) >= 2, contradiction.contradiction_id
        assert sides[0][1] != sides[1][1]


def _toy_document(sentence: str):
    """A one-sentence document whose single statement must be grounded."""
    document = CdmDocument(
        document_id="doc:toy01", doc_type="memorandum", structure_id="fixture:toy",
        sections=[Section(section_id="sec:toy01/01", depth=0, number="1",
                          heading="Toy",
                          blocks=[Block(block_id="blk:toy01/01.01", kind="paragraph",
                                        runs=[InlineRun(text=sentence,
                                                        fact_ref="fact:edge:toy01:01")])])],
        statements=[FactStatement(statement_id="stmt:toy01/01.a",
                                  fact_id="fact:edge:toy01:01",
                                  node_id="blk:toy01/01.01", surface=sentence,
                                  value_slots={})],
        plan_fingerprint="fixture:toy")
    canonical = canonicalize(document)
    return apply_statement_sentences(document, canonical), canonical


def _toy_view(sentences: list[str]) -> DocView:
    parts: list[str] = []
    rows = []
    cursor = 0
    for index, text in enumerate(sentences):
        if index:
            parts.append("\n")
            cursor += 1
        rows.append({"sentence_id": f"snt_toy{index:03d}", "idx": index,
                     "char_start": cursor, "char_end": cursor + len(text),
                     "filtered": False})
        parts.append(text)
        cursor += len(text)
    return DocView(doc={"doc_id": "doc:toy01", "pages": 1}, text="".join(parts),
                   text_version=1, sentences=rows, config=None)


def test_missing_alignment_is_rejected():
    document, canonical = _toy_document("Alpha beta gamma.")
    view = _toy_view(["Completely unrelated material about shipping and freight."])
    alignments = align_document(canonical, view)
    assert {alignment.kind for alignment in alignments} == {"missing"}
    evidence = align_facts(document, canonical, alignments,
                           {"fact:edge:toy01:01": ["Alpha beta gamma."]}, view.text)
    assert not evidence[0].acceptable
    with pytest.raises(AlignmentError):
        require_fact_evidence(evidence)


def test_ambiguous_alignment_is_rejected():
    document, canonical = _toy_document("Alpha beta gamma")
    # both parser sentences *contain* the canonical sentence: no order-based choice
    view = _toy_view(["Alpha beta gamma delta epsilon zeta.",
                      "Alpha beta gamma delta epsilon eta."])
    alignments = align_document(canonical, view)
    body = alignments[-1]
    assert body.kind == "ambiguous"
    assert len(body.sentence_ids) == 2
    evidence = align_facts(document, canonical, alignments,
                           {"fact:edge:toy01:01": ["Alpha beta gamma"]}, view.text)
    assert not evidence[0].acceptable
    with pytest.raises(AlignmentError):
        require_fact_evidence(evidence)


def test_split_alignment_keeps_the_whole_quote():
    document, canonical = _toy_document("Alpha beta gamma delta.")
    view = _toy_view(["Alpha beta gamma", "delta."])
    alignments = align_document(canonical, view)
    body = alignments[-1]
    assert body.kind == "split"
    assert body.sentence_ids == ("snt_toy000", "snt_toy001")
    assert body.text == "Alpha beta gamma\ndelta."
    evidence = align_facts(document, canonical, alignments,
                           {"fact:edge:toy01:01": ["Alpha beta gamma delta."]}, view.text)
    assert evidence[0].acceptable
    assert evidence[0].quote in view.text


def test_merged_alignment_is_accepted_when_the_quote_contains_the_fact():
    document, canonical = _toy_document("Alpha beta gamma.")
    view = _toy_view(["- Alpha beta gamma."])
    alignments = align_document(canonical, view)
    assert alignments[-1].kind == "merged"
    evidence = align_facts(document, canonical, alignments,
                           {"fact:edge:toy01:01": ["Alpha beta gamma."]}, view.text)
    assert evidence[0].acceptable


def test_fact_without_sentences_is_rejected(txt_ingested):
    """A statement with no canonical sentence id must never pass as evidence."""
    ingested = next(item for item in txt_ingested.values()
                    if item.rendered.doc_id.startswith("doc:000"))
    document = ingested.rendered.document
    stripped = document.model_copy(update={
        "statements": [statement.model_copy(update={"canonical_sentence_ids": []})
                       for statement in document.statements]})
    canonical = canonicalize(stripped)
    alignments = align_document(canonical, ingested.view)
    evidence = align_facts(stripped, canonical, alignments,
                           ingested.rendered.result.fact_surfaces, ingested.view.text)
    with pytest.raises(AlignmentError):
        require_fact_evidence(evidence)


def test_ingest_does_not_write_back_into_the_model(txt_ingested):
    """Parsing must not mutate the canonical document it was rendered from."""
    for ingested in txt_ingested.values():
        assert ingested.rendered.document.statements
        for statement in ingested.rendered.document.statements:
            assert statement.canonical_sentence_ids
            assert all(sentence_id.startswith("snt:")
                       for sentence_id in statement.canonical_sentence_ids)
