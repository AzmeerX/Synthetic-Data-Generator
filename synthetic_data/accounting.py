"""Parser-content accounting (§8.5 "extra fact", checkpoint 1).

Alignment proves every *fact* is grounded.  It does not prove the converse: that
every sentence the parser returned was explained by the model.  A duplicated
paragraph, an inserted sentence or a table the renderer never wrote all pass
grounding silently, because nothing claims them.

This module closes that direction.  Each non-filtered parser sentence must be
accounted for by exactly one category:

``aligned``            a canonical unit's alignment claims it;
``declared-row``       a declared table row (a repeated header or total row the
                       alignment did not need to claim);
``declared-block``     a declared paragraph/note/exhibit/signature block;
``declared-heading``   a declared section heading — matched as a word run, because a
                       PDF drops the section number the model carries;
``declared-metadata``  a line the renderer draws from ``DocumentMetadata``;
``declared-furniture`` a declared header, footer or footnote line;
``blank``              no alphanumeric content (a rule line, a stray glyph).

Two kinds of matching are used, and the difference matters:

* **structural units** (canonical sentences, workbook rows) match by whole text and
  are *consumed one per successful alignment*, so a duplicated sentence finds its
  declaration already spent and is reported;
* **drawn lines** (headings, metadata, headers, footers, footnotes) match as a
  *contiguous run of their own words*, consumed once, because a renderer may wrap a
  title, a parser may split ``Reference: CO-00006`` into two sentences, and a PDF
  may merge ``Notes`` with the note that follows.  The words still have to come from
  the declared line: nothing is inferred from body text.

The parser's own ``filtered`` flag decides what is page furniture; it is never
second-guessed here.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from synthetic_data.alignment import AlignmentError, normalise
from synthetic_data.canonical import CanonicalDocument, block_text, heading_text
from synthetic_data.model import CdmDocument, CdmWorkbook

__all__ = [
    "AccountingGap",
    "AccountingReport",
    "account_parser_content",
    "require_full_accounting",
    "declared_lines",
    "workbook_row_texts",
    "ACCOUNTING_CATEGORIES",
]

ACCOUNTING_CATEGORIES = ("aligned", "declared-row", "declared-block",
                         "declared-heading", "declared-caption",
                         "declared-metadata", "declared-furniture", "blank",
                         "filtered")
_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class AccountingGap:
    sentence_id: str
    idx: int
    kind: str
    page: int | None
    text: str

    def describe(self) -> str:
        body = self.text if len(self.text) <= 160 else self.text[:157] + "..."
        return (f"  {self.sentence_id} (idx={self.idx}, kind={self.kind}, "
                f"page={self.page}): {body!r}\n"
                f"    unexplained: not claimed by an alignment, and no declared "
                f"row/block/heading/metadata/furniture line accounts for it")


@dataclass(frozen=True)
class AccountingReport:
    document_id: str
    format: str
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    gaps: tuple[AccountingGap, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.gaps

    @property
    def accounted(self) -> int:
        return self.total - len(self.gaps)

    def describe(self) -> str:
        head = (f"{self.document_id} ({self.format}): {len(self.gaps)} of "
                f"{self.total} parser sentences are unexplained")
        return "\n".join([head, *(gap.describe() for gap in self.gaps)])

    def as_dict(self) -> dict:
        return {"document_id": self.document_id, "format": self.format,
                "total": self.total, "counts": dict(self.counts),
                "gaps": [gap.describe() for gap in self.gaps]}


class _Line:
    """A declared drawn line, consumed word by word as parser pieces match it."""

    __slots__ = ("text", "category", "words", "used", "fold")

    def __init__(self, text: str, category: str, *, fold: bool = False) -> None:
        self.text = text
        self.category = category
        self.fold = fold
        self.words = _words(text)
        if fold:
            self.words = [word.casefold() for word in self.words]
        self.used = [False] * len(self.words)

    def take(self, piece: list[str]) -> bool:
        if self.fold:
            piece = [word.casefold() for word in piece]
        length = len(piece)
        if not length or length > len(self.words):
            return False
        for start in range(len(self.words) - length + 1):
            window = range(start, start + length)
            if any(self.used[index] for index in window):
                continue
            if [self.words[index] for index in window] != piece:
                continue
            for index in window:
                self.used[index] = True
            return True
        return False


def _key(text: str) -> str:
    return normalise(text)


def _words(text: str) -> list[str]:
    return [word for word in (_key(part) for part in _WORD.findall(text or "")) if word]


# ---------------------------------------------------------------------------
_NUMBER_PREFIX = re.compile(r"^\s*(?:\(?[0-9A-Za-z]{1,3}[.)]?\s+)+")


def running_header_text(heading: str) -> str:
    """A section heading as a page header shows it: no number, lower case."""
    return _NUMBER_PREFIX.sub("", heading).strip().lower()


def declared_lines(document: CdmDocument,
                   canonical: CanonicalDocument | None,
                   pages: int = 1) -> list[_Line]:
    """Every drawn line the model or the renderers draw.

    ``pages`` bounds the running headers: a paginated renderer repeats the current
    section name at the top of every page, and the parser in this pipeline does not
    classify those lines as page furniture.  Declaring one copy per page keeps the
    repetition explained without excusing an unlimited number of copies.
    """
    lines: list[_Line] = []
    metadata = document.metadata
    for value in (metadata.title, metadata.subtitle):
        if value:
            lines.append(_Line(value, "declared-metadata"))
    pairs = [
        ("Reference", metadata.reference),
        ("Date", metadata.document_date.iso() if metadata.document_date else None),
        ("Jurisdiction", metadata.jurisdiction),
        ("Classification", metadata.classification),
        ("Version", metadata.version),
    ]
    if metadata.thread is not None:
        pairs += [("Thread", metadata.thread.thread_id),
                  ("Message", f"{metadata.thread.message_id} "
                              f"({metadata.thread.sequence})")]
    for label, value in pairs:
        if value:
            lines.append(_Line(f"{label}: {value}", "declared-metadata"))
            lines.append(_Line(f"{label}:", "declared-metadata"))
            lines.append(_Line(str(value), "declared-metadata"))

    for header in [*document.headers, *document.footers]:
        text = " ".join(run.text.strip() for run in header.runs or []
                        if run.text and run.text.strip())
        if text:
            lines.append(_Line(text, "declared-furniture"))

    if document.signatures:
        # a paginated renderer labels the signature block; the label is furniture
        lines.append(_Line("Signature", "declared-furniture"))

    if document.footnotes:
        lines.append(_Line("Notes", "declared-furniture"))
        for footnote in document.footnotes:
            text = " ".join(run.text.strip() for run in footnote.runs or []
                            if run.text and run.text.strip())
            marker = f"[{footnote.marker}]"
            lines.append(_Line(f"{marker} {text}", "declared-furniture"))
            lines.append(_Line(text, "declared-furniture"))
            lines.append(_Line(marker, "declared-furniture"))
            # a paginated PDF may run the notes label into the first note
            lines.append(_Line(f"Notes {marker} {text}", "declared-furniture"))

    if metadata.title and metadata.subtitle:
        # a paginated layout draws the title and subtitle on adjacent lines and the
        # parser may return them as one sentence
        lines.append(_Line(f"{metadata.title} {metadata.subtitle}",
                          "declared-metadata"))

    # a table caption is drawn above its table and is not a sentence in the
    # canonical projection, so it needs its own declaration
    for section in document.sections:
        for block in section.blocks:
            caption = block.table.caption if block.table is not None else None
            if caption:
                lines.append(_Line(caption, "declared-caption"))
    if isinstance(document, CdmWorkbook):
        for sheet in document.workbook.sheets:
            for table in [*(b.table for b in sheet.blocks if b.table is not None),
                          *sheet.tables]:
                if table.caption:
                    lines.append(_Line(table.caption, "declared-caption"))

    if canonical is not None:
        for section in document.sections:
            heading = heading_text(section)
            # a page header may re-case the heading ("Parties and Representation"
            # head, "Parties and representation" running head), so headings match
            # case-insensitively; nothing else does
            lines.append(_Line(heading, "declared-heading", fold=True))
            running = running_header_text(heading)
            for _ in range(max(pages, 1)):
                lines.append(_Line(running, "declared-heading", fold=True))
    return lines


def _structural(document, canonical: CanonicalDocument | None
                ) -> tuple[Counter, dict[str, str]]:
    """Whole-text declarations, consumed one per successful alignment."""
    counts: Counter = Counter()
    category: dict[str, str] = {}

    def add(text: str, kind: str) -> None:
        key = _key(text)
        if not key:
            return
        counts[key] += 1
        category.setdefault(key, kind)

    if canonical is not None:
        for sentence in canonical.sentences:
            add(sentence.text,
                "declared-heading" if sentence.kind == "heading"
                else "declared-row" if sentence.kind == "table_row"
                else "declared-block")
    if isinstance(document, CdmWorkbook):
        for sheet in document.workbook.sheets:
            for block in sheet.blocks:
                add(block_text(block), "declared-block")
            for table in [*(b.table for b in sheet.blocks if b.table is not None),
                          *sheet.tables]:
                for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                    cells = [cell.text or cell.display or "" for cell in row.cells]
                    add(" | ".join(cell.strip() for cell in cells), "declared-row")
    return counts, category


def workbook_row_texts(document: CdmDocument) -> dict[str, str]:
    """row id -> the declared ``a | b | c`` text of that row."""
    mapping: dict[str, str] = {}
    if not isinstance(document, CdmWorkbook):
        return mapping
    for sheet in document.workbook.sheets:
        for table in [*(b.table for b in sheet.blocks if b.table is not None),
                      *sheet.tables]:
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                cells = [cell.text or cell.display or "" for cell in row.cells]
                mapping[row.row_id] = " | ".join(cell.strip() for cell in cells)
    return mapping


def _consume(counts: Counter, texts) -> None:
    for text in texts:
        key = _key(text)
        if key and counts.get(key):
            counts[key] -= 1
            if counts[key] == 0:
                counts.pop(key)


# ---------------------------------------------------------------------------
def account_parser_content(document: CdmDocument,
                           canonical: CanonicalDocument | None,
                           view,
                           alignments,
                           *, workbook_row_ids=(), claimed_sentence_ids=(),
                           fmt: str = "") -> AccountingReport:
    """Account for every non-filtered parser sentence, or report it.

    ``claimed_sentence_ids`` covers formats whose alignment is not sentence-based
    (a workbook row), so a claimed row is never mistaken for extras.
    """
    counts, category = _structural(document, canonical)
    claimed = {sentence_id for alignment in alignments if alignment.kind != "missing"
               for sentence_id in alignment.sentence_ids} | set(claimed_sentence_ids)

    consumed: list[str] = []
    if canonical is not None:
        by_id = {sentence.sentence_id: sentence.text for sentence in canonical.sentences}
        for alignment in alignments:
            if alignment.kind == "missing":
                continue
            text = by_id.get(alignment.canonical_id)
            if text is not None:
                consumed.append(text)
    declared_rows = workbook_row_texts(document)
    for row_id in workbook_row_ids:
        text = declared_rows.get(row_id)
        if text is not None:
            consumed.append(text)
    _consume(counts, consumed)

    pages = max((row.get("page") or 1) for row in view.sentences) if view.sentences else 1
    lines = declared_lines(document, canonical, pages=pages)

    report_counts: dict[str, int] = {}
    gaps: list[AccountingGap] = []
    total = 0
    for row in view.sentences:
        total += 1
        if row.get("filtered"):
            report_counts["filtered"] = report_counts.get("filtered", 0) + 1
            continue
        sentence_id = row["sentence_id"]
        if sentence_id in claimed:
            report_counts["aligned"] = report_counts.get("aligned", 0) + 1
            continue
        text = view.text[row["char_start"]:row["char_end"]]
        key = _key(text)
        if key and counts.get(key):
            counts[key] -= 1
            if counts[key] == 0:
                counts.pop(key)
            kind = category.get(key, "declared-block")
            report_counts[kind] = report_counts.get(kind, 0) + 1
            continue
        piece = _words(text)
        match = next((line for line in lines if line.take(piece)), None)
        if match is not None:
            report_counts[match.category] = report_counts.get(match.category, 0) + 1
            continue
        if not piece:
            report_counts["blank"] = report_counts.get("blank", 0) + 1
            continue
        gaps.append(AccountingGap(
            sentence_id=sentence_id, idx=row.get("idx", -1),
            kind=row.get("kind") or "text", page=row.get("page"), text=text))

    return AccountingReport(document_id=document.document_id, format=fmt,
                            total=total, counts=report_counts, gaps=tuple(gaps))


def require_full_accounting(report: AccountingReport) -> None:
    """Fail the build when any non-filtered parser sentence is unexplained."""
    if report.ok:
        return
    raise AlignmentError(report.describe())
