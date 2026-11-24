"""Gold artifacts for V3: the v2 superset and the alignment record (DEC-3, §8.7).

Three files per document-format, written together:

``gold/v1``
    A ``synth.eval.gold.GoldSet`` — the vendored model, unchanged.  Its quotes are
    parser substrings and its sentence ids are parser ids, so every existing
    consumer and ``synth.eval.gold.validate()`` keep working.

``gold/v2``
    The V3 contract: the same items plus, per evidence item, the canonical node,
    the canonical sentence ids, the alignment verdict and the render location, and
    per edge/proposition the canonical fact id.  Provenance is additive only: a
    field that cannot be justified by an aligned parser span is never written.

``gold/alignment``
    The canonical-to-renderer-to-parser map itself.  Not an annotation: it records
    every verdict, including the ones that would block a gold build.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from synth.eval.gold import (
    GoldDoc,
    GoldEdge,
    GoldEntity,
    GoldEvidence,
    GoldFlag,
    GoldLink,
    GoldProposition,
    GoldTime,
    GoldTimeRef,
    GoldTrap,
)

__all__ = [
    "GoldEvidenceV2",
    "GoldEntityV2",
    "GoldEdgeV2",
    "GoldPropositionV2",
    "GoldTimeRefV2",
    "GoldDocV2",
    "GoldContradiction",
    "AlignmentSummary",
    "GoldV2",
    "AlignmentUnit",
    "AlignmentArtifact",
    "GOLD_VERSION",
]

GOLD_VERSION = 2
ALIGNMENT_KINDS = frozenset({"exact", "normalised", "split", "merged",
                             "partial", "ambiguous", "missing"})
EVIDENCE_KINDS = frozenset({"exact", "normalised", "split", "merged"})


class _V2(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldEvidenceV2(GoldEvidence):
    """A v1 evidence item plus everything that proves where it came from."""

    canonical_node_id: str | None = None
    canonical_sentence_ids: list[str] = Field(default_factory=list)
    alignment_kind: str | None = None
    render_format: str | None = None
    render_loc: dict[str, Any] = Field(default_factory=dict)
    #: how the value in ``quote`` was compared to the typed value
    value_kind: str | None = None
    typed_value: str | None = None
    currency: str | None = None
    #: True only when the currency code/symbol is *in the parser text of this span*
    currency_in_quote: bool | None = None
    #: workbook only: True when the row the cell sits in states the currency as text
    currency_in_row: bool | None = None
    #: canonical ids this value is derived from (derived workbook values only)
    derived_from: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind(self) -> "GoldEvidenceV2":
        if self.alignment_kind is not None and self.alignment_kind not in ALIGNMENT_KINDS:
            raise ValueError(f"unknown alignment kind {self.alignment_kind!r}")
        return self


class GoldEntityV2(GoldEntity):
    canonical_entity_id: str | None = None
    mentions: list[GoldEvidenceV2] = Field(default_factory=list)


class GoldEdgeV2(GoldEdge):
    canonical_fact_id: str | None = None
    evidence: list[GoldEvidenceV2] = Field(default_factory=list)


class GoldPropositionV2(GoldProposition):
    canonical_fact_id: str | None = None
    #: set when the proposition is a computed value rather than a stated one
    derived_from: list[str] = Field(default_factory=list)
    evidence: list[GoldEvidenceV2] = Field(default_factory=list)


class GoldTimeRefV2(GoldTimeRef):
    evidence: GoldEvidenceV2


class GoldDocV2(GoldDoc):
    document_id: str | None = None
    doc_type: str | None = None
    doc_kind: str | None = None
    format: str | None = None
    render_sha256: str | None = None
    pages: int | None = None
    pageless: bool | None = None


class GoldContradiction(_V2):
    """A contradiction as the corpus recorded it (both sides, both documents)."""

    contradiction_id: str
    fact_a: str
    fact_b: str
    dimension: str
    documents: list[str] = Field(default_factory=list)
    #: facts of this contradiction that this document states
    stated_here: list[str] = Field(default_factory=list)


class AlignmentSummary(_V2):
    counts: dict[str, int] = Field(default_factory=dict)
    cells: int = 0
    rows: int = 0
    pages: int | None = None
    pageless: bool | None = None
    bbox_coverage: float | None = None
    #: money evidence items whose currency is stated in the parser text
    currency_stated: int = 0
    #: money evidence items whose currency exists only in the model
    currency_not_stated: int = 0
    problems: list[str] = Field(default_factory=list)


class GoldV2(_V2):
    gold_version: Literal[2] = GOLD_VERSION
    doc: GoldDocV2
    first_idx: int
    last_idx: int
    annotator: str = ""
    mode: str = ""
    notes: str = ""
    corpus_seed: int | None = None
    entities: list[GoldEntityV2] = Field(default_factory=list)
    edges: list[GoldEdgeV2] = Field(default_factory=list)
    propositions: list[GoldPropositionV2] = Field(default_factory=list)
    links: list[GoldLink] = Field(default_factory=list)
    expected_flags: list[GoldFlag] = Field(default_factory=list)
    identity_traps: list[GoldTrap] = Field(default_factory=list)
    time_refs: list[GoldTimeRefV2] = Field(default_factory=list)
    contradictions: list[GoldContradiction] = Field(default_factory=list)
    alignment: AlignmentSummary = Field(default_factory=AlignmentSummary)


class AlignmentUnit(_V2):
    canonical_id: str
    kind: str
    parser_sentence_ids: list[str] = Field(default_factory=list)
    char_start: int | None = None
    char_end: int | None = None
    page: int | None = None
    render_loc: dict[str, Any] = Field(default_factory=dict)
    text_sha256: str | None = None
    missing_row_structure: bool = False


class AlignmentArtifact(_V2):
    document_id: str
    format: str
    parse_doc_id: str
    render_sha256: str | None = None
    page_kind: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    units: list[AlignmentUnit] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_kinds(self) -> "AlignmentArtifact":
        bad = {unit.kind for unit in self.units} - ALIGNMENT_KINDS
        if bad:
            raise ValueError(f"unknown alignment kinds {sorted(bad)}")
        return self
