"""Rendered value surfaces: presentation versus value.

A renderer writes money as ``£4,000,000``, ``GBP 4,000,000.00`` or
``4,000,000.00 pounds sterling`` depending on the document's chosen style.  Those
are three presentations of one value, and comparison must never confuse a
presentation with a different amount.  This module owns

* the currency tables, so generator and comparer agree on what a surface means;
* :func:`money_key` — the normalised identity of a typed value;
* :func:`money_surface_key` — the identity denoted by a *rendered* surface;
* :func:`money_surfaces` — every money surface inside a piece of text;
* :func:`surfaces_equivalent` — formatting-insensitive equality.

It depends only on the canonical model, so a renderer or an alignment step can use
it without importing the generator.
"""
from __future__ import annotations

import re

from synthetic_data.model import Money

__all__ = [
    "CURRENCY_SYMBOLS",
    "CURRENCY_NAMES",
    "money_key",
    "money_surface_key",
    "money_surfaces",
    "date_surface_key",
    "percent_surface_key",
    "surfaces_equivalent",
]

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}
CURRENCY_NAMES = {
    "GBP": "pounds sterling", "USD": "US dollars", "EUR": "euros",
    "JPY": "yen", "CHF": "Swiss francs",
}

_CURRENCY_BY_NAME = {name: code for code, name in CURRENCY_NAMES.items()}
_CURRENCY_BY_SYMBOL = {symbol: code for code, symbol in CURRENCY_SYMBOLS.items()}

#: symbol, ISO code, or amount-then-name — the three surfaces the renderer writes
_MONEY_SURFACE = re.compile(
    r"(?P<sym>[\u00a3$\u20ac\u00a5])\s?(?P<a1>\d[\d,]*(?:\.\d{1,2})?)"
    r"|(?P<code>GBP|USD|EUR|JPY|CHF)\s?(?P<a2>\d[\d,]*(?:\.\d{1,2})?)"
    r"|(?P<a3>\d[\d,]*(?:\.\d{1,2})?)\s(?P<name>pounds sterling|US dollars|"
    r"euros|yen|Swiss francs)")


def money_key(money: Money) -> str:
    """The normalised identity of a typed value: ``GBP:400000000`` (minor units)."""
    return f"{money.currency}:{money.amount_minor}"


def _minor_units(amount: str) -> int:
    whole, _, pence = amount.partition(".")
    pence = (pence + "00")[:2] if pence else "00"
    return int(whole.replace(",", "")) * 100 + int(pence)


def money_surface_key(text: str) -> str | None:
    """Normalise one money surface to :func:`money_key` form, or ``None``.

    ``None`` means "not a single money surface", so arbitrary prose is never
    coerced into a value.
    """
    match = _MONEY_SURFACE.fullmatch(text.strip())
    if match is None:
        return None
    if match.group("sym"):
        return f"{_CURRENCY_BY_SYMBOL[match.group('sym')]}:" \
               f"{_minor_units(match.group('a1'))}"
    if match.group("code"):
        return f"{match.group('code')}:{_minor_units(match.group('a2'))}"
    return f"{_CURRENCY_BY_NAME[match.group('name')]}:" \
           f"{_minor_units(match.group('a3'))}"


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_MONTH_NUMBER = {name.lower(): index for index, name in enumerate(_MONTHS, start=1)}
_MONTH_NUMBER.update({name[:3].lower(): index
                      for index, name in enumerate(_MONTHS, start=1)})

_DATE_PATTERNS = (
    re.compile(r"(?P<day>\d{1,2})(?:st|nd|rd|th)? (?P<month>[A-Za-z]{3,9}) (?P<year>\d{4})"),
    re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"),
    re.compile(r"(?P<month>[A-Za-z]{3,9}) (?P<day>\d{1,2}), (?P<year>\d{4})"),
)
_PERCENT_SURFACE = re.compile(
    r"(?P<pct>-?\d+(?:\.\d+)?)\s?(?:%|per cent)"
    r"|(?P<bp>-?\d+) basis points")


def date_surface_key(text: str) -> str | None:
    """Normalise a rendered date to ISO ``YYYY-MM-DD``, or ``None``."""
    candidate = " ".join(text.strip().split())
    for pattern in _DATE_PATTERNS:
        match = pattern.fullmatch(candidate)
        if match is None:
            continue
        parts = match.groupdict()
        month = parts.get("month") or ""
        if month.isdigit():
            month_number = int(month)
        else:
            month_number = _MONTH_NUMBER.get(month.lower())
        if not month_number:
            return None
        return f"{int(parts['year']):04d}-{month_number:02d}-{int(parts['day']):02d}"
    return None


def percent_surface_key(text: str) -> str | None:
    """Normalise a rendered percentage to basis points, or ``None``."""
    match = _PERCENT_SURFACE.fullmatch(" ".join(text.strip().split()))
    if match is None:
        return None
    if match.group("bp") is not None:
        return f"{int(match.group('bp'))}bp"
    return f"{round(float(match.group('pct')) * 100)}bp"


def money_surfaces(text: str) -> list[str]:
    """Every money surface inside ``text``, in reading order."""
    return [match.group(0) for match in _MONEY_SURFACE.finditer(text)]


def surfaces_equivalent(left: str, right: str) -> bool:
    """True when two surfaces denote the same value, formatting aside.

    Money is compared by value; non-money text by normalised equality.  A money
    surface never equals a non-money surface, so "£4,000,000" != "4,000,000".
    """
    for key in (money_surface_key, date_surface_key, percent_surface_key):
        left_key = key(left)
        right_key = key(right)
        if left_key is not None and right_key is not None:
            return left_key == right_key
        if left_key is not None or right_key is not None:
            return False
    return " ".join(left.split()) == " ".join(right.split())
