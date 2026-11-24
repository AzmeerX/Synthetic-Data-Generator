"""Builders for a small, fully valid V3 corpus used by the model tests.

One corpus, four documents:

* ``doc1`` board minutes — two sections, two paragraphs, one table with header,
  two data rows and a total row; four statements, one fact stated twice (prose
  paragraph and table cell).
* ``doc2`` memorandum — states the other side of an amount contradiction.
* ``doc3``/``doc4`` correspondence — one thread, ordered, the second replies to
  the first.

Everything is constructed bottom-up from a single :class:`IdRegistry`, so every
id in the fixtures comes from the same deterministic allocation the generator
will use.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from synthetic_data.model import (
    Block,
    CdmDocument,
    ColumnSpec,
    Condition,
    Contradiction,
    Corpus,
    CorpusWorld,
    CrossReference,
    DocumentMetadata,
    Entity,
    EntityRef,
    Event,
    FactStatement,
    IdRegistry,
    InlineRun,
    IsoDate,
    MessageMeta,
    Money,
    Section,
    SemanticFact,
    Table,
    TableCell,
    TableRow,
    Thread,
    TimeScope,
    Transaction,
)

SEED = 7
CORPUS_NAME = "v3_test"


@dataclass
class Fixture:
    corpus: Corpus
    ids: dict[str, str]
    registry: IdRegistry
    names: dict[str, str] = field(default_factory=dict)

    def id(self, key: str) -> str:
        return self.ids[key]


def _scopes() -> list[tuple[str, str]]:
    scopes: list[tuple[str, str]] = [
        ("ent:company", "company:0001"),
        ("ent:company", "company:0002"),
        ("ent:bank", "bank:0001"),
        ("ent:person", "person:0001"),
        ("ent:person", "person:0002"),
        ("ent:asset", "asset:0001"),
        ("ent:law_firm", "law_firm:0001"),
        ("fact:edge", "00001:01"),
        ("fact:edge", "00001:02"),
        ("fact:edge", "00001:03"),
        ("fact:prop", "00001:01"),
        ("fact:edge", "00002:01"),
        ("cnd", "00001:01"),
        ("evt", "0001"),
        ("txn", "0001"),
        ("ctr", "0001"),
        ("thr", f"{CORPUS_NAME}:0001"),
        ("msg", "0001/01"),
        ("msg", "0001/02"),
    ]
    scopes += [("doc", f"{n:05d}") for n in range(1, 5)]
    scopes += [
        ("sec", "00001/01"), ("sec", "00001/02"),
        ("sec", "00002/01"), ("sec", "00003/01"), ("sec", "00004/01"),
    ]
    scopes += [
        ("blk", "00001/01.01"), ("blk", "00001/01.02"), ("blk", "00001/02.01"),
        ("blk", "00002/01.01"), ("blk", "00003/01.01"), ("blk", "00004/01.01"),
    ]
    table = "00001/02.t01"
    scopes.append(("tbl", table))
    for r in range(4):
        scopes.append(("tbl", f"{table}/r{r:02d}"))
        for c in range(2):
            scopes.append(("tbl", f"{table}/r{r:02d}c{c:02d}"))
    scopes += [
        ("stmt", "00001/01.a"), ("stmt", "00001/01.b"),
        ("stmt", "00001/02.c"), ("stmt", "00001/02.d"),
        ("stmt", "00002/01.a"), ("stmt", "00003/01.a"), ("stmt", "00004/01.a"),
    ]
    return scopes


def _run(text: str, *, fact_ref: str | None = None, value=None,
         entity_ref: str | None = None) -> InlineRun:
    return InlineRun(
        text=text,
        fact_ref=fact_ref,
        value=value,
        entity_ref=EntityRef(entity_id=entity_ref) if entity_ref else None,
    )


def _cell(cell_id: str, *, text: str | None = None, value=None, display: str = "",
          fact_ref: str | None = None) -> TableCell:
    return TableCell(cell_id=cell_id, text=text, value=value, display=display,
                     fact_ref=fact_ref)


def build_fixture(digest_size: int = 8) -> Fixture:
    reg = IdRegistry(seed=SEED, corpus_name=CORPUS_NAME, digest_size=digest_size)
    reg.register_many(_scopes())
    ids = reg.mapping()

    def i(key: str) -> str:                       # noqa: E306 - local shorthand
        return ids[key]

    # ---- entities ------------------------------------------------------
    company1 = Entity(entity_id=i("ent:company:company:0001"), entity_type="organization",
                      subtype="company", canonical_name="Oakhaven Holdings Limited")
    company2 = Entity(entity_id=i("ent:company:company:0002"), entity_type="organization",
                      subtype="company", canonical_name="Beta Trading Limited")
    bank = Entity(entity_id=i("ent:bank:bank:0001"), entity_type="organization",
                  subtype="bank", canonical_name="Caldervale Bank Limited")
    person1 = Entity(entity_id=i("ent:person:person:0001"), entity_type="person",
                     canonical_name="Amina Kerr", aliases=["A. Kerr"])
    person2 = Entity(entity_id=i("ent:person:person:0002"), entity_type="person",
                     canonical_name="Marek Nyle")
    asset = Entity(entity_id=i("ent:asset:asset:0001"), entity_type="asset",
                   canonical_name="Juniper Data Centre")
    law_firm = Entity(entity_id=i("ent:law_firm:law_firm:0001"), entity_type="organization",
                      subtype="law_firm", canonical_name="Harrow & Finch LLP")
    entities = {e.entity_id: e for e in
                (company1, company2, bank, person1, person2, asset, law_firm)}

    def ref(entity: Entity) -> EntityRef:
        return EntityRef(entity_id=entity.entity_id)

    # ---- table ---------------------------------------------------------
    table_id = i("tbl:00001/02.t01")

    def row(row_index: int, cells: list[TableCell]) -> TableRow:
        return TableRow(row_id=i(f"tbl:00001/02.t01/r{row_index:02d}"), cells=cells)

    def cell(row_index: int, col_index: int) -> str:
        return i(f"tbl:00001/02.t01/r{row_index:02d}c{col_index:02d}")

    amount = Money(amount_minor=400_000_000, currency="GBP")
    table = Table(
        table_id=table_id,
        caption="Facility drawdown",
        columns=[ColumnSpec(name="Party", type="text"),
                 ColumnSpec(name="Amount", type="money", align="right")],
        header_rows=[row(0, [_cell(cell(0, 0), text="Party"),
                             _cell(cell(0, 1), text="Amount")])],
        body_rows=[
            row(1, [_cell(cell(1, 0), text="Oakhaven Holdings Limited"),
                    _cell(cell(1, 1), value=amount, display="GBP 4,000,000.00",
                          fact_ref=i("fact:edge:00001:02"))]),
            row(2, [_cell(cell(2, 0), text="Beta Trading Limited"),
                    _cell(cell(2, 1), text="—")]),
        ],
        total_rows=[row(3, [_cell(cell(3, 0), text="Total"),
                            _cell(cell(3, 1), value=amount,
                                  display="GBP 4,000,000.00")])],
        cell_refs={"r01c01": i("fact:edge:00001:02")},
    )

    # ---- blocks --------------------------------------------------------
    b1 = Block(block_id=i("blk:00001/01.01"), kind="paragraph",
               runs=[_run("Oakhaven Holdings Limited owns Juniper Data Centre.",
                          fact_ref=i("fact:edge:00001:01"),
                          entity_ref=company1.entity_id)])
    b2 = Block(block_id=i("blk:00001/01.02"), kind="paragraph",
               runs=[_run("Caldervale Bank Limited lent GBP 4,000,000 to Oakhaven "
                          "Holdings Limited on 3 March 2024.",
                          fact_ref=i("fact:edge:00001:02"), value=amount,
                          entity_ref=bank.entity_id)])
    b3 = Block(block_id=i("blk:00001/02.01"), kind="table", table=table)
    b4 = Block(block_id=i("blk:00002/01.01"), kind="paragraph",
               runs=[_run("The bank lent GBP 4,500,000 to the company, per the memo.",
                          fact_ref=i("fact:edge:00002:01"),
                          value=Money(amount_minor=450_000_000, currency="GBP"))])
    b5 = Block(block_id=i("blk:00003/01.01"), kind="paragraph",
               runs=[_run("We refer to your letter of 1 March 2024.")])
    b6 = Block(block_id=i("blk:00004/01.01"), kind="paragraph",
               runs=[_run("Thank you for your letter; we reply as follows.")])

    # ---- sections ------------------------------------------------------
    s1 = Section(section_id=i("sec:00001/01"), depth=0, number="1",
                 heading="Purpose", blocks=[b1, b2])
    s2 = Section(section_id=i("sec:00001/02"), depth=0, number="2",
                 heading="Resolutions", blocks=[b3])
    s3 = Section(section_id=i("sec:00002/01"), depth=0, number="1",
                 heading="Analysis", blocks=[b4])
    s4 = Section(section_id=i("sec:00003/01"), depth=0, number="1",
                 heading="Letter", blocks=[b5])
    s5 = Section(section_id=i("sec:00004/01"), depth=0, number="1",
                 heading="Reply", blocks=[b6])

    # ---- statements ----------------------------------------------------
    def statement(scope: str, fact_key: str, node_key: str, surface: str,
                  mentions: list[Entity], slots: dict[str, str] | None = None) -> FactStatement:
        return FactStatement(
            statement_id=i(f"stmt:{scope}"),
            fact_id=i(fact_key),
            node_id=i(node_key),
            surface=surface,
            mention_ids=[m.entity_id for m in mentions],
            value_slots=slots or {},
        )

    st_owns = statement("00001/01.a", "fact:edge:00001:01", "blk:00001/01.01",
                        "Oakhaven Holdings Limited owns Juniper Data Centre.",
                        [company1, asset])
    st_lent = statement("00001/01.b", "fact:edge:00001:02", "blk:00001/01.02",
                        "Caldervale Bank Limited lent GBP 4,000,000 to Oakhaven Holdings Limited.",
                        [bank, company1], {"amount": "GBP 4,000,000"})
    st_prop = statement("00001/02.c", "fact:prop:00001:01", "blk:00001/01.02",
                        "The board approved the facility.", [company1])
    st_lent_cell = statement("00001/02.d", "fact:edge:00001:02",
                             "tbl:00001/02.t01/r01c01",
                             "Oakhaven Holdings Limited | GBP 4,000,000.00",
                             [company1], {"amount": "GBP 4,000,000"})
    st_alt = statement("00002/01.a", "fact:edge:00002:01", "blk:00002/01.01",
                       "The bank lent GBP 4,500,000 to the company.",
                       [bank, company1], {"amount": "GBP 4,500,000"})
    st_thread1 = statement("00003/01.a", "fact:edge:00001:02", "blk:00003/01.01",
                           "We refer to your letter of 1 March 2024.", [person1])
    st_thread2 = statement("00004/01.a", "fact:edge:00001:01", "blk:00004/01.01",
                           "Thank you for your letter; we reply as follows.", [person2])

    # ---- facts ---------------------------------------------------------
    f_owns = SemanticFact(
        fact_id=i("fact:edge:00001:01"), kind="edge", subject=ref(company1),
        relation="owns", object=ref(asset), status="completed",
        statement_ids=[st_owns.statement_id, st_thread2.statement_id],
    )
    f_lent = SemanticFact(
        fact_id=i("fact:edge:00001:02"), kind="edge", subject=ref(bank),
        relation="lent_to", object=ref(company1), status="completed", amount=amount,
        effective=TimeScope(kind="instant", start=IsoDate(value=dt.date(2024, 3, 3))),
        conditions=[i("cnd:00001:01")],
        statement_ids=[st_lent.statement_id, st_lent_cell.statement_id,
                       st_thread1.statement_id],
    )
    f_chair = SemanticFact(
        fact_id=i("fact:edge:00001:03"), kind="edge", subject=ref(person1),
        relation="chair_of", object=ref(company1), status="held",
    )
    f_prop = SemanticFact(
        fact_id=i("fact:prop:00001:01"), kind="proposition",
        text="The board approved the facility", status="approved",
        speaker=ref(company1), statement_ids=[st_prop.statement_id],
    )
    f_alt = SemanticFact(
        fact_id=i("fact:edge:00002:01"), kind="edge", subject=ref(bank),
        relation="lent_to", object=ref(company1), status="completed",
        amount=Money(amount_minor=450_000_000, currency="GBP"),
        effective=TimeScope(kind="instant", start=IsoDate(value=dt.date(2024, 3, 3))),
        statement_ids=[st_alt.statement_id],
    )
    facts = {f.fact_id: f for f in (f_owns, f_lent, f_chair, f_prop, f_alt)}

    # ---- other semantic objects ---------------------------------------
    condition = Condition(condition_id=i("cnd:00001:01"), kind="provided_that",
                          constrains=f_lent.fact_id,
                          predicate="the borrower delivers a compliance certificate",
                          satisfied="yes")
    event = Event(event_id=i("evt:0001"), name="Board meeting", kind="meeting",
                  event_date=IsoDate(value=dt.date(2024, 3, 3)),
                  participants=[ref(person1), ref(person2)],
                  fact_ids=[f_prop.fact_id])
    txn = Transaction(transaction_id=i("txn:0001"), kind="disbursement",
                      from_entity=ref(bank), to_entity=ref(company1), amount=amount,
                      date=IsoDate(value=dt.date(2024, 3, 3)), status="completed",
                      instrument="facility agreement", fact_id=f_lent.fact_id)
    contradiction = Contradiction(
        contradiction_id=i("ctr:0001"), fact_a=f_lent.fact_id, fact_b=f_alt.fact_id,
        dimension="amount", documents=[i("doc:00001"), i("doc:00002")],
    )
    thread = Thread(thread_id=i(f"thr:{CORPUS_NAME}:0001"), subject="Facility drawdown",
                    participants=[ref(person1), ref(person2)],
                    root_document=i("doc:00003"),
                    messages=[i("doc:00003"), i("doc:00004")],
                    latest_document=i("doc:00004"))
    cross_ref = CrossReference(
        reference_id=f"xref:{i('ctr:0001').split(':', 1)[1]}", kind="contradicts",
        src_document=i("doc:00001"), dst_document=i("doc:00002"),
        src_fact=f_lent.fact_id, dst_fact=f_alt.fact_id,
    )

    world = CorpusWorld(
        corpus_id=f"corp:{CORPUS_NAME}:1234", seed=SEED,
        entities=entities, facts=facts,
        conditions={condition.condition_id: condition},
        events={event.event_id: event},
        transactions={txn.transaction_id: txn},
        contradictions={contradiction.contradiction_id: contradiction},
        threads={thread.thread_id: thread},
        cross_references=[cross_ref],
    )

    # ---- documents -----------------------------------------------------
    doc1 = CdmDocument(
        document_id=i("doc:00001"), doc_type="board_minutes",
        structure_id="board_minutes/v1",
        metadata=DocumentMetadata(title="Board minutes",
                                  document_date=IsoDate(value=dt.date(2024, 3, 3)),
                                  issuer=ref(company1), register="formal"),
        sections=[s1, s2],
        statements=[st_owns, st_lent, st_prop, st_lent_cell],
        plan_fingerprint="fp-board-001",
    )
    doc2 = CdmDocument(
        document_id=i("doc:00002"), doc_type="memorandum", structure_id="memorandum/v1",
        metadata=DocumentMetadata(title="Facility memorandum", register="neutral"),
        sections=[s3], statements=[st_alt], plan_fingerprint="fp-memo-001",
    )
    meta1 = MessageMeta(thread_id=thread.thread_id, message_id=i("msg:0001/01"), sequence=1)
    meta2 = MessageMeta(thread_id=thread.thread_id, message_id=i("msg:0001/02"), sequence=2,
                        in_reply_to=meta1.message_id,
                        references=[meta1.message_id])
    doc3 = CdmDocument(
        document_id=i("doc:00003"), doc_type="correspondence",
        structure_id="correspondence/v1",
        metadata=DocumentMetadata(title="Letter of 1 March 2024", register="formal",
                                  thread=meta1),
        sections=[s4], statements=[st_thread1], plan_fingerprint="fp-corr-001",
    )
    doc4 = CdmDocument(
        document_id=i("doc:00004"), doc_type="correspondence",
        structure_id="correspondence/v1",
        metadata=DocumentMetadata(title="Reply of 8 March 2024", register="formal",
                                  thread=meta2),
        sections=[s5], statements=[st_thread2], plan_fingerprint="fp-corr-002",
    )
    documents = {d.document_id: d for d in (doc1, doc2, doc3, doc4)}

    corpus = Corpus(corpus_id=world.corpus_id, seed=SEED, world=world, documents=documents)
    return Fixture(corpus=corpus, ids=ids, registry=reg,
                   names={"company1": company1.entity_id, "bank": bank.entity_id,
                          "asset": asset.entity_id, "person1": person1.entity_id})
