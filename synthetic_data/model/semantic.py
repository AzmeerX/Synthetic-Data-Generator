"""Layer 1 — semantic model (architecture §3.1, §4.1).

The semantic layer is format-independent and document-independent.  Facts live
here once; documents *state* them.  Nothing in this module knows about TXT, PDF,
DOCX, XLSX, renderers or parsers.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .values import (
    CanonicalId,
    ConditionKind,
    ConditionSatisfied,
    ContradictionDimension,
    EntityRef,
    FactKind,
    FactStatus,
    IsoDate,
    Money,
)

__all__ = [
    "TimeScope",
    "Entity",
    "SemanticFact",
    "Condition",
    "Event",
    "Transaction",
    "Contradiction",
    "Thread",
    "CrossReference",
    "CorpusWorld",
    "TransactionStatus",
    "CrossReferenceKind",
]

TransactionStatus = Literal["completed", "pending", "failed", "reversed"]
CrossReferenceKind = Literal[
    "amends", "supersedes", "responds_to", "exhibits", "repeats",
    "contradicts", "supports", "references",
]


class _Semantic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TimeScope(_Semantic):
    """Temporal extent attached to a fact.  Values are typed, never free text."""

    kind: Literal["instant", "interval", "period", "duration", "relative"] = "instant"
    start: IsoDate | None = None
    end: IsoDate | None = None
    anchor: str | None = None          # "document_date", "execution", ...
    granularity: Literal["day", "month", "year"] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "TimeScope":
        if self.kind == "instant" and self.start is None:
            raise ValueError("instant TimeScope requires start")
        if self.kind in {"interval", "period", "duration"} and self.start is None:
            raise ValueError(f"{self.kind} TimeScope requires start")
        if self.kind == "interval" and self.end is None:
            raise ValueError("interval TimeScope requires end")
        if self.kind == "relative" and not self.anchor:
            raise ValueError("relative TimeScope requires an anchor")
        if self.start and self.end and self.end.value < self.start.value:
            raise ValueError("TimeScope end precedes start")
        return self


class Entity(_Semantic):
    entity_id: CanonicalId
    entity_type: str = Field(min_length=1, max_length=64)
    subtype: str | None = Field(default=None, max_length=64)
    canonical_name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=64)
    attributes: dict[str, str] = Field(default_factory=dict)
    jurisdiction: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _names(self) -> "Entity":
        if not self.canonical_name.strip():
            raise ValueError("canonical_name must not be blank")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("duplicate alias on entity")
        if self.canonical_name in self.aliases:
            raise ValueError("canonical_name must not be repeated as an alias")
        return self


class SemanticFact(_Semantic):
    """A relationship (edge) or a statement (proposition), corpus-scoped."""

    fact_id: CanonicalId
    kind: FactKind
    subject: EntityRef | None = None
    relation: str | None = Field(default=None, max_length=64)
    object: EntityRef | None = None
    status: FactStatus = "completed"
    negated: bool = False
    nested: bool = False
    modality: Literal["may", "shall", "must", "intends_to", "expected_to"] | None = None
    effective: TimeScope | None = None
    amount: Money | None = None
    text: str | None = Field(default=None, max_length=4000)
    speaker: EntityRef | None = None
    reporter: EntityRef | None = None
    conditions: list[CanonicalId] = Field(default_factory=list)
    source_event: CanonicalId | None = None
    #: statements that realise this fact; kept in sync by the corpus validator
    statement_ids: list[CanonicalId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> "SemanticFact":
        if self.kind == "edge":
            if self.subject is None or self.object is None or not self.relation:
                raise ValueError("edge requires subject, relation and object")
            if self.text is not None:
                raise ValueError("edge must not carry proposition text")
        else:
            if not self.text or not self.text.strip():
                raise ValueError("proposition requires non-empty text")
            if self.relation is not None:
                raise ValueError("proposition must not carry a relation")
            if self.subject is not None or self.object is not None:
                raise ValueError("proposition must not carry subject/object")
        if self.negated is False and self.status == "denied":
            raise ValueError("denied status requires negated=True")
        if self.negated and self.status in {"completed", "approved", "held", "signed"}:
            raise ValueError("negated fact must not carry a positive completion status")
        if len(set(self.conditions)) != len(self.conditions):
            raise ValueError("duplicate condition id on fact")
        if len(set(self.statement_ids)) != len(self.statement_ids):
            raise ValueError("duplicate statement id on fact")
        return self


class Condition(_Semantic):
    condition_id: CanonicalId
    kind: ConditionKind
    constrains: CanonicalId
    predicate: str = Field(min_length=1, max_length=500)
    satisfied: ConditionSatisfied = "unknown"


class Event(_Semantic):
    event_id: CanonicalId
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=64)
    event_date: IsoDate
    participants: list[EntityRef] = Field(default_factory=list)
    place: EntityRef | None = None
    fact_ids: list[CanonicalId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "Event":
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("duplicate fact id on event")
        return self


class Transaction(_Semantic):
    transaction_id: CanonicalId
    kind: str = Field(min_length=1, max_length=64)
    from_entity: EntityRef
    to_entity: EntityRef
    amount: Money
    date: IsoDate
    status: TransactionStatus
    instrument: str | None = Field(default=None, max_length=120)
    fact_id: CanonicalId

    @model_validator(mode="after")
    def _parties(self) -> "Transaction":
        if self.from_entity.entity_id == self.to_entity.entity_id:
            raise ValueError("transaction parties must differ")
        return self


class Contradiction(_Semantic):
    contradiction_id: CanonicalId
    fact_a: CanonicalId
    fact_b: CanonicalId
    dimension: ContradictionDimension
    documents: list[CanonicalId] = Field(min_length=2)

    @model_validator(mode="after")
    def _distinct(self) -> "Contradiction":
        if self.fact_a == self.fact_b:
            raise ValueError("contradiction must name two different facts")
        if len(set(self.documents)) < 2:
            raise ValueError("contradiction must span at least two documents")
        return self


class Thread(_Semantic):
    """Correspondence thread (decision DEC-7).  Messages stay separate documents."""

    thread_id: CanonicalId
    subject: str = Field(min_length=1, max_length=300)
    participants: list[EntityRef] = Field(min_length=1)
    root_document: CanonicalId
    messages: list[CanonicalId] = Field(min_length=1)
    latest_document: CanonicalId
    closed_by: CanonicalId | None = None

    @model_validator(mode="after")
    def _order(self) -> "Thread":
        if len(set(self.messages)) != len(self.messages):
            raise ValueError("thread messages must be unique")
        if self.root_document not in self.messages:
            raise ValueError("root_document must appear in messages")
        if self.latest_document not in self.messages:
            raise ValueError("latest_document must appear in messages")
        if self.messages[-1] != self.latest_document:
            raise ValueError("latest_document must be the last message")
        if self.closed_by is not None and self.closed_by not in self.messages:
            raise ValueError("closed_by must be a message in this thread")
        return self


class CrossReference(_Semantic):
    """A typed link between documents, optionally between specific facts."""

    reference_id: CanonicalId
    kind: CrossReferenceKind
    src_document: CanonicalId
    dst_document: CanonicalId
    src_fact: CanonicalId | None = None
    dst_fact: CanonicalId | None = None

    @model_validator(mode="after")
    def _not_self(self) -> "CrossReference":
        if self.src_document == self.dst_document and self.src_fact == self.dst_fact:
            raise ValueError("cross-reference must not point at itself")
        return self


class CorpusWorld(_Semantic):
    """Layer-1 aggregate: every semantic object in one corpus, keyed by id."""

    corpus_id: CanonicalId
    seed: StrictInt
    entities: dict[CanonicalId, Entity] = Field(default_factory=dict)
    facts: dict[CanonicalId, SemanticFact] = Field(default_factory=dict)
    conditions: dict[CanonicalId, Condition] = Field(default_factory=dict)
    events: dict[CanonicalId, Event] = Field(default_factory=dict)
    transactions: dict[CanonicalId, Transaction] = Field(default_factory=dict)
    contradictions: dict[CanonicalId, Contradiction] = Field(default_factory=dict)
    threads: dict[CanonicalId, Thread] = Field(default_factory=dict)
    cross_references: list[CrossReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _referential_integrity(self) -> "CorpusWorld":
        _check_keys("entities", self.entities)
        _check_keys("facts", self.facts)
        _check_keys("conditions", self.conditions)
        _check_keys("events", self.events)
        _check_keys("transactions", self.transactions)
        _check_keys("contradictions", self.contradictions)
        _check_keys("threads", self.threads)

        entity_ids = set(self.entities)
        fact_ids = set(self.facts)

        for fact in self.facts.values():
            for ref in (fact.subject, fact.object, fact.speaker, fact.reporter):
                _require_entity(ref, entity_ids, f"fact {fact.fact_id}")
            for condition_id in fact.conditions:
                if condition_id not in self.conditions:
                    raise ValueError(f"fact {fact.fact_id} cites unknown condition {condition_id}")
            if fact.source_event and fact.source_event not in self.events:
                raise ValueError(f"fact {fact.fact_id} cites unknown event {fact.source_event}")

        for condition in self.conditions.values():
            if condition.constrains not in fact_ids:
                raise ValueError(
                    f"condition {condition.condition_id} constrains unknown fact {condition.constrains}")

        for event in self.events.values():
            for ref in event.participants:
                _require_entity(ref, entity_ids, f"event {event.event_id}")
            if event.place:
                _require_entity(event.place, entity_ids, f"event {event.event_id}")
            for fact_id in event.fact_ids:
                if fact_id not in fact_ids:
                    raise ValueError(f"event {event.event_id} cites unknown fact {fact_id}")

        for txn in self.transactions.values():
            _require_entity(txn.from_entity, entity_ids, f"transaction {txn.transaction_id}")
            _require_entity(txn.to_entity, entity_ids, f"transaction {txn.transaction_id}")
            fact = self.facts.get(txn.fact_id)
            if fact is None:
                raise ValueError(
                    f"transaction {txn.transaction_id} cites unknown fact {txn.fact_id}")
            if fact.kind != "edge" or fact.negated or fact.status != "completed":
                raise ValueError(
                    f"transaction {txn.transaction_id} must reference a completed, "
                    f"non-negated edge (got kind={fact.kind}, negated={fact.negated}, "
                    f"status={fact.status})")

        for contradiction in self.contradictions.values():
            for fact_id in (contradiction.fact_a, contradiction.fact_b):
                if fact_id not in fact_ids:
                    raise ValueError(
                        f"contradiction {contradiction.contradiction_id} cites unknown fact {fact_id}")
            if self.facts[contradiction.fact_a].kind != self.facts[contradiction.fact_b].kind:
                raise ValueError(
                    f"contradiction {contradiction.contradiction_id} compares different fact kinds")

        for thread in self.threads.values():
            for ref in thread.participants:
                _require_entity(ref, entity_ids, f"thread {thread.thread_id}")

        for ref in self.cross_references:
            for fact_id in (ref.src_fact, ref.dst_fact):
                if fact_id is not None and fact_id not in fact_ids:
                    raise ValueError(
                        f"cross-reference {ref.reference_id} cites unknown fact {fact_id}")
        return self

    # -- convenience --------------------------------------------------------
    def fact(self, fact_id: str) -> SemanticFact:
        try:
            return self.facts[fact_id]
        except KeyError as exc:
            raise KeyError(f"unknown fact {fact_id!r}") from exc

    def entity(self, entity_id: str) -> Entity:
        try:
            return self.entities[entity_id]
        except KeyError as exc:
            raise KeyError(f"unknown entity {entity_id!r}") from exc


def _check_keys(label: str, mapping: dict) -> None:
    for key, value in mapping.items():
        if getattr(value, "fact_id", None) == key:
            continue
        if getattr(value, "entity_id", None) == key:
            continue
        if getattr(value, "condition_id", None) == key:
            continue
        if getattr(value, "event_id", None) == key:
            continue
        if getattr(value, "transaction_id", None) == key:
            continue
        if getattr(value, "contradiction_id", None) == key:
            continue
        if getattr(value, "thread_id", None) == key:
            continue
        raise ValueError(f"{label} key {key!r} does not match the object id")


def _require_entity(ref: EntityRef | None, known: set[str], where: str) -> None:
    if ref is None:
        return
    if ref.entity_id not in known:
        raise ValueError(f"{where} references unknown entity {ref.entity_id}")
