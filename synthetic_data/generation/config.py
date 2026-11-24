"""Corpus configuration and the depth targets from architecture §11.

The targets here are the acceptance ranges Phase B is measured against, not
aspirational prose: ``compute_metrics`` and the tests read these tables.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

__all__ = [
    "DOC_TYPES",
    "PROSE_TYPES",
    "CORE_TYPES",
    "DEPTH_TARGETS",
    "DepthTarget",
    "CorpusConfig",
    "matter_sizes",
]

#: The seven prose types (architecture §9).  Correspondence is the only type a
#: matter may repeat, because a thread is made of letters.
PROSE_TYPES = (
    "board_minutes",
    "due_diligence",
    "financial_agreement",
    "case_notes",
    "memorandum",
    "press_release",
    "correspondence",
)
DOC_TYPES = PROSE_TYPES
CORE_TYPES = tuple(t for t in PROSE_TYPES if t != "correspondence")


@dataclass(frozen=True)
class DepthTarget:
    """One row of the §11 table.  Documented ranges, in the doc's units."""

    words: tuple[int, int]
    sections: tuple[int, int]
    paragraphs: tuple[int, int]
    sentences: tuple[int, int]
    tables: tuple[int, int]
    row_cols: tuple[int, int]          # body rows per table
    columns: tuple[int, int]
    entities: tuple[int, int]
    facts: tuple[int, int]
    transactions: tuple[int, int]
    cross_refs: tuple[int, int]

    def within(self, value: int, field_name: str) -> bool:
        low, high = getattr(self, field_name)
        return low <= value <= high


DEPTH_TARGETS: dict[str, DepthTarget] = {
    "press_release": DepthTarget(
        words=(350, 650), sections=(3, 5), paragraphs=(8, 14), sentences=(28, 50),
        tables=(0, 1), row_cols=(3, 6), columns=(2, 4), entities=(8, 18),
        facts=(8, 16), transactions=(0, 2), cross_refs=(0, 2)),
    "correspondence": DepthTarget(
        words=(500, 900), sections=(3, 6), paragraphs=(10, 20), sentences=(38, 70),
        tables=(0, 1), row_cols=(3, 8), columns=(2, 4), entities=(8, 20),
        facts=(10, 20), transactions=(0, 2), cross_refs=(1, 3)),
    "case_notes": DepthTarget(
        words=(1200, 1900), sections=(5, 8), paragraphs=(22, 38), sentences=(90, 140),
        tables=(2, 3), row_cols=(5, 12), columns=(3, 5), entities=(15, 30),
        facts=(25, 45), transactions=(0, 3), cross_refs=(1, 3)),
    "memorandum": DepthTarget(
        words=(1300, 2000), sections=(5, 8), paragraphs=(20, 36), sentences=(100, 150),
        tables=(1, 2), row_cols=(4, 10), columns=(3, 5), entities=(14, 28),
        facts=(25, 45), transactions=(0, 3), cross_refs=(1, 3)),
    "board_minutes": DepthTarget(
        words=(1400, 2200), sections=(6, 9), paragraphs=(24, 40), sentences=(105, 160),
        tables=(2, 3), row_cols=(5, 12), columns=(4, 6), entities=(18, 35),
        facts=(30, 50), transactions=(0, 4), cross_refs=(1, 4)),
    "due_diligence": DepthTarget(
        words=(2000, 3200), sections=(7, 11), paragraphs=(35, 60), sentences=(150, 240),
        tables=(3, 5), row_cols=(6, 14), columns=(3, 6), entities=(25, 45),
        facts=(40, 70), transactions=(2, 6), cross_refs=(2, 6)),
    "financial_agreement": DepthTarget(
        words=(2500, 4500), sections=(8, 12), paragraphs=(45, 80), sentences=(190, 330),
        tables=(3, 6), row_cols=(5, 15), columns=(3, 6), entities=(20, 40),
        facts=(50, 90), transactions=(4, 12), cross_refs=(2, 5)),
}


@dataclass(frozen=True)
class CorpusConfig:
    """Everything that makes a corpus what it is (DEC-6: determinism is per config)."""

    seed: int = 7
    corpus_name: str = "v3_pilot"
    documents: int = 7
    workbooks: int = 0
    start_date: dt.date = dt.date(2023, 1, 9)
    include_contradictions: bool = True
    include_threads: bool = True
    registers: tuple[str, ...] = ("formal", "neutral", "terse")
    #: upper bound on letters in one matter; the matter's thread shape is chosen
    #: from the resulting count
    max_letters: int = 5

    def __post_init__(self) -> None:
        if self.documents < len(PROSE_TYPES):
            raise ValueError(
                f"a corpus needs at least {len(PROSE_TYPES)} prose documents to cover "
                f"every type (got {self.documents})")
        if self.workbooks < 0:
            raise ValueError("workbooks must be >= 0")
        if not self.corpus_name or not all(
                ch.isalnum() or ch in "._-" for ch in self.corpus_name):
            raise ValueError("corpus_name must be alphanumeric with . _ - only")
        if not self.registers:
            raise ValueError("at least one register is required")


def matter_sizes(total: int, max_letters: int = 5) -> list[int]:
    """Split ``total`` prose documents into matters of 7..6+max_letters documents.

    A matter always carries the six non-correspondence types plus 1..max_letters
    letters.  Sizes follow a fixed cycle (7, 10, 8, 9) so letter counts — and
    therefore thread shapes — vary across the corpus instead of collapsing to one
    size.  Any shortfall is spread one document at a time over matters that still
    have room.
    """
    low = len(PROSE_TYPES)
    high = len(CORE_TYPES) + max_letters
    if total < low:
        raise ValueError(f"a corpus needs at least {low} prose documents")
    if high < low:
        raise ValueError("max_letters must be >= 1")
    pattern = (7, 10, 8, 9)
    pattern = tuple(min(max(value, low), high) for value in pattern)

    sizes: list[int] = []
    remaining = total
    while remaining >= low:
        value = pattern[len(sizes) % len(pattern)]
        if remaining - value < low:
            value = remaining
        if value > high:                    # pragma: no cover - pattern is clamped
            value = high
        sizes.append(value)
        remaining -= value

    index = 0
    while remaining:
        if sizes[index % len(sizes)] < high:
            sizes[index % len(sizes)] += 1
            remaining -= 1
        index += 1
        if index > 10_000:                  # pragma: no cover - arithmetic guard
            raise AssertionError("cannot distribute the remaining documents")
    if sum(sizes) != total:  # pragma: no cover - defensive
        raise AssertionError(f"matter sizes {sizes} do not sum to {total}")
    return sizes
