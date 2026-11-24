"""Prediction fixtures built from a gold corpus (Phase I).

These exist to test the *evaluator*, not the extractor.  A perfect bundle is
constructed from gold and must score 1.0; corrupted bundles each break one thing and
must move exactly the metric that covers it.  The bundle is produced through the
public prediction schema, so the evaluator sees exactly what a real system would
submit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from synthetic_data.benchmark.gold import GoldDocument
from synthetic_data.benchmark.schema import (PREDICTION_SCHEMA_VERSION,
                                             PredictedAnswer, PredictedDocument,
                                             PredictedEntity, PredictedEvidence,
                                             PredictedRelation, PredictionBundle)

__all__ = ["perfect_bundle", "empty_bundle", "corrupt", "CORRUPTIONS", "entity_name"]


def entity_name(document: GoldDocument, entity_id: str) -> str:
    """The most specific proper-noun run in an entity's mentions."""
    entity = next((item for item in document.entities if item.entity_id == entity_id),
                  None)
    if entity is None:
        return entity_id
    best = ""
    for quote in entity.quotes:
        words = quote.replace("|", " ").split()
        run: list[str] = []
        for word in words:
            stripped = word.strip(".,;:()[]{}\"'")
            if stripped[:1].isupper() and any(ch.isalpha() for ch in stripped):
                run.append(stripped)
            else:
                if len(run) > len(best.split()):
                    best = " ".join(run)
                run = []
        if len(run) > len(best.split()):
            best = " ".join(run)
    return best or entity_id


def _evidence(items) -> list[PredictedEvidence]:
    return [PredictedEvidence(quote=item.quote, sentence_id=item.sentence_id,
                              page=item.page, section=item.section,
                              sheet=item.sheet, cell=item.cell)
            for item in items]


def perfect_document(gold: GoldDocument) -> PredictedDocument:
    entities = [PredictedEntity(entity_id=entity.entity_id,
                                name=entity_name(gold, entity.entity_id),
                                mentions=_evidence([])) for entity in gold.entities]
    relations = []
    for relation in gold.relations:
        amount, currency = _money_from(relation.evidence)
        relations.append(PredictedRelation(
            subject=relation.subject, object=relation.object,
            relation=relation.relation, negated=relation.negated,
            status=relation.status, amount_minor=amount, currency=currency,
            effective=relation.effective, evidence=_evidence(relation.evidence)))
    answers = [PredictedAnswer(question_id=answer.question_id, answer=answer.answer,
                               answerable=answer.answerable,
                               evidence=_evidence(answer.evidence))
               for answer in gold.answers]
    return PredictedDocument(document_id=gold.document_id, format=gold.format,
                             doc_type=gold.doc_type, entities=entities,
                             relations=relations, answers=answers)


def _money_from(evidence) -> tuple[int | None, str | None]:
    """A typed amount from gold evidence: quote currency, else the record's."""
    from synthetic_data.benchmark.metrics import parse_money
    from synthetic_data.benchmark.metrics import find_money
    for item in evidence:
        if not item.quote.strip():
            continue
        found = find_money(item.quote)
        if found is not None:
            return found
        parsed = parse_money(item.quote)
        if parsed is None:
            continue
        minor, currency = parsed
        currency = currency or item.currency
        if currency is None:
            continue
        return minor, currency
    return None, None


def perfect_bundle(gold: dict[tuple[str, str], GoldDocument], *,
                   corpus: str = "v3_pilot") -> PredictionBundle:
    return PredictionBundle(
        schema_version=PREDICTION_SCHEMA_VERSION, corpus=corpus,
        adapter="fixture:perfect",
        documents=[perfect_document(document) for _, document in sorted(gold.items())])


def empty_bundle(corpus: str = "v3_pilot") -> PredictionBundle:
    return PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION, corpus=corpus,
                            adapter="fixture:empty", documents=[])


@dataclass(frozen=True)
class Corruption:
    name: str
    description: str
    apply: Callable[[PredictedDocument, GoldDocument], bool]


def _first_relation(document: PredictedDocument):
    return next((relation for relation in document.relations if relation.amount_minor),
                document.relations[0] if document.relations else None)


def _swap_direction(document: PredictedDocument, gold: GoldDocument) -> bool:
    if not document.relations:
        return False
    relation = document.relations[0]
    relation.subject, relation.object = relation.object, relation.subject
    return True


def _wrong_amount(document: PredictedDocument, gold: GoldDocument) -> bool:
    relation = _first_relation(document)
    if relation is None or not relation.amount_minor:
        return False
    relation.amount_minor += 100
    return True


def _wrong_currency(document: PredictedDocument, gold: GoldDocument) -> bool:
    relation = _first_relation(document)
    if relation is None or not relation.currency:
        return False
    relation.currency = "USD" if relation.currency != "USD" else "EUR"
    return True


def _wrong_status(document: PredictedDocument, gold: GoldDocument) -> bool:
    if not document.relations:
        return False
    relation = document.relations[0]
    relation.status = "proposed" if relation.status != "proposed" else "completed"
    return True


def _wrong_negation(document: PredictedDocument, gold: GoldDocument) -> bool:
    if not document.relations:
        return False
    document.relations[0].negated = not document.relations[0].negated
    return True


def _all_evidence(document: PredictedDocument):
    for relation in document.relations:
        yield from relation.evidence
    for answer in document.answers:
        yield from answer.evidence
    for entity in document.entities:
        yield from entity.mentions


def _wrong_page(document: PredictedDocument, gold: GoldDocument) -> bool:
    for evidence in _all_evidence(document):
        if evidence.page is not None:
            evidence.page = evidence.page + 1
            return True
    return False


def _wrong_cell(document: PredictedDocument, gold: GoldDocument) -> bool:
    for evidence in _all_evidence(document):
        if evidence.cell:
            evidence.cell = evidence.cell + "9"
            return True
    return False


def _wrong_section(document: PredictedDocument, gold: GoldDocument) -> bool:
    for evidence in _all_evidence(document):
        if evidence.section is not None and evidence.page is None:
            evidence.section = evidence.section + 1
            return True
    return False


def _wrong_quote(document: PredictedDocument, gold: GoldDocument) -> bool:
    for relation in document.relations:
        for evidence in relation.evidence:
            evidence.quote = evidence.quote + " (paraphrased)"
            return True
    return False


def _wrong_identity(document: PredictedDocument, gold: GoldDocument) -> bool:
    """Give one entity the name of a different entity in the same document."""
    if len(document.entities) < 2:
        return False
    document.entities[0].name = document.entities[1].name
    return True


def _wrong_date(document: PredictedDocument, gold: GoldDocument) -> bool:
    for relation in document.relations:
        if relation.effective:
            year, month, day = relation.effective.split("-")
            relation.effective = f"{int(year) - 1}-{month}-{day}"
            return True
    return False


def _drop_entities(document: PredictedDocument, gold: GoldDocument) -> bool:
    if not document.entities:
        return False
    document.entities = document.entities[:-1]
    return True


def _wrong_answer(document: PredictedDocument, gold: GoldDocument) -> bool:
    for answer in document.answers:
        if answer.answerable:
            answer.answer = "not stated in the document"
            return True
    return False


def _answer_unanswerable(document: PredictedDocument, gold: GoldDocument) -> bool:
    for answer in document.answers:
        if not answer.answerable:
            answer.answerable = True
            answer.answer = "GBP 1,000,000.00"
            return True
    return False


def _drop_answers(document: PredictedDocument, gold: GoldDocument) -> bool:
    if not document.answers:
        return False
    document.answers = document.answers[1:]
    return True


CORRUPTIONS: tuple[Corruption, ...] = (
    Corruption("wrong_identity", "give one entity another entity's name",
               _wrong_identity),
    Corruption("reversed_direction", "swap subject and object of one relation",
               _swap_direction),
    Corruption("wrong_date", "move one relation's anchor date back a year",
               _wrong_date),
    Corruption("wrong_amount", "shift one amount by one pound", _wrong_amount),
    Corruption("wrong_currency", "change one currency code", _wrong_currency),
    Corruption("wrong_status", "flip one proposition status", _wrong_status),
    Corruption("wrong_negation", "flip one negation flag", _wrong_negation),
    Corruption("wrong_page", "shift one PDF page by one", _wrong_page),
    Corruption("wrong_cell", "shift one XLSX cell reference", _wrong_cell),
    Corruption("wrong_section", "shift one DOCX section by one", _wrong_section),
    Corruption("wrong_quote", "paraphrase one evidence quote", _wrong_quote),
    Corruption("dropped_entity", "omit one extracted entity", _drop_entities),
    Corruption("wrong_answer", "replace one answer with a refusal", _wrong_answer),
    Corruption("answered_unanswerable", "answer an unanswerable question",
               _answer_unanswerable),
    Corruption("dropped_answer", "omit one answer", _drop_answers),
)


def corrupt(gold: dict[tuple[str, str], GoldDocument], corruption: Corruption,
            *, corpus: str = "v3_pilot") -> PredictionBundle:
    """One deliberate error, applied to the first document where it is meaningful.

    Corrupting every document would make the metric move for the wrong reason (or
    hide a metric that never fires); one precise mutation per fixture keeps the
    expected change unambiguous.
    """
    bundle = perfect_bundle(gold, corpus=corpus)
    for document in bundle.documents:
        gold_document = gold[(document.document_id, document.format)]
        if corruption.apply(document, gold_document):
            bundle.adapter = f"fixture:corrupt:{corruption.name}"
            return bundle
    raise RuntimeError(f"corruption {corruption.name!r} did not apply to any document")
