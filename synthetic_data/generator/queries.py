"""Question/answer dataset generation from structured corpus facts.

Every expected answer is computed from the world model or from a document plan
written by this package.  No language model is involved.  Each answerable
question carries exact, canonical evidence; unanswerable questions are checked
against the whole corpus before they are emitted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from synth.modules.common import DocView

from synthetic_data.generator.documents import DocumentPlan, format_date
from synthetic_data.generator.world import (
    FACILITY_CAPACITY_INCREASE,
    FACILITY_CAPACITY_ORIGINAL,
    Entity,
    Fact,
    World,
)
from synthetic_data.schemas.qa import (
    QADocument,
    QADataset,
    QAQuery,
    QueryEvidence,
    UnanswerableKind,
    UnanswerableProof,
)


class QueryGenerationError(RuntimeError):
    """Raised when a QA answer or unanswerable claim cannot be justified."""


@dataclass(frozen=True)
class DocumentContext:
    """One ingested document plus the plan that produced it."""

    document_id: str
    plan: DocumentPlan
    synth_doc_id: str
    filename: str
    text: str
    text_sha256: str
    sentences: tuple[str, ...]
    sentence_ids: tuple[str, ...]
    sentence_index: dict[str, int]
    sentence_text_by_idx: dict[int, str]

    @classmethod
    def from_view(cls, plan: DocumentPlan, synth_doc_id: str, view: DocView) -> "DocumentContext":
        actual = tuple(view.sentence_text(s) for s in view.sentences)
        if list(actual) != plan.sentences:
            raise QueryGenerationError(
                f"{plan.document_id}: plan sentences do not match ingested canonical sentences")
        sentence_index: dict[str, int] = {}
        sentence_ids: list[str] = []
        sentence_text_by_idx: dict[int, str] = {}
        for sentence in view.sentences:
            text = view.sentence_text(sentence)
            sentence_index[text] = sentence["idx"]
            sentence_text_by_idx[sentence["idx"]] = text
            sentence_ids.append(f"s{sentence['idx']}")
        return cls(
            document_id=plan.document_id,
            plan=plan,
            synth_doc_id=synth_doc_id,
            filename=plan.filename,
            text=view.text,
            text_sha256=hashlib.sha256(view.text.encode("utf-8")).hexdigest(),
            sentences=actual,
            sentence_ids=tuple(sentence_ids),
            sentence_index=sentence_index,
            sentence_text_by_idx=sentence_text_by_idx,
        )

    def document(self) -> QADocument:
        return QADocument(
            document_id=self.document_id,
            filename=self.filename,
            text_sha256=self.text_sha256,
            pages=self.plan.pages,
            doc_type=self.plan.doc_type,
            doc_date=self.plan.doc_date.isoformat(),
            sentence_count=len(self.sentences),
        )


@dataclass(frozen=True)
class FactOccurrence:
    fact: Fact
    document_id: str
    synth_doc_id: str
    filename: str
    text_sha256: str
    sentence_idx: int
    sentence_id: str
    quote: str


@dataclass
class CorpusIndex:
    """Fast lookups over the generated corpus, never over the database."""

    documents: list[DocumentContext]
    occurrences: dict[str, list[FactOccurrence]]
    by_document: dict[str, DocumentContext]
    company_documents: dict[str, list[DocumentContext]]
    _corpus_text: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def build(cls, documents: Iterable[DocumentContext]) -> "CorpusIndex":
        docs = list(documents)
        occurrences: dict[str, list[FactOccurrence]] = {}
        by_document: dict[str, DocumentContext] = {}
        company_documents: dict[str, list[DocumentContext]] = {}
        for ctx in docs:
            if ctx.document_id in by_document:
                raise QueryGenerationError(f"duplicate document id {ctx.document_id}")
            by_document[ctx.document_id] = ctx
            company_documents.setdefault(ctx.plan.subject_entity_id, []).append(ctx)
            for stated in ctx.plan.stated_facts:
                sentence_idx = ctx.sentence_index.get(stated.sentence_text)
                if sentence_idx is None:
                    raise QueryGenerationError(
                        f"{ctx.document_id}: fact {stated.fact.fact_id} has no canonical sentence")
                occurrences.setdefault(stated.fact.fact_id, []).append(FactOccurrence(
                    fact=stated.fact,
                    document_id=ctx.document_id,
                    synth_doc_id=ctx.synth_doc_id,
                    filename=ctx.filename,
                    text_sha256=ctx.text_sha256,
                    sentence_idx=sentence_idx,
                    sentence_id=f"s{sentence_idx}",
                    quote=stated.sentence_text,
                ))
        return cls(
            documents=docs,
            occurrences=occurrences,
            by_document=by_document,
            company_documents=company_documents,
        )

    def occurrences_for(self, fact_id: str) -> list[FactOccurrence]:
        return self.occurrences.get(fact_id, [])

    def occurrence_in(self, fact_id: str, document_id: str) -> FactOccurrence | None:
        return next((o for o in self.occurrences.get(fact_id, [])
                     if o.document_id == document_id), None)

    def corpus_text(self) -> str:
        # Precompute once: the unanswerable check must never rescan the whole
        # corpus for every document.
        if self._corpus_text is None:
            self._corpus_text = "\n".join(
                ctx.text for ctx in self.documents).casefold()
        return self._corpus_text


#: Tokens that identify an attribute mention in a fact's note or text.
_ATTRIBUTE_TOKENS = {
    "auditor": ("auditor", "audited"),
    "vat_registration_number": ("vat", "registration number"),
}


@dataclass
class FactIndex:
    """Structural index over world facts and every document-stated fact.

    Unanswerable questions are justified from this index, never from the
    wording of the question.  Facts created by document plans (proposals,
    negated guarantees, facility capacities) are included: a near miss is a
    fact of a related but different relation/status, and it is recorded in the
    proof rather than silently ignored.
    """

    world: World
    facts: dict[str, Fact]
    by_subject: dict[str, list[Fact]]
    by_object: dict[str, list[Fact]]
    by_entity: dict[str, list[Fact]]
    financial_by_entity: dict[str, list[Fact]]
    dividend_parties: set[str]
    attribute_mentions: dict[str, set[str]]
    edges: list[Fact]
    propositions: list[Fact]
    fact_documents: dict[str, list[str]]
    entity_documents: dict[str, list[str]]
    document_subjects: list[str]
    attributes: dict[str, dict[str, str]]

    @classmethod
    def build(cls, world: World, plans: Iterable[DocumentPlan] = ()) -> "FactIndex":
        facts: dict[str, Fact] = {fact.fact_id: fact for fact in world.facts}
        fact_documents: dict[str, list[str]] = {}
        entity_documents: dict[str, list[str]] = {}
        document_subjects: list[str] = []
        for plan in plans:
            document_subjects.append(plan.subject_entity_id)
            for stated in plan.stated_facts:
                facts.setdefault(stated.fact.fact_id, stated.fact)
                fact_documents.setdefault(stated.fact.fact_id, []).append(plan.document_id)
            for mention in plan.mentions:
                entity_documents.setdefault(mention.entity_id, []).append(plan.document_id)
            entity_documents.setdefault(plan.subject_entity_id, []).append(plan.document_id)
        by_subject: dict[str, list[Fact]] = defaultdict(list)
        by_object: dict[str, list[Fact]] = defaultdict(list)
        edges: list[Fact] = []
        propositions: list[Fact] = []
        for fact in facts.values():
            if fact.subject_id:
                by_subject[fact.subject_id].append(fact)
            if fact.object_id:
                by_object[fact.object_id].append(fact)
            if fact.kind == "edge":
                edges.append(fact)
            elif fact.kind == "proposition":
                propositions.append(fact)

        # Same order as facts_for_subject() + facts_for_object(), deduplicated
        # by fact id.  This is the scope for a per-entity absence claim.
        by_entity: dict[str, list[Fact]] = {}
        entity_ids = list(dict.fromkeys(
            entity_id
            for fact in facts.values()
            for entity_id in (fact.subject_id, fact.object_id)
            if entity_id is not None))
        for entity_id in entity_ids:
            seen_fact_ids: set[str] = set()
            related: list[Fact] = []
            for fact in by_subject.get(entity_id, []) + by_object.get(entity_id, []):
                if fact.fact_id in seen_fact_ids:
                    continue
                seen_fact_ids.add(fact.fact_id)
                related.append(fact)
            by_entity[entity_id] = related

        financial_relations = {"lent_to", "owes", "paid", "guarantor_for"}
        financial_by_entity: dict[str, list[Fact]] = {}
        for entity_id, related in by_entity.items():
            financial = [
                fact for fact in related
                if (fact.amount_gbp is not None
                    or fact.relation in financial_relations)
            ]
            if financial:
                financial_by_entity[entity_id] = sorted(
                    financial, key=lambda fact: fact.fact_id)

        dividend_parties: set[str] = set()
        for fact in facts.values():
            if _dividend_fact(fact):
                if fact.subject_id:
                    dividend_parties.add(fact.subject_id)
                if fact.object_id:
                    dividend_parties.add(fact.object_id)

        attribute_mentions: dict[str, set[str]] = {}
        for entity_id, related in by_entity.items():
            mentioned: set[str] = set()
            for fact in related:
                for attribute, tokens in _ATTRIBUTE_TOKENS.items():
                    if attribute not in mentioned and _fact_mentions_tokens(fact, tokens):
                        mentioned.add(attribute)
            attribute_mentions[entity_id] = mentioned

        return cls(
            world=world,
            facts=facts,
            by_subject={key: list(value) for key, value in by_subject.items()},
            by_object={key: list(value) for key, value in by_object.items()},
            by_entity=by_entity,
            financial_by_entity=financial_by_entity,
            dividend_parties=dividend_parties,
            attribute_mentions=attribute_mentions,
            edges=edges,
            propositions=propositions,
            fact_documents=fact_documents,
            entity_documents=entity_documents,
            document_subjects=document_subjects,
            attributes={entity.entity_id: dict(entity.attributes)
                        for entity in world.entities.values()},
        )

    # -- structural queries -----------------------------------------------------
    def facts_for_subject(self, entity_id: str) -> list[Fact]:
        return list(self.by_subject.get(entity_id, []))

    def facts_for_object(self, entity_id: str) -> list[Fact]:
        return list(self.by_object.get(entity_id, []))

    def facts_for_entity(self, entity_id: str) -> list[Fact]:
        return list(self.by_entity.get(entity_id, []))

    def mentions_attribute(self, entity_id: str, attribute: str) -> bool:
        if attribute in _ATTRIBUTE_TOKENS:
            return attribute in self.attribute_mentions.get(entity_id, ())
        tokens = (attribute.replace("_", " "),)
        return any(_fact_mentions_tokens(fact, tokens)
                   for fact in self.facts_for_entity(entity_id))

    def has_dividend(self, entity_id: str) -> bool:
        return entity_id in self.dividend_parties

    def edges_for(self, subject_id: str, relation: str | None = None, *,
                  include_negated: bool = False) -> list[Fact]:
        return [
            fact for fact in self.by_subject.get(subject_id, [])
            if fact.kind == "edge"
            and (relation is None or fact.relation == relation)
            and (include_negated or not fact.negated)
        ]

    def has_relation(self, subject_id: str, relation: str,
                     object_id: str | None = None, *,
                     include_negated: bool = False) -> bool:
        return any(
            fact.relation == relation
            and (object_id is None or fact.object_id == object_id)
            for fact in self.edges_for(subject_id, relation,
                                       include_negated=include_negated)
        )

    def chair_facts(self, company_id: str) -> list[Fact]:
        return [
            fact for fact in self.by_object.get(company_id, [])
            if fact.kind == "edge" and fact.relation == "chair_of"
        ]

    def financial_facts(self, entity_id: str) -> list[Fact]:
        return list(self.financial_by_entity.get(entity_id, []))

    def proposals_for(self, subject_id: str | None = None) -> list[Fact]:
        return [
            fact for fact in self.propositions
            if fact.status == "proposed"
            and (subject_id is None or fact.subject_id == subject_id)
        ]

    def documents_for(self, entity_id: str) -> list[str]:
        return list(dict.fromkeys(self.entity_documents.get(entity_id, [])))

    def checked_documents(self, fact_ids: Iterable[str],
                          entity_id: str | None = None) -> list[str]:
        found: list[str] = []
        for fact_id in fact_ids:
            found.extend(self.fact_documents.get(fact_id, []))
        if entity_id:
            found.extend(self.documents_for(entity_id))
        return list(dict.fromkeys(found))


# --------------------------------------------------------------------------- helpers
def _money(amount_gbp: int) -> str:
    if amount_gbp % 1_000_000 == 0:
        return f"GBP {amount_gbp // 1_000_000} million"
    if amount_gbp % 1_000 == 0:
        return f"GBP {amount_gbp // 1_000:,} thousand"
    return f"GBP {amount_gbp:,}"


def normalize_question(text: str) -> str:
    """Stable normalization used for duplicate detection.

    It folds case, punctuation and whitespace but deliberately keeps entity
    names, dates and document names: two questions about different documents
    are not duplicates merely because they share a template.
    """
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _variant(key: str, options: list[str]) -> str:
    """Deterministically choose one wording pattern for a stable key."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return options[int(digest[:16], 16) % len(options)]


#: Every answerable query type has at least two deterministic wording patterns.
#: Tests assert this table stays populated; the selected pattern depends only on
#: a stable fact/document key, never on the requested corpus size.
QUESTION_VARIANTS: dict[str, tuple[str, ...]] = {
    "entity_lookup": (
        "Which organization is identified in the title of {filename}?",
        "What organization is named in the document title of {filename}?",
        "Which company's name appears in the title of {filename}?",
    ),
    "relationship_chair": (
        "Who is the chair of {company} according to {filename}?",
        "Who currently chairs {company} according to {filename}?",
    ),
    "relationship_shareholder": (
        "Who is a shareholder of {company} according to {filename}?",
        "Which person holds shares in {company} according to {filename}?",
    ),
    "relationship_represents": (
        "Which law firm represents {company} in {filename}?",
        "Which law firm acts for {company} in {filename}?",
    ),
    "relationship_family": (
        "Who is {person} a family member of according to {filename}?",
        "Of whom is {person} a family member according to {filename}?",
    ),
    "relationship_owns": (
        "Which entity does {owner} own according to {filename}?",
        "What entity is owned by {owner} according to {filename}?",
    ),
    "relationship_owns_kind": (
        "Which {kind} does {owner} own according to {filename}?",
        "Which {kind} is owned by {owner} according to {filename}?",
    ),
    "relationship_guarantor": (
        "Which company guaranteed the loan to {company} according to {filename}?",
        "Which company provided the guarantee for the loan to {company} according to {filename}?",
    ),
    "financial_lend": (
        "How much did {bank} lend to {company} on {date} according to {filename}?",
        "What amount did {bank} lend to {company} on {date} according to {filename}?",
        "According to {filename}, how much did {bank} lend to {company} on {date}?",
    ),
    "financial_facility_increase": (
        "What was the facility increased to according to {filename}?",
        "To what amount was the facility increased according to {filename}?",
    ),
    "financial_facility_original": (
        "What was the original facility amount according to {filename}?",
        "What original facility capacity is stated in {filename}?",
    ),
    "financial_owes": (
        "How much does {company} owe {bank} as at {date} according to {filename}?",
        "What amount does {company} owe {bank} as at {date} according to {filename}?",
    ),
    "temporal_relative": (
        "When will the facility described in {filename} be reviewed?",
        "On what date will the facility in {filename} be reviewed?",
    ),
    "temporal_fiscal": (
        "What period does {label} cover in {filename}?",
        "Which dates does {label} cover in {filename}?",
    ),
    "temporal_chair": (
        "When did {person} become chair of {company} according to {filename}?",
        "On what date did {person} become chair of {company} according to {filename}?",
    ),
    "historical": (
        "Who was the chair of {company} until {date} according to {filename}?",
        "Who chaired {company} until {date} according to {filename}?",
    ),
    "identity_same": (
        "Are {name_a} and {name_b} in {filename} the same person?",
        "Do {name_a} and {name_b} in {filename} refer to the same individual?",
    ),
    "identity_alias": (
        "Which person is also known as {alias} in {filename}?",
        "Which individual uses the alias {alias} in {filename}?",
    ),
    "negation": (
        "Did {subject} guarantee the facility for {object} on {date} according to {filename}?",
        "According to {filename}, did {subject} guarantee the facility for {object} on {date}?",
    ),
    "proposal_target": (
        "What did {company} propose to acquire according to {filename}?",
        "Which company did {company} propose to acquire according to {filename}?",
    ),
    "proposal_completed": (
        "Had {company} completed the acquisition of {target} when {filename} was written?",
        "As at {filename}, had {company} completed the acquisition of {target}?",
    ),
    "alleged": (
        "According to {filename}, was the claim that {company} misstated its revenue alleged or proven?",
        "In {filename}, is the revenue-misstatement claim alleged or proven?",
    ),
    "provenance_reference": (
        "Which document has the reference {reference}?",
        "Which generated document carries the reference {reference}?",
    ),
    "provenance_fact": (
        "Which document states that {bank} lent {amount} to {company} on {date}?",
        "In which document is the loan of {amount} from {bank} to {company} on {date} stated?",
    ),
    "multi_hop_represent": (
        "Which law firm represents the company owned by {owner}?",
        "Which law firm acts for the company owned by {owner}?",
    ),
    "multi_hop_loan": (
        "Which bank lent money to the company owned by {owner}?",
        "Which bank provided lending to the company owned by {owner}?",
    ),
    "contradiction": (
        "What amount did {bank} lend to {company} on {date}?",
        "Which amount did {bank} lend to {company} on {date}?",
    ),
    "unanswerable_attribute": (
        "What is the {attribute} of {company}?",
        "Which {attribute} is recorded for {company}?",
    ),
    "unanswerable_relationship": (
        "Which law firm advises {company}?",
        "Which law firm acts as adviser to {company}?",
    ),
    "unanswerable_history": (
        "Who was the chair of {company} on {date}?",
        "Who chaired {company} on {date}?",
    ),
    "unanswerable_financial": (
        "How much did {company} pay in dividends in {year}?",
        "What dividend did {company} pay in {year}?",
    ),
    "unanswerable_presupposition": (
        "Why did {company} acquire {target} in {year}?",
        "What was the reason for the acquisition of {target} by {company} in {year}?",
    ),
}


def _iso_date(value: str | None) -> dt.date:
    if not value:
        raise QueryGenerationError("missing ISO date")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise QueryGenerationError(f"bad ISO date {value!r}") from exc


def _sentence_evidence(ctx: DocumentContext, sentence_text: str,
                       quote: str | None = None) -> QueryEvidence:
    idx = ctx.sentence_index.get(sentence_text)
    if idx is None:
        raise QueryGenerationError(f"{ctx.document_id}: unknown sentence {sentence_text!r}")
    return QueryEvidence(
        document_id=ctx.document_id,
        filename=ctx.filename,
        text_sha256=ctx.text_sha256,
        sentence_idx=idx,
        sentence_id=f"s{idx}",
        quote=quote or sentence_text,
    )


def _time_evidence(ctx: DocumentContext, spec) -> QueryEvidence:
    return _sentence_evidence(ctx, spec.sentence_text, quote=spec.surface)


def _doc_date_evidence(ctx: DocumentContext) -> QueryEvidence:
    for spec in ctx.plan.times:
        if spec.surface == format_date(ctx.plan.doc_date) and spec.sentence_text.startswith("Date:"):
            return _time_evidence(ctx, spec)
    raise QueryGenerationError(f"{ctx.document_id}: no document-date evidence")


def _stated(ctx: DocumentContext, *, relation: str | None = None,
            kind: str | None = None, status: str | None = None,
            negated: bool | None = None) -> list:
    out = []
    for stated in ctx.plan.stated_facts:
        fact = stated.fact
        if relation is not None and fact.relation != relation:
            continue
        if kind is not None and fact.kind != kind:
            continue
        if status is not None and fact.status != status:
            continue
        if negated is not None and fact.negated != negated:
            continue
        out.append(stated)
    return out


def _first_stated(ctx: DocumentContext, *, relation: str | None = None,
                  kind: str | None = None, status: str | None = None,
                  negated: bool | None = None):
    found = _stated(ctx, relation=relation, kind=kind, status=status, negated=negated)
    return found[0] if found else None


def _fact_evidence(ctx: DocumentContext, stated) -> QueryEvidence:
    return _sentence_evidence(ctx, stated.sentence_text)


def _sentence_containing(ctx: DocumentContext, needle: str) -> str:
    for sentence in ctx.sentences:
        if needle in sentence:
            return sentence
    raise QueryGenerationError(f"{ctx.document_id}: no sentence contains {needle!r}")


def _current_chair(ctx: DocumentContext):
    chairs = _stated(ctx, relation="chair_of")
    if not chairs:
        return None
    return max(chairs, key=lambda s: (s.fact.date or dt.date.min, s.fact.fact_id))


def _old_chair(ctx: DocumentContext):
    chairs = [s for s in _stated(ctx, relation="chair_of") if s.fact.end_date is not None]
    if not chairs:
        return None
    return max(chairs, key=lambda s: (s.fact.end_date or dt.date.min, s.fact.fact_id))


def _company_by_name(world: World, name: str) -> Entity:
    for company in world.companies():
        if company.name == name:
            return company
    raise QueryGenerationError(f"no generated company named {name!r}")


def _display(world: World, entity_id: str) -> str:
    return world.display_name(entity_id)


def _target_label(kind: str) -> str:
    return {"company": "company", "asset": "asset"}.get(kind, "entity")


class _Builder:
    def __init__(self, index: CorpusIndex, world: World, *,
                 variants: bool = False) -> None:
        self.index = index
        self.world = world
        self.variants = variants
        self.queries: list[QAQuery] = []
        self._seen_questions: set[str] = set()
        self._seen_pairs: Counter[tuple[str, str]] = Counter()
        self._per_document_counter = 0
        self._global_counter = 0

    def phrase(self, key: str, default: str, **values: str) -> str:
        """Return a deterministic wording variant, or the frozen default.

        ``variants=False`` preserves the Phase 1 question stream byte for byte.
        Phase 2 uses stable keys (fact/document ids) so the selected wording
        never depends on the requested corpus size.
        """
        if not self.variants:
            return default
        variant_key = key.split(":", 1)[0]
        template = _variant(key, list(QUESTION_VARIANTS[variant_key]))
        return template.format(**values)

    def add(self, *, question: str, query_type: str, expected_answer: str,
            answer_type: str, answerable: bool,
            evidence: list[QueryEvidence] | None = None,
            gold_fact_ids: list[str] | None = None,
            notes: str = "", global_query: bool = False,
            query_id: str | None = None,
            unanswerable_kind: UnanswerableKind | None = None,
            unanswerable_proof: UnanswerableProof | None = None) -> QAQuery | None:
        question = " ".join(question.split())
        if not question.endswith("?"):
            question += "?"
        if question in self._seen_questions:
            return None
        normalized = normalize_question(question)
        pair = (normalized, normalize_question(expected_answer))
        # A normalized Q/A pair may occur at most twice.  This is a hard safety
        # cap; the v2 selector reports the actual duplicate-form rate.
        if self._seen_pairs[pair] >= 2:
            return None
        evidence = list(evidence or [])
        gold_fact_ids = list(gold_fact_ids or [])
        relevant = list(dict.fromkeys(e.document_id for e in evidence))
        if query_id is None:
            if global_query:
                self._global_counter += 1
                query_id = f"xq_{self._global_counter:05d}"
            else:
                self._per_document_counter += 1
                query_id = f"q_{self._per_document_counter:05d}"
        query = QAQuery(
            query_id=query_id,
            question=question,
            query_type=query_type,
            expected_answer=expected_answer,
            answer_type=answer_type,
            answerable=answerable,
            supporting_evidence=evidence,
            gold_fact_ids=gold_fact_ids,
            relevant_document_ids=relevant,
            notes=notes,
            unanswerable_kind=unanswerable_kind,
            unanswerable_proof=unanswerable_proof,
        )
        self._seen_questions.add(question)
        self._seen_pairs[pair] += 1
        self.queries.append(query)
        return query


def _add_entity_lookup(builder: _Builder, ctx: DocumentContext) -> None:
    company = builder.world.entity(ctx.plan.subject_entity_id)
    title = ctx.sentences[0]
    builder.add(
        question=builder.phrase(
            f"entity_lookup:{ctx.document_id}",
            f"Which organization is identified in the title of {ctx.filename}?",
            filename=ctx.filename),
        query_type="entity_lookup",
        expected_answer=company.name,
        answer_type="entity",
        answerable=True,
        evidence=[_sentence_evidence(ctx, title)],
        notes="The document title names the subject organization.",
    )


def _add_relationship_lookup(builder: _Builder, ctx: DocumentContext) -> None:
    choice = ctx.plan.index % 5
    relation = ["chair_of", "shareholder_of", "represents", "family_of", "family_of"][choice]
    stated = _first_stated(ctx, relation=relation)
    if stated is None:
        return
    fact = stated.fact
    world = builder.world
    subject = world.entity(fact.subject_id or "")
    obj = world.entity(fact.object_id or "")
    if relation == "chair_of":
        current = _current_chair(ctx)
        if current is None:
            return
        stated = current
        fact = current.fact
        subject = world.entity(fact.subject_id or "")
        obj = world.entity(fact.object_id or "")
        question = builder.phrase(
            f"relationship_chair:{fact.fact_id}:{ctx.document_id}",
            f"Who is the chair of {obj.name} according to {ctx.filename}?",
            company=obj.name, filename=ctx.filename)
        expected = _display(world, subject.entity_id)
    elif relation == "shareholder_of":
        question = builder.phrase(
            f"relationship_shareholder:{fact.fact_id}:{ctx.document_id}",
            f"Who is a shareholder of {obj.name} according to {ctx.filename}?",
            company=obj.name, filename=ctx.filename)
        expected = _display(world, subject.entity_id)
    elif relation == "represents":
        question = builder.phrase(
            f"relationship_represents:{fact.fact_id}:{ctx.document_id}",
            f"Which law firm represents {obj.name} in {ctx.filename}?",
            company=obj.name, filename=ctx.filename)
        expected = subject.name
    elif relation == "family_of":
        # Direction: subject is a family member of object.  Do not reverse it.
        question = builder.phrase(
            f"relationship_family:{fact.fact_id}:{ctx.document_id}",
            (f"Who is {_display(world, subject.entity_id)} a family member of "
             f"according to {ctx.filename}?"),
            person=_display(world, subject.entity_id), filename=ctx.filename)
        expected = _display(world, obj.entity_id)
    else:  # pragma: no cover - defensive
        return
    builder.add(
        question=question,
        query_type="relationship_lookup",
        expected_answer=expected,
        answer_type="entity",
        answerable=True,
        evidence=[_fact_evidence(ctx, stated)],
        gold_fact_ids=[fact.fact_id],
        notes=f"Critical relation: {relation}; direction follows the gold edge.",
    )


def _add_ownership_lookups(builder: _Builder, ctx: DocumentContext) -> None:
    """Emit only ownership questions with exactly one valid document answer.

    A document commonly states both a company-style ownership and an asset
    ownership.  An unqualified "What does X own?" question would then have two
    valid answers, so qualifying by target type is mandatory unless the
    document contains a single ownership statement.
    """
    world = builder.world
    stated = [s for s in _stated(ctx, relation="owns") if not s.fact.negated]
    by_subject: dict[str, list] = {}
    for item in stated:
        by_subject.setdefault(item.fact.subject_id or "", []).append(item)

    for subject_id, items in by_subject.items():
        subject = world.entity(subject_id)
        if len(items) == 1:
            item = items[0]
            expected = world.entity(item.fact.object_id or "").name
            builder.add(
                question=builder.phrase(
                    f"relationship_owns:{item.fact.fact_id}:{ctx.document_id}",
                    (f"Which entity does {subject.name} own according to "
                     f"{ctx.filename}?"),
                    owner=subject.name, filename=ctx.filename),
                query_type="relationship_lookup",
                expected_answer=expected,
                answer_type="entity",
                answerable=True,
                evidence=[_fact_evidence(ctx, item)],
                gold_fact_ids=[item.fact.fact_id],
                notes="Unique ownership statement in this document.",
            )
            continue

        groups: dict[str, list] = {}
        for item in items:
            target = world.entity(item.fact.object_id or "")
            groups.setdefault(target.kind, []).append(item)
        for kind, group in sorted(groups.items()):
            # Explicit uniqueness check: only ask when this qualified target
            # type has exactly one valid answer in the document.
            if len(group) != 1:
                continue
            item = group[0]
            expected = world.entity(item.fact.object_id or "").name
            builder.add(
                question=builder.phrase(
                    f"relationship_owns_kind:{kind}:{item.fact.fact_id}:{ctx.document_id}",
                    (f"Which {_target_label(kind)} does {subject.name} own "
                     f"according to {ctx.filename}?"),
                    kind=_target_label(kind), owner=subject.name,
                    filename=ctx.filename),
                query_type="relationship_lookup",
                expected_answer=expected,
                answer_type="entity",
                answerable=True,
                evidence=[_fact_evidence(ctx, item)],
                gold_fact_ids=[item.fact.fact_id],
                notes=(f"Ownership qualified by target type {kind!r}; exactly one "
                       "valid answer in this document."),
            )


def _add_extra_critical_relation(builder: _Builder, ctx: DocumentContext) -> None:
    world = builder.world
    if ctx.plan.index % 5 == 1:
        stated = _first_stated(ctx, relation="guarantor_for", negated=False)
        if stated:
            fact = stated.fact
            guarantor = world.entity(fact.subject_id or "")
            company = world.entity(fact.object_id or "")
            builder.add(
                question=builder.phrase(
                    f"relationship_guarantor:{fact.fact_id}:{ctx.document_id}",
                    (f"Which company guaranteed the loan to {company.name} "
                     f"according to {ctx.filename}?"),
                    company=company.name, filename=ctx.filename),
                query_type="relationship_lookup",
                expected_answer=guarantor.name,
                answer_type="entity",
                answerable=True,
                evidence=[_fact_evidence(ctx, stated)],
                gold_fact_ids=[fact.fact_id],
                notes="Critical relation: guarantor_for.",
            )


def _add_financial(builder: _Builder, ctx: DocumentContext) -> None:
    world = builder.world
    for stated in _stated(ctx, relation="lent_to"):
        fact = stated.fact
        if fact.amount_gbp is None or fact.date is None:
            continue
        bank = world.entity(fact.subject_id or "")
        company = world.entity(fact.object_id or "")
        date_text = format_date(fact.date)
        builder.add(
            question=builder.phrase(
                f"financial_lend:{fact.fact_id}:{ctx.document_id}",
                (f"How much did {bank.name} lend to {company.name} on "
                 f"{date_text} according to {ctx.filename}?"),
                bank=bank.name, company=company.name, date=date_text,
                filename=ctx.filename),
            query_type="financial",
            expected_answer=_money(fact.amount_gbp),
            answer_type="money",
            answerable=True,
            evidence=[_fact_evidence(ctx, stated)],
            gold_fact_ids=[fact.fact_id],
            notes="Completed loan amount stated in the document.",
        )
    # Facility capacity is a proposition, not a loan.  Ask about the facility
    # itself; never phrase the increase as money that was lent.
    for stated in _stated(ctx, kind="proposition"):
        fact = stated.fact
        if fact.amount_gbp is None or fact.note not in (
                FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
            continue
        if fact.note == FACILITY_CAPACITY_INCREASE:
            question = builder.phrase(
                f"financial_facility_increase:{fact.fact_id}:{ctx.document_id}",
                (f"What was the facility increased to according to "
                 f"{ctx.filename}?"), filename=ctx.filename)
            notes = ("Facility capacity increase; not a completed loan or a "
                     "disbursement.")
        else:
            question = builder.phrase(
                f"financial_facility_original:{fact.fact_id}:{ctx.document_id}",
                (f"What was the original facility amount according to "
                 f"{ctx.filename}?"), filename=ctx.filename)
            notes = ("Original facility capacity; not a completed loan or a "
                     "disbursement.")
        builder.add(
            question=question,
            query_type="financial",
            expected_answer=_money(fact.amount_gbp),
            answer_type="money",
            answerable=True,
            evidence=[_fact_evidence(ctx, stated)],
            gold_fact_ids=[fact.fact_id],
            notes=notes,
        )
    owes = _first_stated(ctx, relation="owes")
    if owes:
        fact = owes.fact
        if fact.amount_gbp is not None and fact.date is not None:
            company = world.entity(fact.subject_id or "")
            bank = world.entity(fact.object_id or "")
            date_text = format_date(fact.date)
            builder.add(
                question=builder.phrase(
                    f"financial_owes:{fact.fact_id}:{ctx.document_id}",
                    (f"How much does {company.name} owe {bank.name} as at "
                     f"{date_text} according to {ctx.filename}?"),
                    company=company.name, bank=bank.name, date=date_text,
                    filename=ctx.filename),
                query_type="financial",
                expected_answer=_money(fact.amount_gbp),
                answer_type="money",
                answerable=True,
                evidence=[_fact_evidence(ctx, owes)],
                gold_fact_ids=[fact.fact_id],
                notes="Outstanding amount as at the stated date; not a payment.",
            )


def _add_temporal(builder: _Builder, ctx: DocumentContext) -> None:
    world = builder.world
    relative = next((t for t in ctx.plan.times if t.kind == "RELATIVE"), None)
    if relative and relative.value_start:
        due = _iso_date(relative.value_start)
        builder.add(
            question=builder.phrase(
                f"temporal_relative:{ctx.document_id}",
                f"When will the facility described in {ctx.filename} be reviewed?",
                filename=ctx.filename),
            query_type="temporal",
            expected_answer=format_date(due),
            answer_type="date",
            answerable=True,
            evidence=[_time_evidence(ctx, relative), _doc_date_evidence(ctx)],
            notes="Relative date resolved from the document date.",
        )

    fiscal = next((t for t in ctx.plan.times
                   if t.kind == "PERIOD" and t.label), None)
    if fiscal and fiscal.value_start and fiscal.value_end:
        start = _iso_date(fiscal.value_start)
        end = _iso_date(fiscal.value_end)
        builder.add(
            question=builder.phrase(
                f"temporal_fiscal:{ctx.document_id}:{fiscal.label}",
                f"What period does {fiscal.label} cover in {ctx.filename}?",
                label=fiscal.label or "", filename=ctx.filename),
            query_type="temporal",
            expected_answer=f"{format_date(start)} to {format_date(end)}",
            answer_type="period",
            answerable=True,
            evidence=[_time_evidence(ctx, fiscal)],
            notes=("Fiscal period is stated explicitly in the document; no external "
                   "fiscal-year assumption is required."),
        )

    current = _current_chair(ctx)
    if current and current.fact.end_date is None:
        specs = [t for t in ctx.plan.times if t.sentence_text == current.sentence_text]
        if specs:
            person = world.entity(current.fact.subject_id or "")
            company = world.entity(current.fact.object_id or "")
            spec = specs[0]
            builder.add(
                question=builder.phrase(
                    f"temporal_chair:{current.fact.fact_id}:{ctx.document_id}",
                    (f"When did {_display(world, person.entity_id)} become chair of "
                     f"{company.name} according to {ctx.filename}?"),
                    person=_display(world, person.entity_id),
                    company=company.name, filename=ctx.filename),
                query_type="temporal",
                expected_answer=spec.surface,
                answer_type="date",
                answerable=True,
                evidence=[_time_evidence(ctx, spec)],
                gold_fact_ids=[current.fact.fact_id],
                notes="Explicit appointment date.",
            )


def _add_historical(builder: _Builder, ctx: DocumentContext) -> None:
    old = _old_chair(ctx)
    if old is None:
        return
    world = builder.world
    person = world.entity(old.fact.subject_id or "")
    company = world.entity(old.fact.object_id or "")
    end_text = format_date(old.fact.end_date or ctx.plan.doc_date)
    builder.add(
        question=builder.phrase(
            f"historical:{old.fact.fact_id}:{ctx.document_id}",
            (f"Who was the chair of {company.name} until {end_text} "
             f"according to {ctx.filename}?"),
            company=company.name, date=end_text, filename=ctx.filename),
        query_type="historical_change",
        expected_answer=_display(world, person.entity_id),
        answer_type="entity",
        answerable=True,
        evidence=[_fact_evidence(ctx, old)],
        gold_fact_ids=[old.fact.fact_id],
        notes="Historical office-holder, not the current chair.",
    )


def _add_identity(builder: _Builder, ctx: DocumentContext) -> None:
    world = builder.world
    if ctx.plan.identity_traps:
        a, b, _ = ctx.plan.identity_traps[0]
        name_a = world.display_name(a)
        name_b = world.display_name(b)
        sentence = _sentence_containing(ctx, "are different individuals")
        builder.add(
            question=builder.phrase(
                f"identity_same:{ctx.document_id}",
                (f"Are {name_a} and {name_b} in {ctx.filename} the same "
                 f"person?"),
                name_a=name_a, name_b=name_b, filename=ctx.filename),
            query_type="identity_resolution",
            expected_answer="No",
            answer_type="boolean",
            answerable=True,
            evidence=[_sentence_evidence(ctx, sentence)],
            notes="The document explicitly distinguishes the two namesakes.",
        )
    alias_entity = world.entity(world.alias_person)
    aliases = set(alias_entity.aliases)
    mention = next((m for m in ctx.plan.mentions
                    if m.entity_id == alias_entity.entity_id and m.surface in aliases), None)
    if mention:
        builder.add(
            question=builder.phrase(
                f"identity_alias:{ctx.document_id}",
                (f"Which person is also known as {mention.surface} in "
                 f"{ctx.filename}?"),
                alias=mention.surface, filename=ctx.filename),
            query_type="identity_resolution",
            expected_answer=alias_entity.name,
            answer_type="entity",
            answerable=True,
            evidence=[_sentence_evidence(ctx, mention.sentence_text)],
            notes="Alias resolution: both names occur in the same sentence.",
        )


def _add_negation(builder: _Builder, ctx: DocumentContext) -> None:
    stated = _first_stated(ctx, kind="edge", negated=True)
    if stated is None:
        return
    fact = stated.fact
    world = builder.world
    subject = world.entity(fact.subject_id or "")
    obj = world.entity(fact.object_id or "")
    date_text = format_date(fact.date or ctx.plan.doc_date)
    if fact.relation == "guarantor_for":
        question = builder.phrase(
            f"negation:{fact.fact_id}:{ctx.document_id}",
            (f"Did {subject.name} guarantee the facility for {obj.name} on "
             f"{date_text} according to {ctx.filename}?"),
            subject=subject.name, object=obj.name, date=date_text,
            filename=ctx.filename)
    else:
        question = (f"Does {subject.name} have the relation {fact.relation} to "
                    f"{obj.name} according to {ctx.filename}?")
    builder.add(
        question=question,
        query_type="negation",
        expected_answer="No",
        answer_type="boolean",
        answerable=True,
        evidence=[_fact_evidence(ctx, stated)],
        gold_fact_ids=[fact.fact_id],
        notes="The relationship is explicitly negated in the document.",
    )


def _add_proposal(builder: _Builder, ctx: DocumentContext) -> None:
    stated = _first_stated(ctx, kind="proposition", status="proposed")
    if stated is None:
        return
    fact = stated.fact
    world = builder.world
    match = re.match(r"The acquisition of (.+?) by (.+)$", fact.text or "")
    if not match:
        raise QueryGenerationError(f"{ctx.document_id}: malformed proposal text {fact.text!r}")
    target = _company_by_name(world, match.group(1))
    company = world.entity(fact.subject_id or "")
    evidence = _fact_evidence(ctx, stated)
    builder.add(
        question=builder.phrase(
            f"proposal_target:{fact.fact_id}:{ctx.document_id}",
            f"What did {company.name} propose to acquire according to {ctx.filename}?",
            company=company.name, filename=ctx.filename),
        query_type="proposed_vs_completed",
        expected_answer=target.name,
        answer_type="entity",
        answerable=True,
        evidence=[evidence],
        gold_fact_ids=[fact.fact_id],
        notes="Proposal, not a completed acquisition.",
    )
    if builder.variants:
        # Make the closed-world assumption explicit in the question itself, not
        # only in the annotation notes.  Two deterministic phrasings keep the
        # v2 wording varied while v1 stays literal.
        completion_options = (
            ("Under the closed-world assumption that every completed acquisition "
             "is recorded in this corpus, had {company} completed the acquisition "
             "of {target} when {filename} was written?"),
            ("Applying this corpus's closed-world assumption to completed "
             "acquisitions, had {company} completed the acquisition of {target} "
             "when {filename} was written?"),
        )
        completion_question = _variant(
            f"proposal_completed_cw:{fact.fact_id}:{ctx.document_id}",
            list(completion_options)).format(
                company=company.name, target=target.name, filename=ctx.filename)
    else:
        completion_question = (
            f"Had {company.name} completed the acquisition of {target.name} when "
            f"{ctx.filename} was written?")
    builder.add(
        question=completion_question,
        query_type="proposed_vs_completed",
        expected_answer="No",
        answer_type="boolean",
        answerable=True,
        evidence=[evidence],
        gold_fact_ids=[fact.fact_id],
        notes=("The proposition status is proposed, not completed. Closed-world: no "
               f"document in this corpus records a completed acquisition of {target.name} "
               f"by {company.name}."),
    )


def _add_allegation(builder: _Builder, ctx: DocumentContext) -> None:
    stated = _first_stated(ctx, kind="proposition", status="alleged")
    if stated is None:
        return
    fact = stated.fact
    company = builder.world.entity(fact.subject_id or "")
    builder.add(
        question=builder.phrase(
            f"alleged:{fact.fact_id}:{ctx.document_id}",
            (f"According to {ctx.filename}, was the claim that {company.name} "
             f"misstated its revenue alleged or proven?"),
            company=company.name, filename=ctx.filename),
        query_type="alleged_status",
        expected_answer="Alleged",
        answer_type="text",
        answerable=True,
        evidence=[_fact_evidence(ctx, stated)],
        gold_fact_ids=[fact.fact_id],
        notes=("Allegation, not a proven finding. The speaker is an unnamed former "
               "employee; the named person only reported the allegation."),
    )


def _add_provenance(builder: _Builder, ctx: DocumentContext) -> None:
    reference_sentence = next(
        (s for s in ctx.sentences if s.startswith("Document reference: ")), None)
    if reference_sentence:
        reference = reference_sentence.split(": ", 1)[1].rstrip(".")
        builder.add(
            question=builder.phrase(
                f"provenance_reference:{ctx.document_id}",
                f"Which document has the reference {reference}?",
                reference=reference),
            query_type="provenance",
            expected_answer=ctx.filename,
            answer_type="document",
            answerable=True,
            evidence=[_sentence_evidence(ctx, reference_sentence)],
            notes="Unique document reference in the corpus.",
        )

    if ctx.plan.index % 5 != 1:
        return
    stated = _first_stated(ctx, relation="lent_to")
    if stated is None:
        return
    occurrences = builder.index.occurrences_for(stated.fact.fact_id)
    if len(occurrences) != 1:
        return
    fact = stated.fact
    if fact.amount_gbp is None or fact.date is None:
        return
    world = builder.world
    bank = world.entity(fact.subject_id or "")
    company = world.entity(fact.object_id or "")
    builder.add(
        question=builder.phrase(
            f"provenance_fact:{fact.fact_id}:{ctx.document_id}",
            (f"Which document states that {bank.name} lent {_money(fact.amount_gbp)} "
             f"to {company.name} on {format_date(fact.date)}?"),
            bank=bank.name, amount=_money(fact.amount_gbp),
            company=company.name, date=format_date(fact.date)),
        query_type="provenance",
        expected_answer=ctx.filename,
        answer_type="document",
        answerable=True,
        evidence=[_fact_evidence(ctx, stated)],
        gold_fact_ids=[fact.fact_id],
        notes="The fact occurs in exactly one document in this corpus.",
    )


_UNANSWERABLE_TEMPLATES = [
    (
        "What is the VAT registration number of {company}?",
        ("vat", "registration number"),
    ),
    (
        "Who is the chief executive officer of {company}?",
        ("chief executive",),
    ),
    (
        "What is the postal address of {company}?",
        ("postal address",),
    ),
    (
        "How much did {company} pay in dividends in {year}?",
        ("dividend",),
    ),
    (
        "Who audited the accounts of {company}?",
        ("auditor", "audited"),
    ),
]


def _add_unanswerable(builder: _Builder, ctx: DocumentContext) -> None:
    if ctx.plan.index % 5 != 0:
        return
    company = builder.world.entity(ctx.plan.subject_entity_id)
    template, keywords = _UNANSWERABLE_TEMPLATES[
        (ctx.plan.index // 5) % len(_UNANSWERABLE_TEMPLATES)]
    corpus = builder.index.corpus_text()
    found = [keyword for keyword in keywords if keyword in corpus]
    if found:
        raise QueryGenerationError(
            f"unanswerable query is actually supported: corpus contains {found!r}")
    builder.add(
        question=template.format(company=company.name, year=ctx.plan.doc_date.year),
        query_type="unanswerable",
        expected_answer="",
        answer_type="unanswerable",
        answerable=False,
        notes=("No supporting statement exists in the corpus; checked absent keywords: "
               + ", ".join(keywords)),
    )


# --------------------------------------------------------------------------- v2 unanswerables
#: Attribute questions are structural: the attribute is absent from both the
#: entity record and every fact involving that individual entity.  The two
#: wordings per attribute are selected deterministically from the stable
#: entity id.
_ATTRIBUTE_VARIANTS = {
    "auditor": (
        "Who is the auditor of {company}?",
        "Which firm audited the accounts of {company}?",
    ),
    "vat_registration_number": (
        "What is the VAT registration number of {company}?",
        "Which VAT registration number is recorded for {company}?",
    ),
}


def _ordered_companies(fact_index: FactIndex) -> list[Entity]:
    """Document subject companies first, then the rest of the world."""
    world = fact_index.world
    seen: set[str] = set()
    ordered: list[Entity] = []
    for entity_id in fact_index.document_subjects:
        entity = world.entities.get(entity_id)
        if entity is not None and entity.kind == "company" and entity_id not in seen:
            ordered.append(entity)
            seen.add(entity_id)
    for company in world.companies():
        if company.entity_id not in seen:
            ordered.append(company)
            seen.add(company.entity_id)
    return ordered


_TOKEN_PATTERN_CACHE: dict[tuple[str, ...], tuple[re.Pattern[str], ...]] = {}


def _fact_mentions_tokens(fact: Fact, tokens: tuple[str, ...]) -> bool:
    patterns = _TOKEN_PATTERN_CACHE.get(tokens)
    if patterns is None:
        patterns = tuple(re.compile(rf"\b{re.escape(token)}\b") for token in tokens)
        _TOKEN_PATTERN_CACHE[tokens] = patterns
    haystack = f"{fact.note} {fact.text or ''}".casefold()
    return any(pattern.search(haystack) for pattern in patterns)


def _attribute_absent(fact_index: FactIndex, company: Entity, attribute: str) -> bool:
    if attribute in fact_index.attributes.get(company.entity_id, {}):
        return False
    return not fact_index.mentions_attribute(company.entity_id, attribute)


def _absent_attribute_specs(fact_index: FactIndex) -> list[dict]:
    specs: list[dict] = []
    subject_ids = {entity_id for entity_id in fact_index.document_subjects
                   if fact_index.world.entities[entity_id].kind == "company"}
    companies = [company for company in _ordered_companies(fact_index)
                 if company.entity_id in subject_ids]
    for attribute in ("auditor", "vat_registration_number"):
        for company in companies:
            if not _attribute_absent(fact_index, company, attribute):
                continue
            related = fact_index.facts_for_entity(company.entity_id)
            checked = [fact.fact_id for fact in related[:12]]
            if not checked:
                continue
            distractor = next(
                (other for other in companies
                 if attribute in fact_index.attributes.get(other.entity_id, {})), None)
            distractor_text = (
                f" {distractor.name} has {attribute}="
                f"{fact_index.attributes[distractor.entity_id][attribute]!r}, but that "
                f"value belongs to a different entity."
                if distractor is not None else "")
            explanation = (
                f"{company.name} has no {attribute} attribute in the world model, and "
                f"none of the {len(checked)} indexed facts involving it states one."
                + distractor_text)
            specs.append({
                "company": company,
                "attribute": attribute,
                "checked_fact_ids": checked,
                "checked_document_ids": fact_index.checked_documents(
                    checked, company.entity_id),
                "explanation": explanation,
            })
    return specs


def _absent_relationship_specs(fact_index: FactIndex) -> list[dict]:
    specs: list[dict] = []
    for company in _ordered_companies(fact_index):
        if fact_index.has_relation(company.entity_id, "advises",
                                   include_negated=True):
            continue
        distractors = [
            fact for fact in fact_index.facts_for_object(company.entity_id)
            if fact.kind == "edge" and fact.relation == "represents"
            and not fact.negated
        ]
        if not distractors:
            continue
        checked = [fact.fact_id for fact in distractors[:6]]
        explanation = (
            f"No edge with relation 'advises' has {company.name} as subject anywhere "
            f"in the world or the corpus. The nearest professional relationship is "
            f"a representation edge ({len(distractors)} fact(s)), which does not "
            "establish an advisory mandate.")
        specs.append({
            "company": company,
            "checked_fact_ids": checked,
            "checked_document_ids": fact_index.checked_documents(
                checked, company.entity_id),
            "explanation": explanation,
        })
    return specs


def _unsupported_historical_specs(fact_index: FactIndex) -> list[dict]:
    specs: list[dict] = []
    for company in _ordered_companies(fact_index):
        chairs = fact_index.chair_facts(company.entity_id)
        dated = [fact for fact in chairs if fact.date is not None]
        if not dated:
            continue
        earliest = min(fact.date for fact in dated)
        assert earliest is not None
        reference = earliest - dt.timedelta(days=365)
        covers = any(
            fact.date is not None and fact.date <= reference
            and (fact.end_date is None or fact.end_date >= reference)
            for fact in dated)
        if covers:
            continue
        checked = [fact.fact_id for fact in chairs]
        explanation = (
            f"The earliest indexed chair_of fact for {company.name} starts on "
            f"{format_date(earliest)}. No chair fact covers {format_date(reference)}, "
            "so the historical office-holder on that date is unsupported.")
        specs.append({
            "company": company,
            "reference_date": reference,
            "checked_fact_ids": checked,
            "checked_document_ids": fact_index.checked_documents(
                checked, company.entity_id),
            "explanation": explanation,
        })
    return specs


def _dividend_fact(fact: Fact) -> bool:
    if fact.relation in ("dividend", "paid_dividend"):
        return True
    return bool(re.search(r"\bdividend\b", f"{fact.note} {fact.text or ''}".casefold()))


def _absent_financial_specs(fact_index: FactIndex) -> list[dict]:
    specs: list[dict] = []
    year = 2024
    for company in _ordered_companies(fact_index):
        if fact_index.has_dividend(company.entity_id):
            continue
        financial = fact_index.financial_facts(company.entity_id)
        if not financial:
            continue
        checked = [fact.fact_id for fact in financial[:12]]
        explanation = (
            f"No fact in the world or corpus records a dividend for {company.name}. "
            f"Its indexed financial facts are lending, balances or fees "
            f"({len(financial)} fact(s), checked), none of which is a dividend in "
            f"{year}.")
        specs.append({
            "company": company,
            "year": year,
            "checked_fact_ids": checked,
            "checked_document_ids": fact_index.checked_documents(
                checked, company.entity_id),
            "explanation": explanation,
        })
    return specs


def _proposal_target(world: World, fact: Fact) -> Entity | None:
    match = re.match(r"The acquisition of (.+?) by (.+)$", fact.text or "")
    if not match:
        return None
    try:
        target = _company_by_name(world, match.group(1))
    except QueryGenerationError:
        return None
    return target


def _false_presupposition_specs(fact_index: FactIndex) -> list[dict]:
    world = fact_index.world
    companies = _ordered_companies(fact_index)
    proposal_first = sorted(
        companies,
        key=lambda company: 0 if fact_index.proposals_for(company.entity_id) else 1)
    specs: list[dict] = []
    for company in proposal_first:
        owns_facts = fact_index.edges_for(company.entity_id, "owns")
        proposals = fact_index.proposals_for(company.entity_id)
        if not owns_facts and not proposals:
            continue
        owned = {
            fact.object_id for fact in owns_facts
            if fact.object_id is not None
        }
        target: Entity | None = None
        proposal: Fact | None = None
        for candidate in proposals:
            possible = _proposal_target(world, candidate)
            if (possible is not None and possible.entity_id != company.entity_id
                    and possible.entity_id not in owned
                    and not fact_index.has_relation(
                        possible.entity_id, "subsidiary_of", company.entity_id,
                        include_negated=True)):
                target = possible
                proposal = candidate
                break
        if target is None:
            for other in companies:
                if other.entity_id == company.entity_id:
                    continue
                if other.entity_id in owned:
                    continue
                if fact_index.has_relation(other.entity_id, "subsidiary_of",
                                           company.entity_id, include_negated=True):
                    continue
                target = other
                break
        if target is None:
            continue
        if fact_index.has_relation(company.entity_id, "owns", target.entity_id,
                                   include_negated=True):
            continue
        checked = [fact.fact_id for fact in
                   fact_index.edges_for(company.entity_id, "owns")[:6]]
        if proposal is not None:
            checked.insert(0, proposal.fact_id)
            year = proposal.date.year if proposal.date else 2024
            near_miss = (
                f"The only acquisition-related statement is a proposal dated "
                f"{format_date(proposal.date) if proposal.date else 'undated'}; it "
                "has status 'proposed', not a completed acquisition.")
        else:
            year = 2024
            near_miss = "No related proposal or ownership edge connects the parties."
        explanation = (
            f"{company.name} has no completed ownership or acquisition edge to "
            f"{target.name} anywhere in the world or corpus. {near_miss} The why/"
            "reason presupposition is therefore false.")
        specs.append({
            "company": company,
            "target": target,
            "year": year,
            "checked_fact_ids": checked,
            "checked_document_ids": fact_index.checked_documents(
                checked, company.entity_id),
            "explanation": explanation,
        })
    return specs


#: Stable order and proof kind for the five required unanswerable categories.
UNANSWERABLE_KINDS: tuple[UnanswerableKind, ...] = (
    "absent_attribute",
    "absent_relationship",
    "unsupported_historical_date",
    "absent_financial_information",
    "false_presupposition",
)


def _unanswerable_specs(fact_index: FactIndex) -> dict[str, list[dict]]:
    return {
        "absent_attribute": _absent_attribute_specs(fact_index),
        "absent_relationship": _absent_relationship_specs(fact_index),
        "unsupported_historical_date": _unsupported_historical_specs(fact_index),
        "absent_financial_information": _absent_financial_specs(fact_index),
        "false_presupposition": _false_presupposition_specs(fact_index),
    }


def _add_unanswerable_v2(builder: _Builder, fact_index: FactIndex, *,
                         count: int) -> None:
    """Add exactly ``count`` unanswerables, evenly across the five kinds."""
    if count < 5:
        raise QueryGenerationError("v2 needs at least five unanswerable queries")
    specs = _unanswerable_specs(fact_index)
    base, remainder = divmod(count, len(UNANSWERABLE_KINDS))
    added = 0
    for position, kind in enumerate(UNANSWERABLE_KINDS):
        needed = base + (1 if position < remainder else 0)
        available = specs[kind]
        if len(available) < needed:
            raise QueryGenerationError(
                f"only {len(available)} provable {kind} questions, need {needed}")
        chosen: list[dict] = []
        seen_subjects: set[str] = set()
        for spec in available:
            subject_id = spec["company"].entity_id
            if subject_id in seen_subjects:
                continue
            chosen.append(spec)
            seen_subjects.add(subject_id)
            if len(chosen) == needed:
                break
        if len(chosen) < needed:
            for spec in available:
                if spec in chosen:
                    continue
                chosen.append(spec)
                if len(chosen) == needed:
                    break
        used = 0
        for spec in chosen:
            if used >= needed:
                break
            company: Entity = spec["company"]
            if kind == "absent_attribute":
                question = _variant(
                    f"unans_attr:{spec['attribute']}:{company.entity_id}",
                    list(_ATTRIBUTE_VARIANTS[spec["attribute"]])).format(
                        company=company.name)
                proof = UnanswerableProof(
                    kind=kind, subject_id=company.entity_id,
                    attribute=spec["attribute"],
                    checked_fact_ids=spec["checked_fact_ids"],
                    checked_document_ids=spec["checked_document_ids"],
                    explanation=spec["explanation"])
            elif kind == "absent_relationship":
                question = _variant(
                    f"unans_rel:{company.entity_id}",
                    list(QUESTION_VARIANTS["unanswerable_relationship"])).format(
                        company=company.name)
                proof = UnanswerableProof(
                    kind=kind, subject_id=company.entity_id, relation="advises",
                    checked_fact_ids=spec["checked_fact_ids"],
                    checked_document_ids=spec["checked_document_ids"],
                    explanation=spec["explanation"])
            elif kind == "unsupported_historical_date":
                reference = spec["reference_date"]
                question = _variant(
                    f"unans_hist:{company.entity_id}",
                    list(QUESTION_VARIANTS["unanswerable_history"])).format(
                        company=company.name, date=format_date(reference))
                proof = UnanswerableProof(
                    kind=kind, subject_id=company.entity_id, relation="chair_of",
                    reference_date=reference.isoformat(),
                    checked_fact_ids=spec["checked_fact_ids"],
                    checked_document_ids=spec["checked_document_ids"],
                    explanation=spec["explanation"])
            elif kind == "absent_financial_information":
                question = _variant(
                    f"unans_fin:{company.entity_id}",
                    list(QUESTION_VARIANTS["unanswerable_financial"])).format(
                        company=company.name, year=spec["year"])
                proof = UnanswerableProof(
                    kind=kind, subject_id=company.entity_id,
                    attribute="dividend",
                    reference_date=f"{spec['year']}-12-31",
                    checked_fact_ids=spec["checked_fact_ids"],
                    checked_document_ids=spec["checked_document_ids"],
                    explanation=spec["explanation"])
            else:  # false_presupposition
                target: Entity = spec["target"]
                question = _variant(
                    f"unans_presup:{company.entity_id}:{target.entity_id}",
                    list(QUESTION_VARIANTS["unanswerable_presupposition"])).format(
                        company=company.name, target=target.name, year=spec["year"])
                proof = UnanswerableProof(
                    kind=kind, subject_id=company.entity_id, relation="owns",
                    object_id=target.entity_id,
                    reference_date=f"{spec['year']}-12-31",
                    checked_fact_ids=spec["checked_fact_ids"],
                    checked_document_ids=spec["checked_document_ids"],
                    explanation=spec["explanation"])
            added += 1
            result = builder.add(
                question=question,
                query_type="unanswerable",
                expected_answer="",
                answer_type="unanswerable",
                answerable=False,
                notes=proof.explanation,
                query_id=f"uq_{added:05d}",
                unanswerable_kind=kind,
                unanswerable_proof=proof,
            )
            if result is None:
                added -= 1
                continue
            used += 1
        if used != needed:
            raise QueryGenerationError(
                f"added {used} {kind} questions, need {needed}")


def _add_multi_hop(builder: _Builder) -> None:
    world = builder.world
    index = builder.index
    for company_index, source in enumerate(world.companies()):
        bundle = world.facts_for_company(company_index)
        owns_fact = (bundle.get("owns") or [None])[0]
        if owns_fact is None or owns_fact.object_id is None:
            continue
        target = world.entity(owns_fact.object_id)
        if target.kind != "company":
            continue
        source_docs = [ctx for ctx in index.company_documents.get(source.entity_id, [])
                       if index.occurrence_in(owns_fact.fact_id, ctx.document_id)]
        target_docs = index.company_documents.get(target.entity_id, [])
        if not source_docs or not target_docs:
            continue
        source_ctx = source_docs[0]

        target_bundle = world.facts_for_company(company_index + 1)
        represents = (target_bundle.get("represents") or [None])[0]
        if represents is not None:
            target_ctx = next(
                (ctx for ctx in target_docs
                 if index.occurrence_in(represents.fact_id, ctx.document_id)), None)
            if target_ctx is not None and target_ctx.document_id != source_ctx.document_id:
                law_firm = world.entity(represents.subject_id or "")
                ownership = index.occurrence_in(owns_fact.fact_id, source_ctx.document_id)
                representation = index.occurrence_in(represents.fact_id, target_ctx.document_id)
                if ownership and representation:
                    builder.add(
                        question=builder.phrase(
                            f"multi_hop_represent:{source.entity_id}",
                            (f"Which law firm represents the company owned by "
                             f"{source.name}?"),
                            owner=source.name),
                        query_type="multi_hop",
                        expected_answer=law_firm.name,
                        answer_type="entity",
                        answerable=True,
                        evidence=[
                            QueryEvidence(
                                document_id=ownership.document_id,
                                filename=ownership.filename,
                                text_sha256=ownership.text_sha256,
                                sentence_idx=ownership.sentence_idx,
                                sentence_id=ownership.sentence_id,
                                quote=ownership.quote,
                            ),
                            QueryEvidence(
                                document_id=representation.document_id,
                                filename=representation.filename,
                                text_sha256=representation.text_sha256,
                                sentence_idx=representation.sentence_idx,
                                sentence_id=representation.sentence_id,
                                quote=representation.quote,
                            ),
                        ],
                        gold_fact_ids=[owns_fact.fact_id, represents.fact_id],
                        notes=("Cross-document join: ownership in "
                               f"{source_ctx.filename}; representation in {target_ctx.filename}."),
                        global_query=True,
                        query_id=f"xq_mh_represent_{source.entity_id}",
                    )

        loan = (target_bundle.get("loan") or [None])[0]
        if loan is not None and loan.amount_gbp is not None:
            target_ctx = next(
                (ctx for ctx in target_docs if index.occurrence_in(loan.fact_id, ctx.document_id)),
                None)
            if target_ctx is not None and target_ctx.document_id != source_ctx.document_id:
                bank = world.entity(loan.subject_id or "")
                ownership = index.occurrence_in(owns_fact.fact_id, source_ctx.document_id)
                lending = index.occurrence_in(loan.fact_id, target_ctx.document_id)
                if ownership and lending:
                    builder.add(
                        question=builder.phrase(
                            f"multi_hop_loan:{source.entity_id}",
                            f"Which bank lent money to the company owned by {source.name}?",
                            owner=source.name),
                        query_type="multi_hop",
                        expected_answer=bank.name,
                        answer_type="entity",
                        answerable=True,
                        evidence=[
                            QueryEvidence(
                                document_id=ownership.document_id,
                                filename=ownership.filename,
                                text_sha256=ownership.text_sha256,
                                sentence_idx=ownership.sentence_idx,
                                sentence_id=ownership.sentence_id,
                                quote=ownership.quote,
                            ),
                            QueryEvidence(
                                document_id=lending.document_id,
                                filename=lending.filename,
                                text_sha256=lending.text_sha256,
                                sentence_idx=lending.sentence_idx,
                                sentence_id=lending.sentence_id,
                                quote=lending.quote,
                            ),
                        ],
                        gold_fact_ids=[owns_fact.fact_id, loan.fact_id],
                        notes=("Cross-document join: ownership in "
                               f"{source_ctx.filename}; lending in {target_ctx.filename}."),
                        global_query=True,
                        query_id=f"xq_mh_loan_{source.entity_id}",
                    )


def _add_contradiction(builder: _Builder) -> None:
    world = builder.world
    index = builder.index
    for contradiction in world.contradictions:
        occurrences_a = index.occurrences_for(contradiction.fact_a_id)
        occurrences_b = index.occurrences_for(contradiction.fact_b_id)
        if not occurrences_a or not occurrences_b:
            continue
        a = occurrences_a[0]
        b = occurrences_b[0]
        fact_a = a.fact
        fact_b = b.fact
        if fact_a.amount_gbp is None or fact_a.date is None:
            continue
        bank = world.entity(fact_a.subject_id or "")
        company = world.entity(fact_a.object_id or "")
        amounts = sorted({_money(fact_a.amount_gbp), _money(fact_b.amount_gbp or 0)})
        builder.add(
            question=builder.phrase(
                f"contradiction:{fact_a.fact_id}:{fact_b.fact_id}",
                (f"What amount did {bank.name} lend to {company.name} on "
                 f"{format_date(fact_a.date)}?"),
                bank=bank.name, company=company.name,
                date=format_date(fact_a.date)),
            query_type="contradiction",
            expected_answer=("The documents conflict: " + " and ".join(amounts) + "."),
            answer_type="contradiction",
            answerable=True,
            evidence=[
                QueryEvidence(
                    document_id=a.document_id, filename=a.filename,
                    text_sha256=a.text_sha256, sentence_idx=a.sentence_idx,
                    sentence_id=a.sentence_id, quote=a.quote),
                QueryEvidence(
                    document_id=b.document_id, filename=b.filename,
                    text_sha256=b.text_sha256, sentence_idx=b.sentence_idx,
                    sentence_id=b.sentence_id, quote=b.quote),
            ],
            gold_fact_ids=[fact_a.fact_id, fact_b.fact_id],
            notes=(f"{contradiction.description} Sources: {a.filename}, {b.filename}."),
            global_query=True,
            query_id=f"xq_contradiction_{contradiction.contradiction_id}",
        )


def _validate_grounding(index: CorpusIndex, dataset: QADataset) -> None:
    """Every evidence quote must be a literal substring of its cited sentence."""
    for query in dataset.queries:
        for evidence in query.supporting_evidence:
            ctx = index.by_document.get(evidence.document_id)
            if ctx is None:
                raise QueryGenerationError(
                    f"{query.query_id}: evidence cites unknown document {evidence.document_id!r}")
            if evidence.filename != ctx.filename:
                raise QueryGenerationError(
                    f"{query.query_id}: evidence filename does not match document")
            if evidence.text_sha256 != ctx.text_sha256:
                raise QueryGenerationError(
                    f"{query.query_id}: evidence text hash does not match document")
            sentence = ctx.sentence_text_by_idx.get(evidence.sentence_idx)
            if sentence is None:
                raise QueryGenerationError(
                    f"{query.query_id}: evidence cites missing sentence index "
                    f"{evidence.sentence_idx}")
            if evidence.sentence_id != f"s{evidence.sentence_idx}":
                raise QueryGenerationError(
                    f"{query.query_id}: evidence sentence label does not match index")
            if evidence.quote not in sentence:
                raise QueryGenerationError(
                    f"{query.query_id}: evidence quote is not in the cited sentence: "
                    f"{evidence.quote!r}")
        for fact_id in query.gold_fact_ids:
            if fact_id not in index.occurrences:
                raise QueryGenerationError(
                    f"{query.query_id}: gold fact {fact_id!r} is not stated in the corpus")


# --------------------------------------------------------------------------- v2 selection
def _select_answerable(candidates: list[QAQuery], target: int) -> list[QAQuery]:
    """Deterministically select ``target`` answerable queries.

    Selection is round-robin across document groups, with cross-document
    queries forming one extra group.  This keeps every document represented and
    avoids a single long document dominating the benchmark.
    """
    if len(candidates) < target:
        raise QueryGenerationError(
            f"only {len(candidates)} answerable candidates, need {target}")
    groups: dict[str, list[tuple[int, QAQuery]]] = {}
    group_order: list[str] = []
    for position, query in enumerate(candidates):
        if query.query_id.startswith("xq_"):
            key = "__global__"
        else:
            key = query.relevant_document_ids[0] if query.relevant_document_ids else "__global__"
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append((position, query))
    # The global group is always considered last so a full round of local
    # documents is taken before a cross-document join.
    if "__global__" in group_order:
        group_order.remove("__global__")
        group_order.append("__global__")

    cursors = {key: 0 for key in group_order}
    selected: set[int] = set()
    while len(selected) < target:
        progressed = False
        for key in group_order:
            items = groups[key]
            cursor = cursors[key]
            if cursor >= len(items) or len(selected) >= target:
                continue
            position, _ = items[cursor]
            cursors[key] = cursor + 1
            selected.add(position)
            progressed = True
        if not progressed:
            break
    if len(selected) != target:  # pragma: no cover - guarded above
        raise QueryGenerationError(
            f"round-robin selected {len(selected)} answerable queries, need {target}")

    # Guard broad query-type coverage: if a type exists in the candidate pool
    # but was skipped, swap it in for a duplicated type rather than silently
    # losing the category.
    all_types = {query.query_type for query in candidates}
    while True:
        chosen = [query for position, query in enumerate(candidates)
                  if position in selected]
        present = {query.query_type for query in chosen}
        missing = sorted(all_types - present)
        if not missing:
            break
        type_counts = Counter(query.query_type for query in chosen)
        replacement = next(
            ((position, query) for position, query in enumerate(candidates)
             if position not in selected and query.query_type == missing[0]), None)
        if replacement is None:  # pragma: no cover - all_types came from candidates
            break
        remove_position = next(
            (position for position, query in reversed(list(enumerate(candidates)))
             if position in selected and type_counts[query.query_type] > 1), None)
        if remove_position is None:  # pragma: no cover - target leaves room
            break
        selected.remove(remove_position)
        selected.add(replacement[0])
    return [query for position, query in enumerate(candidates)
            if position in selected]


def duplicate_metrics(dataset: QADataset) -> dict[str, Any]:
    """Deterministic duplicate measurements for the manifest and tests."""
    forms = Counter(normalize_question(query.question)
                    for query in dataset.queries)
    pairs = Counter((normalize_question(query.question),
                     normalize_question(query.expected_answer))
                    for query in dataset.queries)
    duplicate_instances = sum(count - 1 for count in forms.values() if count > 1)
    total = len(dataset.queries)
    return {
        "total": total,
        "unique_normalized_forms": len(forms),
        "duplicate_normalized_instances": duplicate_instances,
        "duplicate_normalized_percent": round(
            100.0 * duplicate_instances / total, 2) if total else 0.0,
        "max_pair_occurrences": max(pairs.values()) if pairs else 0,
    }


def generate_qa_dataset_v2(index: CorpusIndex, world: World, *,
                           target_pages: int) -> QADataset:
    """Generate the Phase 2 benchmark: 70/25 answerable/unanswerable at 20 pages.

    Answerable questions are selected deterministically from all per-document
    and cross-document candidates.  Unanswerables carry a structured proof
    derived from the fact/entity index and are evenly distributed across the
    five required kinds.
    """
    candidate_builder = _Builder(index, world, variants=True)
    for ctx in index.documents:
        _add_entity_lookup(candidate_builder, ctx)
        _add_relationship_lookup(candidate_builder, ctx)
        _add_ownership_lookups(candidate_builder, ctx)
        _add_extra_critical_relation(candidate_builder, ctx)
        _add_financial(candidate_builder, ctx)
        _add_temporal(candidate_builder, ctx)
        _add_historical(candidate_builder, ctx)
        _add_identity(candidate_builder, ctx)
        _add_negation(candidate_builder, ctx)
        _add_proposal(candidate_builder, ctx)
        _add_allegation(candidate_builder, ctx)
        _add_provenance(candidate_builder, ctx)
    _add_multi_hop(candidate_builder)
    _add_contradiction(candidate_builder)

    answerable_target = min(10 * len(index.documents), len(candidate_builder.queries))
    unanswerable_target = max(20, round(answerable_target * 25 / 70))
    selected = _select_answerable(candidate_builder.queries, answerable_target)

    builder = _Builder(index, world, variants=True)
    local = 0
    cross = 0
    for query in selected:
        if query.query_id.startswith("xq_"):
            cross += 1
            query_id = f"xq_{cross:05d}"
        else:
            local += 1
            query_id = f"q_{local:05d}"
        result = builder.add(
            question=query.question,
            query_type=query.query_type,
            expected_answer=query.expected_answer,
            answer_type=query.answer_type,
            answerable=True,
            evidence=query.supporting_evidence,
            gold_fact_ids=query.gold_fact_ids,
            notes=query.notes,
            query_id=query_id,
        )
        if result is None:  # pragma: no cover - selected candidates are unique
            raise QueryGenerationError(
                f"selected answerable query {query.query_id} was rejected as duplicate")

    fact_index = FactIndex.build(world, [ctx.plan for ctx in index.documents])
    _add_unanswerable_v2(builder, fact_index, count=unanswerable_target)

    actual_pages = sum(ctx.plan.pages for ctx in index.documents)
    dataset = QADataset(
        version=2,
        seed=world.seed,
        target_pages=target_pages,
        actual_pages=actual_pages,
        documents=[ctx.document() for ctx in index.documents],
        queries=builder.queries,
        notes=("Phase 2 improved benchmark. Expected answers are derived "
               "programmatically from the structured world and document plans; "
               "unanswerable queries carry a fact-index proof."),
    )
    _validate_grounding(index, dataset)
    return dataset


def generate_qa_dataset(index: CorpusIndex, world: World, *, target_pages: int) -> QADataset:
    """Generate a deterministic QA benchmark for one ingested corpus."""
    builder = _Builder(index, world)
    for ctx in index.documents:
        _add_entity_lookup(builder, ctx)
        _add_relationship_lookup(builder, ctx)
        _add_ownership_lookups(builder, ctx)
        _add_extra_critical_relation(builder, ctx)
        _add_financial(builder, ctx)
        _add_temporal(builder, ctx)
        _add_historical(builder, ctx)
        _add_identity(builder, ctx)
        _add_negation(builder, ctx)
        _add_proposal(builder, ctx)
        _add_allegation(builder, ctx)
        _add_provenance(builder, ctx)
        _add_unanswerable(builder, ctx)
    _add_multi_hop(builder)
    _add_contradiction(builder)

    if not builder.queries:
        raise QueryGenerationError("no QA queries were generated")
    actual_pages = sum(ctx.plan.pages for ctx in index.documents)
    dataset = QADataset(
        version=1,
        seed=world.seed,
        target_pages=target_pages,
        actual_pages=actual_pages,
        documents=[ctx.document() for ctx in index.documents],
        queries=builder.queries,
        notes=("Expected answers are derived programmatically from the structured world "
               "and generated document plans; they are not model-invented."),
    )
    _validate_grounding(index, dataset)
    return dataset
