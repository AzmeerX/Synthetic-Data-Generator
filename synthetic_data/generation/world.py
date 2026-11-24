"""Deterministic semantic world: one matter per group of documents.

A *matter* is the semantic unit behind a group of documents: one group of
companies, one facility, one dispute, one board, one correspondence thread.  All
seven document types are planned from the same matter, so a repayment stated in
the facility agreement, the due-diligence report, the workbook and a demand
letter is **one** :class:`SemanticFact` with one id (architecture §3.2).

The world is built before any document, and documents are pure functions of
``(world, ordinal)``.  No fact is created without at least one document type
assigned to state it (``fact_roles``), so ``Corpus`` cannot contain an orphan
fact.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

from synthetic_data.model import (
    Condition,
    Contradiction,
    CorpusWorld,
    CrossReference,
    Entity,
    EntityRef,
    Event,
    IsoDate,
    MessageMeta,
    Money,
    Percentage,
    SemanticFact,
    Thread,
    TimeScope,
    Transaction,
)

from .config import CORE_TYPES, CorpusConfig, matter_sizes
from .ids import IdAllocator
from .rng import pick, sample, scope_rng, shuffled
from .vocab import NameFactory

__all__ = [
    "Shareholding",
    "ThreadShape",
    "Matter",
    "WorldDraft",
    "build_world",
    "contradiction_schedule",
]

#: contradictory document-type pairs; the schedule walks this table so the same
#: pair is not reused until every pair has been used (B6 / DEC-5)
CONTRADICTION_PAIRS: tuple[tuple[str, str], ...] = (
    ("financial_agreement", "due_diligence"),
    ("board_minutes", "memorandum"),
    ("case_notes", "press_release"),
    ("due_diligence", "memorandum"),
    ("correspondence", "financial_agreement"),
    ("board_minutes", "due_diligence"),
    ("case_notes", "memorandum"),
    ("press_release", "board_minutes"),
    ("correspondence", "case_notes"),
    ("due_diligence", "press_release"),
    ("financial_agreement", "memorandum"),
    ("case_notes", "financial_agreement"),
)
CONTRADICTION_DIMENSIONS = ("amount", "date", "status", "relationship")

#: per-document family caps.  A document may not carry more transaction facts
#: than §11 allows for its type even though the world holds more.
_FACT_CAPS: dict[str, dict[str, int]] = {
    "due_diligence": {"repayment": 2},
    "correspondence": {"repayment": 2, "drawdown": 0},
    "board_minutes": {"drawdown": 2},
    "memorandum": {"repayment": 0, "drawdown": 0},
    "case_notes": {"repayment": 0, "drawdown": 0},
    "press_release": {"repayment": 0, "drawdown": 0},
}


def _cap_families(fact_ids: list[str], family: dict[str, str],
                  caps: dict[str, int]) -> list[str]:
    if not caps:
        return fact_ids
    kept: list[str] = []
    counts: dict[str, int] = {}
    for fact_id in fact_ids:
        name = family[fact_id]
        if name in caps:
            if counts.get(name, 0) >= caps[name]:
                continue
            counts[name] = counts.get(name, 0) + 1
        kept.append(fact_id)
    return kept

_THREAD_SUBJECTS = (
    "Covenant compliance certificate",
    "Facility drawdown request",
    "Notice of default under the facility",
    "Request for information",
    "Proposed amendment to the facility",
    "Settlement proposal",
)

_ALLEGATIONS = (
    "the reported revenue for the year was overstated by {amt}",
    "a related-party loan of {amt} was not disclosed in the statutory accounts",
    "the valuation of the charged assets was prepared on an unsupported basis",
    "the borrower failed to disclose a material adverse change before drawdown",
    "the transfer of the data centre was executed without board approval",
    "management bonuses of {amt} were paid without the committee's authorisation",
)
_FINDINGS = (
    "the disclosures made by {company} were materially accurate",
    "the related-party balance of {company} was disclosed in note 24 of the accounts",
    "the valuation methodology used for {company} was consistent with the previous year",
    "the covenant certificate for {company} was delivered on time",
    "the board minute of {company} recorded the acquisition approval",
)
_RECOMMENDATIONS = (
    "the lender should obtain an updated compliance certificate from {company} before the next drawdown",
    "the group should document the related-party balance of {company} in the next quarterly pack",
    "the borrower should appoint an independent valuer for the charged assets of {company}",
    "the committee should review the treasury policy applying to {company} annually",
)
_COVENANTS = (
    "the Borrower maintained a leverage ratio below {ratio}:1 throughout the period",
    "interest cover remained above {cover}:1 for the year",
    "the Borrower kept net debt below {amt} at each quarter end",
    "the group maintained minimum liquidity of {amt} throughout the period",
)
_RESOLUTIONS = (
    "the facility agreement with {lender} be approved and the directors be authorised to execute it",
    "the acquisition of {target} for {amt} be approved",
    "the group treasury policy be adopted with immediate effect",
    "the disposal of the non-core logistics unit be authorised",
    "the related-party loan to {sub} be approved on the terms circulated",
    "the re-appointment of the auditor be recommended to shareholders",
)
_APPOINTMENTS = (
    "the appointment of {person} as a director with effect from {date} be confirmed",
    "the resignation of {person} as company secretary be noted",
    "the appointment of {person} as chair of the audit committee be approved",
    "the resignation of {person} as a director at the conclusion of the meeting be noted",
)
_REPRESENTATIONS = (
    "the group's consolidated accounts for the year ended 31 December {year} were prepared in accordance with the applicable accounting standards",
    "the Borrower had good and marketable title to the Charged Assets at the date of this agreement",
    "no event of default had occurred and was continuing at the date of the last compliance certificate",
    "the information supplied to the Lender in connection with the facility of {amount} was accurate in all material respects",
    "the Borrower held all licences necessary for the operation of its business in {jurisdiction}",
)


@dataclass(frozen=True)
class Shareholding:
    parent_id: str
    child_id: str
    percent: Percentage
    fact_id: str


@dataclass(frozen=True)
class ThreadShape:
    """Shape of one correspondence thread: lengths, reply style, correction slot."""

    lengths: tuple[int, ...]
    style: str                  # "chain" or "fan"
    correction_at: int | None   # 1-based position within its thread, or None

    def identity(self) -> tuple:
        return (sum(self.lengths), self.style, self.correction_at)


#: shape pools by letter count, so a matter with k letters can host a realistic
#: mix of single letters, chains and fan-outs
_THREAD_SHAPES: dict[int, tuple[tuple[int, ...], ...]] = {
    1: ((1,),),
    2: ((2,), (1, 1)),
    3: ((3,), (2, 1), (1, 1, 1)),
    4: ((4,), (3, 1), (2, 2), (2, 1, 1)),
    5: ((5,), (4, 1), (3, 2), (3, 1, 1), (2, 2, 1)),
}


@dataclass
class Matter:
    ordinal: int
    doc_ids: list[str]
    doc_types: list[str]
    doc_by_type: dict[str, list[str]]
    dates: dict[str, IsoDate]
    entities: dict[str, Entity]
    entity_roles: dict[str, str]
    facts: dict[str, SemanticFact]
    fact_roles: dict[str, set[str]]
    fact_family: dict[str, str]
    conditions: dict[str, Condition]
    events: dict[str, Event]
    transactions: dict[str, Transaction]
    threads: list[Thread]
    contradictions: list[Contradiction]
    cross_refs: list[CrossReference]
    shareholdings: list[Shareholding]
    letter_meta: dict[str, MessageMeta]
    exhibits: dict[str, str]              # letter doc id -> exhibit id
    contradiction_side: dict[str, str]    # doc type -> fact id it must state
    ctx: dict[str, str]
    typed: dict[str, object] = field(default_factory=dict)
    #: exact fact list each document states, so coverage is planned, not incidental
    doc_facts: dict[str, list[str]] = field(default_factory=dict)

    # -- lookups -----------------------------------------------------------
    def doc_id(self, doc_type: str, index: int = 0) -> str:
        ids = self.doc_by_type.get(doc_type, [])
        if not ids:
            raise KeyError(f"matter {self.ordinal} has no {doc_type} document")
        return ids[index % len(ids)]

    def facts_for(self, doc_type: str) -> list[str]:
        return [fid for fid, roles in self.fact_roles.items() if doc_type in roles]

    def by_family(self, doc_type: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for fact_id in self.facts_for(doc_type):
            out.setdefault(self.fact_family[fact_id], []).append(fact_id)
        return out


@dataclass
class WorldDraft:
    config: CorpusConfig
    corpus_id: str
    matters: list[Matter]
    alloc: IdAllocator
    #: (document_id, doc_type, matter ordinal) in corpus order
    document_slots: list[tuple[str, str, int]]
    contradiction_schedule: list[tuple[int, str, str, str]]   # (matter, type_a, type_b, dimension)

    def world(self) -> CorpusWorld:
        entities: dict[str, Entity] = {}
        facts: dict[str, SemanticFact] = {}
        conditions: dict[str, Condition] = {}
        events: dict[str, Event] = {}
        transactions: dict[str, Transaction] = {}
        contradictions: dict[str, Contradiction] = {}
        threads: dict[str, Thread] = {}
        cross_refs: list[CrossReference] = []
        for matter in self.matters:
            for bundle in (matter.entities, matter.facts, matter.conditions,
                           matter.events, matter.transactions):
                for key, value in bundle.items():
                    if key in entities or key in facts or key in conditions \
                            or key in events or key in transactions:
                        raise AssertionError(f"duplicate semantic id {key}")
            entities.update(matter.entities)
            facts.update(matter.facts)
            conditions.update(matter.conditions)
            events.update(matter.events)
            transactions.update(matter.transactions)
            for contradiction in matter.contradictions:
                contradictions[contradiction.contradiction_id] = contradiction
            for thread in matter.threads:
                threads[thread.thread_id] = thread
            cross_refs.extend(matter.cross_refs)
        return CorpusWorld(
            corpus_id=self.corpus_id,
            seed=self.config.seed,
            entities=entities,
            facts=facts,
            conditions=conditions,
            events=events,
            transactions=transactions,
            contradictions=contradictions,
            threads=threads,
            cross_references=cross_refs,
        )


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------
def contradiction_schedule(config: CorpusConfig, sizes: list[int] | None = None
                           ) -> list[tuple[int, str, str, str]]:
    """Deterministic assignment of contradictions to matters.

    Density is one grounded contradiction per 50 documents, rounded up
    (architecture §11.3).  Pairs walk :data:`CONTRADICTION_PAIRS` so no pair
    repeats until the table is exhausted, and dimensions rotate so no dimension
    exceeds 40% of a corpus at the checkpoints the gate names.
    """
    if not config.include_contradictions:
        return []
    total_documents = config.documents + config.workbooks
    count = -(-total_documents // 50)          # ceil
    sizes = sizes or matter_sizes(config.documents)
    count = min(count, len(sizes))
    key = hashlib.blake2b(
        f"{config.seed}:{config.corpus_name}:contradiction".encode("utf-8"),
        digest_size=16).digest()
    rng = scope_rng(key, "schedule")
    pairs = list(CONTRADICTION_PAIRS)
    rng.shuffle(pairs)
    dimensions = list(CONTRADICTION_DIMENSIONS)
    schedule: list[tuple[int, str, str, str]] = []
    for index in range(count):
        type_a, type_b = pairs[index % len(pairs)]
        dimension = dimensions[index % len(dimensions)]
        # spread contradictions over the corpus rather than clustering them
        matter_index = (index * max(1, len(sizes) // count)) % len(sizes)
        schedule.append((matter_index, type_a, type_b, dimension))
    return schedule


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------
class _MatterBuilder:
    def __init__(self, config: CorpusConfig, alloc: IdAllocator, names: NameFactory,
                 ordinal: int, doc_ids: list[str], doc_types: list[str],
                 schedule: tuple[str, str, str] | None,
                 entity_counters: dict[str, int]) -> None:
        self.config = config
        self.alloc = alloc
        self.names = names
        self.ordinal = ordinal
        self.doc_ids = doc_ids
        self.doc_types = doc_types
        self.schedule = schedule
        self.entities: dict[str, Entity] = {}
        self.entity_roles: dict[str, str] = {}
        self.facts: dict[str, SemanticFact] = {}
        self.fact_roles: dict[str, set[str]] = {}
        self.fact_family: dict[str, str] = {}
        self.conditions: dict[str, Condition] = {}
        self.events: dict[str, Event] = {}
        self.transactions: dict[str, Transaction] = {}
        self.shareholdings: list[Shareholding] = []
        self.contradictions: list[Contradiction] = []
        self.cross_refs: list[CrossReference] = []
        self.contradiction_side: dict[str, str] = {}
        self.letter_meta: dict[str, MessageMeta] = {}
        self.exhibits: dict[str, str] = {}
        self._fact_counter = 0
        self._event_counter = 0
        self._txn_counter = 0
        # shared across matters: entity ordinals are corpus-global, so two
        # matters can never allocate the same entity id
        self._entity_counter = entity_counters
        self._rng = scope_rng(alloc.key, "matter", str(ordinal))
        self.doc_scope = doc_ids[0].split(":")[1]
        self.ctx: dict[str, str] = {}
        self.typed: dict[str, object] = {}
        self.doc_by_type: dict[str, list[str]] = {}
        for doc_id, doc_type in zip(doc_ids, doc_types):
            self.doc_by_type.setdefault(doc_type, []).append(doc_id)
        self.base_date = config.start_date + dt.timedelta(days=7 * ordinal)

    # -- ids ---------------------------------------------------------------
    def _entity(self, kind: str, subtype: str, name: str, **kwargs) -> Entity:
        counter = self._entity_counter.get(kind, 0) + 1
        self._entity_counter[kind] = counter
        prefix = f"ent:{kind}" if kind in {"company", "bank", "person", "asset"} else "ent:org"
        entity_id = self.alloc.alloc(prefix, f"{kind}:{counter:04d}")
        entity = Entity(entity_id=entity_id, entity_type="person" if kind == "person"
                        else "organization", subtype=subtype, canonical_name=name, **kwargs)
        self.entities[entity.entity_id] = entity
        return entity

    def _fact(self, *, kind: str, family: str, roles: set[str], **payload) -> str:
        self._fact_counter += 1
        prefix = f"fact:{kind}"
        fact_id = self.alloc.alloc(prefix, f"{self.doc_scope}:{self._fact_counter:02d}")
        stmt_ids = payload.pop("statement_ids", [])
        fact = SemanticFact(fact_id=fact_id, kind=kind, statement_ids=stmt_ids, **payload)
        self.facts[fact_id] = fact
        self.fact_roles[fact_id] = set(roles)
        self.fact_family[fact_id] = family
        return fact_id

    def _event(self, name: str, kind: str, date: dt.date, participants: list[str],
               place: str | None = None, facts: list[str] | None = None) -> str:
        self._event_counter += 1
        event_id = self.alloc.alloc("evt", f"{self.doc_scope}/e{self._event_counter:02d}")
        event = Event(
            event_id=event_id, name=name, kind=kind,
            event_date=IsoDate(value=date),
            participants=[EntityRef(entity_id=p) for p in participants],
            place=EntityRef(entity_id=place) if place else None,
            fact_ids=list(facts or []),
        )
        self.events[event_id] = event
        return event_id

    def _txn(self, kind: str, src: str, dst: str, amount: Money, date: dt.date,
             status: str, instrument: str, fact_id: str) -> Transaction:
        self._txn_counter += 1
        txn_id = self.alloc.alloc("txn", f"{self.doc_scope}/t{self._txn_counter:02d}")
        txn = Transaction(transaction_id=txn_id, kind=kind,
                          from_entity=EntityRef(entity_id=src),
                          to_entity=EntityRef(entity_id=dst),
                          amount=amount, date=IsoDate(value=date), status=status,
                          instrument=instrument, fact_id=fact_id)
        self.transactions[txn_id] = txn
        return txn

    # -- scenario ----------------------------------------------------------
    def build(self) -> Matter:
        rng = self._rng
        holdco = self._entity("company", "holding_company", self.names.company(),
                              jurisdiction=self.names.jurisdiction(),
                              attributes={"sector": self.names.sector()})
        lender = self._entity("bank", "bank", self.names.bank(),
                              jurisdiction=self.names.jurisdiction())
        subsidiaries = [
            self._entity("company", "subsidiary", self.names.company(),
                         jurisdiction=holdco.jurisdiction or "England and Wales",
                         attributes={"sector": self.names.sector()})
            for _ in range(4)
        ]
        target = self._entity("company", "target", self.names.company(),
                              jurisdiction=self.names.jurisdiction())
        guarantors = [
            self._entity("company", "guarantor", self.names.company(),
                         jurisdiction=holdco.jurisdiction or "England and Wales")
            for _ in range(2)
        ]
        directors = [self._entity("person", None, self.names.person(),
                                  jurisdiction=holdco.jurisdiction or "England and Wales")
                     for _ in range(6)]
        chair = directors[0]
        secretary = self._entity("person", None, self.names.person())
        counsel = [self._entity("law_firm", "law_firm", self.names.law_firm())
                   for _ in range(2)]
        counsel_reps = [self._entity("person", None, self.names.person()) for _ in range(2)]
        auditor = self._entity("org", "auditor", self.names.company())
        court = self._entity("org", "court", pick(rng, (
            "The High Court of Justice, Business and Property Courts",
            "The Commercial Court",
            "The Court of Session, Outer House",
            "The District Court of Delaware")))
        regulator = self._entity("org", "regulator", pick(rng, (
            "The Financial Conduct Authority", "The Prudential Regulation Authority",
            "The Registrar of Companies")))
        assets = [self._entity("asset", pick(rng, (
            "freehold property", "leasehold premises", "plant and machinery",
            "intellectual property", "data centre equipment", "shareholding")),
            self.names.asset(), jurisdiction=holdco.jurisdiction or "England and Wales")
            for _ in range(3)]
        counterparty = self._entity("company", "counterparty", self.names.company())
        judge = self._entity("person", None, self.names.person(),
                             jurisdiction=holdco.jurisdiction or "England and Wales")

        self.entity_roles.update({
            holdco.entity_id: "the Borrower",
            lender.entity_id: "the Lender",
            target.entity_id: "the Target",
            court.entity_id: "the Court",
            regulator.entity_id: "the Regulator",
            auditor.entity_id: "the Auditor",
            counterparty.entity_id: "the Counterparty",
        })
        for entity in subsidiaries:
            self.entity_roles[entity.entity_id] = "a Subsidiary"
        for entity in guarantors:
            self.entity_roles[entity.entity_id] = "a Guarantor"
        for firm in counsel:
            self.entity_roles[firm.entity_id] = "the Adviser"
        for person in counsel_reps:
            self.entity_roles[person.entity_id] = "counsel"
        for person in directors:
            self.entity_roles[person.entity_id] = "a Director"
        for asset in assets:
            self.entity_roles[asset.entity_id] = "the Charged Asset"

        # ---- deal numbers -------------------------------------------------
        principal = Money(
                amount_minor=pick(rng, (8_000_000, 12_500_000, 18_000_000, 24_000_000, 30_000_000)) * 100,
                currency="GBP",
            )
        rate = Percentage(basis_points=pick(rng, (475, 525, 550, 610)))
        margin = Percentage(basis_points=pick(rng, (225, 250, 275, 300)))
        start = self.base_date + dt.timedelta(days=rng.randrange(10, 45))
        maturity = start + dt.timedelta(days=365 * pick(rng, (4, 5, 6)))
        facility_ref = f"FA-{start.year}-{rng.randrange(100, 999):03d}"
        acquisition_amount = Money(
                amount_minor=principal.amount_minor * pick(rng, (2, 3)) // 2,
                currency="GBP",
            )
        acquisition_date = start - dt.timedelta(days=rng.randrange(40, 160))

        intangible = {
            "principal": principal, "rate": rate, "margin": margin,
            "all_in_rate": Percentage(basis_points=rate.basis_points + margin.basis_points),
            "outstanding": principal, "acquisition": acquisition_amount,
        }

        # ---- facts --------------------------------------------------------
        share_pcts = [4000, 2500, 2000, 1500]
        for entity, basis_points in zip(subsidiaries, share_pcts):
            fact_id = self._fact(
                kind="edge", family="ownership",
                roles={"due_diligence", "board_minutes", "memorandum",
                       "financial_agreement", "workbook", "press_release"},
                subject=EntityRef(entity_id=holdco.entity_id), relation="owns",
                object=EntityRef(entity_id=entity.entity_id), status="completed")
            self.shareholdings.append(Shareholding(
                parent_id=holdco.entity_id, child_id=entity.entity_id,
                percent=Percentage(basis_points=basis_points), fact_id=fact_id))
            self._fact(
                kind="edge", family="subsidiary",
                roles={"due_diligence", "memorandum", "case_notes",
                       "financial_agreement"},
                subject=EntityRef(entity_id=entity.entity_id),
                relation="is_subsidiary_of",
                object=EntityRef(entity_id=holdco.entity_id), status="completed")

        acquisition_fact = self._fact(
            kind="edge", family="acquisition",
            roles={"press_release", "memorandum", "due_diligence", "board_minutes",
                   "financial_agreement"},
            subject=EntityRef(entity_id=holdco.entity_id), relation="acquired",
            object=EntityRef(entity_id=target.entity_id), status="completed",
            amount=acquisition_amount,
            effective=TimeScope(kind="instant", start=IsoDate(value=acquisition_date)))

        for index, person in enumerate(directors):
            company = holdco if index in (0, 1, 2) else subsidiaries[(index - 3) % 4]
            self._fact(
                kind="edge", family="director",
                roles={"board_minutes", "due_diligence", "financial_agreement",
                       "memorandum", "case_notes"},
                subject=EntityRef(entity_id=person.entity_id), relation="is_director_of",
                object=EntityRef(entity_id=company.entity_id), status="held",
                effective=TimeScope(kind="instant",
                                    start=IsoDate(value=start - dt.timedelta(days=400))))

        chair_fact = self._fact(
            kind="edge", family="chair",
            roles={"board_minutes"},
            subject=EntityRef(entity_id=chair.entity_id), relation="chairs",
            object=EntityRef(entity_id=holdco.entity_id), status="held",
            effective=TimeScope(kind="instant", start=IsoDate(value=start)))

        for firm, rep in zip(counsel, counsel_reps):
            self._fact(
                kind="edge", family="representative",
                roles={"financial_agreement", "case_notes", "due_diligence"},
                subject=EntityRef(entity_id=firm.entity_id), relation="represents",
                object=EntityRef(entity_id=holdco.entity_id), status="completed")
            self._fact(
                kind="edge", family="representative",
                roles={"case_notes", "financial_agreement"},
                subject=EntityRef(entity_id=rep.entity_id), relation="represents",
                object=EntityRef(entity_id=holdco.entity_id), status="completed")

        self._fact(
            kind="edge", family="advisor",
            roles={"due_diligence", "memorandum", "financial_agreement"},
            subject=EntityRef(entity_id=auditor.entity_id), relation="advises",
            object=EntityRef(entity_id=holdco.entity_id), status="completed")

        for index, template in enumerate(_REPRESENTATIONS):
            speaker = (auditor, regulator, counsel_reps[0], counsel_reps[1], auditor)[index]
            self._fact(
                kind="proposition", family="representation",
                roles={"financial_agreement", "due_diligence"},
                text=template.format(year=start.year - 1,
                                     amount=_amount_phrase(principal),
                                     jurisdiction=holdco.jurisdiction or "England and Wales"),
                status="stated", speaker=EntityRef(entity_id=speaker.entity_id))

        facility_fact = self._fact(
            kind="edge", family="facility",
            roles={"financial_agreement", "due_diligence", "memorandum",
                   "board_minutes", "workbook", "press_release", "case_notes",
                   "correspondence"},
            subject=EntityRef(entity_id=lender.entity_id), relation="lent_to",
            object=EntityRef(entity_id=holdco.entity_id), status="completed",
            amount=principal,
            effective=TimeScope(kind="instant", start=IsoDate(value=start),
                                granularity="day"))

        # conditions precedent constrain the facility (architecture §4.1)
        condition_predicates = (
            "the Borrower delivers a certified copy of its constitutional documents",
            "the Lender receives a satisfactory legal opinion from counsel",
            "the Borrower provides evidence that the insurance over the Charged Assets is in force",
            "the Borrower delivers a compliance certificate signed by two directors",
        )
        condition_ids: list[str] = []
        for predicate in condition_predicates:
            cid = self.alloc.alloc("cnd", f"{self.doc_scope}/{len(condition_ids) + 1:02d}")
            self.conditions[cid] = Condition(
                condition_id=cid, kind=pick(rng, ("provided_that", "subject_to")),
                constrains=facility_fact, predicate=predicate,
                satisfied=pick(rng, ("yes", "yes", "unknown")))
            condition_ids.append(cid)
        self.facts[facility_fact] = self.facts[facility_fact].model_copy(
            update={"conditions": condition_ids})
        self.typed["conditions"] = condition_ids

        # ---- drawdowns and repayments -------------------------------------
        drawdown_shares = (40, 30, 20, 10)
        drawdowns: list[Transaction] = []
        drawdown_facts: list[str] = []
        for index, share in enumerate(drawdown_shares):
            amount = Money(
                amount_minor=principal.amount_minor * share // 100,
                currency=principal.currency,
            )
            drawdown_date = start + dt.timedelta(days=index * 7)
            fact_id = self._fact(
                kind="edge", family="drawdown", roles={"financial_agreement",
                                                       "due_diligence", "workbook"},
                subject=EntityRef(entity_id=lender.entity_id), relation="disbursed",
                object=EntityRef(entity_id=holdco.entity_id), status="completed",
                amount=amount,
                effective=TimeScope(kind="instant", start=IsoDate(value=drawdown_date)))
            self._txn("drawdown", lender.entity_id, holdco.entity_id, amount,
                      drawdown_date, "completed", "facility agreement", fact_id)
            drawdown_facts.append(fact_id)
        instalments = 8
        instalment = principal.amount_minor // instalments
        if instalment * instalments != principal.amount_minor:  # pragma: no cover
            raise AssertionError("principal must divide into equal installments")
        balance = principal.amount_minor
        repayments: list[Transaction] = []
        repayment_facts: list[str] = []
        repayment_rows: list[dict[str, object]] = []
        for index in range(instalments):
            due = start + dt.timedelta(days=90 * (index + 1))
            opening = balance
            balance -= instalment
            interest = opening * intangible["all_in_rate"].basis_points // 10_000 // 4
            amount = Money(
                amount_minor=instalment,
                currency=principal.currency,
            )
            fact_id = self._fact(
                kind="edge", family="repayment",
                roles={"financial_agreement", "due_diligence", "workbook",
                       "correspondence"},
                subject=EntityRef(entity_id=holdco.entity_id), relation="repaid",
                object=EntityRef(entity_id=lender.entity_id), status="completed",
                amount=amount,
                effective=TimeScope(kind="instant", start=IsoDate(value=due)))
            txn = self._txn("repayment", holdco.entity_id, lender.entity_id, amount,
                            due, "completed", "facility agreement", fact_id)
            repayments.append(txn)
            repayment_facts.append(fact_id)
            repayment_rows.append({
                "instalment": index + 1, "due": due, "principal": amount,
                "interest": Money(
                amount_minor=interest,
                currency=principal.currency,
            ),
                "balance": Money(
                amount_minor=balance,
                currency=principal.currency,
            ),
            })
        self.typed["repayment_rows"] = repayment_rows
        self.typed["drawdowns"] = drawdowns
        self.typed["repayments"] = repayments

        as_at = repayment_rows[4]["due"]
        outstanding_amount = Money(
                amount_minor=principal.amount_minor - instalment * 5,
                currency=principal.currency,
            )
        outstanding_fact = self._fact(
            kind="edge", family="outstanding",
            roles={"financial_agreement", "due_diligence", "memorandum",
                   "board_minutes", "correspondence"},
            subject=EntityRef(entity_id=holdco.entity_id), relation="owes",
            object=EntityRef(entity_id=lender.entity_id), status="completed",
            amount=outstanding_amount,
            effective=TimeScope(kind="instant", start=IsoDate(value=as_at)))
        self.typed["outstanding"] = outstanding_amount
        self.typed["as_at"] = as_at

        for guarantor in guarantors:
            self._fact(
                kind="edge", family="guarantee",
                roles={"financial_agreement", "due_diligence", "memorandum",
                       "correspondence", "board_minutes", "case_notes"},
                subject=EntityRef(entity_id=guarantor.entity_id), relation="guarantees",
                object=EntityRef(entity_id=holdco.entity_id), status="completed")
        for asset in assets:
            self._fact(
                kind="edge", family="security",
                roles={"financial_agreement", "due_diligence", "memorandum",
                       "correspondence", "board_minutes", "case_notes"},
                subject=EntityRef(entity_id=holdco.entity_id),
                relation="grants_security_over",
                object=EntityRef(entity_id=asset.entity_id), status="completed")

        # intra-group loans: same lending fact stated in two documents
        for entity in subsidiaries[:2]:
            self._fact(
                kind="edge", family="intragroup",
                roles={"financial_agreement", "due_diligence", "workbook"},
                subject=EntityRef(entity_id=holdco.entity_id), relation="lent_to",
                object=EntityRef(entity_id=entity.entity_id), status="completed",
                amount=Money(
                amount_minor=750_000_00,
                currency=principal.currency,
            ),
                effective=TimeScope(kind="instant",
                                    start=IsoDate(value=start + dt.timedelta(days=30))))

        # ---- propositions --------------------------------------------------
        ratio = 250 + ((self.ordinal * 37 + rng.randrange(0, 40)) % 130)
        cover = 175 + ((self.ordinal * 23 + rng.randrange(0, 30)) % 80)
        self.typed["leverage"] = Percentage(basis_points=ratio)
        covenant_facts: list[str] = []
        for template, amount in zip(_COVENANTS, (principal, outstanding_amount,
                                                 principal, outstanding_amount)):
            text = template.format(ratio=f"{ratio / 100:.2f}", cover=f"{cover / 100:.2f}",
                                   amt=_amount_phrase(amount))
            covenant_facts.append(self._fact(
                kind="proposition", family="covenant", roles={"financial_agreement",
                                                              "due_diligence",
                                                              "memorandum",
                                                              "correspondence"},
                text=text, status=pick(rng, ("found", "stated")),
                speaker=EntityRef(entity_id=auditor.entity_id)))

        financial_years = (start.year - 1, start.year)
        revenue_base = principal.amount_minor * 9 + (self.ordinal + 1) * 137_00
        financial_facts: list[str] = []
        financial_numbers: list[dict[str, Money]] = []
        for offset, year in enumerate(financial_years):
            growth = pick(rng, (95, 104, 112, 127))
            revenue = Money(
                amount_minor=revenue_base * growth // 100,
                currency=principal.currency,
            )
            ebitda = Money(
                amount_minor=revenue.amount_minor * pick(rng, (18, 21, 24)) // 100,
                currency=principal.currency,
            )
            net_debt = Money(
                amount_minor=principal.amount_minor * pick(rng, (85, 110, 135)) // 100,
                currency=principal.currency,
            )
            financial_numbers.append({"revenue": revenue, "ebitda": ebitda,
                                      "net_debt": net_debt})
            for label, money in (("revenue", revenue), ("EBITDA", ebitda),
                                 ("net debt", net_debt)):
                financial_facts.append(self._fact(
                    kind="proposition", family="financial",
                    roles={"due_diligence", "press_release", "memorandum",
                           "board_minutes", "financial_agreement", "case_notes"},
                    text=f"group {label} for the year ended 31 December {year} was "
                         f"{_amount_phrase(money)}",
                    status="found", amount=money,
                    speaker=EntityRef(entity_id=auditor.entity_id),
                    effective=TimeScope(kind="period",
                                        start=IsoDate(value=dt.date(year, 1, 1)),
                                        end=IsoDate(value=dt.date(year, 12, 31)))))
        self.typed["financial"] = financial_facts

        allegation_facts: list[str] = []
        for index, template in enumerate(sample(rng, _ALLEGATIONS, 3)):
            text = template.format(amt=_amount_phrase(
                Money(
                amount_minor=principal.amount_minor * (index + 2) // 20
                + (self.ordinal + 1) * 251_00,
                currency=principal.currency,
            )))
            allegation_facts.append(self._fact(
                kind="proposition", family="allegation",
                roles={"due_diligence", "case_notes", "memorandum", "correspondence"},
                text=text, status="alleged",
                speaker=EntityRef(entity_id=counsel_reps[index % len(counsel_reps)].entity_id)))
        finding_facts: list[str] = []
        for index, template in enumerate(sample(rng, _FINDINGS, 3)):
            finding_facts.append(self._fact(
                kind="proposition", family="finding",
                roles={"due_diligence", "case_notes", "memorandum", "correspondence"},
                text=template.format(company=holdco.canonical_name), status="found",
                speaker=EntityRef(entity_id=auditor.entity_id)))
        recommendation_facts: list[str] = []
        for template in sample(rng, _RECOMMENDATIONS, 2):
            recommendation_facts.append(self._fact(
                kind="proposition", family="recommendation",
                roles={"due_diligence", "memorandum"},
                text=template.format(company=holdco.canonical_name),
                status="proposed", speaker=EntityRef(entity_id=counsel[0].entity_id)))

        filed_fact = self._fact(
            kind="edge", family="proceeding",
            roles={"case_notes", "due_diligence", "press_release"},
            subject=EntityRef(entity_id=counterparty.entity_id), relation="filed",
            object=EntityRef(entity_id=holdco.entity_id), status="completed",
            effective=TimeScope(kind="instant",
                                start=IsoDate(value=start + dt.timedelta(days=120))))
        served_fact = self._fact(
            kind="edge", family="order",
            roles={"case_notes", "correspondence"},
            subject=EntityRef(entity_id=counsel[0].entity_id), relation="served",
            object=EntityRef(entity_id=holdco.entity_id), status="completed",
            effective=TimeScope(kind="instant",
                                start=IsoDate(value=start + dt.timedelta(days=150))))
        self._fact(
            kind="edge", family="order",
            roles={"case_notes"},
            subject=EntityRef(entity_id=court.entity_id), relation="ordered",
            object=EntityRef(entity_id=holdco.entity_id), status="completed",
            effective=TimeScope(kind="instant",
                                start=IsoDate(value=start + dt.timedelta(days=180))))
        self._fact(
            kind="edge", family="order",
            roles={"case_notes", "correspondence"},
            subject=EntityRef(entity_id=holdco.entity_id), relation="settled",
            object=EntityRef(entity_id=counterparty.entity_id), status="completed",
            effective=TimeScope(kind="instant",
                                start=IsoDate(value=start + dt.timedelta(days=240))))

        quote_facts: list[str] = []
        for index, template in enumerate((
            "this facility gives the group the headroom to complete the integration of {target}",
            "the group's order book remained strong through the second half of the year",
        )):
            quote_facts.append(self._fact(
                kind="proposition", family="quote",
                roles={"press_release", "memorandum"},
                text=template.format(target=target.canonical_name), status="stated",
                speaker=EntityRef(entity_id=chair.entity_id)))
        forward_fact = self._fact(
            kind="proposition", family="forward",
            roles={"press_release", "due_diligence"},
            text="the integration of {target} will be completed during the next financial year"
                 .format(target=target.canonical_name),
            status="stated", modality="expected_to",
            speaker=EntityRef(entity_id=chair.entity_id))

        resolution_facts: list[str] = []
        for index, template in enumerate(_RESOLUTIONS):
            text = template.format(lender=lender.canonical_name, target=target.canonical_name,
                                   amt=_amount_phrase(acquisition_amount),
                                   sub=subsidiaries[index % len(subsidiaries)].canonical_name)
            status = ("approved", "approved", "rejected", "deferred",
                      "approved", "approved")[index]
            resolution_facts.append(self._fact(
                kind="proposition", family="resolution", roles={"board_minutes"},
                text=text, status=status,
                speaker=EntityRef(entity_id=chair.entity_id)))

        appointment_facts: list[str] = []
        for index, template in enumerate(_APPOINTMENTS[:2]):
            person = directors[-1 - index]
            appointment_facts.append(self._fact(
                kind="proposition", family="resolution", roles={"board_minutes"},
                text=template.format(person=person.canonical_name,
                                     date=_date_phrase(start - dt.timedelta(days=30))),
                status="approved", speaker=EntityRef(entity_id=chair.entity_id)))

        prior_fact = self._fact(
            kind="proposition", family="prior_minutes", roles={"board_minutes"},
            text="the minutes of the meeting held on {date} were approved as a correct record"
                 .format(date=_date_phrase(start - dt.timedelta(days=91))),
            status="approved", speaker=EntityRef(entity_id=secretary.entity_id))

        attendance_facts: list[str] = []
        meeting_date = start - dt.timedelta(days=14)
        meeting_event = self._event("meeting of the board", "meeting", meeting_date,
                                    [d.entity_id for d in directors],
                                    facts=resolution_facts + [prior_fact])
        for person in directors:
            attendance_facts.append(self._fact(
                kind="edge", family="attendance", roles={"board_minutes"},
                subject=EntityRef(entity_id=person.entity_id), relation="attends",
                object=EntityRef(entity_id=holdco.entity_id), status="completed",
                effective=TimeScope(kind="instant", start=IsoDate(value=meeting_date))))
        self._event("execution of the facility agreement", "execution", start,
                    [lender.entity_id, holdco.entity_id], facts=[facility_fact])
        self._event("acquisition of the target", "acquisition", acquisition_date,
                    [holdco.entity_id, target.entity_id], facts=[acquisition_fact])
        self._event("commencement of proceedings", "litigation",
                    start + dt.timedelta(days=120),
                    [counterparty.entity_id, holdco.entity_id], facts=[filed_fact])
        self._event("service of notice", "notice", start + dt.timedelta(days=150),
                    [counsel[0].entity_id, holdco.entity_id], facts=[served_fact])

        # ---- contradiction -------------------------------------------------
        if self.schedule is not None:
            self._apply_contradiction(self.schedule, facility_fact, acquisition_fact,
                                      outstanding_fact, start, principal)

        # ---- context slots -------------------------------------------------
        self.ctx.update({
            "holdco": holdco.canonical_name,
            "lender": lender.canonical_name,
            "target": target.canonical_name,
            "guarantor": guarantors[0].canonical_name,
            "guarantor2": guarantors[1].canonical_name,
            "sub1": subsidiaries[0].canonical_name,
            "sub2": subsidiaries[1].canonical_name,
            "sub3": subsidiaries[2].canonical_name,
            "sub4": subsidiaries[3].canonical_name,
            "chair": chair.canonical_name,
            "secretary": secretary.canonical_name,
            "director1": directors[1].canonical_name,
            "director2": directors[2].canonical_name,
            "auditor": auditor.canonical_name,
            "counsel": counsel[0].canonical_name,
            "counsel2": counsel[1].canonical_name,
            "court": court.canonical_name,
            "regulator": regulator.canonical_name,
            "counterparty": counterparty.canonical_name,
            "asset1": assets[0].canonical_name,
            "asset2": assets[1].canonical_name,
            "asset3": assets[2].canonical_name,
            "jurisdiction": holdco.jurisdiction or "England and Wales",
            "facility_ref": facility_ref,
            "principal": format_money_text(principal),
            "principal_short": format_money_text(principal, style=1),
            "outstanding": format_money_text(outstanding_amount),
            "acquisition": format_money_text(acquisition_amount),
            "rate": f"{rate.basis_points / 100:.2f}%",
            "margin": f"{margin.basis_points / 100:.2f}%",
            "all_in": f"{intangible['all_in_rate'].basis_points / 100:.2f}%",
            "tenor_years": str((maturity - start).days // 365),
            "start_date": format_date_text(start),
            "maturity": format_date_text(maturity),
            "acquisition_date": format_date_text(acquisition_date),
            "meeting_date": format_date_text(meeting_date),
            "as_at": format_date_text(as_at),
            "first_payment": format_date_text(repayment_rows[0]["due"]),
            "leverage": f"{ratio / 100:.2f}:1",
            "cover": f"{cover / 100:.2f}:1",
            "period": f"the year ended 31 December {financial_years[-1]}",
            "period_start": str(financial_years[0]),
            "period_end": str(financial_years[-1]),
            "revenue_latest": format_money_text(financial_numbers[-1]["revenue"]),
            "revenue_prior": format_money_text(financial_numbers[0]["revenue"]),
            "ebitda_latest": format_money_text(financial_numbers[-1]["ebitda"]),
            "ebitda_prior": format_money_text(financial_numbers[0]["ebitda"]),
            "netdebt": format_money_text(financial_numbers[-1]["net_debt"]),
            "prior_meeting": format_date_text(meeting_date - dt.timedelta(days=91)),
            "case_number": f"HC-{start.year}-{rng.randrange(1000, 9999):04d}",
            "judge": judge.canonical_name,
            "sector": holdco.attributes.get("sector", "specialist industrial"),
            "outstanding_correct": format_money_text(Money(
                amount_minor=outstanding_amount.amount_minor + 25_000_00,
                currency=principal.currency)),
        })
        self.typed.update({
            "holdco": holdco.entity_id, "lender": lender.entity_id,
            "target": target.entity_id, "subsidiaries": [s.entity_id for s in subsidiaries],
            "guarantors": [g.entity_id for g in guarantors],
            "directors": [d.entity_id for d in directors],
            "assets": [a.entity_id for a in assets],
            "principal": principal, "acquisition": acquisition_amount,
            "rate": rate, "margin": margin, "start": start, "maturity": maturity,
            "meeting_date": meeting_date, "facility_fact": facility_fact,
            "outstanding_fact": outstanding_fact, "acquisition_fact": acquisition_fact,
            "repayment_facts": repayment_facts, "drawdown_facts": drawdown_facts,
            "covenant_facts": covenant_facts, "allegation_facts": allegation_facts,
            "finding_facts": finding_facts, "recommendation_facts": recommendation_facts,
            "financial_facts": financial_facts, "resolution_facts": resolution_facts,
            "attendance_facts": attendance_facts, "quote_facts": quote_facts,
            "forward_fact": forward_fact, "prior_fact": prior_fact,
            "filed_fact": filed_fact, "condition_ids": condition_ids,
            "facility_ref": facility_ref, "meeting_event": meeting_event,
        })

        # ---- threads, cross-references, dates ------------------------------
        threads = self._build_threads()
        dates = self._document_dates(start, acquisition_date, meeting_date, as_at)
        cross_refs = self._build_cross_references(dates)
        doc_facts = self._assign_doc_facts()

        return Matter(
            ordinal=self.ordinal, doc_ids=list(self.doc_ids),
            doc_types=list(self.doc_types), doc_by_type=self.doc_by_type,
            dates=dates, entities=self.entities, entity_roles=self.entity_roles,
            facts=self.facts, fact_roles=self.fact_roles,
            fact_family=self.fact_family, conditions=self.conditions,
            events=self.events, transactions=self.transactions,
            threads=threads, contradictions=self.contradictions,
            cross_refs=cross_refs, shareholdings=self.shareholdings,
            letter_meta=self.letter_meta, exhibits=self.exhibits,
            contradiction_side=self.contradiction_side,
            ctx=self.ctx, typed=self.typed, doc_facts=doc_facts)

    # -- per-document fact allocation --------------------------------------
    def _assign_doc_facts(self) -> dict[str, list[str]]:
        """Every fact is stated by at least one planned document.

        Each type states its whole role set, subject to a per-type family cap so
        a due-diligence report or a letter does not masquerade as the repayment
        schedule (§11 transaction ranges).  A thread's letters share the base set
        and rotate through the remaining transaction facts; the facility
        agreement states them all, so no fact loses its last home.
        """
        out: dict[str, list[str]] = {}
        letters = self.doc_by_type.get("correspondence", [])
        base_families = {"facility", "outstanding", "guarantee", "security",
                         "covenant", "allegation", "finding"}
        for doc_id, doc_type in zip(self.doc_ids, self.doc_types):
            facts = [fid for fid in self.facts if doc_type in self.fact_roles[fid]]
            facts = _cap_families(facts, self.fact_family, _FACT_CAPS.get(doc_type, {}))
            if doc_type == "correspondence":
                base = [f for f in facts if self.fact_family[f] in base_families]
                rest = [f for f in facts if self.fact_family[f] not in base_families]
                if len(letters) > 1:
                    index = letters.index(doc_id)
                    facts = base + rest[index::len(letters)]
                else:
                    facts = base + rest[:3]
                facts = facts[:20]
            elif doc_type == "press_release":
                facts = facts[:16]
            out[doc_id] = facts
        return out

    # -- contradiction -----------------------------------------------------
    def _apply_contradiction(self, schedule: tuple[str, str, str], facility_fact: str,
                             acquisition_fact: str, outstanding_fact: str,
                             start: dt.date, principal: Money) -> None:
        type_a, type_b, dimension = schedule
        if type_a not in self.doc_by_type or type_b not in self.doc_by_type:
            raise AssertionError(f"contradiction pair {type_a}/{type_b} missing a document")
        base_fact_id = {"amount": facility_fact, "date": facility_fact,
                        "status": outstanding_fact,
                        "relationship": self.shareholdings[0].fact_id}[dimension]
        base = self.facts[base_fact_id]
        alt_payload: dict[str, object] = {
            "kind": base.kind, "family": "contradiction_alt", "subject": base.subject,
            "relation": base.relation, "object": base.object,
            "text": base.text, "speaker": base.speaker,
        }
        if dimension == "amount":
            alt_payload["amount"] = Money(
                amount_minor=(base.amount.amount_minor * 94) // 100,
                currency=base.amount.currency,
            )
            alt_payload["effective"] = base.effective
            alt_payload["status"] = base.status
        elif dimension == "date":
            alt_payload["amount"] = base.amount
            alt_payload["effective"] = TimeScope(
                kind="instant",
                start=IsoDate(value=start - dt.timedelta(days=9)),
                granularity="day")
            alt_payload["status"] = base.status
        elif dimension == "status":
            alt_payload["amount"] = base.amount
            alt_payload["effective"] = base.effective
            alt_payload["status"] = "denied"
            alt_payload["negated"] = True
        else:                                   # relationship
            alt_payload["relation"] = ("controls" if base.relation == "owns"
                                       else "controls")
            alt_payload["status"] = base.status
        alt_fact_id = self._fact(roles=set(), **alt_payload)   # type: ignore[arg-type]

        # the base side loses the other document type; the alternative is exclusive
        self.fact_roles[base_fact_id].discard(type_b)
        self.fact_roles[base_fact_id].add(type_a)
        self.fact_roles[alt_fact_id] = {type_b}
        self.contradiction_side[type_b] = alt_fact_id
        self.contradiction_side[type_a] = base_fact_id

        doc_a = self.doc_by_type[type_a][0]
        doc_b = self.doc_by_type[type_b][0]
        contradiction_id = self.alloc.alloc("ctr", f"{self.doc_scope}/c01")
        self.contradictions.append(Contradiction(
            contradiction_id=contradiction_id, fact_a=base_fact_id, fact_b=alt_fact_id,
            dimension=dimension, documents=[doc_a, doc_b]))
        self.cross_refs.append(CrossReference(
            reference_id=self.alloc.alloc("xref", f"{self.doc_scope}/ctr"),
            kind="contradicts", src_document=doc_a, dst_document=doc_b,
            src_fact=base_fact_id, dst_fact=alt_fact_id))
        self.typed["contradiction_id"] = contradiction_id
        self.typed["contradiction_dimension"] = dimension

    # -- threads -----------------------------------------------------------
    def _build_threads(self) -> list[Thread]:
        letters = self.doc_by_type.get("correspondence", [])
        if not letters:
            return []
        rng = scope_rng(self.alloc.key, "thread", str(self.ordinal))
        pool = list(_THREAD_SHAPES.get(len(letters), ((len(letters),),)))
        partition = pick(rng, pool)
        while sum(partition) != len(letters):      # pragma: no cover - pool is exact
            partition = pick(rng, pool)
        style = pick(rng, ("chain", "chain", "fan"))
        correction_at = None
        if len(letters) >= 3 and rng.random() < 0.7:
            correction_at = rng.randrange(2, len(letters) + 1)
        subject = pick(rng, _THREAD_SUBJECTS)
        threads: list[Thread] = []
        correction_docs: set[str] = set()
        cursor = 0
        for length in partition:
            members = letters[cursor:cursor + length]
            cursor += length
            thread_scope = f"{self.config.corpus_name}:{self.ordinal * 10 + len(threads) + 1:04d}"
            thread_id = self.alloc.alloc("thr", thread_scope)
            metas: list[MessageMeta] = []
            previous: str | None = None
            for position, doc_id in enumerate(members, start=1):
                message_id = self.alloc.alloc("msg", f"{thread_scope}/{position:02d}")
                in_reply_to = None
                references: list[str] = []
                if position > 1:
                    if style == "fan" or position == 2:
                        in_reply_to = metas[0].message_id
                    else:
                        in_reply_to = previous
                    references = [m.message_id for m in metas]
                exhibit_id = self.alloc.alloc("exh", f"{doc_id.split(':')[1]}/01")
                self.exhibits[doc_id] = exhibit_id
                meta = MessageMeta(
                    thread_id=thread_id, message_id=message_id, sequence=position,
                    in_reply_to=in_reply_to, references=references,
                    attachments=[exhibit_id])
                self.letter_meta[doc_id] = meta
                metas.append(meta)
                previous = message_id
                if correction_at is not None and cursor - length + position == correction_at:
                    correction_docs.add(doc_id)
            thread = Thread(
                thread_id=thread_id, subject=subject,
                participants=[EntityRef(entity_id=self.typed["holdco"]),
                              EntityRef(entity_id=self.typed["lender"])],
                root_document=members[0], messages=list(members),
                latest_document=members[-1],
                closed_by=members[-1] if len(members) > 1 else None)
            threads.append(thread)
        self.typed["correction_docs"] = correction_docs
        return threads

    # -- dates and cross references ---------------------------------------
    def _document_dates(self, start: dt.date, acquisition: dt.date,
                        meeting: dt.date, as_at: dt.date) -> dict[str, IsoDate]:
        dates: dict[str, IsoDate] = {}
        offsets = {
            "board_minutes": meeting,
            "due_diligence": start + dt.timedelta(days=35),
            "financial_agreement": start,
            "case_notes": start + dt.timedelta(days=160),
            "memorandum": start + dt.timedelta(days=45),
            "press_release": acquisition + dt.timedelta(days=1),
        }
        for doc_type, date in offsets.items():
            for index, doc_id in enumerate(self.doc_by_type.get(doc_type, [])):
                dates[doc_id] = IsoDate(value=date + dt.timedelta(days=index))
        for index, doc_id in enumerate(self.doc_by_type.get("correspondence", [])):
            dates[doc_id] = IsoDate(value=start + dt.timedelta(days=70 + 11 * index))
        return dates

    def _build_cross_references(self, dates: dict[str, IsoDate]) -> list[CrossReference]:
        refs: list[CrossReference] = list(self.cross_refs)   # contradiction refs
        plans = (
            ("financial_agreement", "due_diligence", "references"),
            ("financial_agreement", "memorandum", "references"),
            ("due_diligence", "financial_agreement", "exhibits"),
            ("due_diligence", "case_notes", "references"),
            ("memorandum", "financial_agreement", "references"),
            ("case_notes", "due_diligence", "references"),
            ("case_notes", "memorandum", "references"),
            ("press_release", "financial_agreement", "references"),
            ("board_minutes", "financial_agreement", "supports"),
            ("board_minutes", "due_diligence", "references"),
            ("correspondence", "financial_agreement", "references"),
        )
        index = 0
        for src_type, dst_type, kind in plans:
            if src_type not in self.doc_by_type or dst_type not in self.doc_by_type:
                continue
            src = self.doc_by_type[src_type][0]
            dst = self.doc_by_type[dst_type][0]
            index += 1
            refs.append(CrossReference(
                reference_id=self.alloc.alloc("xref", f"{self.doc_scope}/x{index:02d}"),
                kind=kind, src_document=src, dst_document=dst))
        letters = self.doc_by_type.get("correspondence", [])
        for position, doc_id in enumerate(letters[1:], start=1):
            refs.append(CrossReference(
                reference_id=self.alloc.alloc("xref", f"{self.doc_scope}/l{position:02d}"),
                kind="responds_to", src_document=doc_id,
                dst_document=letters[position - 1]))
        return refs


def format_money_text(money: Money, style: int = 0) -> str:
    from .surface import fmt_money
    return fmt_money(money, style)


def format_date_text(value: dt.date) -> str:
    from .surface import fmt_date
    return fmt_date(IsoDate(value=value), 0)


def _amount_phrase(money: Money) -> str:
    from .surface import fmt_money
    return fmt_money(money, 1)


def _date_phrase(value: dt.date) -> str:
    return format_date_text(value)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def build_world(config: CorpusConfig) -> WorldDraft:
    corpus_hash = hashlib.blake2b(
        f"{config.seed}:{config.corpus_name}".encode("utf-8"), digest_size=6).hexdigest()
    corpus_id = f"corp:{config.corpus_name}:{corpus_hash}"
    alloc = IdAllocator(config.seed, config.corpus_name)
    names = NameFactory(scope_rng(alloc.key, "names"))

    sizes = matter_sizes(config.documents, config.max_letters)
    schedule = contradiction_schedule(config, sizes)
    schedule_by_matter: dict[int, tuple[str, str, str]] = {}
    for matter_index, type_a, type_b, dimension in schedule:
        schedule_by_matter.setdefault(matter_index, (type_a, type_b, dimension))

    entity_counters: dict[str, int] = {}
    document_slots: list[tuple[str, str, int]] = []
    matters: list[Matter] = []
    ordinal = 0
    order_rng_key = alloc.key
    for matter_index, size in enumerate(sizes):
        letters = size - len(CORE_TYPES)
        order = shuffled(scope_rng(order_rng_key, "order", str(matter_index)),
                         CORE_TYPES)
        types = order + ["correspondence"] * letters
        doc_ids = [alloc.alloc("doc", f"{ordinal + offset:05d}")
                   for offset in range(size)]
        ordinal += size
        document_slots.extend((doc_id, doc_type, matter_index)
                              for doc_id, doc_type in zip(doc_ids, types))
        builder = _MatterBuilder(config, alloc, names, matter_index, doc_ids, types,
                                 schedule_by_matter.get(matter_index), entity_counters)
        matters.append(builder.build())

    # workbook documents are planned from the same matter facts
    return WorldDraft(config=config, corpus_id=corpus_id, matters=matters,
                      alloc=alloc, document_slots=document_slots,
                      contradiction_schedule=schedule)
