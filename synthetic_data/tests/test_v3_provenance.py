"""Phase A — Layer 3 provenance: four location kinds and render coverage."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from synthetic_data.model import (
    CanonicalSourceLoc,
    EvidenceRef,
    ParseLoc,
    RendererCapabilities,
    RenderLoc,
    RenderMap,
    RenderOccurrence,
    RenderResult,
    RenderSpan,
    assert_render_coverage,
)

SHA = "0" * 64


def txt_loc(**kw) -> RenderLoc:
    return RenderLoc(format="txt", page=2, line_no=14, char_start=0, char_end=12, **kw)


def pdf_loc(**kw) -> RenderLoc:
    return RenderLoc(format="pdf", page=3, bbox=(72.0, 400.0, 520.0, 430.0), **kw)


def docx_loc(**kw) -> RenderLoc:
    return RenderLoc(format="docx", pageless=True, section_index=2,
                     paragraph_index=11, **kw)


def xlsx_loc(**kw) -> RenderLoc:
    return RenderLoc(format="xlsx", pageless=True, sheet_name="Transactions",
                     row=4, column=2, cell_ref="B4", **kw)


def test_txt_uses_logical_pages():
    loc = txt_loc()
    assert loc.page == 2 and loc.pageless is False
    with pytest.raises(ValidationError):
        RenderLoc(format="txt", page=1, bbox=(0, 0, 10, 10))


def test_pdf_requires_a_physical_page():
    assert pdf_loc().page == 3
    with pytest.raises(ValidationError):
        RenderLoc(format="pdf", page=None)
    with pytest.raises(ValidationError):
        RenderLoc(format="pdf", page=1, pageless=True)


def test_pdf_bbox_is_optional_but_validated():
    loc = RenderLoc(format="pdf", page=1)
    assert loc.bbox is None
    with pytest.raises(ValidationError):
        RenderLoc(format="pdf", page=1, bbox=(10.0, 10.0, 5.0, 20.0))
    with pytest.raises(ValidationError):
        RenderLoc(format="pdf", page=1, bbox=(0.0, 0.0, 0.0, 0.0))


def test_docx_is_pageless_and_needs_a_block_reference():
    assert docx_loc().page is None
    with pytest.raises(ValidationError):
        RenderLoc(format="docx", page=1, pageless=True, paragraph_index=0)
    with pytest.raises(ValidationError):
        RenderLoc(format="docx", pageless=True)                    # no index at all
    with pytest.raises(ValidationError):
        RenderLoc(format="docx", pageless=False, paragraph_index=0)


def test_xlsx_requires_a_sheet_and_never_a_page():
    assert xlsx_loc().sheet_name == "Transactions"
    with pytest.raises(ValidationError):
        RenderLoc(format="xlsx", pageless=True, row=1, column=1)
    with pytest.raises(ValidationError):
        RenderLoc(format="xlsx", pageless=True, sheet_name="S", page=1)


def test_parse_location_page_contract():
    loc = ParseLoc(doc_id="01HXYZ", sentence_id="s19", idx=19, char_start=100,
                   char_end=140, page=2)
    assert loc.page == 2
    with pytest.raises(ValidationError):
        ParseLoc(doc_id="01HXYZ", sentence_id="s19", idx=19, char_start=100,
                 char_end=140, pageless=True, page=2)
    with pytest.raises(ValidationError):
        ParseLoc(doc_id="01HXYZ", sentence_id="blk:00001:01", idx=1,
                 char_start=0, char_end=5)                          # canonical id, not parser id


def test_evidence_rejects_ambiguous_and_missing_alignment():
    for kind in ("ambiguous", "missing"):
        with pytest.raises(ValidationError):
            EvidenceRef(sentence_id="s19", quote="text", canonical_node_id="blk:00001:01.01:aaaa",
                        alignment_kind=kind, render_format="txt", page=1)


def test_evidence_page_rules():
    ok = EvidenceRef(sentence_id="s19", quote="text",
                     canonical_node_id="blk:00001:01.01:aaaa",
                     alignment_kind="exact", render_format="pdf", page=2)
    assert ok.page == 2
    with pytest.raises(ValidationError):
        EvidenceRef(sentence_id="s19", quote="text",
                    canonical_node_id="blk:00001:01.01:aaaa",
                    alignment_kind="exact", render_format="pdf")
    with pytest.raises(ValidationError):
        EvidenceRef(sentence_id="s19", quote="   ",
                    canonical_node_id="blk:00001:01.01:aaaa",
                    alignment_kind="exact", render_format="txt", page=1)


def test_evidence_render_loc_format_must_agree():
    with pytest.raises(ValidationError):
        EvidenceRef(sentence_id="s19", quote="text",
                    canonical_node_id="blk:00001:01.01:aaaa",
                    alignment_kind="exact", render_format="pdf", page=2,
                    render_loc=txt_loc())


def test_canonical_source_location_is_structural():
    loc = CanonicalSourceLoc(document_id="doc:00001:aaaa", block_id="blk:00001:01.01:aaaa",
                             char_start=0, char_end=40)
    assert loc.char_end == 40
    with pytest.raises(ValidationError):
        CanonicalSourceLoc(document_id="doc:00001:aaaa", cell_id="tbl:1/r01c01:x")
    with pytest.raises(ValidationError):
        CanonicalSourceLoc(document_id="doc:00001:aaaa", char_start=5)


def test_occurrence_id_embeds_its_format():
    occ = RenderOccurrence(occurrence_id="rnd:pdf:00001:01", canonical_id="blk:00001:01.01:aaaa",
                           format="pdf", loc=pdf_loc())
    assert occ.occurrence_id.startswith("rnd:pdf:")
    with pytest.raises(ValidationError):
        RenderOccurrence(occurrence_id="rnd:txt:00001:01", canonical_id="blk:00001:01.01:aaaa",
                         format="pdf", loc=pdf_loc())


def test_render_map_rejects_duplicate_nodes_and_foreign_formats():
    span = RenderSpan(canonical_id="blk:00001:01.01:aaaa", kind="block", format="txt",
                      loc=txt_loc(), text="x", text_sha256=SHA)
    with pytest.raises(ValidationError):
        RenderMap(document_id="doc:00001:aaaa", format="txt", spans=[span, span])
    with pytest.raises(ValidationError):
        RenderMap(document_id="doc:00001:aaaa", format="pdf", spans=[span])


def test_render_coverage_passes_and_fails():
    a = "blk:00001:01.01:aaaa"
    b = "blk:00001:01.02:bbbb"
    spans = [RenderSpan(canonical_id=i, kind="block", format="txt", loc=txt_loc(),
                        text="x", text_sha256=SHA) for i in (a, b)]
    full = RenderMap(document_id="doc:00001:aaaa", format="txt", spans=spans)
    assert_render_coverage([a, b], full)
    with pytest.raises(ValueError):
        assert_render_coverage([a, b, "blk:00001:01.03:cccc"], full)
    extra = RenderMap(document_id="doc:00001:aaaa", format="txt",
                      spans=spans + [RenderSpan(canonical_id="blk:999:01:dddd", kind="block",
                                                format="txt", loc=txt_loc(), text="y",
                                                text_sha256=SHA)])
    with pytest.raises(ValueError):
        assert_render_coverage([a, b], extra)


def test_render_result_capabilities_must_match():
    caps = RendererCapabilities(format="pdf", paginates=True, page_kind="physical",
                                tables=True, bbox=True, footnotes=True, headers_footers=True)
    result = RenderResult(format="pdf", path="/tmp/a.pdf", sha256=SHA, capabilities=caps)
    assert result.capabilities.page_kind == "physical"
    txt_caps = RendererCapabilities(format="txt", paginates=True, page_kind="logical",
                                    tables=True, bbox=False, footnotes=False,
                                    headers_footers=True)
    with pytest.raises(ValidationError):
        RenderResult(format="pdf", path="/tmp/a.pdf", sha256=SHA, capabilities=txt_caps)


def test_capability_contract_is_coherent():
    with pytest.raises(ValidationError):
        RendererCapabilities(format="pdf", paginates=False, page_kind="physical",
                             tables=True, bbox=False, footnotes=False, headers_footers=False)
    with pytest.raises(ValidationError):
        RendererCapabilities(format="docx", paginates=True, page_kind="none",
                             tables=True, bbox=False, footnotes=False, headers_footers=False)
