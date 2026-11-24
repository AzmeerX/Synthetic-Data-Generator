"""Typed, format-free values (architecture §4.1).

Money, percentages and dates are objects, never strings.  Renderers format
them; nothing re-parses a rendered string to recover a number.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

__all__ = [
    "CANONICAL_ID_PATTERN",
    "PARSER_SENTENCE_PATTERN",
    "CanonicalId",
    "ParserSentenceId",
    "Money",
    "Percentage",
    "IsoDate",
    "EntityRef",
    "DocumentRef",
    "CellValue",
    "Emphasis",
    "Register",
    "FactKind",
    "FactStatus",
    "ConditionKind",
    "ConditionSatisfied",
    "AlignmentKind",
    "ContradictionDimension",
]

# Canonical ids are lowercase family + hierarchical readable scope + hash tag.
# Allowed inside the scope: digits, letters, ':', '.', '/', '_', '-'.
CANONICAL_ID_PATTERN = r"^[a-z][a-z0-9_]*:[A-Za-z0-9][A-Za-z0-9:._/\-]*$"
# Parser sentence ids are artifact-scoped, not canonical: 's19' or 'sen_01H...'.
PARSER_SENTENCE_PATTERN = r"^(?:s\d+|sen_[A-Za-z0-9]+)$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

CanonicalId = Annotated[str, Field(min_length=3, max_length=200, pattern=CANONICAL_ID_PATTERN)]
ParserSentenceId = Annotated[str, Field(min_length=1, max_length=64)]

Emphasis = Literal["none", "bold", "italic"]
Register = Literal["formal", "neutral", "terse"]
FactKind = Literal["edge", "proposition"]
FactStatus = Literal[
    "completed", "proposed", "alleged", "held", "denied", "stated",
    "approved", "rejected", "deferred", "found", "signed", "waived",
]
ConditionKind = Literal["provided_that", "unless", "subject_to", "notwithstanding"]
ConditionSatisfied = Literal["yes", "no", "unknown"]
ContradictionDimension = Literal["amount", "date", "status", "relationship"]
AlignmentKind = Literal[
    "exact", "normalised", "split", "merged", "partial", "ambiguous", "missing",
]

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Money(_Value):
    """Exact monetary amount in minor units.  ``Money(400_000_000, "GBP")`` is £4,000,000."""

    amount_minor: StrictInt
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        if not _CURRENCY_RE.match(value):
            raise ValueError("currency must be a 3-letter ISO 4217 code in upper case")
        return value

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_minor) / Decimal(100)

    def display(self, *, symbol: str | None = None) -> str:
        """Renderer-independent display string; grouping is presentation, not value."""
        whole = f"{abs(self.amount_minor) // 100:,}"
        pence = abs(self.amount_minor) % 100
        body = f"{whole}.{pence:02d}"
        prefix = symbol if symbol is not None else f"{self.currency} "
        return f"-{prefix}{body}" if self.amount_minor < 0 else f"{prefix}{body}"


class Percentage(_Value):
    """Percentage in basis points: ``Percentage(350)`` is 3.50%."""

    basis_points: StrictInt = Field(ge=-1_000_000, le=1_000_000)

    @property
    def fraction(self) -> Decimal:
        return Decimal(self.basis_points) / Decimal(10_000)

    def display(self) -> str:
        return f"{Decimal(self.basis_points) / Decimal(100):.2f}%"


class IsoDate(_Value):
    """A date with explicit precision; inconsistent precision is rejected, not repaired."""

    value: dt.date
    precision: Literal["day", "month", "year"] = "day"

    @field_validator("value", mode="before")
    @classmethod
    def _parse(cls, value):
        if isinstance(value, str):
            try:
                return dt.date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"not an ISO date: {value!r}") from exc
        return value

    @field_validator("precision")
    @classmethod
    def _coherent(cls, precision: str, info):
        value = info.data.get("value")
        if value is None:
            return precision
        if precision == "year" and (value.month, value.day) != (1, 1):
            raise ValueError("year precision requires 1 January; use day or month precision instead")
        if precision == "month" and value.day != 1:
            raise ValueError("month precision requires day 1; use day precision instead")
        return precision

    def iso(self) -> str:
        return self.value.isoformat()


class EntityRef(_Value):
    entity_id: CanonicalId


class DocumentRef(_Value):
    document_id: CanonicalId
    version: StrictInt | None = Field(default=None, ge=1)


# Union used by table cells and inline runs.  StrictInt keeps ``True`` out.
CellValue = Money | Percentage | IsoDate | StrictInt
