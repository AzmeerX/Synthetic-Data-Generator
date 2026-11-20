"""U3 — Time Anchor: time expressions -> normalized time references (S3). Rules, no model.

Purpose: turn every time expression of a document into a reference to *time
itself* (P1 §6): kind, ISO 8601 value(s) at the stated precision, granularity,
anchor and the rule that fired. Precision over recall: what cannot be anchored
is stored as ``RELATIVE`` + ``UNRESOLVED`` with null values — never guessed.

Contract: :func:`run_document` ``(ctx, doc_id) -> dict`` (counts). Inputs:

* the document's ``OPEN`` time mentions from U2 (labels ``date``, ``time period``,
  ``duration``);
* the document date (``documents.doc_date`` + ``notes["doc_date_span"]``);
* a deterministic regex scan (:func:`find_time_expressions`) for time
  expressions no time mention covers, so a date GLiNER missed is not lost
  (those mentions are created with ``source="rules"``).

Each expression -> one ``time_refs`` row (span = the mention's span) and its
mention -> ``RESOLVED`` with ``time_id``. The document date gets its own time
ref (``INSTANT``, ``ABSOLUTE``, rule ``doc_date``) when a span for it exists.

Normalization rules (:func:`parse_time` then :func:`resolve`):

1. full dates -> ``INSTANT``, ``ABSOLUTE``, granularity day (or hour/minute);
2. dates without a year -> rolling base: the last date with a stated year
   earlier in the document (``ROLLING_BASE``), else the document date
   (``DOC_DATE``); confidence 0.7;
3. relative expressions ("three weeks later", "the following year") -> the
   nearest anchored date in the same or the previous sentence (``EVENT``,
   ``anchor_time_id`` set), else ``RELATIVE`` + ``UNRESOLVED``; deictic ones
   ("today", "two years ago", "next year") anchor on the document date;
4. periods: quarters, fiscal years (``ontology.fiscal_year_start_month``; FY is
   named by the year it ends in), "the 2024 financial year"; months stay months
   (``2024-03``), years stay years (``2024``);
5. ranges "from … to …", "between … and …", "June–August 2023" -> ``INTERVAL``;
   durations -> ``DURATION`` (ISO 8601 duration, e.g. ``P3Y``); "every quarter"
   -> ``RECURRING`` (``R/P3M``).

``dateparser`` is a last-resort fallback for complete dates only (absolute
parser, strict, never relative to "now").

Stores written: ``time_refs`` (S3), ``mentions`` (status/time_id; new
``source="rules"`` rows), ``spans`` (S1), ``stage_metrics``; change log (L4) via Tx.
Token caps: none (no model). Spec: P1 §3 U3, §6.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from synth import states as S
from synth.ids import new_id
from synth.modules.common import DocView, Stopwatch, StageContext, record_stage_metrics, write_span
from synth.ontology import TIME_LABELS
from synth.store.repo import Tx

log = logging.getLogger("synth.u3")

MODULE = "u03_time"

CONF_ABSOLUTE = 0.95
CONF_NUMERIC = 0.85
CONF_PERIOD = 0.9
CONF_ROLLING = 0.7
CONF_EVENT = 0.6
CONF_DEICTIC = 0.7
CONF_UNRESOLVED = 0.0

# ------------------------------------------------------------------ vocabulary
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_SMALL = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen"]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_WORD_VALUE = {w: i + 1 for i, w in enumerate(_SMALL)}
_WORD_VALUE.update({w: 20 + 10 * i for i, w in enumerate(_TENS)})
_ORDINAL_Q = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
              "fourth": 4, "4th": 4, "final": 4, "last": 4}
_UNIT_GRAN = {"day": "day", "week": "week", "fortnight": "week", "month": "month",
              "quarter": "quarter", "year": "year"}
_UNIT_ISO = {"day": (1, "D"), "week": (1, "W"), "fortnight": (2, "W"), "month": (1, "M"),
             "quarter": (3, "M"), "year": (1, "Y")}
_ADVERB_RECURRING = {"daily": ("P1D", "day"), "weekly": ("P1W", "week"),
                     "fortnightly": ("P2W", "week"), "monthly": ("P1M", "month"),
                     "quarterly": ("P3M", "quarter"), "annually": ("P1Y", "year"),
                     "yearly": ("P1Y", "year"), "semi-annually": ("P6M", "month"),
                     "half-yearly": ("P6M", "month")}
_GRAN_ORDER = ["minute", "hour", "day", "week", "month", "quarter", "year"]

# ------------------------------------------------------------------ regex pieces
_SMALL_ALT = "|".join(sorted(_SMALL, key=len, reverse=True))
NUM = (rf"(?:\d+(?:\.\d+)?|(?:{'|'.join(_TENS)})(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine)"
       rf"(?![a-z]))?|{_SMALL_ALT})")
UNIT = r"(?:day|week|fortnight|month|quarter|year)"
MONTH_BARE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
              r"|sep(?:t(?:ember)?|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?![a-z])")
MONTH = rf"{MONTH_BARE}\.?"
DAY = r"(?:[12]\d|3[01]|0?[1-9])(?:st|nd|rd|th)?(?![\d])"
YEAR = r"(?:1[89]\d\d|2[01]\d\d)(?!\d)"
WEEKDAY = r"(?:mon|tue|tues|wed|wednes|thu|thur|thurs|fri|sat|satur|sun)(?:day)?\.?,?\s+"
TOD = (r"(?:,?\s*(?:at\s+)?(?:[01]?\d|2[0-3])(?:[:.][0-5]\d\s*(?:[ap]\.?m\.?)?|\s*[ap]\.?m\.?)"
       r"(?![a-z\d]))")
FULL_DMY = rf"{DAY}(?:\s+day)?\s+(?:of\s+)?{MONTH},?\s+{YEAR}"
FULL_MDY = rf"{MONTH}\s+{DAY},?\s+{YEAR}"
ISO_DATE = r"(?:1[89]|2[01])\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?:T(?:[01]\d|2[0-3]):[0-5]\d)?"
NUMERIC = r"\d{1,2}[/.]\d{1,2}[/.](?:1[89]|2[01])\d\d(?!\d)"
DM = rf"{DAY}\s+(?:of\s+)?{MONTH}"
MD = rf"{MONTH_BARE}[^\S\n]+{DAY}"
MY = rf"{MONTH},?\s+{YEAR}"
END = rf"(?:{FULL_DMY}|{FULL_MDY}|{MY}|{DM}|{MD}|{MONTH}|{YEAR}|{DAY})"
FYNUM = r"(?:FY|F/Y)\s?'?(?:\d{4}|\d{2})(?:\s*[/-]\s*(?:\d{4}|\d{2}))?(?!\d)"
YSPLIT = rf"{YEAR}(?:\s*[/-]\s*(?:\d{{4}}|\d{{2}})(?!\d))?"
AFTER_END = r"(?=\s*[,.;:)]|\s*$)"

_F = re.IGNORECASE
#: (pattern, label for the mention) — order is irrelevant: longest match wins.
_SCAN: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\bbetween\s+{END}\s+and\s+{END}(?![\w])", _F), "time period"),
    (re.compile(rf"\bfrom\s+{END}\s+(?:to|until|till|through)\s+{END}(?![\w])", _F), "time period"),
    (re.compile(rf"(?<![\w-]){END}\s*[–—-]\s*{END}(?![\w-])", _F), "time period"),
    (re.compile(rf"\b{FYNUM}", _F), "time period"),
    (re.compile(rf"\b(?:the\s+)?{YSPLIT}\s+(?:financial|fiscal)\s+year\b", _F), "time period"),
    (re.compile(rf"\b(?:the\s+)?(?:financial|fiscal)\s+year\s+(?:(?:ended|ending|to)\s+(?:{FULL_DMY}|{FULL_MDY})"
                rf"|{YSPLIT})", _F), "time period"),
    (re.compile(rf"\bQ[1-4]\s*(?:of\s+)?(?:{FYNUM}|{YEAR})", _F), "time period"),
    (re.compile(rf"\b{FYNUM}\s*Q[1-4]\b", _F), "time period"),
    (re.compile(rf"\b(?:the\s+)?(?:first|second|third|fourth|1st|2nd|3rd|4th|final|last)\s+quarter\s+"
                rf"(?:of\s+)?(?:the\s+)?(?:{FYNUM}|{YEAR}|(?:financial|fiscal)\s+year\s+{YEAR})", _F), "time period"),
    (re.compile(r"\bQ[1-4]\b"), "time period"),
    (re.compile(rf"(?<![\w/.-])(?:{WEEKDAY})?(?:{FULL_DMY}|{FULL_MDY}|{ISO_DATE}|{NUMERIC})(?:{TOD})?", _F), "date"),
    (re.compile(rf"(?<![\w-]){MY}", _F), "time period"),
    (re.compile(rf"(?<![\w/.-]){DM}", _F), "date"),
    (re.compile(rf"\b{MD}(?!\s*,?\s*\d)", _F), "date"),
    (re.compile(rf"\b(?:{NUM}|an?)(?:\s+|-){UNIT}s?\s+(?:later|earlier|thereafter|afterwards|beforehand|previously"
                rf"|ago)\b", _F), "date"),
    (re.compile(rf"\b(?:{NUM}|an?)\s+{UNIT}s?\s+(?:after|before|on){AFTER_END}", _F), "date"),
    (re.compile(rf"\bthe\s+(?:following|next|previous|preceding|prior|subsequent|succeeding|same)\s+{UNIT}\b", _F),
     "date"),
    (re.compile(rf"\bthe\s+{UNIT}\s+(?:after|before){AFTER_END}", _F), "date"),
    (re.compile(r"(?<!the\s)\b(?:next|last|this)\s+(?:week|month|quarter|year)\b", _F), "date"),
    (re.compile(r"\b(?:the\s+)?(?:said|aforementioned|aforesaid|relevant)\s+date\b", _F), "date"),
    (re.compile(r"\b(?:today|yesterday|tomorrow)\b", _F), "date"),
    (re.compile(rf"(?<![\d.,])\b{NUM}(?:\s+|-){UNIT}s?\b(?:-long)?", _F), "duration"),
    (re.compile(rf"\bevery\s+(?:other\s+|{NUM}\s+)?{UNIT}s?\b|\beach\s+(?:day|week|month|quarter|year)\b"
                r"|\b(?:daily|weekly|fortnightly|monthly|quarterly|annually|yearly|semi-annually|half-yearly)\b", _F),
     "time period"),
]
_YEAR_CTX = re.compile(rf"\b(?:in|during|since|until|till|by|before|after|from|through|throughout|for|of|to)\s+"
                       rf"({YEAR})\b(?!\s*(?:million|billion|bn|m\b|%|per\b|shares))", _F)
_TIME_TOKEN = re.compile(rf"\d|{MONTH}|\b(?:{UNIT}s?|fy|q[1-4]|date|period|annual\w*|today|yesterday|tomorrow"
                         r"|later|earlier|ago|since|until|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", _F)


# ------------------------------------------------------------------ parsed forms
@dataclass(frozen=True)
class Point:
    """A calendar point at some precision; ``year`` may be missing (rolling base)."""

    year: int | None
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None

    @property
    def granularity(self) -> str:
        if self.minute is not None:
            return "minute"
        if self.hour is not None:
            return "hour"
        if self.day is not None:
            return "day"
        if self.month is not None:
            return "month"
        return "year"

    def iso(self) -> str:
        g = self.granularity
        if g == "year":
            return f"{self.year:04d}"
        if g == "month":
            return f"{self.year:04d}-{self.month:02d}"
        base = f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if g == "hour":
            return f"{base}T{self.hour:02d}"
        if g == "minute":
            return f"{base}T{self.hour:02d}:{self.minute:02d}"
        return base

    def first_day(self) -> _dt.date:
        return _dt.date(self.year, self.month or 1, self.day or 1)

    def last_day(self) -> _dt.date:
        month = self.month or 12
        return _dt.date(self.year, month, self.day or calendar.monthrange(self.year, month)[1])

    def valid(self) -> bool:
        if self.year is None:
            return False
        try:
            self.first_day()
        except ValueError:
            return False
        return True


@dataclass(frozen=True)
class Parsed:
    """What the surface says, before anchoring."""

    kind: str                       # INSTANT | INTERVAL | PERIOD | DURATION | RECURRING | RELATIVE
    rule: str
    start: Point | None = None
    end: Point | None = None
    iso: str | None = None          # DURATION / RECURRING value
    granularity: str | None = None
    confidence: float = CONF_ABSOLUTE
    rel: str | None = None          # offset | step | deictic | anaphoric (RELATIVE)
    n: float = 0.0                  # signed offset / step count
    unit: str | None = None
    period: tuple | None = None     # ("fy", end_year) | ("fy_end", date) | ("q", year|None, q, fiscal)


@dataclass(frozen=True)
class Resolved:
    kind: str
    value_start: str | None
    value_end: str | None
    granularity: str | None
    anchor_type: str
    anchor_time_id: str | None
    confidence: float
    rule: str
    point: Point | None = None      # set for INSTANT points usable as anchors
    stated_year: bool = False       # carries an explicit year (rolling base candidate)


# ------------------------------------------------------------------ helpers
def _num(text: str) -> float | None:
    text = text.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if text in ("a", "an"):
        return 1.0
    total = 0
    for part in re.split(r"[\s-]+", text):
        if part not in _WORD_VALUE:
            return None
        total += _WORD_VALUE[part]
    return float(total)


def _month(text: str) -> int:
    return _MONTHS[text.strip(". ").lower()[:3]]


def _day(text: str) -> int:
    return int(re.match(r"\d+", text).group(0))


def _iso_number(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def _expand_year(text: str, ref: int | None = None) -> int:
    value = int(text)
    if len(text) == 2:
        century = (ref // 100 * 100) if ref else 2000
        value = century + value
        if ref and value < ref:
            value += 100
    return value


_PREP = re.compile(r"^(?:the|on|by|until|till|til|up\s+to|from|since|as\s+at|as\s+of|before|after|in|during|of|dated"
                   r"|effective|commencing|beginning|throughout|for|at|to|no\s+later\s+than)\s+", _F)
_WEEKDAY_RE = re.compile(rf"^{WEEKDAY}", _F)


def _clean(surface: str) -> str:
    s = " ".join((surface or "").split())
    s = s.replace("–", "-").replace("—", "-")
    s = s.strip(" ,;:()[]\"'")
    s = re.sub(r"\.$", "", s) if not re.search(r"[ap]\.m\.$", s, _F) else s
    return s


def _strip_prep(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = _PREP.sub("", s)
        s = _WEEKDAY_RE.sub("", s)
    return s


def _tod(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    m = re.search(r"([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s*([ap])?\.?m?\.?", text, _F)
    if not m:
        return None, None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else None
    ampm = (m.group(3) or "").lower()
    if ampm == "p" and hour < 12:
        hour += 12
    if ampm == "a" and hour == 12:
        hour = 0
    return hour, minute


# ------------------------------------------------------------------ point parsing
def parse_point(s: str, partial: bool = False) -> tuple[Point, str] | None:
    """A single calendar point. ``partial`` also accepts a bare month or day (range ends)."""
    s = _strip_prep(_clean(s))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?", s)
    if m:
        p = Point(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                  int(m.group(4)) if m.group(4) else None, int(m.group(5)) if m.group(5) else None)
        return (p, "iso_date") if p.valid() else None
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return Point(int(m.group(1)), int(m.group(2))), "iso_month"
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        day, month, rule = (b, a, "numeric_mdy") if a <= 12 < b else (a, b, "numeric_dmy")
        p = Point(y, month, day)
        return (p, rule) if month <= 12 and p.valid() else None
    m = re.fullmatch(rf"({DAY})(?:\s+day)?\s+(?:of\s+)?({MONTH}),?\s+({YEAR})({TOD})?", s, _F)
    if m:
        hour, minute = _tod(m.group(4))
        p = Point(int(m.group(3)), _month(m.group(2)), _day(m.group(1)), hour, minute)
        return (p, "full_date") if p.valid() else None
    m = re.fullmatch(rf"({MONTH})\s+({DAY}),?\s+({YEAR})({TOD})?", s, _F)
    if m:
        hour, minute = _tod(m.group(4))
        p = Point(int(m.group(3)), _month(m.group(1)), _day(m.group(2)), hour, minute)
        return (p, "full_date") if p.valid() else None
    m = re.fullmatch(rf"({MONTH}),?\s+({YEAR})", s, _F)
    if m:
        return Point(int(m.group(2)), _month(m.group(1))), "month_year"
    m = re.fullmatch(rf"({DAY})\s+(?:of\s+)?({MONTH})", s, _F) or None
    if m:
        return Point(None, _month(m.group(2)), _day(m.group(1))), "day_month"
    m = re.fullmatch(rf"({MONTH})\s+({DAY})", s, _F)
    if m:
        return Point(None, _month(m.group(1)), _day(m.group(2))), "day_month"
    m = re.fullmatch(rf"({YEAR})", s)
    if m:
        return Point(int(m.group(1))), "year"
    if partial:
        m = re.fullmatch(rf"({MONTH})", s, _F)
        if m:
            return Point(None, _month(m.group(1))), "month"
        m = re.fullmatch(rf"({DAY})", s, _F)
        if m:
            return Point(None, None, _day(m.group(1))), "day"
    return None


# ------------------------------------------------------------------ expression parsing
def _p_recurring(s: str) -> Parsed | None:
    low = s.lower()
    if low in _ADVERB_RECURRING:
        iso, gran = _ADVERB_RECURRING[low]
        return Parsed("RECURRING", "recurring", iso=f"R/{iso}", granularity=gran, confidence=0.85)
    m = re.fullmatch(rf"(?:every|each)\s+(?:(other)\s+|({NUM})\s+)?({UNIT})s?", s, _F)
    if not m:
        return None
    n = 2.0 if m.group(1) else (_num(m.group(2)) if m.group(2) else 1.0)
    if n is None:
        return None
    unit = m.group(3).lower()
    mult, letter = _UNIT_ISO[unit]
    return Parsed("RECURRING", "recurring", iso=f"R/P{_iso_number(n * mult)}{letter}",
                  granularity=_UNIT_GRAN[unit], confidence=0.85)


def _p_relative(s: str) -> Parsed | None:
    m = re.fullmatch(rf"({NUM}|an?)(?:\s+|-)({UNIT})s?\s+(later|earlier|thereafter|afterwards|beforehand|previously"
                     r"|ago|after|before|on)", s, _F)
    if m:
        n = _num(m.group(1))
        if n is None:
            return None
        word = m.group(3).lower()
        sign = -1 if word in ("earlier", "beforehand", "previously", "before", "ago") else 1
        rel = "deictic" if word == "ago" else "offset"
        unit = m.group(2).lower()
        return Parsed("RELATIVE", "relative_ago" if rel == "deictic" else "relative_offset", rel=rel,
                      n=sign * n, unit=unit, granularity=_UNIT_GRAN[unit], confidence=CONF_EVENT)
    m = re.fullmatch(rf"the\s+(following|next|previous|preceding|prior|subsequent|succeeding|same)\s+({UNIT})", s, _F)
    if m:
        word = m.group(1).lower()
        step = 0 if word == "same" else (-1 if word in ("previous", "preceding", "prior") else 1)
        unit = m.group(2).lower()
        return Parsed("RELATIVE", "relative_step", rel="step", n=step, unit=unit,
                      granularity=_UNIT_GRAN[unit], confidence=CONF_EVENT)
    m = re.fullmatch(rf"the\s+({UNIT})\s+(after|before)", s, _F)
    if m:
        unit = m.group(1).lower()
        return Parsed("RELATIVE", "relative_step", rel="step", n=1 if m.group(2).lower() == "after" else -1,
                      unit=unit, granularity=_UNIT_GRAN[unit], confidence=CONF_EVENT)
    m = re.fullmatch(r"(next|last|this)\s+(week|month|quarter|year)", s, _F)
    if m:
        word = m.group(1).lower()
        unit = m.group(2).lower()
        return Parsed("RELATIVE", "deictic", rel="deictic_step", n={"next": 1, "last": -1, "this": 0}[word],
                      unit=unit, granularity=_UNIT_GRAN[unit], confidence=CONF_DEICTIC)
    m = re.fullmatch(r"(today|yesterday|tomorrow)", s, _F)
    if m:
        return Parsed("RELATIVE", "deictic", rel="deictic_step",
                      n={"today": 0, "yesterday": -1, "tomorrow": 1}[m.group(1).lower()],
                      unit="day", granularity="day", confidence=CONF_DEICTIC)
    if re.fullmatch(r"(?:the\s+)?(?:said|aforementioned|aforesaid|relevant|same)\s+date", s, _F):
        return Parsed("RELATIVE", "anaphoric_date", rel="anaphoric", confidence=CONF_UNRESOLVED)
    return None


def _p_duration(s: str) -> Parsed | None:
    m = re.fullmatch(rf"(?:(?:a|the)\s+(?:period|term)\s+of\s+)?({NUM}|an?)(?:\s+|-)({UNIT})s?(?:-long)?", s, _F)
    if not m:
        return None
    n = _num(m.group(1))
    if n is None:
        return None
    unit = m.group(2).lower()
    mult, letter = _UNIT_ISO[unit]
    return Parsed("DURATION", "duration", iso=f"P{_iso_number(n * mult)}{letter}",
                  granularity=_UNIT_GRAN[unit], confidence=CONF_PERIOD)


def _fy_end_year(first: str, second: str | None) -> int:
    if second:
        y1 = _expand_year(first) if len(first) == 4 else _expand_year(first)
        return _expand_year(second, y1) if len(second) == 2 else int(second)
    return _expand_year(first)


def _p_fiscal(s: str) -> Parsed | None:
    fy = r"(?:FY|F/Y)\s?'?(\d{4}|\d{2})(?:\s*[/-]\s*(\d{4}|\d{2}))?"
    m = re.fullmatch(rf"(?:the\s+)?{fy}", s, _F)
    if not m:
        m = re.fullmatch(r"(?:the\s+)?(\d{4})(?:\s*[/-]\s*(\d{4}|\d{2}))?\s+(?:financial|fiscal)\s+year", s, _F)
    if not m:
        m = re.fullmatch(r"(?:the\s+)?(?:financial|fiscal)\s+year\s+(\d{4})(?:\s*[/-]\s*(\d{4}|\d{2}))?", s, _F)
    if m:
        return Parsed("PERIOD", "fiscal_year", period=("fy", _fy_end_year(m.group(1), m.group(2))),
                      granularity="year", confidence=CONF_PERIOD)
    m = re.fullmatch(r"(?:the\s+)?(?:financial|fiscal)\s+year\s+(?:ended|ending|to)\s+(.+)", s, _F)
    if m:
        hit = parse_point(m.group(1))
        if hit and hit[0].granularity == "day":
            return Parsed("PERIOD", "fiscal_year_ending", period=("fy_end", hit[0].first_day()),
                          granularity="year", confidence=CONF_PERIOD)
    return None


def _p_quarter(s: str) -> Parsed | None:
    fy = r"(?:FY|F/Y)\s?'?(\d{4}|\d{2})"
    m = re.fullmatch(rf"Q([1-4])\s*(?:of\s+)?(?:the\s+)?{fy}", s, _F) or re.fullmatch(rf"{fy}\s*Q([1-4])", s, _F)
    if m:
        q, y = (m.group(1), m.group(2)) if s.upper().startswith("Q") else (m.group(2), m.group(1))
        return Parsed("PERIOD", "fiscal_quarter", period=("q", _expand_year(y), int(q), True),
                      granularity="quarter", confidence=CONF_PERIOD)
    m = re.fullmatch(r"Q([1-4])\s*(?:of\s+)?(\d{4})", s, _F)
    if m:
        return Parsed("PERIOD", "quarter", period=("q", int(m.group(2)), int(m.group(1)), False),
                      granularity="quarter", confidence=CONF_PERIOD)
    m = re.fullmatch(r"(?:the\s+)?(first|second|third|fourth|1st|2nd|3rd|4th|final|last)\s+quarter\s+(?:of\s+)?"
                     rf"(?:the\s+)?(?:{fy}|(\d{{4}})|(?:financial|fiscal)\s+year\s+(\d{{4}}))", s, _F)
    if m:
        q = _ORDINAL_Q[m.group(1).lower()]
        if m.group(2) or m.group(4):
            year = _expand_year(m.group(2) or m.group(4))
            return Parsed("PERIOD", "fiscal_quarter", period=("q", year, q, True),
                          granularity="quarter", confidence=CONF_PERIOD)
        return Parsed("PERIOD", "quarter", period=("q", int(m.group(3)), q, False),
                      granularity="quarter", confidence=CONF_PERIOD)
    m = re.fullmatch(r"Q([1-4])", s, _F)
    if m:
        return Parsed("PERIOD", "quarter", period=("q", None, int(m.group(1)), False),
                      granularity="quarter", confidence=CONF_ROLLING)
    return None


_CONNECTOR = re.compile(r"\s+(?:and|to|until|till|through)\s+|\s*-\s*", _F)


def _p_range(s: str) -> Parsed | None:
    body = s
    lead = re.match(r"(between|from)\s+", body, _F)
    if lead:
        body = body[lead.end():]
    for m in _CONNECTOR.finditer(body):
        word = m.group(0).strip().lower()
        if lead and lead.group(1).lower() == "between" and word != "and":
            continue
        if not lead and word in ("and", "until", "till", "through"):
            continue
        left, right = body[:m.start()], body[m.end():]
        b = parse_point(right, partial=False)
        a = parse_point(left, partial=True)
        if not a or not b:
            continue
        pa, pb = a[0], b[0]
        if pa.year is None and pa.month is None and pa.day is not None:      # "1-3 March 2024"
            if pb.month is None or pb.day is None:
                continue
            pa = Point(pb.year, pb.month, pa.day)
        elif pa.year is None and pb.year is not None:                        # "June and August 2023"
            pa = replace(pa, year=pb.year)
            if pa.valid() and pb.valid() and pa.first_day() > pb.last_day():
                pa = replace(pa, year=pb.year - 1)
        explicit = b[0].year is not None
        return Parsed("INTERVAL", "range", start=pa, end=pb,
                      granularity=_finer(pa.granularity, pb.granularity),
                      confidence=CONF_ABSOLUTE if explicit else CONF_ROLLING)
    return None


def _p_point(s: str) -> Parsed | None:
    hit = parse_point(s)
    if not hit:
        return None
    point, rule = hit
    conf = CONF_NUMERIC if rule.startswith("numeric") else CONF_ABSOLUTE
    return Parsed("INSTANT", rule, start=point, granularity=point.granularity, confidence=conf)


_DATEPARSER_SETTINGS = {"STRICT_PARSING": True, "REQUIRE_PARTS": ["day", "month", "year"],
                        "DATE_ORDER": "DMY", "PARSERS": ["custom-formats", "absolute-time"]}


def _p_dateparser(s: str) -> Parsed | None:
    """Last resort for complete dates in unusual shapes (absolute parser only)."""
    if not re.search(r"\d", s) or not re.search(r"(?:1[89]|2[01])\d\d", s):
        return None
    try:
        import dateparser
    except ImportError:  # pragma: no cover - dateparser is a default dependency
        return None
    try:
        value = dateparser.parse(_strip_prep(s), languages=["en"], settings=_DATEPARSER_SETTINGS)
    except Exception:  # dateparser raises assorted errors on odd input
        return None
    if value is None:
        return None
    return Parsed("INSTANT", "dateparser", start=Point(value.year, value.month, value.day),
                  granularity="day", confidence=0.8)


def parse_time(surface: str) -> Parsed | None:
    """Classify and parse one time expression (no context). ``None`` = not understood."""
    s = _clean(surface)
    if not s:
        return None
    for fn in (_p_recurring, _p_relative, _p_duration, _p_fiscal, _p_quarter, _p_point, _p_range):
        out = fn(s)
        if out is not None:
            return out
    stripped = _strip_prep(s)
    if stripped != s:
        for fn in (_p_duration, _p_fiscal, _p_quarter, _p_range):
            out = fn(stripped)
            if out is not None:
                return out
    return _p_dateparser(s)


def has_time_token(surface: str) -> bool:
    return bool(_TIME_TOKEN.search(surface or ""))


# ------------------------------------------------------------------ scanning
def find_time_expressions(text: str) -> list[tuple[int, int, str]]:
    """Deterministic scan: ``[(start, end, label)]`` of parseable time expressions,
    longest match first, no overlaps, in text order. Labels are GLiNER time labels."""
    found: list[tuple[int, int, str]] = []
    for pattern, label in _SCAN:
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), label))
    for m in _YEAR_CTX.finditer(text):
        found.append((m.start(1), m.end(1), "time period"))
    kept: list[tuple[int, int, str]] = []
    for start, end, label in sorted(found, key=lambda t: (-(t[1] - t[0]), t[0])):
        while end > start and text[end - 1] in " ,.;:":
            end -= 1
        if end <= start or any(start < e and end > s for s, e, _ in kept):
            continue
        if parse_time(text[start:end]) is None:
            continue
        kept.append((start, end, label))
    return sorted(kept)


def label_for_kind(kind: str) -> str:
    return {"DURATION": "duration", "INTERVAL": "time period", "PERIOD": "time period",
            "RECURRING": "time period"}.get(kind, "date")


# ------------------------------------------------------------------ resolution
def _finer(a: str, b: str) -> str:
    return a if _GRAN_ORDER.index(a) <= _GRAN_ORDER.index(b) else b


def _add_months(d: _dt.date, months: int) -> _dt.date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return _dt.date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def fiscal_year(end_year: int, start_month: int) -> tuple[_dt.date, _dt.date]:
    """FY named by the year it ends in: start month 7 -> FY2025 = 2024-07-01..2025-06-30."""
    if start_month == 1:
        return _dt.date(end_year, 1, 1), _dt.date(end_year, 12, 31)
    start = _dt.date(end_year - 1, start_month, 1)
    return start, _add_months(start, 12) - _dt.timedelta(days=1)


def _quarter_dates(year: int, q: int, fiscal: bool, start_month: int) -> tuple[_dt.date, _dt.date]:
    start = (_add_months(fiscal_year(year, start_month)[0], 3 * (q - 1)) if fiscal
             else _dt.date(year, 3 * q - 2, 1))
    return start, _add_months(start, 3) - _dt.timedelta(days=1)


def _point_of(d: _dt.date, granularity: str) -> Point:
    if granularity == "year":
        return Point(d.year)
    if granularity == "month":
        return Point(d.year, d.month)
    return Point(d.year, d.month, d.day)


def _shift(anchor: Point, n: float, unit: str, step: bool) -> tuple[str, Point | None, str, str | None] | None:
    """Move ``anchor`` by ``n`` units. Returns ``(kind, point, granularity, value_end)``;
    ``None`` when the anchor is too coarse for the unit (precision over recall)."""
    if not float(n).is_integer():
        return None
    n = int(n)
    g = anchor.granularity
    g = "day" if g in ("hour", "minute") else g
    fine_enough = {"day": {"day"}, "week": {"day"}, "fortnight": {"day"}, "month": {"day", "month"},
                   "quarter": {"day", "month"}, "year": {"day", "month", "year"}}[unit]
    if g not in fine_enough:
        return None
    base = anchor.first_day()
    if unit in ("day", "week", "fortnight"):
        days = n * {"day": 1, "week": 7, "fortnight": 14}[unit]
        d = base + _dt.timedelta(days=days)
        if step and unit == "week":
            iso_year, iso_week, _ = d.isocalendar()
            return "INSTANT", None, "week", f"{iso_year}-W{iso_week:02d}"
        if step and unit == "fortnight":
            return None
        return "INSTANT", _point_of(d, "day"), "day", None
    months = n * (3 if unit == "quarter" else 12 if unit == "year" else 1)
    d = _add_months(base, months)
    if step:
        if unit == "quarter":
            q = (d.month - 1) // 3 + 1
            return "PERIOD", None, "quarter", f"{d.year}-Q{q}"
        return "INSTANT", _point_of(d, unit), unit, None
    return "INSTANT", _point_of(d, g), g, None


@dataclass
class Anchors:
    """What resolution may lean on, for one item."""

    fy_start: int = 7
    base_ref: str | None = None          # last ref with a stated year (rolling base)
    base_year: int | None = None
    doc_ref: str | None = None
    doc_point: Point | None = None
    event_ref: str | None = None         # nearest anchored date, same or previous sentence
    event_point: Point | None = None


def _unresolved(p: Parsed, rule: str | None = None) -> Resolved:
    return Resolved("RELATIVE", None, None, p.granularity, "UNRESOLVED", None, CONF_UNRESOLVED, rule or p.rule)


def _year_base(a: Anchors) -> tuple[int | None, str, str | None]:
    if a.base_year is not None:
        return a.base_year, "ROLLING_BASE", a.base_ref
    if a.doc_point is not None:
        return a.doc_point.year, "DOC_DATE", a.doc_ref
    return None, "UNRESOLVED", None


def _quarter_label(start: _dt.date) -> str:
    return f"{start.year}-Q{(start.month - 1) // 3 + 1}"


def resolve(p: Parsed, a: Anchors) -> Resolved:
    """Anchor a parsed expression (P1 §6 rules 1–5)."""
    if p.kind in ("DURATION", "RECURRING"):
        return Resolved(p.kind, p.iso, None, p.granularity, "ABSOLUTE", None, p.confidence, p.rule)

    if p.kind == "PERIOD":
        tag = p.period[0]
        anchor_type, anchor_id, conf = "ABSOLUTE", None, p.confidence
        if tag == "fy":
            start, end = fiscal_year(p.period[1], a.fy_start)
        elif tag == "fy_end":
            end = p.period[1]
            start = _add_months(end + _dt.timedelta(days=1), -12)
        else:
            _, year, q, fiscal = p.period
            if year is None:
                year, anchor_type, anchor_id = _year_base(a)
                if year is None:
                    return _unresolved(p)
                conf = CONF_ROLLING
            start, end = _quarter_dates(year, q, fiscal, a.fy_start)
        return Resolved("PERIOD", start.isoformat(), end.isoformat(), p.granularity, anchor_type, anchor_id,
                        conf, p.rule)

    if p.kind == "INSTANT":
        point = p.start
        if point.year is not None:
            if not point.valid():
                return _unresolved(p, "invalid_date")
            return Resolved("INSTANT", point.iso(), point.iso(), point.granularity, "ABSOLUTE", None,
                            p.confidence, p.rule, point=point, stated_year=True)
        year, anchor_type, anchor_id = _year_base(a)
        if year is None:
            return _unresolved(p, p.rule)
        point = replace(point, year=year)
        if not point.valid():
            return _unresolved(p, "invalid_date")
        return Resolved("INSTANT", point.iso(), point.iso(), point.granularity, anchor_type, anchor_id,
                        CONF_ROLLING, p.rule, point=point)

    if p.kind == "INTERVAL":
        start, end = p.start, p.end
        anchor_type, anchor_id, conf, stated = "ABSOLUTE", None, p.confidence, True
        if end.year is None:
            year, anchor_type, anchor_id = _year_base(a)
            if year is None:
                return _unresolved(p)
            start, end = replace(start, year=start.year or year), replace(end, year=year)
            if start.valid() and end.valid() and start.first_day() > end.last_day():
                start = replace(start, year=year - 1)
            conf, stated = CONF_ROLLING, False
        if not (start.valid() and end.valid()) or start.first_day() > end.last_day():
            return _unresolved(p, "invalid_range")
        return Resolved("INTERVAL", start.first_day().isoformat(), end.last_day().isoformat(), p.granularity,
                        anchor_type, anchor_id, conf, p.rule, stated_year=stated, point=end)

    # RELATIVE
    if p.rel == "anaphoric" or p.unit is None:
        return _unresolved(p)
    if p.rel in ("deictic", "deictic_step"):
        anchor, anchor_type, anchor_id = a.doc_point, "DOC_DATE", a.doc_ref
    else:
        anchor, anchor_type, anchor_id = a.event_point, "EVENT", a.event_ref
    if anchor is None:
        return _unresolved(p)
    shifted = _shift(anchor, p.n, p.unit, step=p.rel in ("step", "deictic_step"))
    if shifted is None:
        return _unresolved(p)
    kind, point, gran, label = shifted
    if kind == "PERIOD":
        year, q = int(label[:4]), int(label[-1])
        start, end = _quarter_dates(year, q, False, a.fy_start)
        return Resolved("PERIOD", start.isoformat(), end.isoformat(), "quarter", anchor_type, anchor_id,
                        p.confidence, p.rule)
    if point is None:                                                # ISO week
        return Resolved("INSTANT", label, label, gran, anchor_type, anchor_id, p.confidence, p.rule)
    return Resolved("INSTANT", point.iso(), point.iso(), gran, anchor_type, anchor_id, p.confidence, p.rule,
                    point=point)


# ------------------------------------------------------------------ the unit
@dataclass
class _Item:
    start: int
    end: int
    surface: str
    mention: dict[str, Any] | None = None
    label: str | None = None


def _doc_point(doc_date: str | None) -> Point | None:
    if not doc_date:
        return None
    m = re.match(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", str(doc_date))
    if not m:
        return None
    p = Point(int(m.group(1)), int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None)
    return p if p.valid() else None


def _doc_date_span(tx: Tx, ctx: StageContext, doc: DocView, point: Point,
                   spans: dict[str, dict[str, Any]]) -> str | None:
    """The span of the document date: ``notes["doc_date_span"]`` (span id, ``{char_start,
    char_end}`` or ``[start, end]``), else the first matching date in the head of the text."""
    notes = doc.doc.get("notes") or {}
    raw = notes.get("doc_date_span") if isinstance(notes, dict) else None
    start = end = None
    if isinstance(raw, str) and raw in spans:
        return raw
    if isinstance(raw, dict):
        if raw.get("span_id") in spans:
            return raw["span_id"]
        start, end = raw.get("char_start", raw.get("start")), raw.get("char_end", raw.get("end"))
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        start, end = raw
    if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(doc.text):
        return write_span(tx, doc, doc.locator.ground_span(start, end), created_by=MODULE)
    head = doc.text[:ctx.config.ingest.doc_date_window_chars]
    for s, e, _label in find_time_expressions(head):
        parsed = parse_time(head[s:e])
        if parsed and parsed.kind == "INSTANT" and parsed.start.year is not None \
                and parsed.start.first_day() == point.first_day() \
                and parsed.start.granularity in (point.granularity, "hour", "minute"):
            return write_span(tx, doc, doc.locator.ground_span(s, e), created_by=MODULE)
    return None


def run_document(ctx: StageContext, doc_id: str) -> dict[str, Any]:
    """Normalize every time expression of one document into S3."""
    watch = Stopwatch()
    doc = ctx.doc(doc_id)
    with ctx.reader() as r:
        mentions = r.select("mentions", order_by="mention_id", run_id=ctx.run_id, doc_id=doc_id)
        spans = {s["span_id"]: s for s in r.select("spans", doc_id=doc_id)}
    time_mentions = [m for m in mentions if m["label"] in TIME_LABELS and m["span_id"] in spans]
    covered = [(spans[m["span_id"]]["char_start"], spans[m["span_id"]]["char_end"]) for m in time_mentions]

    items: list[_Item] = []
    for m in time_mentions:
        if m["status"] != S.OPEN:
            continue
        sp = spans[m["span_id"]]
        items.append(_Item(sp["char_start"], sp["char_end"], doc.text[sp["char_start"]:sp["char_end"]], mention=m))
    rule_items = 0
    for start, end, label in find_time_expressions(doc.text):
        if any(start < e and end > s for s, e in covered):
            continue
        sents = doc.locator.sentences_overlapping(start, end)
        if not sents or all(doc.by_id[x.sentence_id]["filtered"] for x in sents):
            continue
        items.append(_Item(start, end, doc.text[start:end], label=label))
        rule_items += 1
    items.sort(key=lambda it: (it.start, it.end))

    fy_start = ctx.ontology.fiscal_year_start_month or 7
    doc_point = _doc_point(doc.doc.get("doc_date"))
    counts = {"time_refs": 0, "from_mentions": 0, "from_rules": rule_items, "unresolved": 0,
              "discarded": 0, "doc_date_ref": 0}
    with ctx.tx(S.TIME, MODULE) as tx:
        doc_ref = None
        if doc_point is not None:
            span_id = _doc_date_span(tx, ctx, doc, doc_point, spans)
            if span_id is None:
                tx.log_change("time_ref", "-", "DROPPED", None, {"doc_id": doc_id, "doc_date": doc.doc["doc_date"]},
                              rule_id="doc_date_no_span")
            else:
                doc_ref = new_id("tim")
                surface = doc.text[spans[span_id]["char_start"]:spans[span_id]["char_end"]] if span_id in spans \
                    else _span_quote(tx, span_id)
                tx.insert("time_refs", dict(time_id=doc_ref, doc_id=doc_id, span_id=span_id, surface=surface,
                                            kind="INSTANT", value_start=doc_point.iso(), value_end=doc_point.iso(),
                                            granularity=doc_point.granularity, anchor_type="ABSOLUTE",
                                            anchor_time_id=None, confidence=CONF_ABSOLUTE, rule_id="doc_date"),
                          rule_id="doc_date")
                counts["doc_date_ref"] = 1
                counts["time_refs"] += 1

        anchors_seen: list[tuple[int, int, str, Point]] = []     # (sentence idx, start, time_id, point)
        base_ref: str | None = None
        base_year: int | None = None
        nonfiltered = [s["idx"] for s in doc.sentences if not s["filtered"]]
        for it in items:
            sents = doc.locator.sentences_overlapping(it.start, it.end)
            sidx = sents[0].idx if sents else None
            parsed = parse_time(it.surface)
            if parsed is None:
                if it.mention is not None and not has_time_token(it.surface):
                    tx.update("mentions", {"mention_id": it.mention["mention_id"]},
                              {"status": S.DISCARDED, "discard_reason": "not_time_expression"},
                              rule_id="u3_not_time")
                    counts["discarded"] += 1
                    continue
                parsed = Parsed("RELATIVE", "unparsed", rel="anaphoric", confidence=CONF_UNRESOLVED)
            event_ref = event_point = None
            if parsed.kind == "RELATIVE" and sidx is not None:
                same = [x for x in anchors_seen if x[0] == sidx and x[1] < it.start]
                prev_idx = max((i for i in nonfiltered if i < sidx), default=None)
                prev = [x for x in anchors_seen if x[0] == prev_idx] if prev_idx is not None else []
                pick = (same or prev or [None])[-1]
                if pick is not None:
                    event_ref, event_point = pick[2], pick[3]
            res = resolve(parsed, Anchors(fy_start=fy_start, base_ref=base_ref, base_year=base_year,
                                          doc_ref=doc_ref, doc_point=doc_point,
                                          event_ref=event_ref, event_point=event_point))
            span_id = it.mention["span_id"] if it.mention else \
                write_span(tx, doc, doc.locator.ground_span(it.start, it.end), created_by=MODULE)
            time_id = new_id("tim")
            tx.insert("time_refs", dict(time_id=time_id, doc_id=doc_id, span_id=span_id, surface=it.surface,
                                        kind=res.kind, value_start=res.value_start, value_end=res.value_end,
                                        granularity=res.granularity, anchor_type=res.anchor_type,
                                        anchor_time_id=res.anchor_time_id, confidence=res.confidence,
                                        rule_id=res.rule), rule_id=res.rule)
            if it.mention is not None:
                tx.update("mentions", {"mention_id": it.mention["mention_id"]},
                          {"status": S.RESOLVED, "time_id": time_id}, rule_id=res.rule)
                counts["from_mentions"] += 1
            else:
                tx.insert("mentions", dict(mention_id=new_id("men"), doc_id=doc_id, span_id=span_id,
                                           surface=it.surface, label=it.label or label_for_kind(res.kind),
                                           score=None, source="rules", entity_id=None, time_id=time_id,
                                           status=S.RESOLVED), rule_id="u3_rules_scan")
            counts["time_refs"] += 1
            if res.anchor_type == "UNRESOLVED":
                counts["unresolved"] += 1
            if res.stated_year and res.point is not None:
                base_ref, base_year = time_id, res.point.year
            if res.kind == "INSTANT" and res.point is not None and sidx is not None:
                anchors_seen.append((sidx, it.start, time_id, res.point))
        record_stage_metrics(tx, S.TIME, doc_id, items_in=len(items), items_out=counts["time_refs"],
                             wall_ms=watch.ms)
    return counts


def _span_quote(tx: Tx, span_id: str) -> str:
    row = tx.get("spans", span_id=span_id)
    return (row or {}).get("quote") or ""
