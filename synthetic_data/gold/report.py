"""Alignment and gold reporting (§13.3, §8.5 format drift).

One row per document-format, aggregated per corpus.  The report deliberately
separates what was *measured* (alignment rates, page counts, currency extraction,
bbox coverage) from what was *asserted* (the model's typed values), because the
whole point of Phase G is that the two are not the same thing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from synthetic_data.alignment import surface_in_quote
from synthetic_data.gold.build import GoldPair

__all__ = ["FormatRow", "format_rows", "corpus_summary", "markdown_report",
           "fact_surface_drift"]


@dataclass
class FormatRow:
    document_id: str
    doc_type: str
    format: str
    pages: int | None
    pageless: bool | None
    units: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    facts_grounded: int = 0
    facts_total: int = 0
    cells: int = 0
    rows: int = 0
    bbox_coverage: float | None = None
    currency_stated: int = 0
    currency_not_stated: int = 0
    derived: int = 0
    v1_ok: bool | None = None
    v2_errors: int = 0

    @property
    def alignment_rate(self) -> float:
        return self.facts_grounded / self.facts_total if self.facts_total else 1.0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__, alignment_rate=round(self.alignment_rate, 4))


def format_rows(pairs: Iterable[tuple[Any, GoldPair]], v1_results=None,
                v2_errors=None) -> list[FormatRow]:
    """``pairs`` is ``(document, pair)``; the results are optional so a dry run works."""
    rows: list[FormatRow] = []
    for index, (document, pair) in enumerate(pairs):
        summary = pair.v2.alignment
        evidence = pair.evidence
        row = FormatRow(
            document_id=document.document_id, doc_type=document.doc_type,
            format=pair.v2.doc.format or "",
            pages=pair.v2.doc.pages, pageless=pair.v2.doc.pageless,
            units=len(pair.alignment.units), counts=dict(summary.counts),
            facts_grounded=sum(1 for item in evidence if item.acceptable),
            facts_total=len(evidence), cells=summary.cells, rows=summary.rows,
            bbox_coverage=summary.bbox_coverage,
            currency_stated=summary.currency_stated,
            currency_not_stated=summary.currency_not_stated,
            derived=sum(1 for prop in pair.v2.propositions if prop.derived_from))
        if v1_results is not None:
            row.v1_ok = bool(v1_results[index])
        if v2_errors is not None:
            row.v2_errors = len(v2_errors[index])
        rows.append(row)
    return rows


def corpus_summary(rows: list[FormatRow]) -> dict[str, Any]:
    by_format: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_format.setdefault(row.format, {
            "documents": 0, "facts": 0, "grounded": 0, "v1_ok": 0,
            "v2_errors": 0, "currency_stated": 0, "currency_not_stated": 0})
        entry["documents"] += 1
        entry["facts"] += row.facts_total
        entry["grounded"] += row.facts_grounded
        entry["v1_ok"] += 1 if row.v1_ok else 0
        entry["v2_errors"] += row.v2_errors
        entry["currency_stated"] += row.currency_stated
        entry["currency_not_stated"] += row.currency_not_stated
    for entry in by_format.values():
        entry["alignment_rate"] = (round(entry["grounded"] / entry["facts"], 4)
                                   if entry["facts"] else 1.0)
    kinds: dict[str, int] = {}
    for row in rows:
        for kind, count in row.counts.items():
            kinds[kind] = kinds.get(kind, 0) + count
    return {
        "documents": len(rows),
        "formats": by_format,
        "alignment_kinds": kinds,
        "facts": sum(row.facts_total for row in rows),
        "grounded": sum(row.facts_grounded for row in rows),
        "currency_stated": sum(row.currency_stated for row in rows),
        "currency_not_stated": sum(row.currency_not_stated for row in rows),
        "derived": sum(row.derived for row in rows),
        "v1_valid": sum(1 for row in rows if row.v1_ok),
        "v2_errors": sum(row.v2_errors for row in rows),
    }


def fact_surface_drift(pairs: Iterable[tuple[Any, GoldPair]]) -> list[str]:
    """The same document must state the same fact values in every format.

    A renderer that prints ``£4,000,000`` in one format and ``4,000,000`` in another
    is allowed; a renderer that prints a *different amount* is not.  Comparison is
    formatting-insensitive by construction (``surface_in_quote``).
    """
    by_document: dict[str, list[GoldPair]] = {}
    for document, pair in pairs:
        by_document.setdefault(document.document_id, []).append(pair)
    drift: list[str] = []
    for document_id, document_pairs in by_document.items():
        if len(document_pairs) < 2:
            continue
        baseline = document_pairs[0]
        for other in document_pairs[1:]:
            for base_edge, other_edge in zip(baseline.v1.edges, other.v1.edges):
                if base_edge.id != other_edge.id:
                    continue
                base_quote = " ".join(item.quote for item in base_edge.evidence)
                other_quote = " ".join(item.quote for item in other_edge.evidence)
                if base_quote and other_quote and not _agree(base_quote, other_quote):
                    drift.append(
                        f"{document_id} {base_edge.id}: "
                        f"{baseline.v2.doc.format} and {other.v2.doc.format} disagree")
    return drift


_DATE_LIKE = re.compile(r"\d{1,2} [A-Z][a-z]{2} \d{4}")
_PERCENT_LIKE = re.compile(r"-?\d+(?:\.\d+)?\s?%")
def _value_keys(text: str) -> set[str]:
    """Every typed value a piece of parser text states: money, percentage, date."""
    from synthetic_data.surfaces import (date_surface_key, money_surface_key,
                                         money_surfaces, percent_surface_key)

    keys = {money_surface_key(surface) for surface in money_surfaces(text)}
    keys |= {date_surface_key(match.group(0)) for match in _DATE_LIKE.finditer(text)}
    keys |= {percent_surface_key(match.group(0))
             for match in _PERCENT_LIKE.finditer(text)}
    return {key for key in keys if key}


def _agree(left: str, right: str) -> bool:
    """Do two formats state the same value for the same fact?

    Compared by *value*, not by text.  A PDF flattens a row into cell sentences, so
    its quote is a fragment of the TXT row; the values still have to agree.  An
    empty key set means the format's quote did not surface the value at all, which
    is a provenance limitation, not a disagreement.
    """
    left_keys = _value_keys(left)
    right_keys = _value_keys(right)
    if left_keys and right_keys:
        return bool(left_keys & right_keys)
    return surface_in_quote(left, right) or surface_in_quote(right, left)


def markdown_report(summary: dict[str, Any], rows: list[FormatRow]) -> str:
    lines = ["# Phase G — alignment and gold", ""]
    lines.append(f"Documents: {summary['documents']}  "
                 f"Facts: {summary['facts']}  Grounded: {summary['grounded']}  "
                 f"v1 valid: {summary['v1_valid']}  v2 errors: {summary['v2_errors']}")
    lines.append("")
    lines.append("| format | docs | facts | grounded | rate | v1 valid | currency stated "
                 "| currency not stated |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for fmt, entry in sorted(summary["formats"].items()):
        lines.append(f"| {fmt} | {entry['documents']} | {entry['facts']} | "
                     f"{entry['grounded']} | {entry['alignment_rate']:.3f} | "
                     f"{entry['v1_ok']} | {entry['currency_stated']} | "
                     f"{entry['currency_not_stated']} |")
    lines.append("")
    lines.append("| document | type | format | units | facts | pages | pageless | derived |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append(f"| {row.document_id} | {row.doc_type} | {row.format} | {row.units} | "
                     f"{row.facts_grounded}/{row.facts_total} | {row.pages} | "
                     f"{row.pageless} | {row.derived} |")
    lines.append("")
    lines.append("Alignment verdicts: " + ", ".join(
        f"{kind}={count}" for kind, count in sorted(summary["alignment_kinds"].items())))
    return "\n".join(lines) + "\n"
