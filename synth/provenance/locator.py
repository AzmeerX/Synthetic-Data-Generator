"""Grounding locator: ``{sentence_ids, quote}`` -> an exact span in canonical text.

Purpose: CLAUDE.md invariant 2 — models never produce offsets; this code does.
Contract: :meth:`Locator.locate` returns a :class:`Located` whose
``char_start``/``char_end`` index the document's canonical text, so
``text[char_start:char_end]`` is the evidence. Order of attempts (spec §5):

1. exact substring inside the cited sentences          -> ``EXACT``
2. normalized match (NFKC, casefold, collapsed spaces,
   unified quotes and dashes) mapped back to raw offsets -> ``NORMALIZED``
3. ``rapidfuzz`` partial alignment >= ``fuzzy_accept``   -> ``FUZZY``
   (scores below ``fuzzy_pass`` are grounded but the gate raises
   ``GROUNDING_FUZZY_LOW``; see DECISIONS.md)
4. any of the above elsewhere in the unit              -> ``SENTENCE_MISMATCH``
5. otherwise                                           -> ``NOT_FOUND`` (item is dropped)

Quotes shorter than ``min_words`` are widened to the full sentence(s) they sit in.
Stores written: none (callers write ``spans``). Spec: P1 §5.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz import fuzz

EXACT = "EXACT"
NORMALIZED = "NORMALIZED"
FUZZY = "FUZZY"
SENTENCE_MISMATCH = "SENTENCE_MISMATCH"
NOT_FOUND = "NOT_FOUND"
GROUNDED = (EXACT, NORMALIZED, FUZZY, SENTENCE_MISMATCH)

_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-", "­": "",
    " ": " ", " ": " ", " ": " ", "\f": " ",
}


@dataclass(frozen=True)
class SentenceRef:
    """The minimum the locator needs about a sentence."""

    sentence_id: str
    idx: int
    char_start: int
    char_end: int


@dataclass
class Located:
    grounding: str
    char_start: int | None
    char_end: int | None
    quote: str                         # the canonical slice; the model's quote if NOT_FOUND
    sentence_ids: list[str] = field(default_factory=list)
    fuzzy_score: float | None = None
    extended: bool = False
    model_quote: str = ""

    @property
    def grounded(self) -> bool:
        return self.grounding in GROUNDED

    @property
    def fuzzy_low(self) -> bool:
        return self.grounding in (FUZZY, SENTENCE_MISMATCH) and (self.fuzzy_score or 100.0) < self._pass

    _pass: float = 92.0


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize for comparison and record, per output char, its raw index."""
    out: list[str] = []
    index: list[int] = []
    prev_space = True
    for pos, ch in enumerate(text):
        piece = _FOLD.get(ch)
        if piece is None:
            piece = unicodedata.normalize("NFKC", ch)
        for c in piece.casefold():
            if c.isspace():
                if prev_space:
                    continue
                out.append(" ")
                index.append(pos)
                prev_space = True
            else:
                out.append(c)
                index.append(pos)
                prev_space = False
    while out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def word_count(text: str) -> int:
    return len(text.split())


class Locator:
    """Locates quotes in one document's canonical text."""

    def __init__(self, text: str, sentences: Sequence[SentenceRef], *,
                 fuzzy_accept: float = 85.0, fuzzy_pass: float = 92.0,
                 min_words: int = 5) -> None:
        self.text = text
        self.sentences = sorted(sentences, key=lambda s: s.char_start)
        self._starts = [s.char_start for s in self.sentences]
        self._by_id = {s.sentence_id: s for s in self.sentences}
        self._by_idx = {s.idx: s for s in self.sentences}
        self.fuzzy_accept = fuzzy_accept
        self.fuzzy_pass = fuzzy_pass
        self.min_words = min_words

    # -- lookup --------------------------------------------------------------
    def sentence(self, key: str | int) -> SentenceRef | None:
        """By sentence id (``snt_…``), prompt label (``s41``) or idx (41)."""
        if isinstance(key, int):
            return self._by_idx.get(key)
        if key in self._by_id:
            return self._by_id[key]
        label = key.split(":")[-1]
        if label[:1] == "s" and label[1:].isdigit():
            return self._by_idx.get(int(label[1:]))
        return None

    def resolve(self, keys: Sequence[str | int]) -> list[SentenceRef]:
        found = [self.sentence(k) for k in keys]
        return [s for s in found if s is not None]

    def sentences_overlapping(self, start: int, end: int) -> list[SentenceRef]:
        if end <= start:
            end = start + 1
        i = max(bisect_right(self._starts, start) - 1, 0)
        hits = []
        for s in self.sentences[i:]:
            if s.char_start >= end:
                break
            if s.char_end > start:
                hits.append(s)
        return hits

    # -- the locator -----------------------------------------------------------
    def locate(self, quote: str, cited: Sequence[str | int | SentenceRef] = (),
               unit: Sequence[str | int | SentenceRef] | None = None) -> Located:
        """Ground ``quote``. ``cited`` = the sentences the model named; ``unit`` =
        the sentences the model was shown (default: the whole document)."""
        quote = quote or ""
        cited_refs = self._refs(cited)
        unit_refs = self._refs(unit) if unit is not None else list(self.sentences)
        if not quote.strip():
            return self._not_found(quote)

        if cited_refs:
            hit = self._search(quote, self._region(cited_refs))
            if hit:
                return self._finish(quote, *hit)
        if unit_refs:
            hit = self._search(quote, self._region(unit_refs))
            if hit:
                start, end, kind, score = hit
                kind = kind if not cited_refs else SENTENCE_MISMATCH
                return self._finish(quote, start, end, kind, score)
        return self._not_found(quote)

    def ground_span(self, start: int, end: int) -> Located:
        """Wrap offsets that are already exact (GLiNER mentions)."""
        return Located(EXACT, start, end, self.text[start:end],
                       [s.sentence_id for s in self.sentences_overlapping(start, end)],
                       None, False, self.text[start:end], self.fuzzy_pass)

    # -- internals ---------------------------------------------------------------
    def _refs(self, keys: Sequence[str | int | SentenceRef] | None) -> list[SentenceRef]:
        refs = []
        for k in keys or ():
            ref = k if isinstance(k, SentenceRef) else self.sentence(k)
            if ref is not None:
                refs.append(ref)
        return refs

    @staticmethod
    def _region(refs: Sequence[SentenceRef]) -> tuple[int, int]:
        return min(r.char_start for r in refs), max(r.char_end for r in refs)

    def _search(self, quote: str, region: tuple[int, int]):
        start, end = region
        window = self.text[start:end]
        pos = window.find(quote)
        if pos >= 0:
            return start + pos, start + pos + len(quote), EXACT, None
        stripped = quote.strip()
        if stripped and stripped != quote:
            pos = window.find(stripped)
            if pos >= 0:
                return start + pos, start + pos + len(stripped), EXACT, None
        norm_window, index = normalize_with_map(window)
        norm_quote = normalize(quote)
        if not norm_quote:
            return None
        pos = norm_window.find(norm_quote)
        if pos >= 0:
            raw_start = start + index[pos]
            raw_end = start + index[pos + len(norm_quote) - 1] + 1
            return raw_start, raw_end, NORMALIZED, None
        if len(norm_quote) < 8 or not norm_window:
            return None
        align = fuzz.partial_ratio_alignment(norm_quote, norm_window)
        if align is None or align.score < self.fuzzy_accept or align.dest_end <= align.dest_start:
            return None
        raw_start = start + index[align.dest_start]
        raw_end = start + index[align.dest_end - 1] + 1
        # Snap to word boundaries inside the region: a span never ends mid-word.
        while raw_start > start and self.text[raw_start - 1].isalnum():
            raw_start -= 1
        while raw_end < end and self.text[raw_end].isalnum():
            raw_end += 1
        return raw_start, raw_end, FUZZY, round(float(align.score), 2)

    def _finish(self, quote: str, start: int, end: int, kind: str, score: float | None) -> Located:
        extended = False
        if word_count(quote) < self.min_words:
            covering = self.sentences_overlapping(start, end)
            if covering:
                new_start = min(s.char_start for s in covering)
                new_end = max(s.char_end for s in covering)
                extended = (new_start, new_end) != (start, end)
                start, end = new_start, new_end
        sentence_ids = [s.sentence_id for s in self.sentences_overlapping(start, end)]
        return Located(kind, start, end, self.text[start:end], sentence_ids, score,
                       extended, quote, self.fuzzy_pass)

    def _not_found(self, quote: str) -> Located:
        return Located(NOT_FOUND, None, None, quote, [], None, False, quote, self.fuzzy_pass)
