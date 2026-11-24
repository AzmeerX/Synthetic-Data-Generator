"""Corpus aggregate and cross-layer invariants (architecture §4.4).

Layer 1 (semantic) and Layer 2 (content) are validated independently by their own
models.  This module binds them and enforces the invariants that only exist
between the layers: statement-to-fact reciprocity, cell values against fact
values, thread/message agreement, contradiction grounding, and the rule that one
fact carries exactly one id.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .content import CdmDocument, FactStatement, TableCell
from .semantic import CorpusWorld, SemanticFact
from .values import CanonicalId, IsoDate, Money

__all__ = ["Corpus", "CorpusViolation", "collect_violations"]


class CorpusViolation(ValueError):
    """Raised with every cross-layer violation found, not just the first."""


class Corpus(BaseModel):
    """One corpus: the semantic world plus every document that states it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: CanonicalId
    seed: StrictInt
    world: CorpusWorld
    documents: dict[CanonicalId, CdmDocument] = Field(min_length=1)

    # -- access -------------------------------------------------------------
    def document(self, document_id: str) -> CdmDocument:
        try:
            return self.documents[document_id]
        except KeyError as exc:
            raise KeyError(f"unknown document {document_id!r}") from exc

    def iter_documents(self) -> Iterator[CdmDocument]:
        return iter(self.documents.values())

    def statements(self) -> Iterator[tuple[CdmDocument, FactStatement]]:
        for doc in self.documents.values():
            for statement in doc.statements:
                yield doc, statement

    # -- validation ---------------------------------------------------------
    @model_validator(mode="after")
    def _cross_layer(self) -> "Corpus":
        problems = collect_violations(self)
        if problems:
            raise CorpusViolation(
                f"corpus {self.corpus_id} violates {len(problems)} invariant(s):\n- "
                + "\n- ".join(problems))
        return self


def collect_violations(corpus: Corpus) -> list[str]:
    """Return every cross-layer violation as text; empty means valid."""
    problems: list[str] = []
    world = corpus.world
    docs = corpus.documents

    # ---- key/id agreement (invariant 4) --------------------------------
    for doc_id, doc in docs.items():
        if doc.document_id != doc_id:
            problems.append(f"documents[{doc_id!r}] has document_id {doc.document_id!r}")
    seen_nodes: dict[str, str] = {}
    for doc in docs.values():
        for node_id, _ in doc.iter_nodes():
            if node_id in seen_nodes and seen_nodes[node_id] != doc.document_id:
                problems.append(f"node id {node_id} appears in {seen_nodes[node_id]} and {doc.document_id}")
            seen_nodes[node_id] = doc.document_id

    # ---- statement <-> fact reciprocity (invariants 1, 9) --------------
    stated: dict[str, set[str]] = defaultdict(set)
    for doc, statement in corpus.statements():
        fact = world.facts.get(statement.fact_id)
        if fact is None:
            problems.append(
                f"{doc.document_id}: statement {statement.statement_id} cites unknown "
                f"fact {statement.fact_id}")
            continue
        stated[statement.fact_id].add(statement.statement_id)
        if statement.statement_id not in fact.statement_ids:
            problems.append(
                f"fact {fact.fact_id} does not list its statement {statement.statement_id}")
        if statement.node_id not in doc.node_ids():
            problems.append(
                f"statement {statement.statement_id} cites node {statement.node_id} outside its document")

    for fact in world.facts.values():
        listed = set(fact.statement_ids)
        actual = stated.get(fact.fact_id, set())
        if listed != actual:
            missing = sorted(listed - actual)
            unlisted = sorted(actual - listed)
            problems.append(
                f"fact {fact.fact_id} statement_ids disagree with documents "
                f"(missing={missing[:3]}, unlisted={unlisted[:3]})")

    # one fact, one id: duplicate semantic payloads are a second id for one fact
    payloads: dict[tuple, str] = {}
    for fact in world.facts.values():
        key = _fact_payload_key(fact)
        if key in payloads:
            problems.append(
                f"facts {payloads[key]} and {fact.fact_id} describe the same fact")
        payloads[key] = fact.fact_id

    # ---- table cells (invariant 2) -------------------------------------
    for doc in docs.values():
        for _, node in doc.iter_nodes():
            if not isinstance(node, TableCell):
                continue
            if node.fact_ref is None:
                continue
            fact = world.facts.get(node.fact_ref)
            if fact is None:
                problems.append(
                    f"{doc.document_id}: cell {node.cell_id} cites unknown fact {node.fact_ref}")
                continue
            if isinstance(node.value, Money) and fact.amount is not None:
                if node.value != fact.amount:
                    problems.append(
                        f"cell {node.cell_id} value {node.value.amount_minor} "
                        f"{node.value.currency} disagrees with fact {fact.fact_id} "
                        f"{fact.amount.amount_minor} {fact.amount.currency}")

    # ---- inline runs (invariants 3, and entity refs) --------------------
    for doc in docs.values():
        for run in doc.iter_runs():
            if run.entity_ref is not None and run.entity_ref.entity_id not in world.entities:
                problems.append(
                    f"{doc.document_id}: run cites unknown entity {run.entity_ref.entity_id}")
            if run.fact_ref is None:
                continue
            fact = world.facts.get(run.fact_ref)
            if fact is None:
                problems.append(
                    f"{doc.document_id}: run cites unknown fact {run.fact_ref}")
                continue
            mismatch = _run_value_mismatch(run.value, fact)
            if mismatch:
                problems.append(
                    f"{doc.document_id}: run value {mismatch} disagrees with fact {fact.fact_id}")

    # ---- cross references (invariant 5) --------------------------------
    for ref in world.cross_references:
        for doc_id in (ref.src_document, ref.dst_document):
            if doc_id not in docs:
                problems.append(
                    f"cross-reference {ref.reference_id} cites unknown document {doc_id}")
        for fact_id in (ref.src_fact, ref.dst_fact):
            if fact_id is not None and fact_id not in world.facts:
                problems.append(
                    f"cross-reference {ref.reference_id} cites unknown fact {fact_id}")

    # ---- contradictions (invariants 6, 11) ------------------------------
    for contradiction in world.contradictions.values():
        for doc_id in contradiction.documents:
            if doc_id not in docs:
                problems.append(
                    f"contradiction {contradiction.contradiction_id} cites unknown document {doc_id}")
        fact_a = world.facts[contradiction.fact_a]
        fact_b = world.facts[contradiction.fact_b]
        discrepancy = _contradiction_discrepancy(fact_a, fact_b, contradiction.dimension)
        if discrepancy:
            problems.append(
                f"contradiction {contradiction.contradiction_id}: {discrepancy}")
        a_docs = {doc.document_id for doc, st in corpus.statements()
                  if st.fact_id == fact_a.fact_id}
        b_docs = {doc.document_id for doc, st in corpus.statements()
                  if st.fact_id == fact_b.fact_id}
        if not a_docs:
            problems.append(f"contradiction fact {fact_a.fact_id} is not stated in any document")
        if not b_docs:
            problems.append(f"contradiction fact {fact_b.fact_id} is not stated in any document")
        listed = set(contradiction.documents)
        a_listed = a_docs & listed
        b_listed = b_docs & listed
        if not a_listed:
            problems.append(
                f"contradiction {contradiction.contradiction_id} names no document that "
                f"states fact {fact_a.fact_id}")
        if not b_listed:
            problems.append(
                f"contradiction {contradiction.contradiction_id} names no document that "
                f"states fact {fact_b.fact_id}")
        if a_listed & b_listed:
            problems.append(
                f"contradiction {contradiction.contradiction_id} grounds both sides in "
                f"the same document")

    # ---- threads (invariant 10) -----------------------------------------
    # Thread.messages holds *document* ids; MessageMeta.message_id is a distinct
    # message identifier used by in_reply_to/references.
    messages_seen: dict[str, CdmDocument] = {}
    for thread in world.threads.values():
        metas = []
        for position, doc_id in enumerate(thread.messages, start=1):
            doc = docs.get(doc_id)
            if doc is None:
                problems.append(f"thread {thread.thread_id} cites unknown message document {doc_id}")
                continue
            if doc_id in messages_seen:
                problems.append(f"message document {doc_id} belongs to more than one thread")
            messages_seen[doc_id] = doc
            meta = doc.metadata.thread
            if meta is None:
                problems.append(f"message {doc_id} has no MessageMeta")
                continue
            if meta.thread_id != thread.thread_id:
                problems.append(
                    f"message {doc_id} names thread {meta.thread_id}, thread is {thread.thread_id}")
            if meta.sequence != position:
                problems.append(
                    f"message {doc_id} sequence {meta.sequence} != position {position}")
            metas.append((doc, meta))
        message_ids = [meta.message_id for _, meta in metas]
        if len(set(message_ids)) != len(message_ids):
            problems.append(f"thread {thread.thread_id} has duplicate message ids")
        for index, (doc, meta) in enumerate(metas):
            earlier = message_ids[:index]
            if meta.in_reply_to is not None and meta.in_reply_to not in earlier:
                problems.append(
                    f"message {meta.message_id} replies to {meta.in_reply_to}, "
                    f"which is not an earlier message in thread {thread.thread_id}")
            for cited in meta.references:
                if cited not in earlier:
                    problems.append(
                        f"message {meta.message_id} cites {cited}, which is not earlier in the thread")
            known_exhibits = {e.exhibit_id for e in doc.exhibits}
            for attachment in meta.attachments:
                if attachment not in known_exhibits:
                    problems.append(
                        f"message {meta.message_id} attaches unknown exhibit {attachment}")

    for doc_id, doc in docs.items():
        if doc.metadata.thread is not None and doc_id not in messages_seen:
            problems.append(f"document {doc_id} claims a thread but is not listed in it")

    return problems


def _fact_payload_key(fact: SemanticFact) -> tuple:
    return (
        fact.kind,
        fact.subject.entity_id if fact.subject else None,
        fact.relation,
        fact.object.entity_id if fact.object else None,
        fact.status,
        fact.negated,
        fact.amount.amount_minor if fact.amount else None,
        fact.amount.currency if fact.amount else None,
        fact.effective.start.value if fact.effective and fact.effective.start else None,
        (fact.text or "").strip(),
        fact.speaker.entity_id if fact.speaker else None,
    )


def _run_value_mismatch(value, fact: SemanticFact) -> str | None:
    if value is None:
        return None
    if isinstance(value, Money):
        if fact.amount is None:
            return f"{value.amount_minor} {value.currency} but the fact has no amount"
        if value != fact.amount:
            return (f"{value.amount_minor} {value.currency} but the fact has "
                    f"{fact.amount.amount_minor} {fact.amount.currency}")
        return None
    if isinstance(value, IsoDate):
        if fact.effective and fact.effective.start and value.value != fact.effective.start.value:
            return (f"{value.iso()} but the fact starts {fact.effective.start.iso()}")
    return None


def _contradiction_discrepancy(fact_a: SemanticFact, fact_b: SemanticFact, dimension: str) -> str | None:
    same_parties = (
        (fact_a.subject.entity_id if fact_a.subject else None)
        == (fact_b.subject.entity_id if fact_b.subject else None)
        and (fact_a.object.entity_id if fact_a.object else None)
        == (fact_b.object.entity_id if fact_b.object else None)
        and fact_a.relation == fact_b.relation
    )
    if dimension == "amount":
        if fact_a.amount is None or fact_b.amount is None:
            return "amount contradiction requires both facts to carry an amount"
        if not same_parties:
            return "amount contradiction compares different parties"
        if fact_a.amount == fact_b.amount:
            return "amount contradiction has equal amounts"
    elif dimension == "date":
        a = fact_a.effective.start.value if fact_a.effective and fact_a.effective.start else None
        b = fact_b.effective.start.value if fact_b.effective and fact_b.effective.start else None
        if a is None or b is None:
            return "date contradiction requires both facts to carry a start date"
        if a == b:
            return "date contradiction has equal dates"
    elif dimension == "status":
        if fact_a.status == fact_b.status:
            return "status contradiction has equal statuses"
    elif dimension == "relationship":
        if same_parties:
            return "relationship contradiction has identical parties and relation"
    return None
