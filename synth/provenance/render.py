"""Sentence-tagged rendering for prompts (P1 §5).

One sentence per line, ``[s41] …``; packets that mix documents use
``[d2:s41] …``. Models cite these labels; the locator maps them back.
"""

from __future__ import annotations

import re
from typing import Iterable

_LABEL = re.compile(r"^(?:d(?P<doc>\d+):)?s(?P<idx>\d+)$")


def sentence_label(idx: int, doc_no: int | None = None) -> str:
    return f"d{doc_no}:s{idx}" if doc_no is not None else f"s{idx}"


def parse_label(label: str) -> tuple[int | None, int] | None:
    """``"s41"`` -> ``(None, 41)``; ``"d2:s41"`` -> ``(2, 41)``; junk -> ``None``."""
    match = _LABEL.match(label.strip())
    if not match:
        return None
    doc = match.group("doc")
    return (int(doc) if doc is not None else None), int(match.group("idx"))


def render_sentences(rows: Iterable[tuple[int, str]], doc_no: int | None = None) -> str:
    """``rows`` = ``(idx, sentence text)`` pairs, in order."""
    lines = []
    for idx, text in rows:
        flat = " ".join(text.split())
        lines.append(f"[{sentence_label(idx, doc_no)}] {flat}")
    return "\n".join(lines)
