"""Layer 3 — rendering and provenance models (architecture §6, §7.2).

Four distinct location concepts are modelled separately and must never be
conflated:

* :class:`CanonicalSourceLoc` — where a node lives in the canonical document.
* :class:`RenderLoc`          — where a renderer put it in one artifact.
* :class:`ParseLoc`           — where the parser found it in that artifact.
* :class:`EvidenceRef`        — the parser-grounded evidence a gold item cites.

A page number is ``None`` whenever the format or parser cannot establish one.
Nothing in this module fabricates pagination: DOCX and XLSX are pageless by
contract, and a PDF span without a page is a validation error.
"""
from __future__ import annotations

from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .values import (
    AlignmentKind,
    CanonicalId,
    IsoDate,  # noqa: F401  (kept for schema completeness of value unions)
    PARSER_SENTENCE_PATTERN,
    ParserSentenceId,
)

__all__ = [
    "SourceFormat",
    "PageKind",
    "RenderProfile",
    "RendererCapabilities",
    "CanonicalSourceLoc",
    "RenderLoc",
    "ParseLoc",
    "EvidenceRef",
    "RenderOccurrence",
    "RenderSpan",
    "RenderMap",
    "RenderResult",
    "SpanKind",
    "assert_render_coverage",
]

SourceFormat = Literal["txt", "pdf", "docx", "xlsx"]
PageKind = Literal["physical", "logical", "sheet", "none"]
SpanKind = Literal["section", "block", "table", "row", "cell", "sentence"]

_SHA256 = r"^[0-9a-f]{64}$"
_OCCURRENCE_RE = r"^rnd:[a-z]+:[A-Za-z0-9][A-Za-z0-9:._/\-]*$"


class _Render(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RenderProfile(_Render):
    """Presentation parameters.  They change bytes, never semantics."""

    page_width_chars: StrictInt | None = Field(default=None, ge=20, le=400)
    page_height_lines: StrictInt | None = Field(default=None, ge=10, le=200)
    page_size: Literal["A4", "Letter", "Legal"] | None = None
    margins_mm: tuple[float, float, float, float] | None = None
    font_family: str | None = Field(default=None, max_length=64)
    include_headers: bool = True
    include_footers: bool = True
    table_style: str | None = Field(default=None, max_length=64)
    number_formats: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _margins(self) -> "RenderProfile":
        if self.margins_mm is not None and any(m < 0 for m in self.margins_mm):
            raise ValueError("margins must be non-negative")
        return self


class RendererCapabilities(_Render):
    format: SourceFormat
    paginates: bool
    page_kind: PageKind
    tables: bool
    bbox: bool
    footnotes: bool
    headers_footers: bool

    @model_validator(mode="after")
    def _coherent(self) -> "RendererCapabilities":
        if self.paginates and self.page_kind == "none":
            raise ValueError("a paginating renderer must declare a page_kind")
        if not self.paginates and self.page_kind != "none":
            raise ValueError("a non-paginating renderer must use page_kind='none'")
        if self.format == "xlsx" and self.page_kind not in {"sheet", "none"}:
            raise ValueError("xlsx page_kind must be 'sheet' or 'none'")
        if self.format == "pdf" and self.page_kind not in {"physical", "none"}:
            raise ValueError("pdf page_kind must be 'physical' or 'none'")
        return self


class CanonicalSourceLoc(_Render):
    """Stable location inside the canonical document (no coordinates)."""

    document_id: CanonicalId
    section_id: CanonicalId | None = None
    block_id: CanonicalId | None = None
    table_id: CanonicalId | None = None
    row_id: CanonicalId | None = None
    cell_id: CanonicalId | None = None
    char_start: StrictInt | None = Field(default=None, ge=0)
    char_end: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _range(self) -> "CanonicalSourceLoc":
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if (self.char_start is not None and self.char_end is not None
                and self.char_end < self.char_start):
            raise ValueError("char_end precedes char_start")
        if self.cell_id and not self.row_id:
            raise ValueError("a cell location requires its row")
        if self.row_id and not self.table_id:
            raise ValueError("a row location requires its table")
        return self


class RenderLoc(_Render):
    """Where a canonical node landed in one rendered artifact."""

    format: SourceFormat
    #: physical page (PDF) or logical page (TXT); always None when pageless
    page: StrictInt | None = Field(default=None, ge=1)
    pageless: bool = False
    bbox: tuple[float, float, float, float] | None = None
    line_no: StrictInt | None = Field(default=None, ge=1)
    char_start: StrictInt | None = Field(default=None, ge=0)
    char_end: StrictInt | None = Field(default=None, ge=0)
    section_index: StrictInt | None = Field(default=None, ge=0)
    paragraph_index: StrictInt | None = Field(default=None, ge=0)
    table_index: StrictInt | None = Field(default=None, ge=0)
    run_start: StrictInt | None = Field(default=None, ge=0)
    run_end: StrictInt | None = Field(default=None, ge=0)
    row: StrictInt | None = Field(default=None, ge=1)
    column: StrictInt | None = Field(default=None, ge=1)
    cell_ref: str | None = Field(default=None, max_length=32)
    sheet_name: str | None = Field(default=None, max_length=31)
    table_name: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _by_format(self) -> "RenderLoc":
        if self.format == "pdf":
            if self.pageless:
                raise ValueError("pdf is not pageless")
            if self.page is None:
                raise ValueError("pdf span requires a physical page; null pages are not permitted")
        elif self.format == "txt":
            if self.pageless:
                raise ValueError("txt uses logical pages and is not pageless")
            if self.page is None:
                raise ValueError("txt span requires a logical page")
            if self.bbox is not None:
                raise ValueError("txt spans have no bounding box")
        else:                                    # docx / xlsx
            if not self.pageless:
                raise ValueError(f"{self.format} has no physical pagination; pageless must be True")
            if self.page is not None:
                raise ValueError(f"{self.format} must not carry a page number")
            if self.bbox is not None:
                raise ValueError(f"{self.format} spans have no bounding box")
            if self.format == "docx" and not any(
                    v is not None for v in (self.section_index, self.paragraph_index,
                                            self.table_index)):
                raise ValueError("docx span requires a section, paragraph or table index")
            if self.format == "xlsx" and not self.sheet_name:
                raise ValueError("xlsx span requires a sheet name")

        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if x1 <= x0 or y1 <= y0:
                raise ValueError("bbox must have x1>x0 and y1>y0")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if (self.char_start is not None and self.char_end is not None
                and self.char_end < self.char_start):
            raise ValueError("char_end precedes char_start")
        if (self.run_start is None) != (self.run_end is None):
            raise ValueError("run_start and run_end must be provided together")
        if (self.run_start is not None and self.run_end is not None
                and self.run_end < self.run_start):
            raise ValueError("run_end precedes run_start")
        return self


class ParseLoc(_Render):
    """What the parser actually produced.  ``doc_id`` is Synth's ULID, not canonical."""

    doc_id: str = Field(min_length=1, max_length=64)
    sentence_id: ParserSentenceId = Field(pattern=PARSER_SENTENCE_PATTERN)
    idx: StrictInt = Field(ge=0)
    char_start: StrictInt = Field(ge=0)
    char_end: StrictInt = Field(ge=0)
    page: StrictInt | None = Field(default=None, ge=1)
    pageless: bool = False
    section_path: list[str] = Field(default_factory=list)
    kind: Literal["sentence", "heading", "list_item", "table_row"] = "sentence"

    @model_validator(mode="after")
    def _span(self) -> "ParseLoc":
        if self.char_end <= self.char_start:
            raise ValueError("parser span must be non-empty")
        if self.pageless and self.page is not None:
            raise ValueError("pageless parse location must not carry a page")
        if not self.pageless and self.page is None:
            raise ValueError("paginated parse location requires a page")
        return self


class EvidenceRef(_Render):
    """Gold evidence: a parser sentence and an exact parser substring."""

    sentence_id: ParserSentenceId = Field(pattern=PARSER_SENTENCE_PATTERN)
    quote: str = Field(min_length=1, max_length=4000)
    canonical_node_id: CanonicalId
    canonical_sentence_ids: list[CanonicalId] = Field(default_factory=list)
    alignment_kind: AlignmentKind
    render_format: SourceFormat
    render_loc: RenderLoc | None = None
    page: StrictInt | None = Field(default=None, ge=1)
    pageless: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "EvidenceRef":
        if self.render_loc is not None and self.render_loc.format != self.render_format:
            raise ValueError("evidence render_format disagrees with render_loc.format")
        if self.render_format == "pdf":
            if self.pageless:
                raise ValueError("pdf is never pageless")
            if self.page is None:
                raise ValueError("pdf evidence requires a physical page")
        if self.pageless and self.page is not None:
            raise ValueError("pageless evidence must not carry a page")
        if not self.quote.strip():
            raise ValueError("evidence quote must not be blank")
        if len(set(self.canonical_sentence_ids)) != len(self.canonical_sentence_ids):
            raise ValueError("duplicate canonical sentence id in evidence")
        if self.alignment_kind in {"ambiguous", "missing"}:
            raise ValueError(f"evidence cannot be built from a {self.alignment_kind} alignment")
        return self


class RenderOccurrence(_Render):
    occurrence_id: str = Field(pattern=_OCCURRENCE_RE)
    canonical_id: CanonicalId
    format: SourceFormat
    loc: RenderLoc

    @model_validator(mode="after")
    def _match(self) -> "RenderOccurrence":
        if self.loc.format != self.format:
            raise ValueError("occurrence format disagrees with its render location")
        if not self.occurrence_id.startswith(f"rnd:{self.format}:"):
            raise ValueError("occurrence id must embed its format: rnd:<format>:<doc>:<nn>")
        return self


class RenderSpan(_Render):
    canonical_id: CanonicalId
    kind: SpanKind
    format: SourceFormat
    loc: RenderLoc
    text: str = Field(max_length=100_000)
    text_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _match(self) -> "RenderSpan":
        if self.loc.format != self.format:
            raise ValueError("span format disagrees with its render location")
        return self


class RenderMap(_Render):
    document_id: CanonicalId
    format: SourceFormat
    spans: list[RenderSpan] = Field(default_factory=list)
    #: fact id -> every surface rendered for it (values only, for cross-format checks)
    fact_surfaces: dict[CanonicalId, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique(self) -> "RenderMap":
        ids = [s.canonical_id for s in self.spans]
        if len(set(ids)) != len(ids):
            raise ValueError("a canonical node may appear at most once per render map")
        for span in self.spans:
            if span.format != self.format:
                raise ValueError(f"span {span.canonical_id} has format {span.format}, "
                                 f"map is {self.format}")
        for fact_id, surfaces in self.fact_surfaces.items():
            if not surfaces:
                raise ValueError(f"fact {fact_id} has an empty surface list")
            if any(not s.strip() for s in surfaces):
                raise ValueError(f"fact {fact_id} has a blank rendered surface")
        return self


class RenderResult(_Render):
    format: SourceFormat
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=_SHA256)
    spans: list[RenderSpan] = Field(default_factory=list)
    fact_surfaces: dict[CanonicalId, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    capabilities: RendererCapabilities

    @model_validator(mode="after")
    def _coherent(self) -> "RenderResult":
        if self.capabilities.format != self.format:
            raise ValueError("capabilities describe a different format")
        for span in self.spans:
            if span.format != self.format:
                raise ValueError(f"span {span.canonical_id} belongs to another format")
        return self


def assert_render_coverage(expected_ids: Iterable[str], render_map: RenderMap) -> None:
    """Invariant 8: every canonical node is rendered exactly once.

    ``expected_ids`` is normally ``CdmDocument.node_ids()``.  Page-break blocks
    are addressable nodes and must appear too; a renderer that silently drops one
    fails here rather than in a downstream comparison.
    """
    expected = list(expected_ids)
    rendered = [s.canonical_id for s in render_map.spans]
    missing = sorted(set(expected) - set(rendered))
    extra = sorted(set(rendered) - set(expected))
    duplicates = sorted({cid for cid in rendered if rendered.count(cid) > 1})
    problems = []
    if missing:
        problems.append(f"missing from render map: {missing[:5]}")
    if extra:
        problems.append(f"not in document: {extra[:5]}")
    if duplicates:
        problems.append(f"rendered more than once: {duplicates[:5]}")
    if problems:
        raise ValueError("render coverage failed: " + "; ".join(problems))
