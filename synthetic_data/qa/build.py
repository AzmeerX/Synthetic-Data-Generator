"""Question generation over the Phase G artifacts (Phase H).

The generator is deliberately narrow: it reads the **v2 gold artifact** for
evidence (parser sentence ids, exact quotes, render locations, canonical ids), the
**canonical world** for semantics, and the **alignment** for provenance.  It never
invents an answer from the world alone — a question is emitted only when the
document that is being asked about actually states the fact, in parser text.

Caps come from :mod:`synthetic_data.qa.taxonomy`; a type whose evidence shape is
missing from a document is skipped there rather than faked.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from synthetic_data.alignment import normalise
from synthetic_data.gold.build import GoldPair
from synthetic_data.model import CdmDocument, CdmWorkbook, IsoDate, Money, Percentage
from synthetic_data.qa.model import AbsenceCheck, QAEvidence, QAQuestion, QASet, \
    TextSearch, UnanswerableProof
from synthetic_data.qa.taxonomy import (COPULAR_RELATIONS, cap_for, relation_base,
                                       relation_phrase)
from synthetic_data.surfaces import CURRENCY_NAMES

__all__ = ["QASource", "CorpusTexts", "generate", "generate_unanswerable"]

_STATUS_WORDS = {
    "proposed": ("proposed", "proposes", "proposal"),
    "rejected": ("rejected", "rejects", "refused"),
    "deferred": ("deferred", "defers", "adjourned"),
    "alleged": ("alleged", "alleges", "allegation"),
    "found": ("found", "finds", "finding", "held", "holds", "established"),
    "held": ("held", "holds", "found"),
    "approved": ("approved", "approves", "resolved"),
    "completed": ("completed", "settled", "paid"),
    "stated": ("stated", "records"),
}


@dataclass(frozen=True)
class CorpusTexts:
    """Every parser text in the corpus, for absence proofs and cross-document checks."""

    texts: dict[str, dict[str, str]]          # document id -> format -> text
    titles: dict[str, str]
    #: (document id, sentence) pairs, deduplicated across formats
    sentences: tuple[tuple[str, str], ...] = ()

    @classmethod
    def build(cls, entries: Iterable[tuple[str, str, str, str]]) -> "CorpusTexts":
        texts: dict[str, dict[str, str]] = {}
        titles: dict[str, str] = {}
        for document_id, fmt, text, title in entries:
            texts.setdefault(document_id, {})[fmt] = text
            titles[document_id] = title
        seen: set[str] = set()
        sentences: list[tuple[str, str]] = []
        for document_id, formats in texts.items():
            for text in formats.values():
                for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
                    sentence = sentence.strip()
                    if len(sentence) < 8 or sentence in seen:
                        continue
                    seen.add(sentence)
                    sentences.append((document_id, sentence))
        return cls(texts=texts, titles=titles, sentences=tuple(sentences))

    def co_occurrence(self, subject: str, obj: str | None,
                      relation_words: list[str]) -> AbsenceCheck:
        """How often two names share a sentence, and whether the relation is named."""
        shared = 0
        mentions = 0
        documents: list[str] = []
        for document_id, sentence in self.sentences:
            if subject not in sentence:
                continue
            if obj is not None and obj not in sentence:
                continue
            shared += 1
            documents.append(document_id)
            folded = sentence.lower()
            if any(word.lower() in folded for word in relation_words):
                mentions += 1
        return AbsenceCheck(subject=subject, object=obj,
                            relation_words=list(relation_words),
                            shared_sentences=shared, relation_mentions=mentions,
                            documents=sorted(set(documents)))

    def search(self, needle: str) -> TextSearch:
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        hits = 0
        documents: list[str] = []
        for document_id, formats in self.texts.items():
            found = any(pattern.search(text) for text in formats.values())
            if found:
                hits += 1
                documents.append(document_id)
        return TextSearch(needle=needle, hits=hits, documents=sorted(documents))

    def documents(self) -> list[str]:
        return sorted(self.texts)

    def text_of(self, document_id: str) -> str:
        return "\n".join(self.texts.get(document_id, {}).values())


@dataclass(frozen=True)
class QASource:
    document: CdmDocument
    canonical: Any | None
    render: Any
    view: Any
    pair: GoldPair
    world: Any
    corpus: str
    seed: int
    plan: Any | None = None

    @property
    def format(self) -> str:
        return self.render.format

    @property
    def is_workbook(self) -> bool:
        return isinstance(self.document, CdmWorkbook)

    @property
    def ordinal(self) -> str:
        return self.document.document_id.split(":")[1]


# ---------------------------------------------------------------------------
def _money(value: Money) -> str:
    return value.display()


def _percent(value: Percentage) -> str:
    return value.display()


def _name(world, entity_id: str | None) -> str:
    if entity_id is None:
        return ""
    entity = world.entities.get(entity_id)
    return entity.canonical_name if entity is not None else entity_id


def _evidence(pair: GoldPair, fact_id: str) -> list[QAEvidence]:
    """Evidence for one canonical fact, taken from the v2 artifact."""
    for item in [*pair.v2.edges, *pair.v2.propositions]:
        if item.id != fact_id and getattr(item, "canonical_fact_id", None) != fact_id:
            continue
        return [QAEvidence(
            sentence_id=evidence.sentence_id, quote=evidence.quote,
            canonical_node_id=evidence.canonical_node_id,
            canonical_sentence_ids=list(evidence.canonical_sentence_ids),
            render_format=evidence.render_format, render_loc=dict(evidence.render_loc),
            fact_ids=[fact_id]) for evidence in item.evidence]
    return []


def _quote_has_status(quote: str, status: str) -> bool:
    folded = normalise(quote).lower()
    return any(word in folded for word in _STATUS_WORDS.get(status, (status,)))


def _stated_facts(pair: GoldPair) -> list[str]:
    seen: list[str] = []
    for item in [*pair.v2.edges, *pair.v2.propositions]:
        if item.id not in seen:
            seen.append(item.id)
    return seen


def _add(questions: list[QAQuestion], question: QAQuestion, cap: int,
         counts: dict[str, int], rejected: dict[str, int] | None = None) -> None:
    def refuse(reason: str) -> None:
        if rejected is not None:
            rejected[reason] = rejected.get(reason, 0) + 1

    if counts.get(question.question_type, 0) >= cap:
        refuse("cap")
        return
    if any(existing.question_id == question.question_id for existing in questions):
        refuse("duplicate")
        return
    # two facts can yield the same wording (one document, one subject) or the same
    # words in a different order (a relationship asked in both directions)
    tokens = set(normalise(question.question).lower().split())
    for existing in questions:
        if existing.question == question.question:
            refuse("duplicate")
            return
        if set(normalise(existing.question).lower().split()) == tokens:
            refuse("duplicate")
            return
    questions.append(question)
    counts[question.question_type] = counts.get(question.question_type, 0) + 1


def _qid(source: QASource, question_type: str, key: str) -> str:
    """Stable across formats: the id hashes the facts, not the emission order."""
    digest = hashlib.sha256(
        f"{source.ordinal}|{question_type}|{key}".encode("utf-8")).hexdigest()[:10]
    return f"qa:{source.ordinal}/{question_type}/{digest}"


# ---------------------------------------------------------------------------
def generate(source: QASource, corpus: CorpusTexts) -> QASet:
    world = source.world
    facts = getattr(world, "facts", {}) or {}
    stated = _stated_facts(source.pair)
    questions: list[QAQuestion] = []
    counts: dict[str, int] = {}
    rejected: dict[str, int] = {}

    def emit(question_type: str, question: str, answer: str, kind: str,
             fact_ids: list[str], evidence: list[QAEvidence],
             answer_documents: list[str] | None = None) -> None:
        if not evidence:
            return
        _add(questions, QAQuestion(
            question_id=_qid(source, question_type, "+".join(fact_ids)),
            canonical_document_id=source.document.document_id,
            doc_type=source.document.doc_type, format=source.format,
            question_type=question_type, question=question, answerable=True,
            expected_answer=answer, answer_kind=kind, fact_ids=fact_ids,
            evidence=evidence,
            answer_documents=answer_documents or [source.document.document_id]),
            cap_for(question_type), counts, rejected)

    # ---- relationship / entity / ownership / financial / temporal ----------
    for fact_id in stated:
        fact = facts.get(fact_id)
        if fact is None or fact.kind != "edge":
            continue
        subject = _name(world, fact.subject.entity_id if fact.subject else None)
        obj = _name(world, fact.object.entity_id if fact.object else None)
        if not subject or not obj:
            continue
        phrase = relation_phrase(fact.relation or "")
        evidence = _evidence(source.pair, fact_id)
        if not evidence:
            continue

        emit("relationship_lookup",
             f"How is {subject} related to {obj} in this document?",
             phrase, "text", [fact_id], evidence)

        lookup = _object_question(subject, fact.relation or "")
        if fact.relation in {"owns", "is_subsidiary_of", "acquired"}:
            emit("ownership_lookup", lookup, obj, "entity", [fact_id], evidence)
        else:
            emit("entity_lookup", lookup, obj, "entity", [fact_id], evidence)

        if fact.amount is not None:
            emit("financial",
                 f"What amount is recorded for the {_relation_noun(fact.relation or '')} "
                 f"involving {subject} and {obj}?",
                 _money(fact.amount), "money", [fact_id], evidence)

        if fact.effective is not None and fact.effective.start is not None:
            surface = _date_surface(evidence, fact.effective.start)
            if surface is not None:
                emit("temporal",
                     f"On what date did {subject} {relation_base(fact.relation or '')} "
                     f"{obj}?",
                     fact.effective.start.value.isoformat(), "date", [fact_id],
                     evidence)

    # ---- status questions --------------------------------------------------
    for fact_id in stated:
        fact = facts.get(fact_id)
        if fact is None or fact.status not in _STATUS_WORDS:
            continue
        evidence = [item for item in _evidence(source.pair, fact_id)
                    if _quote_has_status(item.quote, fact.status)]
        if not evidence:
            continue
        subject = _name(world, fact.subject.entity_id if fact.subject else None) \
            or (fact.text or "")[:80]
        obj = _name(world, fact.object.entity_id if fact.object else None)
        if fact.status in {"proposed"}:
            emit("proposed_vs_completed",
                 f"Is the matter concerning {subject} completed, or still proposed?",
                 "proposed", "text", [fact_id], evidence)
        elif fact.status in {"rejected", "deferred"}:
            emit("negation",
                 f"Was the proposal concerning {subject} approved?",
                 f"No — the record marks it '{fact.status}'", "boolean", [fact_id],
                 evidence)
        elif fact.status in {"alleged"}:
            emit("alleged_vs_established",
                 f"Is the claim concerning {subject} alleged or established?",
                 "alleged", "text", [fact_id], evidence)
        elif fact.status in {"found", "held"}:
            emit("alleged_vs_established",
                 f"Is the claim concerning {subject} established?",
                 f"Yes — the record marks it '{fact.status}'", "text", [fact_id],
                 evidence)

    # ---- contradiction -----------------------------------------------------
    for contradiction in getattr(world, "contradictions", {}).values():
        for fact_id in (contradiction.fact_a, contradiction.fact_b):
            fact = facts.get(fact_id)
            if fact is None or fact.amount is None or fact_id not in stated:
                continue
            evidence = _evidence(source.pair, fact_id)
            if not evidence:
                continue
            subject = _name(world, fact.subject.entity_id if fact.subject else None) \
                or "the facility"
            emit("contradiction_resolution",
                 f"The documents disagree about an amount concerning {subject}. "
                 f"Which amount does this document state?",
                 _money(fact.amount), "money", [fact_id], evidence,
                 answer_documents=list(contradiction.documents))

    # ---- provenance --------------------------------------------------------
    for fact_id in stated:
        evidence = _evidence(source.pair, fact_id)
        if not evidence:
            continue
        location = _location(evidence[0], source)
        if location is None:
            continue
        fact = facts.get(fact_id)
        subject = _name(world, fact.subject.entity_id if fact and fact.subject else None)
        label = subject or (fact.text[:60] if fact and fact.text else fact_id)
        emit("provenance",
             f"Where does this document state the fact concerning {label}?",
             location, "location", [fact_id], evidence)

    # ---- multi-hop ---------------------------------------------------------
    for chain in _chains(stated, facts):
        first, second, middle, target = chain
        subject = _name(world, first.subject.entity_id if first.subject else None)
        if not subject or not middle or not target:
            continue
        target_name = _name(world, target)
        if not target_name:
            continue
        emit("multi_hop",
             f"Start from {subject}, follow '{relation_phrase(first.relation or '')}', "
             f"then follow '{relation_phrase(second.relation or '')}'. "
             f"Which entity do you reach?",
             target_name, "entity", [first.fact_id, second.fact_id],
             _evidence(source.pair, first.fact_id) or _evidence(source.pair, second.fact_id),
             answer_documents=sorted({source.document.document_id}))

    # ---- identity resolution ----------------------------------------------
    for question, answer, fact_id, evidence in _identity(source, facts, stated):
        emit("identity_resolution", question, answer, "entity", [fact_id], evidence)

    # ---- workbook-specific -------------------------------------------------
    if source.is_workbook:
        _workbook_questions(source, emit, stated, facts)

    # ---- historically ordered values (ledger sequence) --------------------
    if source.is_workbook:
        _historical(source, emit)

    return QASet(corpus=source.corpus, seed=source.seed,
                 document_id=source.document.document_id,
                 doc_type=source.document.doc_type, format=source.format,
                 questions=questions, rejected=rejected)


# ---------------------------------------------------------------------------
_RELATION_NOUNS = {
    "repaid": "repayment",
    "lent_to": "loan",
    "disbursed": "disbursement",
    "guarantees": "guarantee",
    "owes": "amount owed",
    "grants_security_over": "secured amount",
    "acquired": "acquisition price",
    "settled": "settlement",
    "ordered": "order amount",
}


def _relation_noun(relation: str) -> str:
    return _RELATION_NOUNS.get(relation, relation.replace("_", " "))


def _object_question(subject: str, relation: str) -> str:
    """Grammatical question for the object of a relation (no "does ... represents")."""
    if relation in COPULAR_RELATIONS:
        return f"Which entity is {subject} {relation_phrase(relation)}?"
    return f"Which entity does {subject} {relation_base(relation)}?"


def _date_surface(evidence: list[QAEvidence], value: IsoDate) -> str | None:
    """The date as the parser actually wrote it, or nothing if it did not."""
    for item in evidence:
        for candidate in (value.value.isoformat(), value.value.strftime("%d %B %Y"),
                          value.value.strftime("%d %b %Y")):
            if candidate in item.quote:
                return candidate
    return None


def _location(evidence: QAEvidence, source: QASource) -> str | None:
    loc = evidence.render_loc or {}
    if source.format == "xlsx":
        sheet = loc.get("sheet_name")
        ref = loc.get("cell_ref")
        if sheet and ref:
            return f"sheet {sheet!r}, cell {ref}"
        return None
    if loc.get("page") is not None:
        return f"page {loc['page']}"
    if loc.get("section_index") is not None:
        return f"section {loc['section_index'] + 1}"
    return None


def _chains(stated: list[str], facts) -> list[tuple]:
    """Two-step edge chains whose middle entity is a real entity."""
    edges = [facts[fact_id] for fact_id in stated
             if fact_id in facts and facts[fact_id].kind == "edge"
             and facts[fact_id].subject is not None
             and facts[fact_id].object is not None]
    by_subject: dict[str, list] = {}
    for edge in edges:
        by_subject.setdefault(edge.subject.entity_id, []).append(edge)
    chains = []
    for first in edges:
        for second in by_subject.get(first.object.entity_id, []):
            if second.object is None or second.fact_id == first.fact_id:
                continue
            if second.object.entity_id == first.subject.entity_id:
                continue
            chains.append((first, second, first.object.entity_id,
                           second.object.entity_id))
    return chains[:2]


def _identity(source: QASource, facts, stated: list[str]):
    """Two entities whose names collide on a distinctive token."""
    world = source.world
    names = {entity.entity_id: entity.canonical_name
             for entity in world.entities.values()}
    tokens: dict[str, list[str]] = {}
    for entity_id, name in names.items():
        for token in re.findall(r"[A-Za-z][A-Za-z'-]{4,}", name):
            tokens.setdefault(token.lower(), []).append(entity_id)
    for fact_id in stated:
        fact = facts.get(fact_id)
        if fact is None or fact.kind != "edge" or fact.object is None:
            continue
        answer_id = fact.object.entity_id
        for token in re.findall(r"[A-Za-z][A-Za-z'-]{4,}", names.get(answer_id, "")):
            rivals = [entity_id for entity_id in tokens.get(token.lower(), [])
                      if entity_id != answer_id]
            if not rivals:
                continue
            rival = rivals[0]
            evidence = _evidence(source.pair, fact_id)
            if not evidence:
                continue
            subject = _name(world, fact.subject.entity_id if fact.subject else None)
            question = (f"Which of {names[answer_id]} and {names[rival]} is the entity "
                        f"that {subject} {relation_phrase(fact.relation or '')}?")
            return [(question, names[answer_id], fact_id, evidence)]
    return []


def _workbook_questions(source: QASource, emit, stated: list[str], facts) -> None:
    document = source.document
    workbook = document.workbook
    for value in workbook.derived_values:
        evidence = _evidence(source.pair, value.fact_id)
        if not evidence:
            continue
        cell = _cell(document, value.cell_id)
        if cell is None or cell.value is None:
            continue
        emit("workbook_derived",
             f"What does the worksheet record for '{value.label}'?",
             _money(cell.value) if isinstance(cell.value, Money)
             else _percent(cell.value) if isinstance(cell.value, Percentage)
             else str(cell.value),
             "money" if isinstance(cell.value, Money) else "percent",
             [value.fact_id], evidence)

    for fact_id in stated:
        fact = facts.get(fact_id)
        if fact is None or fact.amount is None:
            continue
        evidence = _evidence(source.pair, fact_id)
        if not evidence:
            continue
        reference = _row_reference(document, evidence[0].render_loc)
        if not reference:
            continue
        emit("workbook_cell",
             f"What amount does the worksheet record against {reference}?",
             _money(fact.amount), "money", [fact_id], evidence)


def _cell(document: CdmWorkbook, cell_id: str):
    from synthetic_data.model import workbook_cells
    for cell in workbook_cells(document.workbook):
        if cell.cell_id == cell_id:
            return cell
    return None


_REFERENCE = re.compile(r"[A-Z]{0,3}-?[0-9]{2,}(?:/[a-z][0-9]{2})?")


def _row_reference(document: CdmWorkbook, loc: dict) -> str | None:
    """A reference-shaped text cell on the same sheet row: the row's natural label."""
    from synthetic_data.model import workbook_cells

    row = loc.get("row")
    sheet_name = loc.get("sheet_name")
    if row is None or not sheet_name:
        return None
    for cell in workbook_cells(document.workbook):
        if not cell.text:
            continue
        match = _REFERENCE.fullmatch(cell.text.strip())
        if match is None:
            continue
        cell_loc = _cell_loc(document, cell.cell_id)
        if cell_loc == (sheet_name, row):
            return cell.text.strip()
    return None


def _cell_loc(document: CdmWorkbook, cell_id: str) -> tuple[str, int] | None:
    for sheet in document.workbook.sheets:
        for table in [*(b.table for b in sheet.blocks if b.table is not None),
                      *sheet.tables]:
            for row_index, table_row in enumerate(
                    [*table.header_rows, *table.body_rows, *table.total_rows], start=1):
                for cell in table_row.cells:
                    if cell.cell_id == cell_id:
                        return sheet.name, row_index
    return None


def _historical(source: QASource, emit) -> None:
    """The ledger's own sequence: balance after each movement."""
    document = source.document
    workbook = document.workbook
    balances = []
    for value in workbook.derived_values:
        if "Running balance" not in value.label:
            continue
        cell = _cell(document, value.cell_id)
        evidence = _evidence(source.pair, value.fact_id)
        if cell is None or cell.value is None or not evidence:
            continue
        balances.append((value, cell, evidence))
    for index in range(1, min(len(balances), 3)):
        previous, current = balances[index - 1], balances[index]
        previous_cell = previous[1].value
        current_cell = current[1].value
        if not (isinstance(previous_cell, Money) and isinstance(current_cell, Money)):
            continue
        change = current_cell.amount_minor - previous_cell.amount_minor
        sign = "decrease" if change < 0 else "increase"
        emit("historical_change",
             f"Between {previous[0].label.replace('Running balance after ', '')} and "
             f"{current[0].label.replace('Running balance after ', '')}, did the "
             f"running balance increase or decrease, and by how much?",
             f"{sign} of {_money(Money(amount_minor=abs(change), currency=current_cell.currency))}",
             "text", [current[0].fact_id], current[2])


# ---------------------------------------------------------------------------
_ATTRIBUTE_CANDIDATES = ("date of incorporation", "registered number",
                         "articles of association", "registered address")
_DISTRIBUTION_CANDIDATES = ("dividend per share", "per share", "buyback",
                            "distribution to shareholders")
_ACQUISITION_WORDS = ("acquir", "purchas", "bought", "takeover")
_DIRECTOR_WORDS = ("director", "board seat", "appointed to the board")


def generate_unanswerable(source: QASource, corpus: CorpusTexts) -> QASet:
    """The five established insufficiency categories, each with a real search.

    Every candidate is *checked before it is used*: an attribute is only asked
    about when the corpus never mentions it, and a relationship only when the two
    entities are never named together with the relation.  A candidate that fails
    its own check is skipped rather than emitted as a false negative.
    """
    world = source.world
    documents = corpus.documents()
    names = [entity.canonical_name for entity in world.entities.values()]
    holdco = world.entities.get(_matter_value(source.plan, "holdco"))
    target = world.entities.get(_matter_value(source.plan, "target"))
    lender = world.entities.get(_matter_value(source.plan, "lender"))
    questions: list[QAQuestion] = []
    counts: dict[str, int] = {}
    rejected: dict[str, int] = {}

    def add(kind: str, question: str, proof: UnanswerableProof) -> None:
        _add(questions, QAQuestion(
            question_id=_qid(source, kind, f"{kind}|{proof.subject}|{proof.predicate}"),
            canonical_document_id=source.document.document_id,
            doc_type=source.document.doc_type, format=source.format,
            question_type="unanswerable", question=question, answerable=False,
            expected_answer=None, answer_kind=None, fact_ids=[],
            evidence=[], unanswerable_kind=kind, proof=proof),
            cap_for("unanswerable"), counts, rejected)

    subject = holdco.canonical_name if holdco else (names[0] if names else "the company")

    attribute = _absent_needle(corpus, _ATTRIBUTE_CANDIDATES)
    if attribute is not None:
        add("absent_attribute",
            f"What is the {attribute} of {subject}?",
            UnanswerableProof(
                kind="absent_attribute", subject=subject, predicate=attribute,
                documents_searched=documents,
                text_searches=[corpus.search(attribute)],
                notes=f"the phrase {attribute!r} occurs in no parser text, and the "
                      f"semantic layer carries no such attribute"))

    rival = _unrelated_pair(world, corpus)
    if rival is not None:
        person, company, check = rival
        add("absent_relationship",
            f"Is {person} a director of {company}?",
            UnanswerableProof(
                kind="absent_relationship", subject=person,
                predicate=f"is_director_of {company}",
                documents_searched=documents, absence_checks=[check],
                notes=f"the two names share {check.shared_sentences} sentence(s), none "
                      f"of which mentions a directorship; no such edge exists"))

    historical = _absent_needle(corpus, ("30 June 2019", "2019", "2018"))
    if historical is not None:
        add("unsupported_historical_date",
            f"What was the outstanding balance on {historical}?",
            UnanswerableProof(
                kind="unsupported_historical_date", subject="the facility",
                predicate=f"outstanding balance on {historical}",
                documents_searched=documents,
                text_searches=[corpus.search(historical)],
                notes="no parser text mentions the year, and no fact carries an "
                      "effective date before the facility started"))

    distribution = _absent_needle(corpus, _DISTRIBUTION_CANDIDATES)
    if distribution is not None:
        add("absent_financial_information",
            f"What {distribution} did {subject} declare?",
            UnanswerableProof(
                kind="absent_financial_information", subject=subject,
                predicate=distribution,
                documents_searched=documents,
                text_searches=[corpus.search(distribution)],
                notes="the corpus states facility amounts, not distributions to "
                      "shareholders; no such fact exists"))

    presupposed = _false_presupposition(world, corpus, target, lender)
    if presupposed is not None:
        question_text, proof = presupposed
        add("false_presupposition", question_text, proof)

    return QASet(corpus=source.corpus, seed=source.seed,
                 document_id=source.document.document_id,
                 doc_type=source.document.doc_type, format=source.format,
                 questions=questions, rejected=rejected)


# ---------------------------------------------------------------------------
def _matter_value(plan, key: str) -> str | None:
    matters = getattr(plan, "matters", None)
    if matters:
        value = matters[0].typed.get(key)
        return value if isinstance(value, str) else None
    return None


def _absent_needle(corpus: CorpusTexts, candidates: Iterable[str]) -> str | None:
    """The first candidate phrase that occurs in no parser text."""
    for candidate in candidates:
        if corpus.search(candidate).hits == 0:
            return candidate
    return None


def _unrelated_pair(world, corpus: CorpusTexts):
    """A person and a company with no directorship, preferring pairs named together."""
    people = [entity for entity in world.entities.values()
              if entity.entity_type == "person"]
    companies = [entity for entity in world.entities.values()
                 if entity.entity_type == "organization"]
    directory = {(fact.subject.entity_id, fact.object.entity_id)
                 for fact in world.facts.values()
                 if fact.kind == "edge" and fact.relation == "is_director_of"
                 and fact.subject and fact.object}
    fallback = None
    for person in people:
        for company in companies:
            if (person.entity_id, company.entity_id) in directory:
                continue
            check = corpus.co_occurrence(person.canonical_name,
                                         company.canonical_name, list(_DIRECTOR_WORDS))
            if check.relation_mentions:
                continue
            if check.shared_sentences:
                return person.canonical_name, company.canonical_name, check
            if fallback is None:
                fallback = (person.canonical_name, company.canonical_name, check)
    return fallback


def _false_presupposition(world, corpus: CorpusTexts, target, lender):
    """A question that presupposes an acquisition the documents never state."""
    if target is None or lender is None:
        return None
    check = corpus.co_occurrence(target.canonical_name, lender.canonical_name,
                                 list(_ACQUISITION_WORDS))
    if check.relation_mentions:
        return None
    edges = {(fact.subject.entity_id, fact.object.entity_id)
             for fact in world.facts.values()
             if fact.kind == "edge" and fact.relation == "acquired"
             and fact.subject and fact.object}
    if (target.entity_id, lender.entity_id) in edges:
        return None
    proof = UnanswerableProof(
        kind="false_presupposition", subject=target.canonical_name,
        predicate=f"acquired {lender.canonical_name}",
        documents_searched=corpus.documents(), absence_checks=[check],
        facts_considered=[fact.fact_id for fact in world.facts.values()
                          if fact.relation == "acquired"],
        notes="the presupposed acquisition never happened: no such edge exists, and "
              "the two names are never stated together in an acquisition sentence")
    return (f"Why did {target.canonical_name} acquire {lender.canonical_name}?", proof)
