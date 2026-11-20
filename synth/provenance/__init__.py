"""Provenance: the grounding locator and sentence-tagged rendering (P1 §5)."""

from synth.provenance.locator import (
    EXACT, FUZZY, GROUNDED, NORMALIZED, NOT_FOUND, SENTENCE_MISMATCH,
    Located, Locator, SentenceRef,
)
from synth.provenance.render import parse_label, render_sentences, sentence_label

__all__ = [
    "EXACT", "NORMALIZED", "FUZZY", "SENTENCE_MISMATCH", "NOT_FOUND", "GROUNDED",
    "Located", "Locator", "SentenceRef", "render_sentences", "sentence_label", "parse_label",
]
