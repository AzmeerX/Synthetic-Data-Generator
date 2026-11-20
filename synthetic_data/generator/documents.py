"""Document plans: realistic documents with controlled, labelled traps.

A plan is not a string blob.  It is an ordered list of sentence specifications,
each of which knows which fact it states, which entity mentions it contains and
which trap it exercises.  Ingestion later maps each specification to the real
canonical sentence; if the splitter disagrees, gold generation fails instead of
silently producing an ill-grounded annotation.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable

from synthetic_data.generator.world import Fact, World

DOC_TYPES = [
    "board_minutes",
    "financial_agreement",
    "due_diligence",
    "memorandum",
    "case_notes",
    "correspondence",
    "press_release",
]

DOC_LABELS = {
    "board_minutes": "Board Meeting Minutes",
    "financial_agreement": "Facility Agreement",
    "due_diligence": "Due Diligence Report",
    "memorandum": "Corporate Memorandum",
    "case_notes": "Legal Case Notes",
    "correspondence": "Correspondence",
    "press_release": "Press Release",
}

#: A block of five documents always totals twenty pages.  Repeating the block
#: makes 20/500/1000/5000 exact and keeps every checkpoint a prefix of the next.
PAGE_BLOCK = (5, 4, 3, 4, 4)


def page_counts(target_pages: int) -> list[int]:
    """Exact per-document page counts for a cumulative checkpoint.

    ``target_pages`` must be a multiple of 20.  The first five entries are
    always ``(5, 4, 3, 4, 4)``, so a 20-page pilot is a byte-stable prefix of a
    larger corpus generated with the same seed.
    """
    if target_pages <= 0 or target_pages % 20 != 0:
        raise ValueError("target_pages must be a positive multiple of 20")
    blocks = target_pages // 20
    counts: list[int] = []
    for block in range(blocks):
        shift = block % len(PAGE_BLOCK)
        counts.extend(PAGE_BLOCK[shift:] + PAGE_BLOCK[:shift])
    if sum(counts) != target_pages:
        raise AssertionError(f"page plan sums to {sum(counts)}, expected {target_pages}")
    return counts


#: Phase 2 block: seven documents, one of every type, exactly twenty pages.
V2_PAGE_BLOCK = (3, 3, 3, 3, 3, 3, 2)
V2_STABLE_DOC_PREFIX = 7


def page_counts_v2(target_pages: int) -> list[int]:
    """Seven-document cumulative page plan for the improved corpus.

    The first seven entries are always ``(3, 3, 3, 3, 3, 3, 2)``.  Repeating
    that block keeps every larger corpus a prefix of the smaller one.
    """
    if target_pages <= 0 or target_pages % 20 != 0:
        raise ValueError("target_pages must be a positive multiple of 20")
    blocks = target_pages // 20
    counts = list(V2_PAGE_BLOCK) * blocks
    if sum(counts) != target_pages:
        raise AssertionError(f"page plan sums to {sum(counts)}, expected {target_pages}")
    return counts


def format_date(value: dt.date) -> str:
    return f"{value.day} {value.strftime('%B %Y')}"


def add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


@dataclass(frozen=True)
class MentionSpec:
    entity_id: str
    sentence_text: str
    surface: str
    occurrence: int = 0


@dataclass(frozen=True)
class StatedFact:
    fact: Fact
    sentence_text: str


@dataclass(frozen=True)
class LinkSpec:
    link_id: str
    src: str
    dst: str
    type: str
    sentence_text: str


@dataclass(frozen=True)
class TimeSpec:
    time_id: str
    sentence_text: str
    surface: str
    kind: str
    value_start: str | None = None
    value_end: str | None = None
    granularity: str | None = None
    anchor_type: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class FlagSpec:
    kind: str
    object_ids: tuple[str, ...]
    note: str = ""


@dataclass
class DocumentPlan:
    index: int
    document_id: str
    filename: str
    doc_type: str
    doc_date: dt.date
    pages: int
    page_lines: list[list[str]]
    sentences: list[str]
    stated_facts: list[StatedFact]
    mentions: list[MentionSpec]
    links: list[LinkSpec]
    times: list[TimeSpec]
    identity_traps: list[tuple[str, str, str]]
    flags: list[FlagSpec]
    trap_kinds: list[str]
    contradiction_id: str | None
    notes: str
    subject_entity_id: str
    counterpart_entity_id: str
    bank_entity_id: str
    law_firm_entity_id: str
    court_entity_id: str
    place_entity_id: str

    @property
    def sentence_by_text(self) -> dict[str, str]:
        return {text: text for text in self.sentences}

    @property
    def entity_ids(self) -> list[str]:
        ids: set[str] = set()
        for mention in self.mentions:
            ids.add(mention.entity_id)
        for stated in self.stated_facts:
            if stated.fact.subject_id:
                ids.add(stated.fact.subject_id)
            if stated.fact.object_id:
                ids.add(stated.fact.object_id)
            if stated.fact.speaker_id:
                ids.add(stated.fact.speaker_id)
        return sorted(ids)

    @property
    def fact_ids(self) -> list[str]:
        return sorted({s.fact.fact_id for s in self.stated_facts})

    @property
    def relationship_count(self) -> int:
        return sum(1 for s in self.stated_facts if s.fact.kind == "edge")

    @property
    def proposition_count(self) -> int:
        return sum(1 for s in self.stated_facts if s.fact.kind == "proposition")

    def render_text(self) -> str:
        pages = ["\n\n".join(lines) for lines in self.page_lines]
        return "\f".join(pages) + "\n"


class _Builder:
    def __init__(self, world: World, index: int, doc_type: str, doc_date: dt.date,
                 pages: int) -> None:
        self.world = world
        self.index = index
        self.doc_type = doc_type
        self.doc_date = doc_date
        self.pages = pages
        self.document_id = f"gen_doc_{index:05d}"
        self.sentences: list[str] = []
        self._sentence_set: set[str] = set()
        self._surface_counts: dict[tuple[str, str], int] = {}
        self.stated_facts: list[StatedFact] = []
        self._stated_ids: set[str] = set()
        self.mentions: list[MentionSpec] = []
        self.links: list[LinkSpec] = []
        self.times: list[TimeSpec] = []
        self.flags: list[FlagSpec] = []
        self.identity_traps: list[tuple[str, str, str]] = []
        self.trap_kinds: list[str] = []
        self.contradiction_id: str | None = None

    # -- primitives -------------------------------------------------------------
    def sentence(self, text: str) -> str:
        text = " ".join(text.split())
        if not text:
            raise ValueError("empty sentence")
        if not text.isascii():
            raise ValueError(f"document text must be ASCII-safe: {text!r}")
        if "\n" in text or "\f" in text:
            raise ValueError(f"sentence may not contain a line/page break: {text!r}")
        if text in self._sentence_set:
            raise ValueError(f"duplicate sentence in {self.document_id}: {text!r}")
        self._sentence_set.add(text)
        self.sentences.append(text)
        return text

    def mention(self, entity_id: str, sentence_text: str,
                surface: str | None = None, occurrence: int | None = None) -> MentionSpec:
        entity = self.world.entity(entity_id)
        qualified = self.world.display_name(entity_id)
        if entity_id in self.world.duplicate_names:
            if surface is not None and surface != qualified:
                raise ValueError(
                    f"{self.document_id}: namesake {entity_id} must be mentioned as "
                    f"{qualified!r}, not {surface!r}")
            surface = qualified
        if surface is None:
            candidates = [qualified, entity.name, *entity.aliases]
            surface = next((c for c in candidates if c in sentence_text), None)
            if surface is None:
                raise ValueError(
                    f"{self.document_id}: cannot find a surface form for "
                    f"{entity_id} in {sentence_text!r}")
        if surface not in sentence_text:
            raise ValueError(f"{self.document_id}: {surface!r} not in {sentence_text!r}")
        key = (sentence_text, surface)
        if occurrence is None:
            occurrence = self._surface_counts.get(key, 0)
        self._surface_counts[key] = max(self._surface_counts.get(key, 0), occurrence + 1)
        spec = MentionSpec(entity_id=entity_id, sentence_text=sentence_text,
                           surface=surface, occurrence=occurrence)
        self.mentions.append(spec)
        return spec

    def edge(self, fact: Fact, sentence_text: str) -> None:
        if fact.kind != "edge":
            raise ValueError(f"{fact.fact_id} is not an edge")
        if fact.fact_id in self._stated_ids:
            raise ValueError(f"fact {fact.fact_id} stated twice in {self.document_id}")
        self._stated_ids.add(fact.fact_id)
        self.stated_facts.append(StatedFact(fact=fact, sentence_text=sentence_text))
        self.mention(fact.subject_id or "", sentence_text)
        self.mention(fact.object_id or "", sentence_text)

    def proposition(self, fact: Fact, sentence_text: str) -> None:
        if fact.kind != "proposition":
            raise ValueError(f"{fact.fact_id} is not a proposition")
        if fact.fact_id in self._stated_ids:
            raise ValueError(f"fact {fact.fact_id} stated twice in {self.document_id}")
        self._stated_ids.add(fact.fact_id)
        self.stated_facts.append(StatedFact(fact=fact, sentence_text=sentence_text))
        self.mention(fact.subject_id or "", sentence_text)
        if fact.speaker_id:
            self.mention(fact.speaker_id, sentence_text)
        if fact.reporter_id:
            self.mention(fact.reporter_id, sentence_text)

    def link(self, link_id: str, src: str, dst: str, link_type: str,
             sentence_text: str) -> None:
        self.links.append(LinkSpec(link_id=link_id, src=src, dst=dst,
                                   type=link_type, sentence_text=sentence_text))

    def time(self, time_id: str, sentence_text: str, surface: str, kind: str,
             value_start: str | None = None, value_end: str | None = None,
             granularity: str | None = None, anchor_type: str | None = None,
             label: str | None = None) -> None:
        if surface not in sentence_text:
            raise ValueError(f"{self.document_id}: time surface {surface!r} not in sentence")
        self.times.append(TimeSpec(
            time_id=time_id, sentence_text=sentence_text, surface=surface, kind=kind,
            value_start=value_start, value_end=value_end,
            granularity=granularity, anchor_type=anchor_type, label=label,
        ))

    def trap(self, kind: str) -> None:
        if kind not in self.trap_kinds:
            self.trap_kinds.append(kind)

    # -- plan assembly ----------------------------------------------------------
    def finish(self, notes: str, *, subject_entity_id: str, counterpart_entity_id: str,
               bank_entity_id: str, law_firm_entity_id: str, court_entity_id: str,
               place_entity_id: str) -> DocumentPlan:
        # Every page must carry text.  A handful of neutral secretariat lines
        # prevents a short plan from collapsing two pages into one.
        n = 1
        while len(self.sentences) < self.pages:
            self.sentence(f"The secretariat recorded filing reference {self.document_id}-{n:03d}.")
            n += 1

        base, extra = divmod(len(self.sentences), self.pages)
        sizes = [base + 1] * extra + [base] * (self.pages - extra)
        page_lines: list[list[str]] = []
        cursor = 0
        for size in sizes:
            page_lines.append(self.sentences[cursor:cursor + size])
            cursor += size
        if len(page_lines) != self.pages or any(not page for page in page_lines):
            raise AssertionError(f"bad page allocation for {self.document_id}")

        return DocumentPlan(
            index=self.index,
            document_id=self.document_id,
            filename=f"{self.document_id}_{self.doc_type}.txt",
            doc_type=self.doc_type,
            doc_date=self.doc_date,
            pages=self.pages,
            page_lines=page_lines,
            sentences=list(self.sentences),
            stated_facts=list(self.stated_facts),
            mentions=list(self.mentions),
            links=list(self.links),
            times=list(self.times),
            identity_traps=list(self.identity_traps),
            flags=list(self.flags),
            trap_kinds=list(self.trap_kinds),
            contradiction_id=self.contradiction_id,
            notes=notes,
            subject_entity_id=subject_entity_id,
            counterpart_entity_id=counterpart_entity_id,
            bank_entity_id=bank_entity_id,
            law_firm_entity_id=law_firm_entity_id,
            court_entity_id=court_entity_id,
            place_entity_id=place_entity_id,
        )


def _add_document_opening(b: _Builder, company, counterpart, bank, court, law_firm) -> None:
    label = DOC_LABELS[b.doc_type]
    title = b.sentence(f"1. {company.name} - {label}")
    b.mention(company.entity_id, title)

    date_text = f"Date: {format_date(b.doc_date)}."
    b.sentence(date_text)
    b.time(f"tr_doc_date_{b.index:05d}", date_text, format_date(b.doc_date),
           "INSTANT", b.doc_date.isoformat(), granularity="day", anchor_type="ABSOLUTE")

    b.sentence(f"Document reference: GEN-{b.index:05d}.")
    b.sentence("2. Parties and purpose")

    if b.doc_type == "board_minutes":
        text = (f"The board of {company.name} met at the offices of {law_firm.name} "
                f"to consider the matter with {counterpart.name}.")
        b.sentence(text)
        for entity in (company, law_firm, counterpart):
            b.mention(entity.entity_id, text)
    elif b.doc_type == "financial_agreement":
        text = (f"The facility is made between {bank.name} as lender and "
                f"{company.name} as borrower.")
        b.sentence(text)
        b.mention(bank.entity_id, text)
        b.mention(company.entity_id, text)
    elif b.doc_type == "due_diligence":
        text = (f"The review covers {company.name}, its subsidiary, and the commercial "
                f"position of {counterpart.name}.")
        b.sentence(text)
        b.mention(company.entity_id, text)
        b.mention(counterpart.entity_id, text)
    elif b.doc_type == "memorandum":
        text = (f"The memorandum is addressed to the board of {company.name} and copied "
                f"to {law_firm.name}.")
        b.sentence(text)
        b.mention(company.entity_id, text)
        b.mention(law_firm.entity_id, text)
    elif b.doc_type == "case_notes":
        text = (f"The claim is brought by {company.name} against {counterpart.name} "
                f"in {court.name}.")
        b.sentence(text)
        for entity in (company, counterpart, court):
            b.mention(entity.entity_id, text)
    elif b.doc_type == "correspondence":
        text = (f"This letter is sent by {law_firm.name} on behalf of {company.name} "
                f"to {counterpart.name}.")
        b.sentence(text)
        for entity in (law_firm, company, counterpart):
            b.mention(entity.entity_id, text)
    else:  # press_release
        text = (f"The announcement concerns {company.name}, {counterpart.name} and "
                f"the transaction described below.")
        b.sentence(text)
        b.mention(company.entity_id, text)
        b.mention(counterpart.entity_id, text)


def _add_group_owns(b: _Builder, company, bundle) -> None:
    world = b.world
    owns = bundle.get("owns", [])
    if not owns:
        return
    fact = owns[0]
    target = world.entity(fact.object_id or "")
    text = f"{company.name} owns {target.name}."
    b.sentence(text)
    b.edge(fact, text)
    b.trap("cross_document_relationship")


def _add_group_chair(b: _Builder, company, bundle) -> None:
    world = b.world
    chairs = bundle.get("chair", [])
    if len(chairs) >= 2:
        old, new = chairs[0], chairs[1]
        old_person = world.display_name(old.subject_id or "")
        new_person = world.display_name(new.subject_id or "")
        text = f"{old_person} served as chair of {company.name} until 30 June 2023."
        b.sentence(text)
        b.edge(old, text)
        b.time(f"tr_{old.fact_id}", text, "30 June 2023", "INSTANT",
               "2023-06-30", granularity="day", anchor_type="ABSOLUTE")
        text = f"{new_person} became chair of {company.name} on 1 July 2023."
        b.sentence(text)
        b.edge(new, text)
        b.time(f"tr_{new.fact_id}", text, "1 July 2023", "INSTANT",
               "2023-07-01", granularity="day", anchor_type="ABSOLUTE")
        b.trap("historical_change")
    elif chairs:
        fact = chairs[0]
        chair = world.display_name(fact.subject_id or "")
        text = f"{chair} is the chair of {company.name}."
        b.sentence(text)
        b.edge(fact, text)


def _add_group_finance(b: _Builder, company, bundle) -> None:
    world = b.world
    loans = bundle.get("loan", [])
    if loans:
        fact = loans[0]
        lender = world.entity(fact.subject_id or "")
        amount = (fact.amount_gbp or 0) // 1_000_000
        text = f"{lender.name} lent {company.name} GBP {amount} million on {format_date(fact.date or b.doc_date)}."
        b.sentence(text)
        b.edge(fact, text)
        b.time(f"tr_{fact.fact_id}", text, format_date(fact.date or b.doc_date),
               "INSTANT", (fact.date or b.doc_date).isoformat(),
               granularity="day", anchor_type="ABSOLUTE")

    guarantees = bundle.get("guarantee", [])
    if guarantees:
        fact = guarantees[0]
        guarantor = world.entity(fact.subject_id or "")
        text = f"{guarantor.name} guaranteed the loan to {company.name}."
        b.sentence(text)
        b.edge(fact, text)

    owes = bundle.get("owes", [])
    if owes:
        fact = owes[0]
        creditor = world.entity(fact.object_id or "")
        amount = (fact.amount_gbp or 0) // 1_000_000
        date_text = format_date(fact.date or b.doc_date)
        text = (f"{company.name} owes {creditor.name} GBP {amount} million "
                f"as at {date_text}.")
        b.sentence(text)
        b.edge(fact, text)
        b.time(f"tr_{fact.fact_id}", text, date_text, "INSTANT",
               (fact.date or b.doc_date).isoformat(),
               granularity="day", anchor_type="ABSOLUTE")


def _add_group_represents(b: _Builder, company, law_firm, bundle) -> None:
    represents = bundle.get("represents", [])
    if represents:
        fact = represents[0]
        # The bundle fact is authoritative: in the expanded v2 pool the
        # selected law firm may differ from the base relationship's subject.
        firm = (b.world.display_name(fact.subject_id) if fact.subject_id
                else law_firm.name)
        text = f"{firm} represents {company.name} in the matter."
        b.sentence(text)
        b.edge(fact, text)


def _add_group_people(b: _Builder, company, bundle) -> None:
    world = b.world
    shareholders = bundle.get("shareholder", [])
    if shareholders:
        fact = shareholders[0]
        person = world.display_name(fact.subject_id or "")
        text = f"{person} is a shareholder of {company.name}."
        b.sentence(text)
        b.edge(fact, text)

    family = bundle.get("family", [])
    if family:
        fact = family[0]
        a = world.display_name(fact.subject_id or "")
        z = world.display_name(fact.object_id or "")
        text = f"{a} is a family member of {z}."
        b.sentence(text)
        b.edge(fact, text)


def _add_group_assets(b: _Builder, company, bundle) -> None:
    world = b.world
    asset_own = bundle.get("asset_own", [])
    asset_loc = bundle.get("asset_loc", [])
    if asset_own:
        fact = asset_own[0]
        asset = world.entity(fact.object_id or "")
        text = f"{company.name} owns {asset.name}."
        b.sentence(text)
        b.edge(fact, text)
    if asset_loc:
        fact = asset_loc[0]
        asset = world.entity(fact.subject_id or "")
        place = world.entity(fact.object_id or "")
        text = f"{asset.name} is located in {place.name}."
        b.sentence(text)
        b.edge(fact, text)


def _add_group_legal(b: _Builder, company, bundle) -> None:
    world = b.world
    legal_party = bundle.get("legal_party", [])
    if legal_party:
        fact = legal_party[0]
        legal = world.entity(fact.object_id or "")
        text = f"{company.name} is a party to the {legal.name}."
        b.sentence(text)
        b.edge(fact, text)

    legal_signed = bundle.get("legal_signed", [])
    if legal_signed:
        fact = legal_signed[0]
        signer = world.display_name(fact.subject_id or "")
        legal = world.entity(fact.object_id or "")
        text = f"{signer} signed the {legal.name}."
        b.sentence(text)
        b.edge(fact, text)


def _add_world_facts(b: _Builder, company, counterpart, bank, law_firm, bundle) -> None:
    # The order is part of the Phase 1 output contract.
    _add_group_owns(b, company, bundle)
    _add_group_chair(b, company, bundle)
    _add_group_finance(b, company, bundle)
    _add_group_represents(b, company, law_firm, bundle)
    _add_group_people(b, company, bundle)
    _add_group_assets(b, company, bundle)
    _add_group_legal(b, company, bundle)


def _add_amount_change(b: _Builder, company, bank, bundle) -> None:
    facts = bundle.get("facility", [])
    if len(facts) < 2:
        return
    old, new = facts[0], facts[1]
    old_amount = (old.amount_gbp or 0) // 1_000_000
    new_amount = (new.amount_gbp or 0) // 1_000_000
    old_date = format_date(old.date or b.doc_date)
    new_date = format_date(new.date or b.doc_date)
    text = (f"{bank.name} provided a facility of GBP {old_amount} million to "
            f"{company.name} on {old_date}.")
    b.sentence(text)
    b.proposition(old, text)
    b.mention(bank.entity_id, text)
    b.time(f"tr_{old.fact_id}", text, old_date, "INSTANT",
           (old.date or b.doc_date).isoformat(), granularity="day",
           anchor_type="ABSOLUTE")
    text = (f"{bank.name} increased the facility available to {company.name} to "
            f"GBP {new_amount} million on {new_date}.")
    b.sentence(text)
    b.proposition(new, text)
    b.mention(bank.entity_id, text)
    b.time(f"tr_{new.fact_id}", text, new_date, "INSTANT",
           (new.date or b.doc_date).isoformat(), granularity="day",
           anchor_type="ABSOLUTE")
    b.trap("changing_financial_amounts")


def _add_contradiction(b: _Builder, world: World, conflict: Fact,
                       counterpart: Fact, counterpart_filename: str) -> None:
    amount = (conflict.amount_gbp or 0) // 1_000_000
    bank = world.entity(conflict.subject_id or "")
    company = world.entity(conflict.object_id or "")
    text = (f"{bank.name} lent {company.name} GBP {amount} million on 15 January 2024.")
    b.sentence(text)
    b.edge(conflict, text)
    b.time(f"tr_{conflict.fact_id}", text, "15 January 2024", "INSTANT",
           "2024-01-15", granularity="day", anchor_type="ABSOLUTE")
    b.trap("cross_document_contradiction")
    b.contradiction_id = "contradiction_loan_01"
    b.flags.append(FlagSpec(
        kind="conflict", object_ids=(conflict.fact_id,),
        note=(f"Paired with {counterpart.fact_id} stated in "
              f"{counterpart_filename}; the same loan is stated at a different amount."),
    ))


def _add_common_traps(b: _Builder, world: World, company, counterpart,
                      law_firm, place) -> None:
    # 1. Same name, different people.  Every fact-bearing mention is qualified
    # with the person's department, so the two IDs stay distinguishable.
    p1, p2 = world.duplicate_names
    name1 = world.display_name(p1)
    name2 = world.display_name(p2)
    text = f"{name1} and {name2} are different individuals."
    b.sentence(text)
    b.mention(p1, text, surface=name1, occurrence=0)
    b.mention(p2, text, surface=name2, occurrence=0)
    b.identity_traps.append((p1, p2, "Two different people share the same display name."))
    b.trap("same_name_different_people")
    b.flags.append(FlagSpec(
        kind="identity_ambiguity", object_ids=(p1, p2),
        note="Do not merge the two people with the same name.",
    ))

    # 2. Same person, different name/alias.
    alias_person = world.alias_person
    alias_entity = world.entity(alias_person)
    alias = alias_entity.aliases[0]
    text = (f"{alias_entity.name}, also known as {alias}, confirmed the "
            f"treasury report.")
    b.sentence(text)
    b.mention(alias_person, text, surface=alias_entity.name, occurrence=0)
    b.mention(alias_person, text, surface=alias, occurrence=0)
    b.trap("alias_same_person")

    # 3. A negated critical relationship.
    guarantee_neg = Fact(
        fact_id=f"{b.document_id}_neg_guarantee", kind="edge",
        subject_id=company.entity_id, relation="guarantor_for",
        object_id=counterpart.entity_id, date=b.doc_date, negated=True,
        note="explicitly denied guarantee",
    )
    text = f"{company.name} did not guarantee the facility for {counterpart.name} on {format_date(b.doc_date)}."
    b.sentence(text)
    b.edge(guarantee_neg, text)
    b.trap("negated_relationship")

    # 4. Proposed versus completed.  The chair is named so the proposition has
    # an explicit speaker whose mention can be grounded in the same sentence.
    target = world.companies()[(b.index + 5) % len(world.companies())]
    chair = world.entity(bundle_chair(world, b.index))
    chair_name = world.display_name(chair.entity_id)
    proposal = Fact(
        fact_id=f"{b.document_id}_proposal", kind="proposition",
        subject_id=company.entity_id, speaker_id=chair.entity_id,
        status="proposed", date=b.doc_date,
        text=f"The acquisition of {target.name} by {company.name}",
    )
    text = (f"{chair_name}, chair of {company.name}, proposed to acquire "
            f"{target.name} on {format_date(b.doc_date)}.")
    b.sentence(text)
    b.proposition(proposal, text)
    b.mention(target.entity_id, text)
    b.trap("proposed_vs_completed")

    # 5. Conditional relationship: a proposition and a logic link.
    approval = Fact(
        fact_id=f"{b.document_id}_approval", kind="proposition",
        subject_id=company.entity_id, speaker_id=chair.entity_id,
        status="stated", negated=True,
        text=f"Regulatory approval for the acquisition of {target.name}",
    )
    text = (f"{chair_name} confirmed that regulatory approval for the acquisition "
            f"of {target.name} by {company.name} has not been granted.")
    b.sentence(text)
    b.proposition(approval, text)
    b.mention(target.entity_id, text)
    text = f"The acquisition of {target.name} by {company.name} is conditional on regulatory approval."
    b.sentence(text)
    b.link(f"{b.document_id}_link_conditional", proposal.fact_id, approval.fact_id,
           "conditional_on", text)
    b.trap("conditional_relationship")

    # 6. Relative date.
    due = add_months(b.doc_date, 6)
    text = "The facility will be reviewed six months after the date of this note."
    b.sentence(text)
    b.time(f"tr_relative_{b.index:05d}", text, "six months after the date of this note",
           "RELATIVE", due.isoformat(), granularity="day", anchor_type="DOC_DATE")
    b.trap("relative_date")

    # 7. Fiscal date.  The document states the period explicitly; the corpus
    # must not ask the reader to infer a fiscal-year start month.
    start_month = world.ontology.fiscal_year_start_month
    if b.doc_date.month >= start_month:
        start_year, end_year = b.doc_date.year, b.doc_date.year + 1
    else:
        start_year, end_year = b.doc_date.year - 1, b.doc_date.year
    fiscal_start = dt.date(start_year, start_month, 1)
    fiscal_end = dt.date(end_year, start_month, 1) - dt.timedelta(days=1)
    label = f"FY{end_year}"
    period_text = f"{format_date(fiscal_start)} to {format_date(fiscal_end)}"
    text = f"The treasury plan covers {label}, which runs from {period_text}."
    b.sentence(text)
    b.time(f"tr_fiscal_{b.index:05d}", text, period_text, "PERIOD",
           fiscal_start.isoformat(), fiscal_end.isoformat(),
           granularity="year", anchor_type="ABSOLUTE", label=label)
    b.trap("fiscal_date")

    # 8. Irrelevant distractor with no gold fact.
    text = (f"The {place.name} office refurbishment was budgeted at GBP "
            f"{(b.index + 3) * 7},000.")
    b.sentence(text)
    b.trap("irrelevant_distractor")

    # 9. Alleged fact, used in the document types where allegations are natural.
    # The speaker is the unnamed former employee who made the allegation; the
    # named person merely reported it.
    if b.doc_type in ("case_notes", "due_diligence", "memorandum", "press_release"):
        allegation = Fact(
            fact_id=f"{b.document_id}_allegation", kind="proposition",
            subject_id=company.entity_id, speaker_id=None,
            reporter_id=chair.entity_id,
            status="alleged", date=b.doc_date,
            text=f"A former employee alleged that {company.name} misstated its revenue",
        )
        text = (f"{chair_name} noted that a former employee alleged that "
                f"{company.name} misstated its revenue.")
        b.sentence(text)
        b.proposition(allegation, text)
        b.trap("alleged_fact")


def _add_selected_traps(b: _Builder, world: World, company, counterpart,
                        law_firm, place, kinds: set[str]) -> None:
    """Phase 2 subset of traps, selected by document type.

    The order is fixed, so a given document always carries the same trap
    statements.  Proposal and conditional traps are always selected together.
    """
    chair_id = bundle_chair(world, b.index)
    chair = world.entity(chair_id)
    chair_name = world.display_name(chair.entity_id)
    target = None
    proposal_fact = None

    if "same_name_different_people" in kinds:
        p1, p2 = world.duplicate_names
        name1 = world.display_name(p1)
        name2 = world.display_name(p2)
        text = f"{name1} and {name2} are different individuals."
        b.sentence(text)
        b.mention(p1, text, surface=name1, occurrence=0)
        b.mention(p2, text, surface=name2, occurrence=0)
        b.identity_traps.append(
            (p1, p2, "Two different people share the same display name."))
        b.trap("same_name_different_people")
        b.flags.append(FlagSpec(
            kind="identity_ambiguity", object_ids=(p1, p2),
            note="Do not merge the two people with the same name.",
        ))

    if "alias_same_person" in kinds:
        alias_person = world.alias_person
        alias_entity = world.entity(alias_person)
        alias = alias_entity.aliases[0]
        text = (f"{alias_entity.name}, also known as {alias}, confirmed the "
                f"treasury report.")
        b.sentence(text)
        b.mention(alias_person, text, surface=alias_entity.name, occurrence=0)
        b.mention(alias_person, text, surface=alias, occurrence=0)
        b.trap("alias_same_person")

    if "negated_relationship" in kinds:
        guarantee_neg = Fact(
            fact_id=f"{b.document_id}_neg_guarantee", kind="edge",
            subject_id=company.entity_id, relation="guarantor_for",
            object_id=counterpart.entity_id, date=b.doc_date, negated=True,
            note="explicitly denied guarantee",
        )
        text = (f"{company.name} did not guarantee the facility for "
                f"{counterpart.name} on {format_date(b.doc_date)}.")
        b.sentence(text)
        b.edge(guarantee_neg, text)
        b.trap("negated_relationship")

    if "proposed_vs_completed" in kinds:
        target = world.companies()[(b.index + 5) % len(world.companies())]
        proposal_fact = Fact(
            fact_id=f"{b.document_id}_proposal", kind="proposition",
            subject_id=company.entity_id, speaker_id=chair.entity_id,
            status="proposed", date=b.doc_date,
            text=f"The acquisition of {target.name} by {company.name}",
        )
        text = (f"{chair_name}, chair of {company.name}, proposed to acquire "
                f"{target.name} on {format_date(b.doc_date)}.")
        b.sentence(text)
        b.proposition(proposal_fact, text)
        b.mention(target.entity_id, text)
        b.trap("proposed_vs_completed")

    if ("conditional_relationship" in kinds and target is not None
            and proposal_fact is not None):
        approval = Fact(
            fact_id=f"{b.document_id}_approval", kind="proposition",
            subject_id=company.entity_id, speaker_id=chair.entity_id,
            status="stated", negated=True,
            text=f"Regulatory approval for the acquisition of {target.name}",
        )
        text = (f"{chair_name} confirmed that regulatory approval for the acquisition "
                f"of {target.name} by {company.name} has not been granted.")
        b.sentence(text)
        b.proposition(approval, text)
        b.mention(target.entity_id, text)
        text = (f"The acquisition of {target.name} by {company.name} is "
                f"conditional on regulatory approval.")
        b.sentence(text)
        b.link(f"{b.document_id}_link_conditional", proposal_fact.fact_id,
               approval.fact_id, "conditional_on", text)
        b.trap("conditional_relationship")

    if "relative_date" in kinds:
        due = add_months(b.doc_date, 6)
        text = "The facility will be reviewed six months after the date of this note."
        b.sentence(text)
        b.time(f"tr_relative_{b.index:05d}", text,
               "six months after the date of this note",
               "RELATIVE", due.isoformat(), granularity="day",
               anchor_type="DOC_DATE")
        b.trap("relative_date")

    if "fiscal_date" in kinds:
        start_month = world.ontology.fiscal_year_start_month
        if b.doc_date.month >= start_month:
            start_year, end_year = b.doc_date.year, b.doc_date.year + 1
        else:
            start_year, end_year = b.doc_date.year - 1, b.doc_date.year
        fiscal_start = dt.date(start_year, start_month, 1)
        fiscal_end = dt.date(end_year, start_month, 1) - dt.timedelta(days=1)
        label = f"FY{end_year}"
        period_text = f"{format_date(fiscal_start)} to {format_date(fiscal_end)}"
        text = f"The treasury plan covers {label}, which runs from {period_text}."
        b.sentence(text)
        b.time(f"tr_fiscal_{b.index:05d}", text, period_text, "PERIOD",
               fiscal_start.isoformat(), fiscal_end.isoformat(),
               granularity="year", anchor_type="ABSOLUTE", label=label)
        b.trap("fiscal_date")

    if "irrelevant_distractor" in kinds:
        text = (f"The {place.name} office refurbishment was budgeted at GBP "
                f"{(b.index + 3) * 7},000.")
        b.sentence(text)
        b.trap("irrelevant_distractor")

    if "alleged_fact" in kinds:
        allegation = Fact(
            fact_id=f"{b.document_id}_allegation", kind="proposition",
            subject_id=company.entity_id, speaker_id=None,
            reporter_id=chair.entity_id,
            status="alleged", date=b.doc_date,
            text=f"A former employee alleged that {company.name} misstated its revenue",
        )
        text = (f"{chair_name} noted that a former employee alleged that "
                f"{company.name} misstated its revenue.")
        b.sentence(text)
        b.proposition(allegation, text)
        b.trap("alleged_fact")


def bundle_chair(world: World, index: int) -> str:
    company_index = index % len(world.companies())
    chairs = world.facts_for_company(company_index).get("chair", [])
    if not chairs:
        raise ValueError(f"no chair fact for company index {company_index}")
    return chairs[-1].subject_id or ""


def _v2_pick(pool: list, base_count: int, index: int):
    """Pick an entity while freezing the first seven documents.

    The first seven Phase 2 documents must be byte-identical at 20, 500 and
    5,000 pages.  They therefore use the base pool with its original modulo
    mapping.  Later documents use the expanded, append-only pool.
    """
    if index < V2_STABLE_DOC_PREFIX:
        return pool[index % base_count]
    return pool[index % len(pool)]


def _v2_company_index(world: World, entity_id: str) -> int:
    # ``companies()`` is sorted lexicographically for Phase 1 stability, so a
    # company's list position is not its fact-bundle index once cohort ids such
    # as company_100 exist.  The world records the authoritative mapping.
    return world.company_index(entity_id)


def _v2_section(b: _Builder, title: str, lead: str | None = None) -> None:
    b.sentence(title)
    if lead:
        b.sentence(lead)


def _add_opening_v2(b: _Builder, doc_type: str, company, counterpart, bank,
                    court, law_firm) -> None:
    world = b.world
    titles = {
        "board_minutes": f"Board meeting minutes of {company.name}",
        "financial_agreement": f"Facility agreement between {bank.name} and {company.name}",
        "due_diligence": f"Due diligence report on {company.name}",
        "memorandum": f"Memorandum regarding {company.name}",
        "case_notes": f"Case notes for {company.name} against {counterpart.name}",
        "correspondence": f"Letter from {law_firm.name} on behalf of {company.name}",
        "press_release": f"Press release concerning {company.name}",
    }
    title = b.sentence(titles[doc_type])
    b.mention(company.entity_id, title)
    if doc_type in ("financial_agreement",):
        b.mention(bank.entity_id, title)
    if doc_type == "case_notes":
        b.mention(counterpart.entity_id, title)
    if doc_type == "correspondence":
        b.mention(law_firm.entity_id, title)

    if doc_type == "press_release":
        date_text = f"Publication date: {format_date(b.doc_date)}."
    elif doc_type == "memorandum":
        date_text = f"Date: {format_date(b.doc_date)}."
    else:
        date_text = f"Date: {format_date(b.doc_date)}."
    b.sentence(date_text)
    b.time(f"tr_doc_date_{b.index:05d}", date_text, format_date(b.doc_date),
           "INSTANT", b.doc_date.isoformat(), granularity="day",
           anchor_type="ABSOLUTE")

    b.sentence(f"Document reference: GEN-{b.index:05d}.")

    if doc_type == "board_minutes":
        _v2_section(b, "1. Attendance",
                    "The meeting was held at the offices of "
                    f"{law_firm.name} and was quorate throughout.")
        chair = world.entity(bundle_chair(
            world, _v2_company_index(world, company.entity_id)))
        director_facts = world.facts_for_company(
            _v2_company_index(world, company.entity_id)).get("director", [])
        director = (world.entity(director_facts[-1].subject_id or chair.entity_id)
                    if director_facts else chair)
        text = (f"The following directors were present: {world.display_name(chair.entity_id)}, "
                f"{world.display_name(director.entity_id)} and {law_firm.name}.")
        b.sentence(text)
        b.mention(chair.entity_id, text)
        b.mention(director.entity_id, text)
        b.mention(law_firm.entity_id, text)
        _v2_section(b, "2. Agenda",
                    "The agenda covered group structure, financing arrangements, "
                    "legal review and the acquisition proposal.")
        _v2_section(b, "3. Matters discussed",
                    "The board reviewed the documents circulated before the meeting.")
    elif doc_type == "financial_agreement":
        _v2_section(b, "1. Recitals",
                    "The borrower requested a term facility for working capital and "
                    "refinancing.")
        _v2_section(b, "2. Parties",
                    f"The lender is {bank.name} and the borrower is {company.name}.")
    elif doc_type == "due_diligence":
        _v2_section(b, "1. Scope",
                    f"The review covers {company.name}, its subsidiary and the "
                    f"commercial position of {counterpart.name}.")
        _v2_section(b, "2. Findings",
                    "The review examined corporate records and the commercial position.")
    elif doc_type == "memorandum":
        b.sentence(f"To: The board of {company.name}.")
        b.mention(company.entity_id, f"To: The board of {company.name}.")
        b.sentence(f"From: {law_firm.name}.")
        b.mention(law_firm.entity_id, f"From: {law_firm.name}.")
        subject = f"Subject: Review of the facility and acquisition proposal."
        b.sentence(subject)
        _v2_section(b, "1. Background",
                    "The board asked for a short review of the group's current position.")
    elif doc_type == "case_notes":
        _v2_section(b, "1. Parties",
                    f"The claim is brought by {company.name} against {counterpart.name} "
                    f"in {court.name}.")
        _v2_section(b, "2. Procedural history",
                    "The claim was managed in the ordinary commercial list.")
    elif doc_type == "correspondence":
        b.sentence(f"Sender: {law_firm.name}.")
        b.mention(law_firm.entity_id, f"Sender: {law_firm.name}.")
        b.sentence(f"Recipient: {counterpart.name}.")
        b.mention(counterpart.entity_id, f"Recipient: {counterpart.name}.")
        subject = "Subject: Facility arrangements and legal review."
        b.sentence(subject)
        _v2_section(b, "1. Body",
                    f"We write on behalf of {company.name} regarding the matter below.")
    else:  # press_release
        _v2_section(b, "1. Corporate statement",
                    f"{company.name} today issued the following statement.",
                    )
        # The sentence above names the company, but the title is the canonical
        # title mention; keep the statement's mention too.
        b.mention(company.entity_id, b.sentences[-1])


def _add_body_sections_v2(b: _Builder, doc_type: str, company, counterpart,
                          bank, law_firm, bundle) -> None:
    world = b.world
    if doc_type == "board_minutes":
        _add_group_owns(b, company, bundle)
        _add_group_chair(b, company, bundle)
        _add_group_people(b, company, bundle)
        _add_group_finance(b, company, bundle)
        _add_group_represents(b, company, law_firm, bundle)
        _add_group_assets(b, company, bundle)
        _add_group_legal(b, company, bundle)

    elif doc_type == "financial_agreement":
        _add_group_owns(b, company, bundle)
        _add_group_finance(b, company, bundle)
        _v2_section(b, "3. Facility terms",
                    "The facility is available subject to the conditions below.")
        if b.index % len(V2_PAGE_BLOCK) == 1:
            _add_amount_change(b, company, bank, bundle)
        _v2_section(b, "4. Conditions precedent",
                    "The conditions precedent are set out in the schedule to the agreement.")
        _v2_section(b, "5. Repayment",
                    "The facility is repayable on the dates set out in the repayment schedule.")
        _add_group_legal(b, company, bundle)
        _v2_section(b, "6. Execution",
                    "The parties executed the agreement on the date stated above.")

    elif doc_type == "due_diligence":
        _add_group_owns(b, company, bundle)
        _add_group_assets(b, company, bundle)
        _add_group_represents(b, company, law_firm, bundle)
        _add_group_people(b, company, bundle)
        _v2_section(b, "3. Risks", "The review identified the following financial exposures.")
        _add_group_finance(b, company, bundle)
        _v2_section(b, "4. Discrepancies",
                    "The review compared the documents provided by management.")
        _add_group_legal(b, company, bundle)
        _v2_section(b, "5. Recommendations",
                    "Management should resolve the discrepancies before completion.")

    elif doc_type == "memorandum":
        _add_group_owns(b, company, bundle)
        _add_group_people(b, company, bundle)
        _v2_section(b, "2. Analysis",
                    "The analysis draws on the documents circulated with this note.")
        _add_group_finance(b, company, bundle)
        _add_group_represents(b, company, law_firm, bundle)
        _add_group_assets(b, company, bundle)
        _add_group_legal(b, company, bundle)
        _v2_section(b, "3. Recommendations",
                    "The board is invited to note the analysis and approve the next steps.")

    elif doc_type == "case_notes":
        _add_group_legal(b, company, bundle)
        _add_group_chair(b, company, bundle)
        _v2_section(b, "3. Submissions",
                    "The parties made written and oral submissions to the court.")
        _add_group_finance(b, company, bundle)
        _add_group_people(b, company, bundle)
        _v2_section(b, "4. Findings and allegations",
                    "The court considered the evidence and the pleaded case.")
        _add_group_owns(b, company, bundle)
        _v2_section(b, "5. Orders and next steps",
                    "The court gave directions for the next hearing.")

    elif doc_type == "correspondence":
        _add_group_owns(b, company, bundle)
        _add_group_people(b, company, bundle)
        _add_group_finance(b, company, bundle)
        _add_group_assets(b, company, bundle)
        _add_group_legal(b, company, bundle)
        _v2_section(b, "2. Closing",
                    "We look forward to your response within fourteen days.")

    else:  # press_release
        _add_group_owns(b, company, bundle)
        _add_group_people(b, company, bundle)
        _add_group_finance(b, company, bundle)
        _v2_section(b, "2. Announcements",
                    "The company also confirmed the following corporate matters.")
        _add_group_assets(b, company, bundle)
        _add_group_legal(b, company, bundle)
        _v2_section(b, "3. Contact",
                    f"Media enquiries should be directed to {law_firm.name}.")
        contact = b.sentences[-1]
        b.mention(law_firm.entity_id, contact)
        _v2_section(b, "4. Closing",
                    "The company will publish further information in due course.")


#: Traps selected for each Phase 2 document type.  Proposal and conditional
#: traps are always paired; cross-document traps are added by the fact builders.
V2_TRAP_SCHEDULE = {
    "board_minutes": {
        "same_name_different_people", "proposed_vs_completed",
        "conditional_relationship", "fiscal_date",
    },
    "financial_agreement": {
        "negated_relationship", "relative_date",
    },
    "due_diligence": {
        "alias_same_person", "alleged_fact", "irrelevant_distractor",
        "fiscal_date",
    },
    "memorandum": {
        "same_name_different_people", "proposed_vs_completed",
        "conditional_relationship", "fiscal_date",
    },
    "case_notes": {
        "alleged_fact", "negated_relationship", "relative_date",
    },
    "correspondence": {
        "alias_same_person", "proposed_vs_completed",
        "conditional_relationship",
    },
    "press_release": {
        "same_name_different_people", "alleged_fact", "fiscal_date",
        "irrelevant_distractor",
    },
}


def _build_document_plan_v2(world: World, index: int, pages: int,
                            doc_type: str | None = None) -> DocumentPlan:
    """Build one seven-type Phase 2 document with section structure."""
    if not 1 <= pages <= 5:
        raise ValueError("each document must have 1-5 pages")
    companies = world.companies()
    banks = world.banks()
    law_firms = world.law_firms()
    courts = world.of_kind("court")
    places = world.places()
    company = _v2_pick(companies, 24, index)
    counterpart = _v2_pick(companies, 24, index + 3)
    bank = _v2_pick(banks, 8, index)
    law_firm = _v2_pick(law_firms, 6, index)
    court = _v2_pick(courts, 3, index)
    place = _v2_pick(places, 12, index)
    doc_type = doc_type or DOC_TYPES[index % len(DOC_TYPES)]
    doc_date = dt.date(2024, 2, 1) + dt.timedelta(days=(index * 7) % 1200)

    b = _Builder(world, index, doc_type, doc_date, pages)
    company_index = _v2_company_index(world, company.entity_id)
    bundle = world.facts_for_company(company_index)

    _add_opening_v2(b, doc_type, company, counterpart, bank, court, law_firm)
    _add_body_sections_v2(b, doc_type, company, counterpart, bank, law_firm, bundle)

    # Contradiction pair: financial agreement and correspondence in every
    # seven-document block carry the two sides.
    if index % len(V2_PAGE_BLOCK) in (1, 5):
        local_id = ("fact_conflict_42m" if index % len(V2_PAGE_BLOCK) == 1
                    else "fact_conflict_55m")
        other_id = ("fact_conflict_55m" if index % len(V2_PAGE_BLOCK) == 1
                    else "fact_conflict_42m")
        other_index = index + 4 if index % len(V2_PAGE_BLOCK) == 1 else index - 4
        other_type = DOC_TYPES[other_index % len(DOC_TYPES)]
        other_filename = f"gen_doc_{other_index:05d}_{other_type}.txt"
        _add_contradiction(b, world, world.fact(local_id),
                           world.fact(other_id), other_filename)

    _add_selected_traps(b, world, company, counterpart, law_firm, place,
                        V2_TRAP_SCHEDULE[doc_type])

    if doc_type == "board_minutes":
        _v2_section(b, "4. Resolutions",
                    "The board resolved to note the facility arrangements and the "
                    "legal review.")
        _v2_section(b, "5. Actions",
                    "The company secretary will circulate the signed minutes within "
                    "five business days.")

    if not 3 <= len(b.trap_kinds) <= 6:
        raise AssertionError(
            f"{b.document_id}: {len(b.trap_kinds)} traps, expected 3-6")

    notes = "Traps: " + ", ".join(b.trap_kinds) + "."
    return b.finish(
        notes,
        subject_entity_id=company.entity_id,
        counterpart_entity_id=counterpart.entity_id,
        bank_entity_id=bank.entity_id,
        law_firm_entity_id=law_firm.entity_id,
        court_entity_id=court.entity_id,
        place_entity_id=place.entity_id,
    )


def build_document_plan(world: World, index: int, pages: int,
                        doc_type: str | None = None,
                        layout: str = "v1") -> DocumentPlan:
    """Build one document plan.  Deterministic in ``(world.seed, index)``.

    ``layout="v1"`` preserves the frozen Phase 1 five-document stream.
    ``layout="v2"`` selects the seven-type improved document stream.
    """
    if layout == "v2":
        return _build_document_plan_v2(world, index, pages, doc_type)
    if layout != "v1":
        raise ValueError(f"unknown document layout {layout!r}")
    if not 1 <= pages <= 5:
        raise ValueError("each document must have 1-5 pages")
    companies = world.companies()
    banks = world.banks()
    law_firms = world.law_firms()
    courts = world.of_kind("court")
    places = world.places()
    company = companies[index % len(companies)]
    counterpart = companies[(index + 3) % len(companies)]
    bank = banks[index % len(banks)]
    law_firm = law_firms[index % len(law_firms)]
    court = courts[index % len(courts)]
    place = places[index % len(places)]
    doc_type = doc_type or DOC_TYPES[index % len(DOC_TYPES)]
    # Every document is dated after the latest event it reports: the chair
    # hand-over (July 2023), the facility increase (June 2023) and the
    # contradictory loan (January 2024).  A document that describes its own
    # future would be incoherent even if every individual sentence parsed.
    doc_date = dt.date(2024, 2, 1) + dt.timedelta(days=(index * 7) % 1200)

    b = _Builder(world, index, doc_type, doc_date, pages)
    bundle = world.facts_for_company(index % len(companies))

    _add_document_opening(b, company, counterpart, bank, court, law_firm)
    _add_world_facts(b, company, counterpart, bank, law_firm, bundle)

    # The amount-change trap belongs to a facility document, not every file.
    if index % 5 == 1:
        _add_amount_change(b, company, bank, bundle)

    # One contradiction pair, stated on documents 0 and 4 of every five-document
    # block.  The two statements are individually valid and jointly conflicting.
    if index % 5 in (0, 4):
        local_id = "fact_conflict_42m" if index % 5 == 0 else "fact_conflict_55m"
        other_id = "fact_conflict_55m" if index % 5 == 0 else "fact_conflict_42m"
        other_index = index + 4 if index % 5 == 0 else index - 4
        other_type = DOC_TYPES[other_index % len(DOC_TYPES)]
        other_filename = f"gen_doc_{other_index:05d}_{other_type}.txt"
        _add_contradiction(b, world, world.fact(local_id),
                           world.fact(other_id), other_filename)

    _add_common_traps(b, world, company, counterpart, law_firm, place)

    if len(b.trap_kinds) < 3:
        raise AssertionError(f"{b.document_id}: only {len(b.trap_kinds)} traps")

    notes = "Traps: " + ", ".join(b.trap_kinds) + "."
    return b.finish(
        notes,
        subject_entity_id=company.entity_id,
        counterpart_entity_id=counterpart.entity_id,
        bank_entity_id=bank.entity_id,
        law_firm_entity_id=law_firm.entity_id,
        court_entity_id=court.entity_id,
        place_entity_id=place.entity_id,
    )
