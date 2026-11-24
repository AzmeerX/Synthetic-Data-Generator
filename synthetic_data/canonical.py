"""Canonical sentence layer (architecture §5.2, §4.2, DEC-9).

A canonical *sentence* is the addressable unit between a block and a renderer:
``snt:<doc>/<section>.<block>.<nn>:<tag>``.  It has no coordinates — those belong
to a renderer — but it does have a stable id and an exact substring of the
canonical text projection.

The projection here is the single source of truth for "what this block says":
the TXT renderer writes exactly these strings, so a sentence id and a rendered
line can be compared without guessing.  Sentence ids are derived from the
document id alone (not from the corpus key), so the same canonical document
renders to the same ids in any corpus and any process.

Nothing here invents text: a sentence is always a substring of a run, a list
item, a table row or a heading.  Parser sentence ids (``s19``, ``sen_…``) never
appear in this namespace.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from synthetic_data.model import (
    Block,
    CdmDocument,
    FactStatement,
    Section,
    Table,
    TableCell,
    TableRow,
)
from synthetic_data.model.ids import derive_id

__all__ = [
    "CanonicalSentence",
    "CanonicalDocument",
    "canonicalize",
    "split_sentences",
    "block_text",
    "list_item_text",
    "row_text",
    "cell_text",
    "heading_text",
    "SENTENCE_PREFIX",
]

SENTENCE_PREFIX = "snt"

#: Abbreviations that must not end a sentence.  Legal/business text only.
_ABBREV = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "st", "no", "ltd", "co", "corp", "inc",
    "plc", "llp", "llc", "gmbh", "vs", "etc", "eg", "ie", "cf", "al", "ed",
    "fig", "para", "cl", "sch", "art", "reg",
})
_TERMINATOR = re.compile(r"[.!?][\"'\u201d\u2019)\]]*(?=\s|$)")


@dataclass(frozen=True)
class CanonicalSentence:
    sentence_id: str
    block_id: str
    section_id: str
    index: int                     # 0-based within the block
    text: str
    char_start: int                # within the block projection
    char_end: int
    kind: str                      # heading | sentence | list_item | table_row
    fact_ids: tuple[str, ...] = ()

    @property
    def canonical_node_id(self) -> str:
        return self.block_id


@dataclass
class CanonicalDocument:
    document: CdmDocument
    sentences: list[CanonicalSentence]
    by_block: dict[str, list[CanonicalSentence]] = field(default_factory=dict)
    statement_sentences: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def sentence_ids(self) -> list[str]:
        return [sentence.sentence_id for sentence in self.sentences]

    def for_block(self, block_id: str) -> list[CanonicalSentence]:
        return self.by_block.get(block_id, [])


# ---------------------------------------------------------------------------
# projections: the one definition of a node's text
# ---------------------------------------------------------------------------
def heading_text(section: Section) -> str:
    number = section.number
    return f"{number}. {section.heading}" if number else section.heading


def block_text(block: Block) -> str:
    """Text of a paragraph/note/exhibit/signature block, runs joined by a space."""
    return " ".join(run.text.strip() for run in block.runs or [] if run.text and run.text.strip())


def list_item_text(run_texts: list[str]) -> str:
    return " ".join(text.strip() for text in run_texts if text.strip())


def cell_text(cell: TableCell) -> str:
    return (cell.text or cell.display or "").strip()


def row_text(row: TableRow) -> str:
    """One pipe-delimited table row — the exact shape the TXT renderer writes.

    Leading and trailing pipes are part of the canonical text so that a row is an
    ``exact`` parser match rather than a substring of a wider row (§8.3).
    """
    return "| " + " | ".join(cell_text(cell) for cell in row.cells) + " |"


# ---------------------------------------------------------------------------
# sentence splitting
# ---------------------------------------------------------------------------
def split_sentences(text: str) -> list[tuple[int, int]]:
    """Split ``text`` into stripped ``(start, end)`` spans without rewriting it.

    A boundary is a ``.``/``!``/``?`` followed by whitespace when the next
    non-space character starts a sentence and the preceding token is not a known
    abbreviation.  The result always covers every non-space character exactly
    once, in order: nothing is dropped and nothing is duplicated.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _TERMINATOR.finditer(text):
        end = match.end()
        before = text[start:end - 1]
        token = re.search(r"([A-Za-z][A-Za-z.]*)$", before)
        if token:
            bare = token.group(1).rstrip(".")
            if bare.lower() in _ABBREV or (len(bare) == 1 and bare.isupper()):
                continue
        following = text[end:].lstrip()
        if following and not (following[0].isupper() or following[0].isdigit()
                              or following[0] in "\"'\u201c\u2018(["):
            continue
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    out: list[tuple[int, int]] = []
    for begin, finish in spans:
        piece = text[begin:finish]
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        begin, finish = begin + lead, finish - trail
        if finish > begin:
            out.append((begin, finish))
    return out


# ---------------------------------------------------------------------------
# canonicalisation
# ---------------------------------------------------------------------------
def _document_key(document_id: str) -> bytes:
    return hashlib.blake2b(f"{SENTENCE_PREFIX}:{document_id}".encode("utf-8"),
                           digest_size=16).digest()


def _sentence_id(key: bytes, anchor_id: str, index: int) -> str:
    """``snt:<doc>/<section>.<block>.<nn>:<tag>`` — the tag is re-derived, never reused."""
    scope = anchor_id.split(":", 1)[1] if ":" in anchor_id else anchor_id
    if ":" in scope:                       # drop the anchor's own tag, if it has one
        scope = scope.rsplit(":", 1)[0]
    return derive_id(SENTENCE_PREFIX, f"{scope}.{index + 1:02d}", key)


def canonicalize(document: CdmDocument) -> CanonicalDocument:
    """Assign a stable id to every sentence of a prose document.

    Table rows, list items and headings are one sentence each because that is how
    the parser treats them; paragraphs and notes are split by
    :func:`split_sentences`.  A sentence never crosses a block, and a row or list
    item is anchored to its own id so evidence can name the row it came from.
    """
    if document.doc_kind != "prose":
        raise ValueError(
            f"{document.document_id} is a {document.doc_kind} document; sentence "
            f"canonicalisation is defined for prose only (§7.7)")
    key = _document_key(document.document_id)
    sentences: list[CanonicalSentence] = []
    by_anchor: dict[str, list[CanonicalSentence]] = {}
    statement_sentences: dict[str, tuple[str, ...]] = {}

    for section in document.sections:
        heading = heading_text(section)
        sentence = CanonicalSentence(
            sentence_id=_sentence_id(key, section.section_id, 0),
            block_id=section.section_id, section_id=section.section_id, index=0,
            text=heading, char_start=0, char_end=len(heading), kind="heading")
        sentences.append(sentence)
        by_anchor.setdefault(section.section_id, []).append(sentence)

        for block in section.blocks:
            produced = _block_sentences(key, block, section.section_id)
            sentences.extend(produced)
            for sentence in produced:
                by_anchor.setdefault(sentence.block_id, []).append(sentence)
            if block.kind != "table":
                projection = block_text(block)
                for statement in document.statements:
                    if statement.node_id != block.block_id:
                        continue
                    covered = _covering(
                        produced, statement_surface_range(block, statement, projection))
                    if covered:
                        statement_sentences[statement.statement_id] = tuple(
                            sentence.sentence_id for sentence in covered)

    # statements anchored to a row, a cell or the whole table resolve to the row
    for section in document.sections:
        for block in section.blocks:
            if block.table is None:
                continue
            rows = [*block.table.header_rows, *block.table.body_rows,
                    *block.table.total_rows]
            used: set[str] = set()
            for statement in document.statements:
                if (statement.statement_id in statement_sentences
                        or statement.node_id not in
                        {block.table.table_id, block.block_id}):
                    continue
                surface = " ".join(statement.surface.split())
                for row in rows:
                    if row.row_id in used:
                        continue
                    haystack = row_text(row)
                    cells = [cell_text(cell) for cell in row.cells]
                    if surface and (surface in haystack or surface in cells):
                        sentence = by_anchor.get(row.row_id, [])
                        if sentence:
                            statement_sentences[statement.statement_id] = (
                                sentence[0].sentence_id,)
                            used.add(row.row_id)
                        break

    return CanonicalDocument(document=document, sentences=sentences,
                             by_block=by_anchor,
                             statement_sentences=statement_sentences)


def _block_sentences(key: bytes, block: Block, section_id: str
                     ) -> list[CanonicalSentence]:
    """Sentences for one block, each anchored to the node it belongs to."""
    out: list[CanonicalSentence] = []

    def emit(anchor_id: str, text: str, base: int, kind: str,
             fact_ids: tuple[str, ...] = ()) -> None:
        out.append(CanonicalSentence(
            sentence_id=_sentence_id(key, anchor_id, len(out)),
            block_id=anchor_id, section_id=section_id, index=len(out),
            text=text, char_start=base, char_end=base + len(text),
            kind=kind, fact_ids=fact_ids))

    if block.kind == "page_break":
        return out
    if block.kind == "table" and block.table is not None:
        base = 0
        for row in [*block.table.header_rows, *block.table.body_rows,
                    *block.table.total_rows]:
            text = row_text(row)
            facts = tuple(sorted({cell.fact_ref for cell in row.cells if cell.fact_ref}))
            emit(row.row_id, text, base, "table_row", facts)
            base += len(text) + 1
        return out
    if block.kind == "list":
        base = 0
        for item in block.items or []:
            text = list_item_text([run.text or "" for run in item.runs])
            facts = tuple(sorted({run.fact_ref for run in item.runs if run.fact_ref}))
            emit(item.item_id, text, base, "list_item", facts)
            base += len(text) + 1
        return out

    text = block_text(block)
    for start, end in split_sentences(text):
        piece = text[start:end]
        facts = tuple(sorted({
            run.fact_ref for run in block.runs or []
            if run.fact_ref and run.text and piece in run.text
        }))
        emit(block.block_id, piece, start, "sentence", facts)
    return out


def statement_surface_range(block: Block, statement: FactStatement, text: str
                            ) -> tuple[int, int] | None:
    """Char range in the block projection that carries this statement's fact.

    Prefers the run that names the fact; falls back to the statement surface when
    it is an exact substring.  Returns ``None`` when neither can be located — the
    caller then leaves ``canonical_sentence_ids`` empty rather than guessing.
    """
    cursor = 0
    for run in block.runs or []:
        run_text = run.text or ""
        position = text.find(run_text, cursor)
        if position < 0:
            continue
        if run.fact_ref == statement.fact_id:
            return position, position + len(run_text)
        cursor = position + len(run_text)
    position = text.find(statement.surface)
    if position >= 0:
        return position, position + len(statement.surface)
    return None


def _covering(sentences: list[CanonicalSentence], span: tuple[int, int] | None
              ) -> list[CanonicalSentence]:
    if span is None:
        return []
    start, end = span
    return [sentence for sentence in sentences
            if sentence.char_start < end and sentence.char_end > start]


def apply_statement_sentences(document: CdmDocument,
                              canonical: CanonicalDocument) -> CdmDocument:
    """Return a copy of ``document`` with ``canonical_sentence_ids`` populated."""
    statements: list[FactStatement] = []
    for statement in document.statements:
        ids = canonical.statement_sentences.get(statement.statement_id, ())
        statements.append(statement.model_copy(update={"canonical_sentence_ids": list(ids)}))
    return document.model_copy(update={"statements": statements})
