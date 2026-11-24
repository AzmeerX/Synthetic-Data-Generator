"""Metrics and typed comparators for the benchmark (Phase I).

Every denominator is explicit: a metric whose denominator is zero is reported as
``None`` (not applicable) rather than as 0.0 or 1.0, because "nothing to score" and
"scored zero" are different facts and the report must not blur them.

Money comparison is currency-aware: an amount is only equal when the minor units and
the currency agree.  A right number in the wrong currency is a distinct outcome, so
it is counted as a *currency error*, not as a plain miss.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

__all__ = ["Tally", "normalise", "token_f1", "exact_match", "normalized_match",
           "parse_money", "find_money", "money_equal", "parse_percent",
           "percent_equal", "parse_date", "date_equal", "reconcile_derived", "mean"]

_MONEY = re.compile(r"^(?:(?P<cur>[A-Z]{3})\s*)?(?P<num>-?\d[\d,]*\.?\d*)$")
_PERCENT = re.compile(r"^(?P<num>-?\d[\d,]*\.?\d*)\s*%$")


@dataclass
class Tally:
    """Counts behind a precision/recall pair."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    extras: dict[str, int] = field(default_factory=dict)

    def hit(self) -> None:
        self.tp += 1

    def false_positive(self) -> None:
        self.fp += 1

    def miss(self) -> None:
        self.fn += 1

    def note(self, key: str) -> None:
        self.extras[key] = self.extras.get(key, 0) + 1

    def merge(self, other: "Tally") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        for key, value in other.extras.items():
            self.extras[key] = self.extras.get(key, 0) + value

    @property
    def precision(self) -> float | None:
        denominator = self.tp + self.fp
        return None if denominator == 0 else self.tp / denominator

    @property
    def recall(self) -> float | None:
        denominator = self.tp + self.fn
        return None if denominator == 0 else self.tp / denominator

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None:
            return None
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def as_dict(self) -> dict[str, float | int | None]:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn,
                "precision": self.precision, "recall": self.recall, "f1": self.f1,
                **{key: value for key, value in sorted(self.extras.items())}}


def mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return None if not present else sum(present) / len(present)


# ---------------------------------------------------------------------------
def normalise(text: str) -> str:
    """Casefolded, whitespace-collapsed, punctuation-trimmed comparison form."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = re.sub(r"\s+", " ", folded).strip()
    folded = folded.strip(" \t\n.,;:!?\"'()[]{}")
    return folded


def exact_match(predicted: str, gold: str) -> bool:
    return predicted.strip() == gold.strip()


def normalized_match(predicted: str, gold: str) -> bool:
    return normalise(predicted) == normalise(gold)


def token_f1(predicted: str, gold: str) -> float:
    predicted_tokens = normalise(predicted).split()
    gold_tokens = normalise(gold).split()
    if not predicted_tokens and not gold_tokens:
        return 1.0
    if not predicted_tokens or not gold_tokens:
        return 0.0
    common = {}
    for token in predicted_tokens:
        common[token] = common.get(token, 0) + 1
    overlap = 0
    for token in gold_tokens:
        if common.get(token, 0) > 0:
            common[token] -= 1
            overlap += 1
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
def parse_money(text: str) -> tuple[int, str | None] | None:
    """``"GBP 12,000,000.00"`` → ``(1200000000, "GBP")``; bare numbers keep None."""
    cleaned = unicodedata.normalize("NFKC", text).strip().replace("\u00a0", " ")
    match = _MONEY.match(cleaned.upper())
    if match is None:
        return None
    number = match.group("num").replace(",", "")
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None
    return int((amount * 100).to_integral_value()), match.group("cur")


_MONEY_IN_TEXT = re.compile(r"\b([A-Z]{3})\s?(\d[\d,]*\.\d{2})\b")


def find_money(text: str) -> tuple[int, str] | None:
    """The first typed amount inside a longer quote: 'advanced GBP 12,000.00 on ...'."""
    match = _MONEY_IN_TEXT.search(unicodedata.normalize("NFKC", text))
    if match is None:
        return None
    parsed = parse_money(f"{match.group(1)} {match.group(2)}")
    return parsed if parsed and parsed[1] else None


def money_equal(predicted_minor: int, predicted_currency: str | None,
                gold_minor: int, gold_currency: str | None) -> tuple[bool, str | None]:
    """``(equal, reason)``; the reason distinguishes a currency error from a miss."""
    if predicted_minor != gold_minor:
        return False, "amount"
    if (predicted_currency or "").upper() != (gold_currency or "").upper():
        return False, "currency"
    return True, None


def parse_percent(text: str) -> Decimal | None:
    cleaned = unicodedata.normalize("NFKC", text).strip().replace(" ", "")
    match = _PERCENT.match(cleaned)
    if match is None:
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    try:
        return Decimal(match.group("num").replace(",", ""))
    except InvalidOperation:
        return None


def percent_equal(predicted: str, gold: str) -> bool:
    left, right = parse_percent(predicted), parse_percent(gold)
    if left is None or right is None:
        return False
    return abs(left - right) <= Decimal("0.005")


def parse_date(text: str):
    import datetime

    cleaned = text.strip()
    try:
        return datetime.date.fromisoformat(cleaned)
    except ValueError:
        pass
    for pattern in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    return None


def date_equal(predicted: str, gold: str) -> bool:
    left, right = parse_date(predicted), parse_date(gold)
    return left is not None and left == right


def reconcile_derived(inputs: list[int], total: int, predicted_total: int,
                      tolerance_minor: int = 0) -> dict[str, object]:
    """Check a derived value against the values it is computed from.

    ``inputs`` and ``total`` are in minor units.  A prediction that reproduces the
    total but not the sum of its inputs is reported as unreconciled: the number can
    be right for the wrong reason.
    """
    computed = sum(inputs)
    agrees_with_inputs = abs(computed - total) <= tolerance_minor
    matches_prediction = abs(predicted_total - total) <= tolerance_minor
    return {"computed": computed, "total": total, "predicted": predicted_total,
            "agrees_with_inputs": agrees_with_inputs,
            "matches_prediction": matches_prediction,
            "ok": agrees_with_inputs and matches_prediction}
