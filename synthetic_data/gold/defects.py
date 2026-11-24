"""Defect detectors (§8.5) for a gold build.

Each detector answers one question about the evidence the alignment produced, and
each answer is a build failure rather than a warning:

``missing``      a stated fact has no acceptable alignment
``ambiguous``    a quote matches more than one parser sentence with different facts
``unsupported``  only a fuzzy (``partial``) alignment remains
``altered``      the aligned text does not carry the fact's value
``duplicated``   one fact lands twice on one parser sentence with different values
``extra``        a money surface in a fact-bearing span is not the fact's amount

The detectors read the *parser* quote.  A defect that only exists in the model is
not a defect in the corpus — it is a modelling question — so nothing here inspects
the intended surface except to compare it with what the parser actually returned.
"""
from __future__ import annotations

from dataclasses import dataclass

from synthetic_data.alignment import FactEvidence, surface_in_quote
from synthetic_data.model import IsoDate, Money, Percentage
from synthetic_data.surfaces import money_surface_key, money_surfaces

__all__ = ["Defect", "detect_defects", "typed_surfaces"]

_BAD_KINDS = {"missing": "missing", "ambiguous": "ambiguous", "partial": "unsupported"}


@dataclass(frozen=True)
class Defect:
    kind: str
    subject: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.kind}: {self.subject}: {self.detail}"


def typed_surfaces(value) -> list[str]:
    """Every surface that legitimately states a typed value."""
    if isinstance(value, Money):
        return [value.display(), f"{value.currency} {value.display()}"]
    if isinstance(value, Percentage):
        return [value.display()]
    if isinstance(value, IsoDate):
        return [value.value.isoformat(), value.value.strftime("%d %B %Y"),
                value.value.strftime("%-d %B %Y")]
    return []


def detect_defects(evidence: list[FactEvidence], typed: dict[str, object],
                   known_money: set[str] | None = None) -> list[Defect]:
    """Every alignment defect in one document's evidence."""
    defects: list[Defect] = []
    by_fact: dict[str, list[FactEvidence]] = {}
    for item in evidence:
        by_fact.setdefault(item.fact_id, []).append(item)
        for kind in item.kinds:
            if kind in _BAD_KINDS:
                defects.append(Defect(_BAD_KINDS[kind], item.statement_id,
                                      f"aligned as {kind}"))
        if not item.kinds and not item.quote:
            defects.append(Defect("missing", item.statement_id, "no alignment at all"))
        if item.problems:
            defects.append(Defect("altered", item.statement_id,
                                  "; ".join(item.problems)))
            continue
        value = typed.get(item.fact_id)
        quote = item.quote
        if value is None or not quote:
            continue
        if isinstance(value, Money):
            keys = {key for key in
                    (money_surface_key(candidate) for candidate in money_surfaces(quote))
                    if key is not None}
            expected = f"{value.currency}:{value.amount_minor}"
            if keys and expected not in keys:
                defects.append(Defect(
                    "altered", item.statement_id,
                    f"quote states {sorted(keys)} but the fact is {expected}"))
            # "extra" is about a single-value span that states a number the corpus
            # cannot account for.  A table row projection ("| a | b |") aggregates
            # columns by construction, so its neighbouring cells are context, not an
            # invented fact; only the row-level "altered" check applies there.
            limit = known_money if known_money is not None else {expected}
            structural = "|" in quote or item.node_id.startswith("tbl:")
            unexpected = (keys - set(limit)) if not structural else set()
            if unexpected:
                defects.append(Defect(
                    "extra", item.statement_id,
                    f"quote carries money no fact accounts for: {sorted(unexpected)}"))
        elif isinstance(value, Percentage):
            if not surface_in_quote(value.display(), quote):
                defects.append(Defect(
                    "altered", item.statement_id,
                    f"quote does not state {value.display()}"))
        elif isinstance(value, IsoDate):
            if not surface_in_quote(value.value.isoformat(), quote):
                defects.append(Defect(
                    "altered", item.statement_id,
                    f"quote does not state {value.value.isoformat()}"))

    # one fact, one parser sentence, two different quotes: the two statements
    # disagree about where (or what) the fact is
    for fact_id, items in by_fact.items():
        by_sentence: dict[str, set[str]] = {}
        for item in items:
            for sentence_id in item.sentence_ids:
                by_sentence.setdefault(sentence_id, set()).add(item.quote)
        for sentence_id, quotes in by_sentence.items():
            quotes = {quote for quote in quotes if quote}
            if len(quotes) > 1:
                defects.append(Defect(
                    "duplicated", fact_id,
                    f"two statements of one fact land on {sentence_id} with "
                    f"different quotes"))
    return defects
