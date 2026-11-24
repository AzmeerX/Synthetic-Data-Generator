"""Deterministic name and attribute pools.

Names are combinatorial and de-duplicated within a corpus, so a 500-document
corpus never reuses a company or a person by accident — and two runs of the same
seed produce the same roster.  Pools are plain data: adding a name must not
re-order an existing corpus, so new entries are appended, never inserted.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

__all__ = ["NameFactory", "JURISDICTIONS", "CITIES", "COURTS", "SECTORS"]

JURISDICTIONS = (
    "England and Wales", "Scotland", "Ireland", "Delaware", "Singapore",
    "Luxembourg", "Netherlands", "New York",
)
CITIES = (
    "London", "Edinburgh", "Dublin", "Manchester", "Bristol", "Leeds",
    "Wilmington", "Singapore", "Luxembourg", "Amsterdam", "New York", "Glasgow",
)
COURTS = (
    "the High Court of Justice, Business and Property Courts",
    "the Commercial Court",
    "the Court of Session, Outer House",
    "the District Court of Delaware",
    "the High Court of Singapore",
    "the Circuit Court of Dublin",
)
SECTORS = (
    "specialist manufacturing", "logistics", "data infrastructure and cloud hosting",
    "renewable energy", "financial services", "healthcare services",
    "business services", "consumer brands", "industrial distribution",
    "software and analytics",
)

_COMPANY_STEMS = (
    "Ash", "Brae", "Calder", "Dun", "Elm", "Fen", "Garth", "Haven", "Inver",
    "Juniper", "Kel", "Lorne", "Mere", "North", "Oak", "Pen", "Quarry", "Raven",
    "Sel", "Thorn", "Ull", "Vale", "West", "Yarrow", "Bracken", "Crag", "Dale",
    "Ferry", "Glen", "Hollow", " Iron", "Kestrel", "Lark", "Marsh", "Ness",
    "Orchard", "Pike", "Ridge", "Stone", "Tarn", "Wold", "Birch", "Copper", "Dove",
)
_COMPANY_TAILS = (
    "haven", "mere", "field", "bridge", "court", "gate", "holm", "stead", "wick",
    "dale", "moor", "crest", "ford", "hurst", "well", "bank", "reach", "ridge",
)
_COMPANY_KINDS = (
    "Holdings", "Trading", "Capital", "Industries", "Partners", "Group",
    "Investments", "Logistics", "Resources", "Systems", "Networks", "Properties",
)
_LEGAL_FORMS = ("Limited", "plc", "LLC", "GmbH")

_BANK_STEMS = (
    "Caldervale", "Northgate", "Harbour", "Kingsmere", "Lombard", "Meridian",
    "Ravensbourne", "Solent", "Thameside", "Wexford", "Ardmore", "Belfry",
    "Cobalt", "Dunmore", "Eastcliff", "Fairhaven", "Granite", "Holloway",
)
_BANK_KINDS = ("Bank", "Banking Group", "Trust", "Credit Partners", "Finance")

_FIRST_NAMES = (
    "Amina", "Marek", "Priya", "Tomas", "Elena", "Ruth", "Daniel", "Sofia",
    "Kwame", "Ines", "Harald", "Mei", "Owen", "Nadia", "Julian", "Farah",
    "Gareth", "Lucia", "Ravi", "Clara", "Benedikt", "Yusuf", "Anika", "Peter",
    "Noor", "Simon", "Ilse", "Diego", "Hanna", "Tariq", "Beatrice", "Iain",
    "Sona", "Emeka", "Margot", "Rafael", "Lena", "Cormac", "Aditi", "Felix",
)
_LAST_NAMES = (
    "Kerr", "Nyle", "Raman", "Dvorak", "Ferreira", "Whitlock", "Okonkwo",
    "Lindqvist", "Hassan", "Moreau", "Brandt", "Chen", "Adeyemi", "Kowalski",
    "Sinclair", "Marchetti", "Vanterpool", "Ostrowski", "Bhatt", "Nakamura",
    "Ellison", "Duarte", "Halvorsen", "Mbeki", "Rasmussen", "Quinn", "Petrov",
    "Almeida", "Fitzgerald", "Haddad", "Nystrom", "Boateng", "Larsen", "Nguyen",
    "Sorensen", "Ibrahim", "Castellanos", "Vogel", "Mistry", "Ahlgren",
)
_FIRM_STEMS = (
    "Harrow", "Finch", "Blackwood", "Carey", "Dunmore", "Ellery", "Fenwick",
    "Grafton", "Holt", "Ingram", "Jarvis", "Kingsley", "Latham", "Merrick",
    "Norwood", "Oakes", "Pemberton", "Quill", "Rothwell", "Sandford",
)
_ASSET_ADJECTIVES = (
    "Juniper", "Kestrel", "Harbour", "Northfield", "Bellwether", "Ashdown",
    "Copperfield", "Larkfield", "Stonebridge", "Wexford", "Ravenswood",
    "Millbrook", "Elmhurst", "Granary", "Tidewater", "Cranfield",
)
_ASSET_KINDS = (
    "House", "Wharf", "Data Centre", "Business Park", "Warehouse", "Depot",
    "Works", "Terminal", "Campus", "Yard",
)
_ASSET_CLASSES = (
    "freehold property", "leasehold premises", "plant and machinery",
    "intellectual property", "receivables ledger", "vessel", "shareholding",
    "data centre equipment",
)


@dataclass
class NameFactory:
    """De-duplicating name generator driven by one corpus-scoped RNG."""

    rng: random.Random
    _used: set[str] = field(default_factory=set)

    # -- helpers -----------------------------------------------------------
    def _unique(self, build, attempts: int = 4_000) -> str:
        for _ in range(attempts):
            candidate = build()
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate
        raise RuntimeError("name pool exhausted")

    def _combo(self, left: tuple[str, ...], right: tuple[str, ...]) -> str:
        return f"{self.rng.choice(left)}{self.rng.choice(right)}"

    # -- entity names ------------------------------------------------------
    def company(self) -> str:
        def build() -> str:
            stem = self._combo(_COMPANY_STEMS, _COMPANY_TAILS).replace(" ", "")
            kind = self.rng.choice(_COMPANY_KINDS)
            form = self.rng.choice(_LEGAL_FORMS)
            return f"{stem} {kind} {form}"
        return self._unique(build)

    def bank(self) -> str:
        def build() -> str:
            stem = self.rng.choice(_BANK_STEMS)
            kind = self.rng.choice(_BANK_KINDS)
            return f"{stem} {kind} Limited"
        return self._unique(build)

    def person(self) -> str:
        def build() -> str:
            first = self.rng.choice(_FIRST_NAMES)
            last = self.rng.choice(_LAST_NAMES)
            if self.rng.random() < 0.35:
                return f"{first} {self.rng.choice(_FIRST_NAMES)[0]}. {last}"
            return f"{first} {last}"
        return self._unique(build)

    def law_firm(self) -> str:
        def build() -> str:
            first = self.rng.choice(_FIRM_STEMS)
            second = self.rng.choice(_FIRM_STEMS)
            while second == first:
                second = self.rng.choice(_FIRM_STEMS)
            return f"{first} & {second} LLP"
        return self._unique(build)

    def asset(self) -> str:
        def build() -> str:
            return f"{self.rng.choice(_ASSET_ADJECTIVES)} {self.rng.choice(_ASSET_KINDS)}"
        return self._unique(build)

    def town(self) -> str:
        return self.rng.choice(CITIES)

    # -- attributes --------------------------------------------------------
    def sector(self) -> str:
        return self.rng.choice(SECTORS)

    def jurisdiction(self) -> str:
        return self.rng.choice(JURISDICTIONS)

    def court(self) -> str:
        return self.rng.choice(COURTS)

    def date(self, start: dt.date, span_days: int) -> dt.date:
        return start + dt.timedelta(days=self.rng.randrange(span_days))

    def registration(self) -> str:
        return f"{self.rng.randrange(10_000_000, 99_999_999)}"
