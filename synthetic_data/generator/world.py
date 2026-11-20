"""Deterministic fictional corporate/legal world.

The world is the single source of truth for every generated document, gold
annotation and QA answer.  Entity ids and fact ids are stable for a given seed
and are independent of the requested corpus size, which makes cumulative
batches reproducible: the first five documents of a 500-page corpus are the
same five documents a 20-page pilot produced.

Nothing here talks to a model, a database or the network.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any, Iterable

from synth.config import REPO_ROOT
from synth.eval.frame import frame_dict
from synth.ontology import Ontology, load_ontology

DEFAULT_ONTOLOGY_PATH = REPO_ROOT / "configs" / "synth" / "ontology" / "corporate_legal.v1.yaml"

#: Proposition notes that distinguish credit-facility capacity from a loan.
#: A facility increase is a stated capacity, never a completed disbursement.
FACILITY_CAPACITY_ORIGINAL = "facility-capacity-original"
FACILITY_CAPACITY_INCREASE = "facility-capacity-increase"

COMPANY_ROOTS = [
    "Ardenmore", "Brackenwold", "Caldervale", "Dunhollow", "Elderglen",
    "Fenwick", "Greyhaven", "Hollowbrook", "Ironside", "Juniper",
    "Kestrel", "Larkspur", "Marrowgate", "Northcliff", "Oakhaven",
    "Pinehurst", "Quarrystone", "Ravenmoor", "Silverbeck", "Thornbury",
    "Valebrook", "Westmarch", "Yarrowfield", "Zephyr",
]
COMPANY_SUFFIXES = ["Holdings Limited", "Industries Limited", "Group Limited", "Logistics Limited"]

BANK_ROOTS = [
    "Northbank", "Silvergate", "Caldervale", "Dunmore",
    "Eastmere", "Fenwick", "Greyhaven", "Harrowgate",
]
LAW_FIRMS = [
    "Harrow & Finch LLP", "Marlowe & Reed LLP", "Cairn & Sable LLP",
    "Pell & Ward LLP", "Quade & Thorne LLP", "Yarrow & Zell LLP",
]
GOVERNMENT_BODIES = [
    "Calderport Port Authority", "Northern Trade Commission",
    "Eastmere Revenue Office", "Dunhollow Planning Board",
    "Greyhaven Maritime Registry", "Kestrelford Transport Agency",
]
COURTS = [
    "Calderport Commercial Court", "Dunhollow High Court", "Eastmere Crown Court",
]
PLACES = [
    "Calderport", "Dunhollow", "Eastmere", "Fenwick Bay",
    "Greyhaven", "Harrowgate", "Ironside", "Juniper Cross",
    "Kestrelford", "Larkspur", "Marrow Bay", "Northcliff",
]
ASSET_ROOTS = [
    "Kestrel Wharf", "Dunhollow Rail Depot", "Eastmere Terminal",
    "Fenwick Cold Store", "Greyhaven Fleet", "Harrowgate Warehouse",
    "Ironside Foundry", "Juniper Data Centre", "Kestrelford Hangar",
    "Larkspur Quay", "Marrow Bay Marina", "Northcliff Works",
    "Oakhaven Yard", "Pinehurst Silo", "Quarrystone Mill",
    "Ravenmoor Dock", "Silverbeck Plant", "Thornbury Estate",
    "Valebrook Farm", "Westmarch Office Park",
]
#: The ten base instrument titles and the thirty base legal entities they
#: produce are part of the frozen Phase 1 world.  Extra titles are used only by
#: append-only cohorts so existing legal ids keep their original names.
LEGAL_TITLES = [
    "Share Purchase Agreement", "Facility Agreement", "Guarantee Deed",
    "Board Resolution", "Security Agreement", "Loan Note",
    "Services Agreement", "Lease", "Trust Deed", "Settlement Agreement",
]
EXTRA_LEGAL_TITLES = [
    "Debenture", "Shareholder Agreement", "Supply Agreement", "Licence",
    "Power of Attorney", "Side Letter",
]

#: Extra name pools used only by append-only cohorts.  They never affect the
#: first 24 companies / 40 people / etc., so Phase 1 documents stay byte-stable.
EXTRA_COMPANY_ROOTS = [
    "Ashcombe", "Belmont", "Carrow", "Dunmere", "Elmsworth", "Fairhaven",
    "Glenmoor", "Harrowden", "Inglemere", "Jarrow", "Kelbrook", "Lowmoor",
    "Merrivale", "Netherby", "Oakhurst", "Pemberton", "Quintrell", "Redmoor",
    "Stanbrook", "Tarrow", "Uldale", "Vellacott", "Westerby", "Yarmouth",
    "Aldergate", "Barrowfield", "Cresswell", "Draycott", "Eastmoor", "Fallow",
    "Grimsdale", "Hazelmere", "Ivybridge", "Jesmond", "Kirkby", "Lambley",
    "Mossgrove", "Norwood", "Overdale", "Penhurst", "Quarrymere", "Rushmere",
    "Saltmarsh", "Trentvale", "Underhill", "Vicarage", "Whitbourne", "Yewdale",
    "Amberley", "Bexley", "Clifford", "Denholm", "Elmstead", "Farnley",
    "Gorse", "Hampstead", "Inkberrow", "Juniper", "Kingsmoor", "Linden",
    "Maplebury", "Norbury", "Osterley", "Pinewood", "Quenington", "Ravensby",
    "Southwold", "Thurlow", "Upton", "Vinehall", "Wexford", "Yardley",
    "Ashby", "Bramley", "Cavendish", "Dunkeld", "Eversley", "Fingal",
    "Grafton", "Holt", "Ickworth", "Jervis", "Kendal", "Langton",
    "Marlowe", "Neston", "Ormsby", "Priors", "Quill", "Rothbury",
    "Selby", "Tavistock", "Ulverston", "Vernon", "Wilton", "Yelverton",
    "Alderney", "Borrowdale", "Culm", "Dartmoor", "Ennerdale", "Fermanagh",
    "Gower", "Hereford", "Isbourne", "Jura", "Kintyre", "Lorne",
    "Moray", "Nairn", "Orkney", "Pembroke", "Quantock", "Rannoch",
    "Skye", "Trossachs", "Uist", "Vale", "Wear", "Yorkshire",
]

EXTRA_PLACE_NAMES = [
    "Alderport", "Bexhaven", "Carrow Bay", "Denmoor", "Elmford",
    "Fairmouth", "Glenport", "Harrow Bay", "Invermere", "Jarrow Cross",
    "Kelbrook", "Lowmoor", "Merrivale", "Netherby", "Oakhurst",
    "Pemberton", "Quintrell", "Redmoor", "Stanbrook", "Tarrow",
    "Uldale", "Vellacott", "Westerby", "Yarmouth", "Aldergate",
    "Barrowfield", "Cresswell", "Draycott", "Eastmoor", "Fallow",
    "Grimsdale", "Hazelmere", "Ivybridge", "Jesmond", "Kirkby",
    "Lambley", "Mossgrove", "Norwood", "Overdale", "Penhurst",
    "Quarrymere", "Rushmere", "Saltmarsh", "Trentvale", "Underhill",
    "Vicarage", "Whitbourne", "Yewdale", "Amberley", "Bexley",
    "Clifford", "Denholm", "Elmstead", "Farnley", "Gorse",
    "Hampstead", "Inkberrow", "Kingsmoor", "Linden", "Maplebury",
]

EXTRA_BANK_ROOTS = [
    "Alder", "Bexley", "Carrow", "Denmoor", "Elm", "Fair",
    "Glen", "Harrow", "Inver", "Jarrow", "Kel", "Low",
    "Merri", "Nether", "Oak", "Pember", "Quill", "Red",
    "Stan", "Tarrow", "Uldale", "Vella", "Wester", "Yar",
    "Amber", "Borrow", "Culm", "Dart", "Enner", "Ferman",
    "Gower", "Hereford", "Isbourne", "Jura", "Kintyre", "Lorne",
    "Moray", "Nairn", "Orkney", "Pembroke", "Quantock", "Rannoch",
    "Skye", "Trossachs", "Uist", "Vale", "Wear", "Yorkshire",
]

GOVERNMENT_FUNCTIONS = [
    "Revenue Office", "Planning Board", "Maritime Registry",
    "Transport Agency", "Port Authority", "Trade Commission",
    "Companies Registry", "Financial Conduct Office",
]

COURT_TYPES = [
    "Commercial Court", "High Court", "Crown Court", "County Court",
    "Appeal Court", "Arbitration Tribunal",
]

ASSET_TYPES = [
    "Wharf", "Rail Depot", "Terminal", "Cold Store", "Fleet",
    "Warehouse", "Foundry", "Data Centre", "Hangar", "Quay",
    "Marina", "Works", "Yard", "Silo", "Mill", "Dock",
    "Plant", "Estate", "Farm", "Office Park",
]

#: Extra pools used by append-only cohorts.  Their order is part of the
#: deterministic name allocator and must not be reordered without a version bump.
EXTRA_COMPANY_SUFFIXES = [
    "Holdings Limited", "Industries Limited", "Group Limited",
    "Logistics Limited", "Trading Limited", "Partners Limited",
    "Capital Limited", "Marine Limited", "Energy Limited",
    "Property Limited", "Ventures Limited", "Systems Limited",
]
COMPANY_QUALIFIERS = [
    "", "North ", "South ", "East ", "West ", "Central ", "United ", "Royal ",
]
BANK_STYLES = [
    "Bank Limited", "Merchant Bank Limited", "Trust Bank Limited",
    "Commercial Bank Limited", "Private Bank Limited",
    "International Bank Limited",
]
PLACE_SUFFIXES = [
    " Bay", " Cross", " Port", " Moor", " Bridge", " Green", " Hill", " Vale",
]
EXTRA_FIRST_NAMES = [
    "Ada", "Alaric", "Beatrix", "Bertram", "Cassia", "Cedric", "Delia",
    "Dominic", "Edith", "Edmund", "Flora", "Florian", "Greta", "Gustav",
    "Hedda", "Hugo", "Imogen", "Isaac", "Josephine", "Julian", "Katya",
    "Konrad", "Lavinia", "Leopold", "Margot", "Marcus", "Nell", "Nikolai",
    "Olive", "Otto", "Philippa", "Piers", "Rosalind", "Rufus", "Sylvia",
    "Sebastian", "Thea", "Theodore", "Viola", "Walter",
]
EXTRA_LAST_NAMES = [
    "Ashby", "Balfour", "Cavendish", "Delacourt", "Everard", "Fairfax",
    "Grenville", "Hastings", "Ingram", "Jeffreys", "Kingsley", "Langford",
    "Montague", "Neville", "Osborne", "Pemberton", "Ravenscroft",
    "Sinclair", "Tremayne", "Underwood", "Villiers", "Whitlock",
    "Wyndham", "Yardley", "Zouch",
]


@dataclass(frozen=True)
class CohortSpec:
    """One append-only expansion checkpoint.

    Cohorts are cumulative: a 500-page world contains the 100-page cohort and
    the 500-page cohort.  Each cohort appends entities, facts and events; it
    never rewrites an earlier cohort or the base world.
    """

    pages: int
    companies: int = 0
    banks: int = 0
    law_firms: int = 0
    government: int = 0
    courts: int = 0
    places: int = 0
    assets: int = 0
    people: int = 0
    legal_documents: int = 0


COHORT_SPECS = (
    CohortSpec(100, companies=24, banks=4, law_firms=4, government=4,
               courts=2, places=8, assets=16, people=32, legal_documents=16),
    CohortSpec(500, companies=160, banks=20, law_firms=20, government=20,
               courts=10, places=40, assets=80, people=160, legal_documents=80),
    CohortSpec(1000, companies=200, banks=20, law_firms=20, government=20,
               courts=10, places=40, assets=80, people=160, legal_documents=80),
    CohortSpec(5000, companies=2000, banks=80, law_firms=80, government=40,
               courts=20, places=80, assets=400, people=1000, legal_documents=400),
)

BASE_COMPANY_COUNT = 24
BASE_BANK_COUNT = 8
BASE_LAW_FIRM_COUNT = 6
BASE_GOVERNMENT_COUNT = 6
BASE_COURT_COUNT = 3
BASE_PLACE_COUNT = 12
BASE_ASSET_COUNT = 20
BASE_PERSON_COUNT = 40
BASE_LEGAL_DOCUMENT_COUNT = 30

FIRST_NAMES = [
    "Amina", "Bastian", "Calder", "Daria", "Elias", "Farah", "Gideon", "Hana",
    "Ivo", "Juno", "Kellan", "Lena", "Marek", "Nadia", "Oren", "Petra",
    "Quin", "Rhea", "Silas", "Tamsin", "Ulric", "Vera", "Wren", "Xander",
    "Yara", "Zane", "Anika", "Bram", "Cora", "Dorian", "Ewan", "Freya",
    "Galen", "Hollis", "Ines", "Jarrah", "Kira", "Lorne", "Mira", "Nolan",
]
LAST_NAMES = [
    "Alden", "Briar", "Cairn", "Dunmore", "Ellery", "Fenwick", "Garrow",
    "Hale", "Ivers", "Jarvis", "Kerr", "Lorne", "Mercer", "Nyle",
    "Orrick", "Pell", "Quade", "Rook", "Sable", "Thorne", "Ulric",
    "Vale", "Ward", "Yarrow", "Zell",
]


@dataclass(frozen=True)
class Entity:
    """A fictional person, organization, place, asset or legal instrument."""

    entity_id: str
    name: str
    kind: str                 # generator category: person/company/bank/...
    entity_type: str          # ontology class: person/organization/place/...
    subtype: str | None = None
    aliases: tuple[str, ...] = ()
    city: str | None = None
    #: Structured facts about the entity that are not relationships.  Used by
    #: unanswerable-query proofs: an attribute genuinely absent here (and absent
    #: from the relationship index) is not merely missing from the question.
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Fact:
    """A relationship or proposition stated in one or more documents."""

    fact_id: str
    kind: str                       # edge | proposition
    subject_id: str | None
    relation: str | None = None
    object_id: str | None = None
    status: str = "completed"       # completed|proposed|alleged|held|...
    date: dt.date | None = None
    end_date: dt.date | None = None
    amount_gbp: int | None = None
    text: str | None = None         # proposition text
    speaker_id: str | None = None   # person/organization making the statement
    reporter_id: str | None = None  # person reporting someone else's statement
    negated: bool = False
    nested: bool = False
    note: str = ""


@dataclass(frozen=True)
class Event:
    event_id: str
    name: str
    event_date: dt.date
    kind: str
    participant_ids: tuple[str, ...]
    place_id: str | None = None


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    kind: str
    from_id: str
    to_id: str
    amount_gbp: int | None
    date: dt.date
    status: str
    fact_id: str


@dataclass(frozen=True)
class Contradiction:
    contradiction_id: str
    kind: str
    fact_a_id: str
    fact_b_id: str
    description: str


@dataclass
class World:
    seed: int
    ontology: Ontology
    frame: dict[str, Any]
    entities: dict[str, Entity]
    facts: list[Fact]
    events: list[Event]
    transactions: list[Transaction]
    contradictions: list[Contradiction]
    duplicate_names: tuple[str, str]
    alias_person: str
    name_qualifiers: dict[str, str] = field(default_factory=dict)
    _by_kind: dict[str, list[Entity]] = field(default_factory=dict, repr=False)
    _company_facts: dict[int, dict[str, list[Fact]]] = field(default_factory=dict, repr=False)
    _fact_index: dict[str, Fact] = field(default_factory=dict, repr=False)
    _company_index_by_entity: dict[str, int] = field(
        default_factory=dict, repr=False)

    # -- lookup -----------------------------------------------------------------
    def entity(self, entity_id: str) -> Entity:
        try:
            return self.entities[entity_id]
        except KeyError as exc:
            raise KeyError(f"unknown generated entity {entity_id!r}") from exc

    def display_name(self, entity_id: str) -> str:
        """Name used in text and answers, qualified when the name is ambiguous."""
        entity = self.entity(entity_id)
        suffix = self.name_qualifiers.get(entity_id)
        return f"{entity.name} {suffix}".strip() if suffix else entity.name

    def fact(self, fact_id: str) -> Fact:
        if not self._fact_index:
            self._fact_index = {fact.fact_id: fact for fact in self.facts}
        try:
            return self._fact_index[fact_id]
        except KeyError as exc:
            raise KeyError(f"unknown generated fact {fact_id!r}") from exc

    def of_kind(self, kind: str) -> list[Entity]:
        return list(self._by_kind.get(kind, []))

    def companies(self) -> list[Entity]:
        return self.of_kind("company")

    def people(self) -> list[Entity]:
        return self.of_kind("person")

    def banks(self) -> list[Entity]:
        return self.of_kind("bank")

    def law_firms(self) -> list[Entity]:
        return self.of_kind("law_firm")

    def government_bodies(self) -> list[Entity]:
        return self.of_kind("government_body")

    def places(self) -> list[Entity]:
        return self.of_kind("place")

    def assets(self) -> list[Entity]:
        return self.of_kind("asset")

    def legal_documents(self) -> list[Entity]:
        return self.of_kind("legal_document")

    def facts_for_company(self, index: int) -> dict[str, list[Fact]]:
        return self._company_facts[index]

    def company_index(self, entity_id: str) -> int:
        """Return the fact-bundle index for a company entity.

        ``of_kind`` sorts entity ids lexicographically (frozen Phase 1
        behaviour), so a company's position in ``companies()`` is not its
        bundle index once cohort ids such as ``company_100`` are appended.
        The bundle index is recovered from the facts themselves and cached.
        """
        if not self._company_index_by_entity:
            for index, bundle in self._company_facts.items():
                # Every generated company owns at least one asset, so the
                # owns-fact subject identifies its bundle unambiguously.  Do
                # not use object ids: a company can be owned by another.
                for fact in bundle.get("owns", []):
                    if fact.subject_id:
                        self._company_index_by_entity.setdefault(
                            fact.subject_id, index)
        try:
            return self._company_index_by_entity[entity_id]
        except KeyError as exc:
            raise KeyError(
                f"no fact bundle for generated company {entity_id!r}") from exc

    def critical_relation_names(self) -> set[str]:
        return {
            f.relation for f in self.facts
            if f.kind == "edge" and f.relation is not None and f.relation in self.ontology.critical_relations
        }

    def assert_valid(self) -> None:
        """Fail loudly on any ontology or reference violation in the world."""
        for entity in self.entities.values():
            if self.ontology.class_of(entity.entity_type) != entity.entity_type:
                raise ValueError(f"entity {entity.entity_id}: bad class {entity.entity_type!r}")
            if entity.subtype and self.ontology.class_of(entity.subtype) is None:
                raise ValueError(f"entity {entity.entity_id}: bad subtype {entity.subtype!r}")

        seen: set[str] = set()
        for fact in self.facts:
            if fact.fact_id in seen:
                raise ValueError(f"duplicate fact id {fact.fact_id!r}")
            seen.add(fact.fact_id)
            if fact.kind == "edge":
                if not fact.relation or self.ontology.canonical_relation(fact.relation) != fact.relation:
                    raise ValueError(f"fact {fact.fact_id}: bad relation {fact.relation!r}")
                subject = self.entity(fact.subject_id or "")
                target = self.entity(fact.object_id or "")
                relation = self.ontology.relation(fact.relation)
                if relation is None or not relation.allows(
                        self.ontology.class_of(subject.entity_type),
                        self.ontology.class_of(target.entity_type)):
                    raise ValueError(
                        f"fact {fact.fact_id}: {subject.entity_type} -{fact.relation}-> "
                        f"{target.entity_type} is not allowed by the ontology")
            elif fact.kind == "proposition":
                if fact.status not in self.ontology.proposition_status:
                    raise ValueError(f"fact {fact.fact_id}: bad proposition status {fact.status!r}")
                self.entity(fact.subject_id or "")
                if fact.speaker_id:
                    self.entity(fact.speaker_id)
                if fact.reporter_id:
                    self.entity(fact.reporter_id)
                if fact.note in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
                    if fact.amount_gbp is None or fact.date is None:
                        raise ValueError(
                            f"fact {fact.fact_id}: facility capacity needs amount and date")
            else:
                raise ValueError(f"fact {fact.fact_id}: unknown kind {fact.kind!r}")

        for transaction in self.transactions:
            fact = self.fact(transaction.fact_id)
            if transaction.kind != "loan":
                raise ValueError(
                    f"transaction {transaction.transaction_id}: only loans may be transactions")
            if fact.kind != "edge" or fact.relation != "lent_to" or fact.negated:
                raise ValueError(
                    f"transaction {transaction.transaction_id}: fact is not a completed loan")
            if transaction.status != "completed":
                raise ValueError(
                    f"transaction {transaction.transaction_id}: not completed")
            if (transaction.amount_gbp, transaction.date) != (fact.amount_gbp, fact.date):
                raise ValueError(
                    f"transaction {transaction.transaction_id}: amount/date mismatch")
            if (transaction.from_id, transaction.to_id) != (fact.subject_id, fact.object_id):
                raise ValueError(
                    f"transaction {transaction.transaction_id}: party mismatch")

    def content_signature(self) -> dict[str, Any]:
        """Stable, JSON-ready description used by reproducibility tests."""
        return {
            "seed": self.seed,
            "entities": [
                {
                    "id": e.entity_id, "name": e.name, "kind": e.kind,
                    "type": e.entity_type, "subtype": e.subtype,
                    "aliases": list(e.aliases),
                    "attributes": [list(pair) for pair in e.attributes],
                }
                for e in sorted(self.entities.values(), key=lambda x: x.entity_id)
            ],
            "facts": [
                {
                    "id": f.fact_id, "kind": f.kind, "subject": f.subject_id,
                    "relation": f.relation, "object": f.object_id,
                    "status": f.status,
                    "date": f.date.isoformat() if f.date else None,
                    "end_date": f.end_date.isoformat() if f.end_date else None,
                    "amount_gbp": f.amount_gbp, "text": f.text,
                    "speaker": f.speaker_id, "reporter": f.reporter_id,
                    "negated": f.negated, "nested": f.nested, "note": f.note,
                }
                for f in sorted(self.facts, key=lambda x: x.fact_id)
            ],
            "name_qualifiers": dict(sorted(self.name_qualifiers.items())),
            "contradictions": [
                {"id": c.contradiction_id, "a": c.fact_a_id, "b": c.fact_b_id}
                for c in self.contradictions
            ],
        }


# --------------------------------------------------------------------------- builders
def _date(day: dt.date, offset_days: int = 0) -> dt.date:
    return day + dt.timedelta(days=offset_days)


def _stable_people(rng: random.Random) -> list[Entity]:
    combos = [(f, l) for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(combos)
    chosen: list[tuple[str, str]] = []
    used: set[str] = set()
    for first, last in combos:
        name = f"{first} {last}"
        if name not in used:
            chosen.append((first, last))
            used.add(name)
        if len(chosen) == 40:
            break

    # Two different people with exactly the same name: the identity trap.
    chosen[7] = chosen[2]

    people: list[Entity] = []
    for i, (first, last) in enumerate(chosen, start=1):
        subtype = "person"
        if i in (1, 2, 8):
            subtype = "judge"
        elif i in (4, 5, 9):
            subtype = "lawyer"
        aliases: tuple[str, ...] = ()
        if i == 5:
            # A maiden-name style alias.  It deliberately contains no period so
            # the sentence splitter cannot break it into two sentences.
            alias_last = next(c for c in LAST_NAMES
                              if c != last and f"{first} {c}" not in used)
            aliases = (f"{first} {alias_last}",)
            used.add(aliases[0])
        people.append(Entity(
            entity_id=f"person_{i:02d}",
            name=f"{first} {last}",
            kind="person",
            entity_type="person",
            subtype=subtype,
            aliases=aliases,
        ))
    return people


def _company_attributes(index: int) -> tuple[tuple[str, str], ...]:
    """Structured attributes for one company.

    Only a subset of companies has an auditor or a VAT number.  The
    unanswerable-query generator can therefore ask about a company where the
    attribute is genuinely absent while another company in the same corpus
    carries a plausible distractor value.
    """
    attributes: list[tuple[str, str]] = [
        ("jurisdiction", "Scotland" if index % 2 == 0 else "England and Wales"),
    ]
    if index % 3 == 0:
        attributes.append(("auditor", f"Alder Audit {index % 7 + 1} LLP"))
    if index % 4 == 0:
        attributes.append(
            ("vat_registration_number", f"GB {100 + index:03d} {index % 97:02d} 41"))
    if index % 5 == 0:
        attributes.append(("headcount", str(40 + (index * 7) % 260)))
    return tuple(attributes)


def _stable_orgs(rng: random.Random) -> tuple[list[Entity], list[Entity], list[Entity], list[Entity], list[Entity]]:
    roots = list(COMPANY_ROOTS)
    rng.shuffle(roots)
    companies = [
        Entity(
            entity_id=f"company_{i:02d}",
            name=f"{roots[i]} {COMPANY_SUFFIXES[i % len(COMPANY_SUFFIXES)]}",
            kind="company",
            entity_type="organization",
            subtype="company",
            attributes=_company_attributes(i),
        )
        for i in range(24)
    ]
    banks = [
        Entity(
            entity_id=f"bank_{i:02d}",
            name=f"{BANK_ROOTS[i]} Bank Limited",
            kind="bank",
            entity_type="organization",
            subtype="bank",
        )
        for i in range(len(BANK_ROOTS))
    ]
    law_firms = [
        Entity(
            entity_id=f"lawfirm_{i:02d}", name=name, kind="law_firm",
            entity_type="organization", subtype="law_firm",
        )
        for i, name in enumerate(LAW_FIRMS, start=1)
    ]
    government = [
        Entity(
            entity_id=f"gov_{i:02d}", name=name, kind="government_body",
            entity_type="organization", subtype="government_body",
        )
        for i, name in enumerate(GOVERNMENT_BODIES, start=1)
    ]
    courts = [
        Entity(
            entity_id=f"court_{i:02d}", name=name, kind="court",
            entity_type="organization", subtype="court",
        )
        for i, name in enumerate(COURTS, start=1)
    ]
    return companies, banks, law_firms, government, courts


def _stable_places() -> list[Entity]:
    return [
        Entity(entity_id=f"place_{i:02d}", name=name, kind="place",
               entity_type="place", subtype="location")
        for i, name in enumerate(PLACES, start=1)
    ]


def _stable_assets() -> list[Entity]:
    return [
        Entity(entity_id=f"asset_{i:02d}", name=name, kind="asset",
               entity_type="thing", subtype="asset")
        for i, name in enumerate(ASSET_ROOTS, start=1)
    ]


def _stable_legal_documents() -> list[Entity]:
    docs: list[Entity] = []
    n = 1
    for title in LEGAL_TITLES:
        for copy in range(1, 4):
            docs.append(Entity(
                entity_id=f"legal_{n:02d}",
                name=f"{title} LEG-{n:03d}",
                kind="legal_document",
                entity_type="legal",
                subtype="agreement" if "Agreement" in title or "Deed" in title or "Note" in title else "document",
            ))
            n += 1
    return docs


# --------------------------------------------------------------------------- cohorts
class _NameAllocator:
    """Deterministic, append-only unique-name allocator.

    The iterator is consumed once, so a 100-page cohort allocates exactly the
    same names whether the requested corpus is 100, 500 or 5,000 pages.
    """

    def __init__(self, pool: Iterable[str], used: set[str]) -> None:
        self._iterator = iter(pool)
        self._used = used

    def take(self, label: str) -> str:
        for name in self._iterator:
            if name not in self._used:
                self._used.add(name)
                return name
        raise RuntimeError(f"name pool exhausted while allocating {label}")


def _company_name_pool() -> Iterable[str]:
    for qualifier in COMPANY_QUALIFIERS:
        for root in EXTRA_COMPANY_ROOTS:
            for suffix in EXTRA_COMPANY_SUFFIXES:
                yield f"{qualifier}{root} {suffix}"


def _bank_name_pool() -> Iterable[str]:
    for root in EXTRA_BANK_ROOTS:
        for style in BANK_STYLES:
            yield f"{root} {style}"


def _law_firm_name_pool() -> Iterable[str]:
    names = LAST_NAMES + EXTRA_LAST_NAMES
    for first in names:
        for second in names:
            if first != second:
                yield f"{first} & {second} LLP"


def _place_name_pool() -> Iterable[str]:
    for name in EXTRA_PLACE_NAMES:
        yield name
    for root in EXTRA_COMPANY_ROOTS:
        for suffix in PLACE_SUFFIXES:
            yield f"{root}{suffix}"


def _government_name_pool() -> Iterable[str]:
    for place in _place_name_pool():
        for function in GOVERNMENT_FUNCTIONS:
            yield f"{place} {function}"


def _court_name_pool() -> Iterable[str]:
    for place in _place_name_pool():
        for court_type in COURT_TYPES:
            yield f"{place} {court_type}"


def _asset_name_pool() -> Iterable[str]:
    for place in PLACES:
        for asset_type in ASSET_TYPES:
            yield f"{place} {asset_type}"
    for place in _place_name_pool():
        for asset_type in ASSET_TYPES:
            yield f"{place} {asset_type}"


def _person_name_pool() -> Iterable[str]:
    for first in FIRST_NAMES + EXTRA_FIRST_NAMES:
        for last in LAST_NAMES + EXTRA_LAST_NAMES:
            yield f"{first} {last}"


def _legal_name(index: int) -> tuple[str, str]:
    titles = LEGAL_TITLES + EXTRA_LEGAL_TITLES
    title = titles[(index - 1) % len(titles)]
    subtype = ("agreement" if "Agreement" in title or "Deed" in title
               or "Note" in title else "document")
    return f"{title} LEG-{index:03d}", subtype


def _expand_cohorts(
    target_pages: int,
    *,
    companies: list[Entity],
    banks: list[Entity],
    law_firms: list[Entity],
    government: list[Entity],
    courts: list[Entity],
    places: list[Entity],
    assets: list[Entity],
    people: list[Entity],
    legal_documents: list[Entity],
    entities: dict[str, Entity],
    facts: list[Fact],
    company_facts: dict[int, dict[str, list[Fact]]],
    events: list[Event],
    transactions: list[Transaction],
) -> None:
    """Append cumulative cohorts up to ``target_pages``.

    Every cohort appends new entity ids after the existing ones and never
    rewrites a name, fact or assignment from an earlier cohort.  Fact ids use a
    separate ``cfact_`` namespace so the frozen ``fact_NNNN`` ids are untouched.
    """
    # Reserve aliases as well as canonical names: a new canonical name must
    # never shadow an existing entity's alias (e.g. person_05 is also known as
    # "Petra Alden").  Without this, large cohorts can allocate that alias as a
    # different person's display name and make identity resolution ambiguous.
    used_names = {
        name
        for entity in entities.values()
        for name in (entity.name, *entity.aliases)
    }
    allocators = {
        "company": _NameAllocator(_company_name_pool(), used_names),
        "bank": _NameAllocator(_bank_name_pool(), used_names),
        "law_firm": _NameAllocator(_law_firm_name_pool(), used_names),
        "government": _NameAllocator(_government_name_pool(), used_names),
        "court": _NameAllocator(_court_name_pool(), used_names),
        "place": _NameAllocator(_place_name_pool(), used_names),
        "asset": _NameAllocator(_asset_name_pool(), used_names),
        "person": _NameAllocator(_person_name_pool(), used_names),
    }
    cfact_counter = 1
    txn_counter = len(transactions) + 1
    base_date = dt.date(2022, 1, 10)

    def next_cfact() -> str:
        nonlocal cfact_counter
        value = f"cfact_{cfact_counter:06d}"
        cfact_counter += 1
        return value

    def add_fact(ci: int, key: str, **kwargs: Any) -> Fact:
        fact = Fact(fact_id=next_cfact(), **kwargs)
        facts.append(fact)
        company_facts.setdefault(ci, {}).setdefault(key, []).append(fact)
        return fact

    def append_entity(kind: str, entity: Entity) -> None:
        if entity.entity_id in entities:
            raise ValueError(f"duplicate cohort entity id {entity.entity_id!r}")
        entities[entity.entity_id] = entity

    for spec in COHORT_SPECS:
        if target_pages < spec.pages:
            break
        start_companies = len(companies)
        start_facts = len(facts)

        for _ in range(spec.companies):
            index = len(companies)
            name = allocators["company"].take(f"company_{index + 1:02d}")
            entity = Entity(
                entity_id=f"company_{index + 1:02d}", name=name, kind="company",
                entity_type="organization", subtype="company",
                attributes=_company_attributes(index),
            )
            companies.append(entity)
            append_entity("company", entity)
            company_facts.setdefault(index, {})

        for _ in range(spec.banks):
            index = len(banks)
            entity = Entity(
                entity_id=f"bank_{index + 1:02d}",
                name=allocators["bank"].take(f"bank_{index + 1:02d}"),
                kind="bank", entity_type="organization", subtype="bank",
            )
            banks.append(entity)
            append_entity("bank", entity)

        for _ in range(spec.law_firms):
            index = len(law_firms)
            entity = Entity(
                entity_id=f"lawfirm_{index + 1:02d}",
                name=allocators["law_firm"].take(f"lawfirm_{index + 1:02d}"),
                kind="law_firm", entity_type="organization", subtype="law_firm",
            )
            law_firms.append(entity)
            append_entity("law_firm", entity)

        for _ in range(spec.government):
            index = len(government)
            entity = Entity(
                entity_id=f"gov_{index + 1:02d}",
                name=allocators["government"].take(f"gov_{index + 1:02d}"),
                kind="government_body", entity_type="organization",
                subtype="government_body",
            )
            government.append(entity)
            append_entity("government_body", entity)

        for _ in range(spec.courts):
            index = len(courts)
            entity = Entity(
                entity_id=f"court_{index + 1:02d}",
                name=allocators["court"].take(f"court_{index + 1:02d}"),
                kind="court", entity_type="organization", subtype="court",
            )
            courts.append(entity)
            append_entity("court", entity)

        for _ in range(spec.places):
            index = len(places)
            entity = Entity(
                entity_id=f"place_{index + 1:02d}",
                name=allocators["place"].take(f"place_{index + 1:02d}"),
                kind="place", entity_type="place", subtype="location",
            )
            places.append(entity)
            append_entity("place", entity)

        for _ in range(spec.assets):
            index = len(assets)
            entity = Entity(
                entity_id=f"asset_{index + 1:02d}",
                name=allocators["asset"].take(f"asset_{index + 1:02d}"),
                kind="asset", entity_type="thing", subtype="asset",
            )
            assets.append(entity)
            append_entity("asset", entity)

        for _ in range(spec.people):
            index = len(people)
            subtype = "person"
            if index % 17 == 0:
                subtype = "judge"
            elif index % 11 == 0:
                subtype = "lawyer"
            entity = Entity(
                entity_id=f"person_{index + 1:02d}",
                name=allocators["person"].take(f"person_{index + 1:02d}"),
                kind="person", entity_type="person", subtype=subtype,
            )
            people.append(entity)
            append_entity("person", entity)

        for _ in range(spec.legal_documents):
            index = len(legal_documents)
            name, subtype = _legal_name(index + 1)
            entity = Entity(
                entity_id=f"legal_{index + 1:02d}", name=name,
                kind="legal_document", entity_type="legal", subtype=subtype,
            )
            legal_documents.append(entity)
            append_entity("legal_document", entity)

        # Facts are created after the whole cohort's entities exist, so every
        # reference resolves inside the cohort and later cohorts cannot affect it.
        for ci in range(start_companies, len(companies)):
            company = companies[ci]
            if ci % 4 != 3 and ci + 1 < len(companies):
                target = companies[ci + 1]
                add_fact(ci, "owns", kind="edge", subject_id=company.entity_id,
                         relation="owns", object_id=target.entity_id,
                         date=base_date + dt.timedelta(days=(ci * 17) % 1700))
            else:
                target = assets[(ci + 3) % len(assets)]
                add_fact(ci, "owns", kind="edge", subject_id=company.entity_id,
                         relation="owns", object_id=target.entity_id,
                         date=base_date + dt.timedelta(days=(ci * 17) % 1700))

            shareholder = people[(ci + 8) % len(people)]
            add_fact(ci, "shareholder", kind="edge",
                     subject_id=shareholder.entity_id,
                     relation="shareholder_of", object_id=company.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 13) % 1700))

            chair = people[(ci + 3) % len(people)]
            add_fact(ci, "chair", kind="edge", subject_id=chair.entity_id,
                     relation="chair_of", object_id=company.entity_id,
                     date=dt.date(2021, 1, 1))

            bank = banks[ci % len(banks)]
            loan_date = base_date + dt.timedelta(days=30 + (ci * 11) % 1700)
            add_fact(ci, "loan", kind="edge", subject_id=bank.entity_id,
                     relation="lent_to", object_id=company.entity_id,
                     date=loan_date, amount_gbp=(12 + (ci % 20) * 2) * 1_000_000)
            add_fact(ci, "owes", kind="edge", subject_id=company.entity_id,
                     relation="owes", object_id=bank.entity_id,
                     date=loan_date + dt.timedelta(days=5),
                     amount_gbp=(4 + ci % 10) * 1_000_000)

            guarantor = companies[(ci + 2) % len(companies)]
            if guarantor.entity_id == company.entity_id:
                guarantor = companies[(ci + 3) % len(companies)]
            add_fact(ci, "guarantee", kind="edge",
                     subject_id=guarantor.entity_id, relation="guarantor_for",
                     object_id=company.entity_id,
                     date=loan_date + dt.timedelta(days=1))

            law_firm = law_firms[ci % len(law_firms)]
            add_fact(ci, "represents", kind="edge", subject_id=law_firm.entity_id,
                     relation="represents", object_id=company.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 7) % 1700))

            employee = people[(ci + 10) % len(people)]
            add_fact(ci, "employed_by", kind="edge", subject_id=employee.entity_id,
                     relation="employed_by", object_id=company.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 3) % 1700))

            director = people[(ci + 5) % len(people)]
            add_fact(ci, "director", kind="edge", subject_id=director.entity_id,
                     relation="director_of", object_id=company.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 3) % 1700))

            asset = assets[(ci + 7) % len(assets)]
            add_fact(ci, "asset_own", kind="edge", subject_id=company.entity_id,
                     relation="owns", object_id=asset.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 19) % 1700))
            place = places[(ci + 7) % len(places)]
            add_fact(ci, "asset_loc", kind="edge", subject_id=asset.entity_id,
                     relation="located_in", object_id=place.entity_id)

            legal = legal_documents[ci % len(legal_documents)]
            add_fact(ci, "legal_party", kind="edge", subject_id=company.entity_id,
                     relation="party_to", object_id=legal.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 5) % 1700))
            signer = people[(ci + 1) % len(people)]
            add_fact(ci, "legal_signed", kind="edge", subject_id=signer.entity_id,
                     relation="signed", object_id=legal.entity_id,
                     date=base_date + dt.timedelta(days=(ci * 5) % 1700))

            kin_a = people[ci % len(people)]
            kin_b = people[(ci + 1) % len(people)]
            add_fact(ci, "family", kind="edge", subject_id=kin_a.entity_id,
                     relation="family_of", object_id=kin_b.entity_id)

            # New fact families, deliberately not stated in the pilot documents.
            if ci % 2 == 0:
                parent = companies[(ci + 3) % len(companies)]
                if parent.entity_id != company.entity_id:
                    add_fact(ci, "subsidiary", kind="edge",
                             subject_id=company.entity_id,
                             relation="subsidiary_of",
                             object_id=parent.entity_id)
            if ci % 3 == 0:
                lawyers = [person for person in people
                           if person.subtype == "lawyer"]
                lawyer = lawyers[(ci // 3) % len(lawyers)]
                add_fact(ci, "partner", kind="edge",
                         subject_id=lawyer.entity_id, relation="partner_at",
                         object_id=law_firms[(ci + 1) % len(law_firms)].entity_id)
            if ci % 4 == 0:
                member = people[(ci + 12) % len(people)]
                add_fact(ci, "member", kind="edge",
                         subject_id=member.entity_id, relation="member_of",
                         object_id=government[ci % len(government)].entity_id)
            if ci % 5 == 0:
                judges = [person for person in people
                          if person.subtype == "judge"]
                judge = judges[(ci // 5) % len(judges)]
                add_fact(ci, "judge", kind="edge", subject_id=judge.entity_id,
                         relation="judge_of",
                         object_id=legal_documents[(ci + 9) % len(legal_documents)].entity_id)
            if ci % 6 == 0:
                add_fact(ci, "cites", kind="edge",
                         subject_id=legal_documents[ci % len(legal_documents)].entity_id,
                         relation="cites",
                         object_id=legal_documents[(ci + 1) % len(legal_documents)].entity_id)
            if ci % 7 == 0:
                add_fact(ci, "paid", kind="edge", subject_id=company.entity_id,
                         relation="paid",
                         object_id=law_firms[(ci + 1) % len(law_firms)].entity_id,
                         date=base_date + dt.timedelta(days=(ci * 13) % 1700),
                         amount_gbp=(1 + ci % 9) * 25_000,
                         note="professional fees")

        for ci in range(start_companies, len(companies)):
            company = companies[ci]
            events.append(Event(
                event_id=f"event_{len(events) + 1:03d}",
                name=f"Board meeting of {company.name}",
                event_date=(base_date + dt.timedelta(days=45 + (ci * 21) % 1700)),
                kind="board_meeting",
                participant_ids=(people[ci % len(people)].entity_id,
                                 people[(ci + 3) % len(people)].entity_id),
                place_id=places[ci % len(places)].entity_id,
            ))

        for fact in facts[start_facts:]:
            if (fact.kind != "edge" or fact.relation != "lent_to"
                    or fact.negated or fact.amount_gbp is None or fact.date is None):
                continue
            transactions.append(Transaction(
                transaction_id=f"txn_{txn_counter:04d}",
                kind="loan",
                from_id=fact.subject_id or "",
                to_id=fact.object_id or "",
                amount_gbp=fact.amount_gbp,
                date=fact.date,
                status="completed",
                fact_id=fact.fact_id,
            ))
            txn_counter += 1


def build_world(seed: int = 7, ontology: Ontology | None = None,
                ontology_path: str | None = None,
                target_pages: int = 20) -> World:
    """Build a deterministic fictional world.

    The same ``seed`` always produces the same base entities, facts and ids.
    Requested corpus sizes at or above a cohort checkpoint append new entities
    and facts after the base world; they never change the base or an earlier
    cohort.
    """
    path = ontology_path or str(DEFAULT_ONTOLOGY_PATH)
    ontology = ontology or load_ontology(path)
    frame = frame_dict(path)
    rng = random.Random(seed)

    people = _stable_people(rng)
    companies, banks, law_firms, government, courts = _stable_orgs(rng)
    places = _stable_places()
    assets = _stable_assets()
    legal_documents = _stable_legal_documents()

    entities: dict[str, Entity] = {}
    for group in (people, companies, banks, law_firms, government, courts, places, assets, legal_documents):
        for entity in group:
            if entity.entity_id in entities:
                raise ValueError(f"duplicate generated entity id {entity.entity_id!r}")
            entities[entity.entity_id] = entity

    facts: list[Fact] = []
    company_facts: dict[int, dict[str, list[Fact]]] = {i: {} for i in range(len(companies))}
    counter = 1

    def next_id(prefix: str = "fact") -> str:
        nonlocal counter
        value = f"{prefix}_{counter:04d}"
        counter += 1
        return value

    def add_for_company(ci: int, key: str, **kwargs: Any) -> Fact:
        fact = Fact(fact_id=next_id(), **kwargs)
        facts.append(fact)
        company_facts[ci].setdefault(key, []).append(fact)
        return fact

    base_date = dt.date(2022, 1, 10)

    for i, company in enumerate(companies):
        # Ownership: a chain of four companies, then an asset at the end.
        if i % 4 != 3:
            target = companies[i + 1]
            add_for_company(i, "owns", kind="edge", subject_id=company.entity_id,
                            relation="owns", object_id=target.entity_id,
                            date=_date(base_date, i * 17))
        else:
            target = assets[i % len(assets)]
            add_for_company(i, "owns", kind="edge", subject_id=company.entity_id,
                            relation="owns", object_id=target.entity_id,
                            date=_date(base_date, i * 17))

        person = people[(i + 8) % len(people)]
        add_for_company(i, "shareholder", kind="edge", subject_id=person.entity_id,
                        relation="shareholder_of", object_id=company.entity_id,
                        date=_date(base_date, i * 13))

        chair = people[i % len(people)]
        if i < 6:
            old_chair = add_for_company(i, "chair", kind="edge", subject_id=chair.entity_id,
                                        relation="chair_of", object_id=company.entity_id,
                                        date=dt.date(2021, 1, 1), end_date=dt.date(2023, 6, 30))
            new_chair = people[(i + 20) % len(people)]
            add_for_company(i, "chair", kind="edge", subject_id=new_chair.entity_id,
                            relation="chair_of", object_id=company.entity_id,
                            date=dt.date(2023, 7, 1), note=f"replaced {old_chair.fact_id}")
        else:
            add_for_company(i, "chair", kind="edge", subject_id=chair.entity_id,
                            relation="chair_of", object_id=company.entity_id,
                            date=dt.date(2021, 1, 1))

        bank = banks[i % len(banks)]
        loan_date = _date(base_date, 30 + i * 11)
        add_for_company(i, "loan", kind="edge", subject_id=bank.entity_id,
                        relation="lent_to", object_id=company.entity_id,
                        date=loan_date, amount_gbp=(12 + i * 2) * 1_000_000)

        add_for_company(i, "owes", kind="edge", subject_id=company.entity_id,
                        relation="owes", object_id=bank.entity_id,
                        date=_date(loan_date, 5), amount_gbp=(4 + i) * 1_000_000)

        guarantor = companies[(i + 2) % len(companies)]
        add_for_company(i, "guarantee", kind="edge", subject_id=guarantor.entity_id,
                        relation="guarantor_for", object_id=company.entity_id,
                        date=_date(loan_date, 1))

        law_firm = law_firms[i % len(law_firms)]
        add_for_company(i, "represents", kind="edge", subject_id=law_firm.entity_id,
                        relation="represents", object_id=company.entity_id,
                        date=_date(base_date, i * 7))

        employee = people[(i + 10) % len(people)]
        add_for_company(i, "employed_by", kind="edge", subject_id=employee.entity_id,
                        relation="employed_by", object_id=company.entity_id,
                        date=_date(base_date, i * 3))

        director = people[(i + 3) % len(people)]
        add_for_company(i, "director", kind="edge", subject_id=director.entity_id,
                        relation="director_of", object_id=company.entity_id,
                        date=_date(base_date, i * 3))

        asset = assets[(i + 7) % len(assets)]
        add_for_company(i, "asset_own", kind="edge", subject_id=company.entity_id,
                        relation="owns", object_id=asset.entity_id,
                        date=_date(base_date, i * 19))
        place = places[(i + 7) % len(places)]
        add_for_company(i, "asset_loc", kind="edge", subject_id=asset.entity_id,
                        relation="located_in", object_id=place.entity_id)

        legal = legal_documents[i % len(legal_documents)]
        add_for_company(i, "legal_party", kind="edge", subject_id=company.entity_id,
                        relation="party_to", object_id=legal.entity_id,
                        date=_date(base_date, i * 5))
        signer = people[(i + 1) % len(people)]
        add_for_company(i, "legal_signed", kind="edge", subject_id=signer.entity_id,
                        relation="signed", object_id=legal.entity_id,
                        date=_date(base_date, i * 5))

        relative = people[i % len(people)]
        kin = people[(i + 1) % len(people)]
        add_for_company(i, "family", kind="edge", subject_id=relative.entity_id,
                        relation="family_of", object_id=kin.entity_id)

    # Changing facility capacity: two dated propositions, not loan disbursements.
    # A credit facility increase is a stated capacity, never a completed loan.
    add_for_company(1, "facility", kind="proposition",
                    subject_id=companies[1].entity_id, status="stated",
                    date=dt.date(2022, 6, 1), amount_gbp=20_000_000,
                    text=(f"{banks[1].name} provided a facility of GBP 20 million to "
                          f"{companies[1].name}"),
                    note=FACILITY_CAPACITY_ORIGINAL)
    add_for_company(1, "facility", kind="proposition",
                    subject_id=companies[1].entity_id, status="stated",
                    date=dt.date(2023, 6, 1), amount_gbp=35_000_000,
                    text=(f"{banks[1].name} increased the facility available to "
                          f"{companies[1].name} to GBP 35 million"),
                    note=FACILITY_CAPACITY_INCREASE)

    # A genuine cross-document contradiction: same parties and date, two amounts.
    conflict_a = Fact(
        fact_id="fact_conflict_42m", kind="edge", subject_id=banks[0].entity_id,
        relation="lent_to", object_id=companies[0].entity_id,
        date=dt.date(2024, 1, 15), amount_gbp=42_000_000,
        note="contradicts fact_conflict_55m",
    )
    conflict_b = Fact(
        fact_id="fact_conflict_55m", kind="edge", subject_id=banks[0].entity_id,
        relation="lent_to", object_id=companies[0].entity_id,
        date=dt.date(2024, 1, 15), amount_gbp=55_000_000,
        note="contradicts fact_conflict_42m",
    )
    facts.extend([conflict_a, conflict_b])
    contradictions = [Contradiction(
        contradiction_id="contradiction_loan_01",
        kind="loan_amount",
        fact_a_id=conflict_a.fact_id,
        fact_b_id=conflict_b.fact_id,
        description="The same loan is stated as GBP 42 million in one document and GBP 55 million in another.",
    )]

    transactions: list[Transaction] = []
    txn_counter = 1
    for fact in facts:
        # Only completed loans produce transactions.  An outstanding balance
        # (``owes``) is not a payment, and a facility-capacity proposition is not
        # a disbursement.
        if (fact.kind != "edge" or fact.relation != "lent_to"
                or fact.negated or fact.amount_gbp is None or fact.date is None):
            continue
        transactions.append(Transaction(
            transaction_id=f"txn_{txn_counter:04d}",
            kind="loan",
            from_id=fact.subject_id or "",
            to_id=fact.object_id or "",
            amount_gbp=fact.amount_gbp,
            date=fact.date,
            status="completed",
            fact_id=fact.fact_id,
        ))
        txn_counter += 1

    events: list[Event] = []
    for i, company in enumerate(companies):
        events.append(Event(
            event_id=f"event_{i + 1:03d}",
            name=f"Board meeting of {company.name}",
            event_date=_date(base_date, 45 + i * 21),
            kind="board_meeting",
            participant_ids=(people[i % len(people)].entity_id,
                             people[(i + 3) % len(people)].entity_id),
            place_id=places[i % len(places)].entity_id,
        ))
    for i in range(8):
        events.append(Event(
            event_id=f"event_{len(events) + 1:03d}",
            name=f"Signing of acquisition {i + 1}",
            event_date=_date(base_date, 90 + i * 31),
            kind="signing",
            participant_ids=(people[i % len(people)].entity_id,
                             people[(i + 3) % len(people)].entity_id),
            place_id=places[(i + 1) % len(places)].entity_id,
        ))

    _expand_cohorts(
        target_pages,
        companies=companies,
        banks=banks,
        law_firms=law_firms,
        government=government,
        courts=courts,
        places=places,
        assets=assets,
        people=people,
        legal_documents=legal_documents,
        entities=entities,
        facts=facts,
        company_facts=company_facts,
        events=events,
        transactions=transactions,
    )

    world = World(
        seed=seed,
        ontology=ontology,
        frame=frame,
        entities=entities,
        facts=facts,
        events=events,
        transactions=transactions,
        contradictions=contradictions,
        # chosen[7] was replaced by chosen[2], so the 1-based people 3 and 8
        # genuinely share a full name.  They remain distinct entities.
        duplicate_names=(people[2].entity_id, people[7].entity_id),
        alias_person=people[4].entity_id,
        name_qualifiers={
            people[2].entity_id: "of the Treasury Department",
            people[7].entity_id: "of the Compliance Department",
        },
        _by_kind={},
        _company_facts=company_facts,
    )
    by_kind: dict[str, list[Entity]] = {}
    for entity in entities.values():
        by_kind.setdefault(entity.kind, []).append(entity)
    for values in by_kind.values():
        values.sort(key=lambda e: e.entity_id)
    world._by_kind = by_kind

    world.assert_valid()
    return world
