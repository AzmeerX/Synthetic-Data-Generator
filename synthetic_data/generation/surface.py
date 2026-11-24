"""Surface realisation: typed values to text, facts to sentences.

Two rules from the architecture shape this module:

* a renderer formats typed values and never parses them (§3.1 rule 4), so every
  amount, date and percentage here is produced from the typed object;
* the same fact may be said in many ways (§10.2 axis *realisation*), so each
  relation has several predicates per register and several sentence frames, all
  selected from a statement-scoped RNG.

``surface`` strings produced here are the *intended* wording of a
:class:`FactStatement`.  They are never gold evidence (§4.4 invariant 7).
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field

from synthetic_data.model import Entity, IsoDate, Money, Percentage
from synthetic_data.surfaces import (CURRENCY_NAMES as _CURRENCY_NAMES,
                                     CURRENCY_SYMBOLS as _CURRENCY_SYMBOLS,
                                     money_key, money_surface_key, money_surfaces,
                                     surfaces_equivalent)

from .rng import pick, scope_rng

__all__ = [
    "Surface",
    "Sentence",
    "RoleMap",
    "fmt_money",
    "fmt_date",
    "fmt_percent",
    "money_key",
    "money_surface_key",
    "money_surfaces",
    "surfaces_equivalent",
    "money_number_format",
    "date_number_format",
    "percent_number_format",
    "REGISTERS",
]

REGISTERS = ("formal", "neutral", "terse")

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_LEGAL_FORMS = ("Limited", "plc", "LLC", "GmbH", "LLP")


# ---------------------------------------------------------------------------
# typed value formatting
# ---------------------------------------------------------------------------
def fmt_money(money: Money, style: int = 0) -> str:
    symbol = _CURRENCY_SYMBOLS.get(money.currency)
    whole = f"{abs(money.amount_minor) // 100:,}"
    pence = abs(money.amount_minor) % 100
    sign = "-" if money.amount_minor < 0 else ""
    if style == 0 and symbol:
        if pence:
            return f"{sign}{symbol}{whole}.{pence:02d}"
        return f"{sign}{symbol}{whole}"
    if style == 1:
        return f"{sign}{money.currency} {whole}.{pence:02d}"
    if style == 2:
        name = _CURRENCY_NAMES.get(money.currency, money.currency)
        return f"{sign}{whole}.{pence:02d} {name}"
    return f"{sign}{money.currency} {whole}.{pence:02d}"


def fmt_date(value: IsoDate, style: int = 0) -> str:
    date = value.value
    if style == 1:
        day = date.day
        suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{day}{suffix} {_MONTHS[date.month - 1]} {date.year}"
    if style == 2:
        return date.isoformat()
    if style == 3:
        return f"{date.day} {_MONTHS[date.month - 1][:3]} {date.year}"
    if style == 4:
        return f"{_MONTHS[date.month - 1]} {date.day}, {date.year}"
    return f"{date.day} {_MONTHS[date.month - 1]} {date.year}"


def fmt_percent(value: Percentage, style: int = 0) -> str:
    if style == 1:
        return f"{value.basis_points} basis points"
    if style == 2:
        whole, frac = divmod(abs(value.basis_points), 100)
        return f"{'-' if value.basis_points < 0 else ''}{whole}.{frac:02d} per cent"
    return value.display()


def money_number_format() -> str:
    return "#,##0.00"


def date_number_format() -> str:
    return "dd mmm yyyy"


def percent_number_format() -> str:
    return "0.00%"


_MONEY_STYLES = 3
_DATE_STYLES = 5
_PERCENT_STYLES = 3


# ---------------------------------------------------------------------------
# referring expressions
# ---------------------------------------------------------------------------
@dataclass
class RoleMap:
    """Preferred referring expressions for one document.

    ``roles`` maps an entity id to a definite description used in formal prose
    ("the Lender", "the Borrower").  A role is only offered for entities the
    document actually has a role for, so a witness is never called "the
    Guarantor".
    """

    entities: dict[str, Entity]
    roles: dict[str, str] = field(default_factory=dict)
    used_full: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        # "a Subsidiary" cannot refer to four different companies: a definite
        # description is only usable while it identifies exactly one entity, and
        # the others fall back to their own name.  This is why two facts about
        # different entities can no longer realise as the same sentence.
        counts = Counter(self.roles.values())
        self.unique = {entity_id: role for entity_id, role in self.roles.items()
                       if counts[role] == 1}

    def full(self, entity_id: str) -> str:
        return self.entities[entity_id].canonical_name

    def short(self, entity_id: str) -> str:
        name = self.entities[entity_id].canonical_name
        words = name.split()
        while len(words) > 1 and words[-1] in _LEGAL_FORMS:
            words.pop()
        return " ".join(words)

    def of(self, entity_id: str, style: str = "auto") -> str:
        if style == "role":
            return self.unique.get(entity_id) or self.short(entity_id)
        if style == "short":
            return self.short(entity_id)
        if style == "full":
            return self.full(entity_id)
        # auto: role on later mentions, full name on the first
        if entity_id in self.used_full:
            return self.unique.get(entity_id) or self.short(entity_id)
        self.used_full.add(entity_id)
        return self.full(entity_id)


# ---------------------------------------------------------------------------
# relation realisation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RelationSpec:
    """Predicate phrases by register; ``{amt}`` marks where the amount lands."""

    formal: tuple[str, ...]
    neutral: tuple[str, ...]
    terse: tuple[str, ...]
    takes_amount: bool = False
    takes_date: bool = True

    def phrases(self, register: str) -> tuple[str, ...]:
        if register == "formal":
            return self.formal
        if register == "terse":
            return self.terse
        return self.neutral


RELATIONS: dict[str, RelationSpec] = {
    "owns": RelationSpec(
        formal=("holds {pct} of the issued share capital of", "is the registered holder of {pct} of the shares of"),
        neutral=("owns {pct} of", "holds {pct} of"),
        terse=("holds {pct} of",),
        takes_date=False),
    "acquired": RelationSpec(
        formal=("acquired the entire issued share capital of", "completed the acquisition of"),
        neutral=("bought", "acquired"),
        terse=("bought",),
        takes_amount=True),
    "controls": RelationSpec(
        formal=("exercises control over", "has a controlling interest in"),
        neutral=("controls", "has control of"),
        terse=("controls",),
        takes_date=False),
    "is_subsidiary_of": RelationSpec(
        formal=("is a wholly owned subsidiary of", "is an indirect subsidiary of"),
        neutral=("is a subsidiary of", "is owned by"),
        terse=("is a subsidiary of",),
        takes_date=False),
    "lent_to": RelationSpec(
        formal=("advanced {amt} to", "made available a term facility of {amt} to"),
        neutral=("lent {amt} to", "provided {amt} to"),
        terse=("lent {amt} to",),
        takes_amount=True),
    "borrowed_from": RelationSpec(
        formal=("borrowed {amt} from", "drew {amt} under the facility made available by"),
        neutral=("borrowed {amt} from",),
        terse=("drew {amt} from",),
        takes_amount=True),
    "repaid": RelationSpec(
        formal=("repaid {amt} to", "made a scheduled repayment of {amt} to"),
        neutral=("paid {amt} back to", "repaid {amt} to"),
        terse=("repaid {amt} to",),
        takes_amount=True),
    "disbursed": RelationSpec(
        formal=("disbursed {amt} to", "paid {amt} to"),
        neutral=("paid {amt} to",),
        terse=("paid {amt} to",),
        takes_amount=True),
    "owes": RelationSpec(
        formal=("was indebted to in the amount of {amt} as at", "owed {amt} to"),
        neutral=("owed {amt} to",),
        terse=("owed {amt} to",),
        takes_amount=True),
    "guarantees": RelationSpec(
        formal=("has guaranteed the obligations of", "stands as guarantor for the liabilities of"),
        neutral=("guaranteed the obligations of",),
        terse=("guarantees",),
        takes_date=False),
    "secures": RelationSpec(
        formal=("has granted security in favour of", "has charged its assets in favour of"),
        neutral=("granted security to",),
        terse=("secured",),
        takes_date=False),
    "grants_security_over": RelationSpec(
        formal=("granted a fixed charge over", "created security over"),
        neutral=("granted security over",),
        terse=("charged",),
        takes_date=False),
    "is_director_of": RelationSpec(
        formal=("was appointed a director of", "serves as a director of"),
        neutral=("became a director of", "is a director of"),
        terse=("joined the board of",),
        takes_date=True),
    "chairs": RelationSpec(
        formal=("chaired the board of", "presided over the meeting of the board of"),
        neutral=("chaired",),
        terse=("chaired",),
        takes_date=True),
    "represents": RelationSpec(
        formal=("acts as legal adviser to", "is instructed on behalf of"),
        neutral=("represents", "acts for"),
        terse=("acts for",),
        takes_date=False),
    "advises": RelationSpec(
        formal=("acts as reporting accountant to", "is appointed auditor of"),
        neutral=("audits", "advises"),
        terse=("advises",),
        takes_date=False),
    "attends": RelationSpec(
        formal=("attended the meeting of the board of", "was present at the meeting of"),
        neutral=("attended",),
        terse=("attended",),
        takes_date=True),
    "incorporated": RelationSpec(
        formal=("was incorporated in {jur} as a limited company on",),
        neutral=("was incorporated in {jur} on",),
        terse=("incorporated in {jur} on",),
        takes_date=True),
    "filed": RelationSpec(
        formal=("commenced proceedings against", "issued a claim against"),
        neutral=("filed a claim against", "sued"),
        terse=("sued",),
        takes_date=True),
    "served": RelationSpec(
        formal=("served a statutory demand on", "served notice on"),
        neutral=("served notice on",),
        terse=("served notice on",),
        takes_date=True),
    "ordered": RelationSpec(
        formal=("was ordered by the court to notify", "was directed to write to"),
        neutral=("was ordered to notify",),
        terse=("was ordered to notify",),
        takes_date=True),
    "amended": RelationSpec(
        formal=("entered into an amendment agreement with", "varied the facility agreement with"),
        neutral=("amended its agreement with",),
        terse=("amended the agreement with",),
        takes_date=True),
    "superseded": RelationSpec(
        formal=("superseded the earlier agreement with",),
        neutral=("replaced the agreement with",),
        terse=("replaced the agreement with",),
        takes_date=True),
    "settled": RelationSpec(
        formal=("settled the claim brought by", "compromised the proceedings brought by"),
        neutral=("settled with",),
        terse=("settled with",),
        takes_date=True),
    "reduced": RelationSpec(
        formal=("reduced its holding in", "disposed of part of its interest in"),
        neutral=("reduced its stake in",),
        terse=("sold down",),
        takes_date=True),
    "transferred": RelationSpec(
        formal=("transferred {amt} of assets to", "assigned its interest to"),
        neutral=("transferred assets to",),
        terse=("transferred assets to",),
        takes_amount=True),
}

_FRAMES_DATE = (
    "{s} {pred} {o} on {date}.",
    "On {date}, {s} {pred} {o}.",
    "{s} {pred} {o} with effect from {date}.",
)
_FRAMES_PLAIN = (
    "{s} {pred} {o}.",
    "{s} {pred} {o} at the relevant time.",
)
_FRAMES_AMOUNT_ONLY = (
    "{s} {pred} {o}.",
    "{s} {pred} {o} in that amount.",
)

# proposition reporting frames by status
_PROPOSITION_FRAMES = {
    "stated": ("{sp} stated that {p}.", "It was stated on behalf of {sp} that {p}."),
    "alleged": ("{sp} alleged that {p}.", "It was alleged, on behalf of {sp}, that {p}."),
    "denied": ("{sp} denied that {p}.", "{sp} rejected the assertion that {p}."),
    "found": ("The review found that {p}.", "The reviewers concluded that {p}."),
    "approved": ("The board approved the proposal that {p}.", "It was resolved that {p}."),
    "rejected": ("The board rejected the proposal that {p}.", "The resolution that {p} was not carried."),
    "deferred": ("The board deferred consideration of the proposal that {p}.",
                 "Consideration of the proposal that {p} was deferred."),
    "proposed": ("{sp} proposed that {p}.", "A proposal was tabled that {p}."),
    "held": ("It is held that {p}.", "The court held that {p}."),
    "signed": ("The parties confirmed that {p}.",),
    "waived": ("The Lender waived the requirement that {p}.",),
    "completed": ("It was confirmed that {p}.",),
}
_MODAL_FRAMES = {
    "may": "{sp} may {p}.",
    "shall": "{sp} shall {p}.",
    "must": "{sp} must {p}.",
    "intends_to": "{sp} intends to {p}.",
    "expected_to": "{sp} is expected to {p}.",
}


@dataclass(frozen=True)
class Sentence:
    """One realised sentence and what it refers to."""

    text: str
    fact_id: str | None = None
    value: Money | Percentage | IsoDate | int | None = None
    entity_id: str | None = None
    mention_ids: tuple[str, ...] = ()
    slots: tuple[tuple[str, str], ...] = ()

    def slot_dict(self) -> dict[str, str]:
        return dict(self.slots)


class Surface:
    """Realises facts for one document, with that document's role map."""

    def __init__(self, roles: RoleMap, key: bytes, scope_prefix: str,
                 register: str = "formal") -> None:
        if register not in REGISTERS:
            raise ValueError(f"unknown register {register!r}")
        self.roles = roles
        self.key = key
        self.scope_prefix = scope_prefix
        self.register = register
        # One surface style per document, so a reader never sees "£30,000,000" and
        # "30,000,000.00 pounds sterling" in the same instrument.  The style is a
        # pure function of the corpus key and the document scope; it varies across
        # documents, which keeps the §10.2 realisation axis without inconsistency.
        style_rng = scope_rng(key, "surface-style", scope_prefix)
        self.money_style = pick(style_rng, (0, 0, 1, 1, 2))
        self.date_style = pick(style_rng, (0, 0, 1, 3))

    # -- helpers -----------------------------------------------------------
    def _rng(self, statement_scope: str):
        return scope_rng(self.key, "surface", self.scope_prefix, statement_scope)

    def _entity(self, entity_id: str, rng) -> str:
        style = pick(rng, ("auto", "auto", "role", "short"))
        return self.roles.of(entity_id, style)

    def _value_slots(self, fact, rng) -> dict[str, str]:
        slots: dict[str, str] = {}
        if fact.amount is not None:
            slots["amount"] = fmt_money(fact.amount, self.money_style)
        if fact.effective is not None and fact.effective.start is not None:
            slots["date"] = fmt_date(fact.effective.start, self.date_style)
        if fact.effective is not None and fact.effective.end is not None:
            slots["end_date"] = fmt_date(fact.effective.end, self.date_style)
        return slots

    # -- facts -------------------------------------------------------------
    def fact(self, fact, scope: str, extra: dict[str, str] | None = None,
             used: set[str] | None = None) -> Sentence:
        """Realise one fact, avoiding wording already used in this document.

        A fact may legitimately be stated more than once in a document (§4.4), but
        two occurrences must not be byte-identical: the re-draw advances the same
        statement-scoped rng, so the retry is deterministic.
        """
        rng = self._rng(scope)
        sentence = None
        for _ in range(8):
            sentence = (self._proposition(fact, rng) if fact.kind == "proposition"
                        else self._edge(fact, rng, extra or {}))
            if used is None or sentence.text not in used:
                break
        assert sentence is not None
        if used is not None:
            used.add(sentence.text)
        return sentence

    def _edge(self, fact, rng, extra: dict[str, str]) -> Sentence:
        spec = RELATIONS.get(fact.relation)
        if spec is None:
            raise KeyError(
                f"no surface template for relation {fact.relation!r}; "
                f"add it to RELATIONS or the sentence would be ungrounded prose")
        subject = self._entity(fact.subject.entity_id, rng) if fact.subject else "the parties"
        obj = self._entity(fact.object.entity_id, rng) if fact.object else "the other party"
        slots = self._value_slots(fact, rng)
        slots["jur"] = "England and Wales"
        slots["s"] = subject
        slots["o"] = obj
        slots.update(extra)
        if fact.amount is not None:
            slots.setdefault("amt", slots["amount"])
        if fact.effective is not None and fact.effective.start is not None:
            slots.setdefault("date", fmt_date(fact.effective.start, self.date_style))

        predicate = pick(rng, spec.phrases(self.register))
        try:
            predicate = predicate.format(**slots)
        except KeyError as exc:  # pragma: no cover - filler bug
            raise KeyError(f"relation {fact.relation} needs slot {exc}") from exc

        if spec.takes_amount and "amt" not in slots:
            raise ValueError(f"relation {fact.relation} requires an amount")
        if spec.takes_amount:
            frames = _FRAMES_PLAIN if not spec.takes_date else _FRAMES_DATE
        elif spec.takes_date and "date" in slots:
            frames = _FRAMES_DATE
        elif "amt" in slots:
            frames = _FRAMES_AMOUNT_ONLY
        else:
            frames = _FRAMES_PLAIN
        frame = pick(rng, frames)
        if "{date}" not in frame:
            slots.pop("date", None)
        text = _sentence(frame.format(pred=predicate, **slots))
        if fact.negated:
            text = f"It was denied that {_lower_first(text.rstrip('.'))}."

        entity_id = fact.subject.entity_id if fact.subject else (
            fact.object.entity_id if fact.object else None)
        mentions = tuple(ref.entity_id for ref in
                         (fact.subject, fact.object, fact.speaker, fact.reporter) if ref)
        return Sentence(text=text, fact_id=fact.fact_id,
                        value=fact.amount if fact.amount is not None else (
                            fact.effective.start if fact.effective else None),
                        entity_id=entity_id, mention_ids=mentions,
                        slots=tuple(sorted(
                            (k, v) for k, v in slots.items()
                            if k in {"amount", "date", "end_date", "jur", "pct"}
                            and v in text)))

    def _proposition(self, fact, rng) -> Sentence:
        speaker = (self._entity(fact.speaker.entity_id, rng)
                   if fact.speaker else "the parties")
        body = (fact.text or "").strip().rstrip(".")
        if fact.modality:
            frame = _MODAL_FRAMES[fact.modality]
            text = frame.format(sp=speaker, p=_lower_first(body))
        else:
            frames = _PROPOSITION_FRAMES.get(fact.status, ("It was noted that {p}.",))
            frame = pick(rng, frames)
            text = frame.format(sp=speaker, p=_lower_first(body))
        if fact.negated and not text.lower().startswith(("it was", "the ")):
            text = f"It was not accepted that {_lower_first(body)}."
        mentions = tuple(ref.entity_id for ref in (fact.speaker, fact.reporter) if ref)
        return Sentence(text=_sentence(text), fact_id=fact.fact_id,
                        entity_id=fact.speaker.entity_id if fact.speaker else None,
                        mention_ids=mentions, slots=())


def _lower_first(text: str) -> str:
    if not text:
        return text
    if text[0].isupper() and (len(text) < 2 or not text[1].isupper()):
        return text[0].lower() + text[1:]
    return text


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise ValueError("empty sentence")
    if text[0].islower():
        text = text[0].upper() + text[1:]
    if not text.endswith((".", "?", "!")):
        text += "."
    return text


def to_iso(value: dt.date) -> IsoDate:
    return IsoDate(value=value)
