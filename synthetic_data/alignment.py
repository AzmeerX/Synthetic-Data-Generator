"""Canonical-to-parser alignment (architecture §8.2–8.5).

This module is the only place where a canonical sentence id and a parser sentence
id meet, and they are never merged into one namespace: a canonical id is produced
by :mod:`synthetic_data.canonical`, a parser id by the vendored parser.  An
alignment record names both.

Evidence rule (§8.3): **quote = parser substring**.  Every quote returned here is
sliced out of ``DocView.text`` and is therefore reproducible from the store by
``text[char_start:char_end]``.  Nothing is reconstructed from model text.

Verdicts (``AlignmentGraph.kind``):

``exact``       the parser sentence text equals the canonical sentence text
``normalised``  equal after Unicode/whitespace normalisation (NFC, runs of space)
``split``       the parser cut one canonical sentence into adjacent sentences; the
                quote is their exact concatenation and must contain every surface
``merged``      the parser kept one sentence where the renderer emitted several;
                exactly one parser sentence may contain the canonical text
``partial``     fuzzy match — never acceptable as fact evidence
``ambiguous``   more than one candidate and no order-based choice — fatal
``missing``     no candidate — fatal

Missing, ambiguous and partial verdicts on a fact-bearing sentence are refused by
:func:`require_fact_evidence`; they are never silently downgraded to "fine".
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from synthetic_data.canonical import CanonicalDocument
from synthetic_data.surfaces import (date_surface_key, money_surface_key,
                                     money_surfaces, percent_surface_key)

__all__ = [
    "SentenceAlignment",
    "FactEvidence",
    "AlignmentError",
    "align_document",
    "align_facts",
    "require_fact_evidence",
    "normalise",
    "EVIDENCE_KINDS",
]

EVIDENCE_KINDS = frozenset({"exact", "normalised", "split", "merged"})
_FUZZY_FLOOR = 0.85
_MAX_SPLIT = 4


class AlignmentError(RuntimeError):
    """Raised when a fact-bearing sentence cannot be grounded unambiguously."""


@dataclass(frozen=True)
class SentenceAlignment:
    canonical_id: str
    kind: str
    sentence_ids: tuple[str, ...] = ()
    text: str = ""
    char_start: int | None = None
    char_end: int | None = None
    page: int | None = None
    score: float = 0.0
    filtered: bool = False
    #: D7: a table row was returned by the parser as its individual cells, so the
    #: row's structure is absent even though its values are present
    missing_row_structure: bool = False
    #: ``(sentence_id, char_start, char_end)`` for each parser sentence consumed
    pieces: tuple[tuple[str, int, int], ...] = ()

    @property
    def grounded(self) -> bool:
        return self.kind in EVIDENCE_KINDS and bool(self.text)


@dataclass(frozen=True)
class FactEvidence:
    fact_id: str
    statement_id: str
    sentence_ids: tuple[str, ...]
    quote: str
    kinds: tuple[str, ...]
    acceptable: bool
    problems: tuple[str, ...] = ()
    char_start: int | None = None
    char_end: int | None = None
    #: canonical sentence ids the statement is anchored to
    canonical_ids: tuple[str, ...] = ()
    #: canonical node (block, row or cell) that states the fact
    node_id: str = ""


def normalise(text: str) -> str:
    """Unicode NFKC, straight quotes, collapsed whitespace — never word-substituting."""
    text = unicodedata.normalize("NFKC", text)
    for source, target in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'),
                           ("\u201d", '"'), ("\u2013", "-"), ("\u2014", "-"),
                           ("\u00a0", " ")):
        text = text.replace(source, target)
    return " ".join(text.split())


@dataclass
class _ParserSentence:
    sentence_id: str
    idx: int
    text: str
    char_start: int
    char_end: int
    filtered: bool
    page: int | None


def _parser_sentences(view) -> list[_ParserSentence]:
    out: list[_ParserSentence] = []
    for row in view.sentences:
        text = view.sentence_text(row)
        out.append(_ParserSentence(
            sentence_id=row["sentence_id"], idx=row["idx"], text=text,
            char_start=row["char_start"], char_end=row["char_end"],
            filtered=bool(row.get("filtered")), page=row.get("page")))
    return out


def _page_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\f":
            starts.append(index + 1)
    return starts


def align_document(canonical: CanonicalDocument, view) -> list[SentenceAlignment]:
    """Map every canonical sentence to the parser sentence(s) that rendered it."""
    parser = _parser_sentences(view)
    starts = _page_starts(view.text)
    claimed: set[str] = set()
    cursor = 0
    out: list[SentenceAlignment] = []

    def page_of(position: int) -> int:
        page = 1
        for number, start in enumerate(starts, start=1):
            if position >= start:
                page = number
        return page

    def record(canonical_id: str, kind: str, chosen: list[_ParserSentence],
               text: str, score: float, advance: bool = True) -> None:
        out.append(SentenceAlignment(
            canonical_id=canonical_id, kind=kind,
            sentence_ids=tuple(sentence.sentence_id for sentence in chosen),
            text=text, char_start=chosen[0].char_start, char_end=chosen[-1].char_end,
            page=chosen[0].page or page_of(chosen[0].char_start),
            score=score, filtered=any(sentence.filtered for sentence in chosen),
            pieces=tuple((sentence.sentence_id, sentence.char_start, sentence.char_end)
                         for sentence in chosen)))
        if advance:
            for sentence in chosen:
                claimed.add(sentence.sentence_id)

    for sentence in canonical.sentences:
        text = normalise(sentence.text)

        if sentence.kind == "table_row":
            flattened = _align_flattened_row(sentence, parser, view, cursor)
            if flattened is not None:
                chosen, last, structure = flattened
                quote = view.text[chosen[0].char_start:chosen[-1].char_end]
                out.append(SentenceAlignment(
                    canonical_id=sentence.sentence_id,
                    kind=("normalised" if structure
                          else ("merged" if len(chosen) == 1 else "split")),
                    sentence_ids=tuple(item.sentence_id for item in chosen),
                    text=quote, char_start=chosen[0].char_start,
                    char_end=chosen[-1].char_end,
                    page=chosen[0].page or page_of(chosen[0].char_start),
                    score=1.0, filtered=any(item.filtered for item in chosen),
                    missing_row_structure=not structure,
                    pieces=tuple((item.sentence_id, item.char_start, item.char_end)
                                 for item in chosen)))
                for item in chosen:
                    claimed.add(item.sentence_id)
                cursor = last + 1
                continue

        exact = next((position for position in range(cursor, len(parser))
                      if normalise(parser[position].text) == text), None)
        if exact is not None:
            chosen = parser[exact]
            if chosen.sentence_id in claimed:
                out.append(SentenceAlignment(sentence.sentence_id, "ambiguous",
                                             (chosen.sentence_id,)))
            else:
                kind = "exact" if chosen.text == sentence.text else "normalised"
                record(sentence.sentence_id, kind, [chosen], chosen.text, 1.0)
            cursor = exact + 1
            continue

        # split: parser sentences whose concatenation is the canonical text.
        # A page break can leave the first fragment several rows before the
        # continuation (furniture, or a heading of the next page between them), so
        # candidates are pruned by "is a prefix of the canonical sentence" and the
        # comparison joins the *text* of the unfiltered sentences.
        split = None
        for start in range(cursor, min(cursor + 12, len(parser))):
            if parser[start].filtered:
                continue
            first_text = normalise(parser[start].text)
            if not first_text or not text.startswith(first_text):
                continue
            pieces: list[int] = [start]
            joined = first_text
            position = start + 1
            # furniture between the fragments must not consume the split window
            while position < len(parser) and len(pieces) < _MAX_SPLIT:
                if parser[position].filtered:
                    position += 1
                    continue
                pieces.append(position)
                joined = normalise(" ".join(parser[index].text for index in pieces))
                if joined == text:
                    split = pieces
                    break
                if not text.startswith(joined):
                    break
                position += 1
            if split:
                break
        if split:
            chosen = [parser[index] for index in split]
            if any(sentence.sentence_id in claimed for sentence in chosen):
                out.append(SentenceAlignment(sentence.sentence_id, "ambiguous",
                                             tuple(s.sentence_id for s in chosen)))
            else:
                quote = view.text[chosen[0].char_start:chosen[-1].char_end]
                record(sentence.sentence_id, "split", chosen, quote, 1.0)
            cursor = split[-1] + 1
            continue

        # merged: exactly one parser sentence contains the canonical text
        containers = [position for position in range(cursor, len(parser))
                      if text and text in normalise(parser[position].text)]
        if len(containers) == 1:
            chosen = parser[containers[0]]
            # a parser sentence may hold several canonical sentences (it merged two
            # lines): the later ones still align to it, and the cursor stays there so
            # they can find it.  Only *distinct* containers are ambiguous.
            already = chosen.sentence_id in claimed
            record(sentence.sentence_id, "merged", [chosen], chosen.text, 1.0,
                   advance=not already)
            # stay on this parser sentence: it may hold the next canonical sentence
            # too, and moving past it would make that one look missing
            cursor = containers[0]
            continue
        if len(containers) > 1:
            out.append(SentenceAlignment(sentence.sentence_id, "ambiguous",
                                         tuple(parser[p].sentence_id for p in containers)))
            cursor = containers[0]
            continue

        # wrapped: a line break can cut the canonical sentence at either end, so the
        # parser sentence opens with the tail of the previous sentence ("6. The
        # analysis ... and") and the canonical sentence starts partway through it.
        # The exact-split search above requires the text to start at the beginning
        # of a parser sentence and cannot see this case; the fuzzy fallback then
        # claims only the first fragment and leaves the last one unexplained.
        wrapped = _wrapped_split(parser, cursor, text, claimed)
        if wrapped:
            indices, ends_inside = wrapped
            chosen = [parser[index] for index in indices]
            quote = view.text[chosen[0].char_start:chosen[-1].char_end]
            record(sentence.sentence_id, "split", chosen, quote, 1.0)
            # the sentence ends partway through the last parser sentence, which also
            # opens the next canonical sentence: stay on it instead of moving past
            cursor = indices[-1] + (0 if ends_inside else 1)
            continue

        # partial: a unique fuzzy candidate above the floor
        scored = sorted(
            ((difflib.SequenceMatcher(None, text, normalise(parser[position].text)).ratio(),
              position) for position in range(cursor, len(parser))), reverse=True)
        if scored and scored[0][0] >= _FUZZY_FLOOR:
            best = scored[0][0]
            rivals = [position for score, position in scored
                      if abs(score - best) < 1e-9]
            if len(rivals) > 1:
                out.append(SentenceAlignment(sentence.sentence_id, "ambiguous",
                                             tuple(parser[p].sentence_id for p in rivals),
                                             score=best))
            else:
                chosen = parser[scored[0][1]]
                record(sentence.sentence_id, "partial", [chosen], chosen.text, best,
                       advance=False)
            continue

        out.append(SentenceAlignment(sentence.sentence_id, "missing"))

    return out


def _wrapped_split(parser: list[_ParserSentence], cursor: int, text: str,
                   claimed: set[str]) -> tuple[list[int], bool] | None:
    """The parser sentences that carry ``text`` when a line break cuts it.

    ``text`` is a normalised canonical sentence.  A run is accepted when it
    contains the whole sentence and the *first* sentence of the run contributes
    to the match: a run that only reaches the text in a later sentence does not
    start where the text starts, so the search moves on.  A first sentence whose
    match begins at its very start and is already claimed is a repetition, not a
    continuation, and is left to the caller.

    The flag says whether the sentence ends inside the last parser sentence; the
    next canonical sentence then starts in that same sentence, so the caller must
    not move the cursor past it.
    """
    for start in range(cursor, min(cursor + 12, len(parser))):
        if parser[start].filtered:
            continue
        first_text = normalise(parser[start].text)
        if not first_text:
            continue
        indices = [start]
        joined = first_text
        position = start + 1
        while True:
            offset = joined.find(text)
            if offset >= 0:
                if offset >= len(first_text):
                    break       # this sentence does not carry the start of the text
                if offset == 0 and parser[start].sentence_id in claimed:
                    break       # a repeat of an already-claimed sentence
                return indices, offset + len(text) < len(joined)
            if len(indices) >= _MAX_SPLIT or position >= len(parser):
                break
            if parser[position].filtered:
                position += 1
                continue
            indices.append(position)
            joined = normalise(" ".join(parser[index].text for index in indices))
            position += 1
    return None


def _row_cells(text: str) -> list[str]:
    """Split a canonical row projection (``| a | b |``) back into its cells."""
    body = text.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _flatten_cells(text: str) -> str:
    """Row text with its delimiters removed: ``| a | b |`` -> ``a b``."""
    return normalise(text.replace("|", " "))


def _align_flattened_row(sentence, parser, view, cursor: int):
    """Align a table row that the parser returned as its individual cells (D7).

    A flattener may return one cell per sentence, several cells in one sentence, or
    one cell wrapped over several sentences.  All three are the same string: the
    row's cells joined by spaces.  So the match is a window of consecutive parser
    sentences whose joined text equals that string — which also stops repeated
    values (an amount recurring in several rows) from pulling the cursor into a
    neighbouring row.
    """
    cells = [cell for cell in _row_cells(sentence.text) if any(c.isalnum() for c in cell)]
    if not cells:
        return None
    expected = _flatten_cells(" ".join(cells))
    if len(cells) == 1:
        # a one-cell row (a spanning header) is exactly that cell's text
        needle = normalise(cells[0])
        for probe in range(cursor, min(cursor + 3, len(parser))):
            if normalise(parser[probe].text) == needle:
                return [parser[probe]], probe, True
        return None

    # one parser sentence that carries the whole row: the row's structure survived
    # (DOCX keeps real rows; TXT keeps pipe-delimited ones)
    for probe in range(cursor, min(cursor + 3, len(parser))):
        if _flatten_cells(parser[probe].text) == expected:
            return [parser[probe]], probe, True
    first_token = cells[0].split()[0]
    # a table can sit many parser sentences after the cursor when an earlier block
    # was long or when page furniture intervenes; the row's first cell is a strong
    # enough anchor that a wider window is safe
    for start in range(cursor, min(cursor + 40, len(parser))):
        if first_token not in parser[start].text:
            continue
        joined = ""
        chosen: list[_ParserSentence] = []
        for end in range(start, min(start + 30, len(parser))):
            piece = _flatten_cells(parser[end].text)
            if not any(char.isalnum() for char in piece):
                continue          # a dropped dash or stray glyph: not part of the row
            joined = f"{joined} {piece}".strip()
            chosen.append(parser[end])
            if expected in joined:
                # one sentence that contains the row (plus its neighbours) keeps the
                # cell order; several sentences mean the row itself was flattened
                return chosen, end, len(chosen) == 1
            if len(joined) > len(expected) + 160:
                break
    return None


def align_facts(document, canonical: CanonicalDocument, alignments, fact_surfaces,
                parser_text: str) -> list[FactEvidence]:
    """Check every stated fact against its aligned parser span (§8.4, §8.5)."""
    by_id = {alignment.canonical_id: alignment for alignment in alignments}
    evidence: list[FactEvidence] = []
    for statement in document.statements:
        if not statement.canonical_sentence_ids:
            evidence.append(FactEvidence(
                fact_id=statement.fact_id, statement_id=statement.statement_id,
                sentence_ids=(), quote="", kinds=(), acceptable=False,
                problems=("statement carries no canonical sentence id",),
                node_id=statement.node_id))
            continue
        problems: list[str] = []
        kinds: list[str] = []
        ids: list[str] = []
        spans: list[tuple[int, int]] = []
        for sentence_id in statement.canonical_sentence_ids:
            alignment = by_id.get(sentence_id)
            if alignment is None:
                problems.append(f"{sentence_id} was never aligned")
                continue
            kinds.append(alignment.kind)
            ids.extend(alignment.sentence_ids)
            if alignment.kind not in EVIDENCE_KINDS:
                problems.append(f"{sentence_id} aligned as {alignment.kind}")
            if alignment.filtered:
                problems.append(f"{sentence_id} aligns to filtered parser text")
            if alignment.char_start is not None and alignment.char_end is not None:
                spans.append((alignment.char_start, alignment.char_end))

        quote = ""
        quote_span: tuple[int, int] | None = None
        narrowed = _narrow_to_cell(statement, by_id, parser_text)
        if narrowed is not None:
            start, end = narrowed
            quote = parser_text[start:end]
            quote_span = (start, end)
        elif spans:
            start = min(span[0] for span in spans)
            end = max(span[1] for span in spans)
            quote = parser_text[start:end]
            quote_span = (start, end)

        # The statement must stand on its own quote.  A second occurrence of the
        # same fact elsewhere in the document is not evidence for this node.
        if quote:
            for slot, value in statement.value_slots.items():
                if value and not surface_in_quote(value, quote):
                    problems.append(
                        f"{slot} value {value!r} is not in this statement's quote")
            if not statement.value_slots and not surface_in_quote(statement.surface,
                                                                  quote):
                problems.append("statement surface is not in its own quote")

        evidence.append(FactEvidence(
            fact_id=statement.fact_id, statement_id=statement.statement_id,
            sentence_ids=tuple(dict.fromkeys(ids)), quote=quote,
            kinds=tuple(dict.fromkeys(kinds)),
            acceptable=not problems and bool(quote), problems=tuple(problems),
            char_start=quote_span[0] if quote_span else None,
            char_end=quote_span[1] if quote_span else None,
            canonical_ids=tuple(statement.canonical_sentence_ids),
            node_id=statement.node_id))
    return _check_fact_surfaces(evidence, fact_surfaces, by_id)


def _narrow_to_cell(statement, by_id, parser_text: str) -> tuple[int, int] | None:
    """When a row was flattened (D7), quote only the cell this statement names.

    A cell-level statement must stand on its own cell, not on the whole row: the row
    quote would let a value from a neighbouring cell support it.
    """
    surfaces = [statement.surface] + list(statement.value_slots.values())
    surfaces = [surface for surface in surfaces if surface]
    if not surfaces:
        return None
    for sentence_id in statement.canonical_sentence_ids:
        alignment = by_id.get(sentence_id)
        if alignment is None or not alignment.missing_row_structure:
            continue
        for piece_id, start, end in alignment.pieces:
            piece = normalise(parser_text[start:end])
            for surface in surfaces:
                if normalise(surface) == piece or surface_in_quote(surface, piece):
                    return start, end
        return None
    return None


_DATE_LIKE = re.compile(
    r"\d{1,2}(?:st|nd|rd|th)? [A-Za-z]{3,9} \d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|[A-Za-z]{3,9} \d{1,2}, \d{4}")
_PERCENT_LIKE = re.compile(r"-?\d+(?:\.\d+)?\s?(?:%|per cent)"
                           r"|-?\d+ basis points")


def _date_like(text: str) -> list[str]:
    return [match.group(0) for match in _DATE_LIKE.finditer(text)]


def _percent_like(text: str) -> list[str]:
    return [match.group(0) for match in _PERCENT_LIKE.finditer(text)]


def surface_in_quote(surface: str, quote: str) -> bool:
    """Is ``surface`` in ``quote``, formatting aside?

    Money is compared by value (``money_surface_key``), so "£4,000,000" matches
    "GBP 4,000,000.00" but never "£4,500,000".  Dates and percentages are compared
    the same way.  Everything else is whitespace- and Unicode-normalised substring
    matching.
    """
    folded_surface = normalise(surface)
    folded_quote = normalise(quote)
    if folded_surface in folded_quote:
        return True
    money = money_surface_key(folded_surface)
    if money is not None:
        return any(money_surface_key(candidate) == money
                   for candidate in money_surfaces(folded_quote))
    date = date_surface_key(folded_surface)
    if date is not None:
        return any(date_surface_key(candidate) == date
                   for candidate in _date_like(folded_quote))
    percent = percent_surface_key(folded_surface)
    if percent is not None:
        return any(percent_surface_key(candidate) == percent
                   for candidate in _percent_like(folded_quote))
    return False


def _check_fact_surfaces(evidence: list[FactEvidence],
                         fact_surfaces: dict[str, list[str]],
                         alignments: dict[str, SentenceAlignment] | None = None
                         ) -> list[FactEvidence]:
    """A fact's surfaces must be recoverable from *its own* rendered occurrences.

    One fact can be stated more than once (a paragraph and a table row, §4.4), so
    the surfaces are checked against every occurrence the fact produced — never
    against a single statement's (possibly narrowed) quote, and never against model
    text.  The occurrence text is the *aligned span*: for a flattened table row (D7)
    that is the whole row, not the one cell the statement is quoted from.
    """
    haystacks: dict[str, list[str]] = {}
    for item in evidence:
        if not item.acceptable:
            continue
        texts = []
        if alignments:
            for canonical_id in item.canonical_ids:
                alignment = alignments.get(canonical_id)
                if alignment is not None and alignment.text:
                    texts.append(alignment.text)
        if not texts and item.quote:
            texts.append(item.quote)
        haystacks.setdefault(item.fact_id, []).extend(texts)

    checked: list[FactEvidence] = []
    for item in evidence:
        problems = list(item.problems)
        surfaces = fact_surfaces.get(item.fact_id, [])
        if surfaces:
            haystack = normalise(" ".join(haystacks.get(item.fact_id, [])))
            for surface in surfaces:
                if not surface_in_quote(surface, haystack):
                    problems.append(f"surface {surface!r} is not in any occurrence of "
                                    f"{item.fact_id}")
        checked.append(FactEvidence(
            fact_id=item.fact_id, statement_id=item.statement_id,
            sentence_ids=item.sentence_ids, quote=item.quote, kinds=item.kinds,
            acceptable=not problems, problems=tuple(problems),
            char_start=item.char_start, char_end=item.char_end,
            canonical_ids=item.canonical_ids, node_id=item.node_id))
    return checked


def require_fact_evidence(evidence: list[FactEvidence]) -> None:
    """Refuse the document when any fact is missing, ambiguous or unsupported."""
    failures = [item for item in evidence if not item.acceptable]
    if not failures:
        return
    lines = [f"{len(failures)} fact(s) cannot be grounded:"]
    for item in failures[:10]:
        lines.append(f"  {item.statement_id} ({item.fact_id}): "
                     f"{'; '.join(item.problems) or 'no quote'}")
    if len(failures) > 10:
        lines.append(f"  ... {len(failures) - 10} more")
    raise AlignmentError("\n".join(lines))
