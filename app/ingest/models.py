"""The internal document model.

Docling's own items cannot be extended (``extra="forbid"``, proved in Stage 0),
so this is the side-table promoted to a first-class model. It holds our passage
ids, our character offsets, and a back-pointer to Docling's ``self_ref`` for
navigation only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Terminal states. Rule 2 permits exactly these three.
YIELDED = "yielded"
EMPTY = "empty"
FAILED = "failed"


@dataclass(frozen=True)
class Anchor:
    """A reference from a graph element back to source.

    ``char_start``/``char_end`` are offsets into the passage's own text, which
    Datanaut stores. They are exact and re-verifiable. ``page_no`` is ``None``
    for office formats, which carry no page provenance at all — a legitimate
    value, not a bug.
    """

    id: str
    document_id: str
    passage_id: str
    char_start: int
    char_end: int
    quote: str
    page_no: int | None = None
    item_ref: str | None = None
    match_kind: str = "exact"
    match_score: float = 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "passage_id": self.passage_id,
            "page_no": self.page_no,
            "item_ref": self.item_ref,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "match_kind": self.match_kind,
            "match_score": self.match_score,
        }


@dataclass
class Passage:
    """One item of text in reading order, with everything needed to anchor it."""

    id: str
    document_id: str
    ordinal: int
    text: str
    char_start: int          # offset into the document's assembled text
    char_end: int
    item_ref: str | None = None
    label: str | None = None
    level: int = 0
    page_no: int | None = None
    bbox: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "document_id": self.document_id, "ordinal": self.ordinal,
            "text": self.text, "char_start": self.char_start, "char_end": self.char_end,
            "item_ref": self.item_ref, "label": self.label, "level": self.level,
            "page_no": self.page_no, "bbox": self.bbox,
        }


@dataclass
class PageOutcome:
    """What happened to one page. Exactly one state, always."""

    page_no: int
    state: str
    reason: str | None = None
    error: str | None = None
    passage_count: int = 0


@dataclass
class OrderFlags:
    """Cheap signals that reading order may be scrambled.

    These *flag*. They never repair. A document whose columns were interleaved
    is a document whose extraction is untrustworthy, and the honest response is
    to say so, not to guess at the correct order.
    """

    passage_count: int = 0
    unterminated_ratio: float = 0.0
    midword_start_ratio: float = 0.0
    length_cv: float = 0.0
    suspicious: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passage_count": self.passage_count,
            "unterminated_ratio": round(self.unterminated_ratio, 4),
            "midword_start_ratio": round(self.midword_start_ratio, 4),
            "length_cv": round(self.length_cv, 4),
            "suspicious": self.suspicious,
            "reasons": self.reasons,
        }


@dataclass
class SourceDocument:
    """A converted document, normalised.

    ``page_count`` is counted **independently** of Docling (Stage 0 finding:
    Docling silently truncated a damaged PDF to one page and reported SUCCESS).
    ``docling_page_count`` is what Docling claimed. When they differ, the
    missing pages are recorded as ``failed``.
    """

    id: str
    source_path: str
    filename: str
    content_hash: str
    mimetype: str
    byte_size: int
    passages: list[Passage] = field(default_factory=list)
    text: str = ""
    page_count: int | None = None
    docling_page_count: int | None = None
    page_outcomes: list[PageOutcome] = field(default_factory=list)
    order_flags: OrderFlags = field(default_factory=OrderFlags)
    duplicate_of: str | None = None
    state: str = YIELDED
    reason: str | None = None
    error: str | None = None
    raw_path: str | None = None

    @property
    def suspicious(self) -> bool:
        return self.order_flags.suspicious

    def passage(self, passage_id: str) -> Passage | None:
        for passage in self.passages:
            if passage.id == passage_id:
                return passage
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_path": self.source_path,
            "filename": self.filename,
            "content_hash": self.content_hash,
            "mimetype": self.mimetype,
            "byte_size": self.byte_size,
            "page_count": self.page_count,
            "docling_page_count": self.docling_page_count,
            "duplicate_of": self.duplicate_of,
            "order_flags": self.order_flags.to_dict(),
            "suspicious": self.suspicious,
            "state": self.state,
            "reason": self.reason,
            "error": self.error,
            "raw_path": self.raw_path,
        }
