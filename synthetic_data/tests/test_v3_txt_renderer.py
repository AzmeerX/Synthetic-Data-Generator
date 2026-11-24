"""Phase C — the canonical TXT renderer: coverage, determinism, layout, provenance."""
from __future__ import annotations

import re
import tempfile
from collections import Counter
from pathlib import Path

import pytest

from synthetic_data.canonical import canonicalize, split_sentences
from synthetic_data.model import RenderMap, RenderSpan
from synthetic_data.renderers import DEFAULT_TXT_PROFILE, TxtRenderer, layout_lines

from synthetic_data.tests.v3_txt import (PILOT, feature_document, pilot_plan,
                                         render_document, subprocess_digests,
                                         subprocess_digests_with_seed, txt_config,
                                         txt_ingested, txt_rendered, txt_store)

PROSE_TYPES = {"case_notes", "due_diligence", "financial_agreement", "memorandum",
               "board_minutes", "press_release", "correspondence"}
SENTENCE_ID = re.compile(r"^snt:[A-Za-z0-9][A-Za-z0-9:._/\-]*$")


@pytest.fixture(scope="module")
def documents(txt_rendered):
    return {item.doc_id: item for item in txt_rendered}


def line_at(rendered, page, line_no):
    """Line ``line_no`` (1-based, page-relative) of logical page ``page``."""
    return rendered.text.split("\f")[page - 1].split("\n")[line_no - 1]


def test_every_canonical_node_is_rendered_exactly_once(documents):
    for rendered in documents.values():
        canonical = canonicalize(rendered.document)
        expected = rendered.document.node_ids() | set(canonical.sentence_ids)
        counted = Counter(span.canonical_id for span in rendered.result.spans)
        assert not (expected - set(counted)), f"{rendered.doc_id}: missing nodes"
        assert not (set(counted) - expected), f"{rendered.doc_id}: unknown nodes"
        assert {cid for cid, n in counted.items() if n > 1} == set()


def test_render_is_deterministic_within_a_process(documents, tmp_path):
    renderer = TxtRenderer()
    for rendered in documents.values():
        path = tmp_path / f"{rendered.doc_id.split(':')[1]}.txt"
        again = renderer.render(rendered.document, DEFAULT_TXT_PROFILE, path)
        assert Path(again.path).read_bytes() == rendered.path.read_bytes()
        assert again.sha256 == rendered.result.sha256
        assert [span.canonical_id for span in again.spans] == [
            span.canonical_id for span in rendered.result.spans]


def test_render_is_byte_identical_across_processes(documents):
    local = {doc_id: item.result.sha256 for doc_id, item in documents.items()
             if doc_id.startswith("doc:000")}
    assert subprocess_digests() == local
    assert subprocess_digests_with_seed(13) == local


def test_sentence_ids_are_deterministic_unique_and_namespaced(documents):
    for rendered in documents.values():
        first = canonicalize(rendered.document)
        second = canonicalize(rendered.document)
        assert [s.sentence_id for s in first.sentences] == [
            s.sentence_id for s in second.sentences]
        ids = [s.sentence_id for s in first.sentences]
        assert len(ids) == len(set(ids))
        for sentence_id in ids:
            assert SENTENCE_ID.match(sentence_id), sentence_id
            assert sentence_id.startswith("snt:")


def test_parser_sentence_ids_do_not_collide_with_canonical_ones(txt_ingested):
    for ingested in txt_ingested.values():
        canonical = {s.sentence_id for s in canonicalize(ingested.rendered.document).sentences}
        parser_ids = set(ingested.view.by_id)
        assert canonical.isdisjoint(parser_ids)
        assert not {pid for pid in parser_ids if SENTENCE_ID.match(pid)}


def test_multi_sentence_paragraphs_are_individually_addressable(documents):
    checked = 0
    for rendered in documents.values():
        canonical = canonicalize(rendered.document)
        synthetic = CanonicalParagraphs(canonical)
        for block_id, count in synthetic.multi.items():
            assert count >= 2
            sentences = canonical.for_block(block_id)
            assert len(sentences) == count
            for sentence in sentences:
                span = next(span for span in rendered.result.spans
                            if span.canonical_id == sentence.sentence_id)
                assert span.text == sentence.text
                assert span.kind == "sentence"
            block = next(block for section in rendered.document.sections
                         for block in section.blocks if block.block_id == block_id)
            source = " ".join(run.text for run in block.runs or [])
            assert " ".join(sentence.text for sentence in sentences) == \
                " ".join(source.split())
            checked += 1
    assert checked >= 10, "expected many multi-sentence paragraphs in the pilot"


class CanonicalParagraphs:
    """Counts sentences per paragraph block (test-only helper)."""

    def __init__(self, canonical):
        self.multi = {}
        for block_id, sentences in canonical.by_block.items():
            if len(sentences) > 1 and all(s.kind == "sentence" for s in sentences):
                self.multi[block_id] = len(sentences)


def test_no_sentence_is_split_across_lines(documents):
    for rendered in documents.values():
        canonical = canonicalize(rendered.document)
        for sentence in canonical.sentences:
            assert "\n" not in sentence.text
            span = next(span for span in rendered.result.spans
                        if span.canonical_id == sentence.sentence_id)
            text = rendered.text[span.loc.char_start:span.loc.char_end]
            assert text == span.text
            assert "\n" not in text


def test_split_sentences_never_lose_or_duplicate_text():
    samples = [
        "One sentence. Two sentences! Three? Four; still four.",
        "Harrow & Finch LLP acted. Dr. Kerr signed on 3 March 2024.",
        "Gareth H. Chen attended. Mei T. Nyle apologised.",
        "Clause 22. The Borrower shall comply.",
    ]
    for sample in samples:
        spans = split_sentences(sample)
        pieces = [sample[start:end] for start, end in spans]
        assert " ".join(pieces) == " ".join(sample.split())
        assert spans[0][0] == 0
        assert spans[-1][1] == len(sample)


def test_fact_surfaces_are_exact_renderings(documents):
    for rendered in documents.values():
        for fact_id, surfaces in rendered.result.fact_surfaces.items():
            assert surfaces, fact_id
            for surface in surfaces:
                assert surface in rendered.text, (rendered.doc_id, fact_id, surface)


def test_every_statement_points_at_a_sentence_and_a_fact(documents):
    world = pilot_plan().world
    pilot_ids = set(pilot_plan().prose_ids)
    for rendered in documents.values():
        if rendered.doc_id not in pilot_ids:
            continue
        for statement in rendered.document.statements:
            assert statement.canonical_sentence_ids, statement.statement_id
            assert statement.fact_id in world.facts, statement.statement_id
            sentences = canonicalize(rendered.document).sentence_ids
            assert set(statement.canonical_sentence_ids) <= set(sentences)


def test_table_rows_are_pipe_delimited_and_cells_locate(documents):
    checked = 0
    for rendered in documents.values():
        row_spans = [span for span in rendered.result.spans if span.kind == "row"]
        for span in row_spans:
            text = rendered.text[span.loc.char_start:span.loc.char_end]
            assert text == span.text
            assert text.startswith("| ") and text.endswith(" |")
            assert line_at(rendered, span.loc.page, span.loc.line_no) == text
            checked += 1
        cell_spans = [span for span in rendered.result.spans if span.kind == "cell"]
        for span in cell_spans:
            assert rendered.text[span.loc.char_start:span.loc.char_end] == span.text
    assert checked > 20


def test_section_hierarchy_follows_document_order(documents):
    for rendered in documents.values():
        section_spans = [span for span in rendered.result.spans if span.kind == "section"]
        assert len(section_spans) == len(rendered.document.sections)
        positions = [span.loc.char_start for span in section_spans]
        assert positions == sorted(positions)
        for span, section in zip(section_spans, rendered.document.sections):
            assert span.canonical_id == section.section_id
            assert span.text.startswith(section.number or "")


def test_character_and_line_spans_are_exact(documents):
    for rendered in documents.values():
        pages = rendered.text.split("\f")
        page_starts = [0]
        for index, char in enumerate(rendered.text):
            if char == "\f":
                page_starts.append(index + 1)
        for span in rendered.result.spans:
            assert rendered.text[span.loc.char_start:span.loc.char_end] == span.text
            page_text = pages[span.loc.page - 1]
            lines = page_text.split("\n")
            assert 1 <= span.loc.line_no <= len(lines)
            line_start = page_starts[span.loc.page - 1] + sum(
                len(line) + 1 for line in lines[:span.loc.line_no - 1])
            assert span.loc.char_start >= line_start
            if span.kind in {"sentence", "section", "row", "cell"}:
                assert span.loc.char_start + len(span.text) <= line_start + len(
                    lines[span.loc.line_no - 1])


def test_page_locations_are_logical_and_monotonic(documents):
    for rendered in documents.values():
        pages = [span.loc.page for span in rendered.result.spans]
        assert min(pages) == 1
        assert max(pages) == rendered.pages
        assert rendered.text.count("\f") == rendered.pages - 1
        assert all(span.loc.pageless is False for span in rendered.result.spans)
        assert all(span.loc.bbox is None for span in rendered.result.spans)
        roots = sum(1 for section in rendered.document.sections if section.depth == 0)
        assert rendered.pages >= roots
        for span in rendered.result.spans:
            assert span.loc.page is not None and span.loc.page >= 1


@pytest.fixture(scope="module")
def feature():
    return render_document(feature_document(), Path(tempfile.mkdtemp()) / "feature.txt")


def test_explicit_page_break_starts_a_new_page(feature):
    rendered = feature
    break_span = next(span for span in rendered.result.spans
                      if span.canonical_id == "blk:90001/02.02")
    after = next(span for span in rendered.result.spans
                 if span.canonical_id == "blk:90001/02.03")
    assert after.loc.page == break_span.loc.page + 1
    assert rendered.text.split("\f")[after.loc.page - 1].startswith(
        "The Lender advanced")


def test_render_map_validates_and_is_complete(documents):
    for rendered in documents.values():
        render_map = RenderMap(document_id=rendered.doc_id, format="txt",
                               spans=rendered.result.spans,
                               fact_surfaces=rendered.result.fact_surfaces)
        assert len(render_map.spans) == len(rendered.result.spans)
        assert all(isinstance(span, RenderSpan) for span in render_map.spans)
        assert render_map.fact_surfaces == rendered.result.fact_surfaces


def test_all_seven_prose_types_are_rendered(documents):
    types = {rendered.doc_type for rendered in documents.values()}
    assert PROSE_TYPES <= types


def test_workbook_documents_are_refused(tmp_path):
    plan = pilot_plan()
    workbook = plan.documents[plan.workbook_ids[0]]
    with pytest.raises(ValueError, match="workbook"):
        TxtRenderer().render(workbook, DEFAULT_TXT_PROFILE, tmp_path / "wb.txt")


def test_blank_line_share_stays_under_the_ceiling(documents):
    for rendered in documents.values():
        assert rendered.blank_share <= 0.25, (rendered.doc_id, rendered.blank_share)


def test_no_line_exceeds_the_wrap_width_unless_it_is_one_long_sentence(documents):
    width = DEFAULT_TXT_PROFILE.page_width_chars
    for rendered in documents.values():
        canonical = canonicalize(rendered.document)
        by_text = {sentence.text: sentence.sentence_id for sentence in canonical.sentences}
        warned = " ".join(rendered.result.warnings)
        for line in rendered.lines:
            if len(line) <= width:
                continue
            body = line
            bullet = re.match(r"^(?:- |\(\d+\) )", line)
            if bullet:
                body = line[bullet.end():]
            if body in by_text:
                assert by_text[body] in warned, \
                    f"{rendered.doc_id}: over-width line without a warning"
                continue
            assert line in by_text, f"{rendered.doc_id}: wrapped line is not one sentence"
            assert by_text[line] in warned, \
                f"{rendered.doc_id}: over-width line without a warning"


def test_workbook_is_never_rendered_as_prose(documents):
    assert not any(rendered.doc_type == "workbook" for rendered in documents.values())


def test_feature_document_renders_its_structures(feature):
    rendered = feature
    lines = rendered.lines
    assert "Notes" in lines
    assert any(line.startswith("[1] ") for line in lines)
    assert any(line.startswith("(1) ") for line in lines)
    assert any("Scope and Construction" in line for line in lines)
    assert any(line.startswith("2.1.") for line in lines)
    assert any(line == "| Undertaking |  |" for line in lines)
    assert any(line.startswith("Signed for and on behalf of") for line in lines)
    assert any("Exhibit A" in line for line in lines)
    assert any(line.startswith("Thread: ") for line in lines)
    assert lines[-1].startswith("FA-90001")
    assert layout_lines(feature_document()) == lines
