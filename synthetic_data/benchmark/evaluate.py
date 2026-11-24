"""The benchmark evaluator (Phase I).

Scores a prediction bundle against a gold corpus.  Three layers, reported separately
so a strong reader cannot hide a weak extractor:

1. **extraction** — entities, directed relations, evidence and locators;
2. **values** — money, percentages, dates, derived reconciliation;
3. **QA** — answers, answerability, contradiction questions.

Two rules are enforced rather than documented:

* a gold document with no prediction is scored, with every item a false negative —
  omitting output never improves a score;
* every metric carries its own numerator and denominator, and a metric with an empty
  denominator is ``None``, never a flattering 1.0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from synthetic_data.benchmark import metrics as M
from synthetic_data.benchmark.gold import GoldDocument, GoldEvidence, GoldRelation
from synthetic_data.benchmark.schema import (PredictedDocument, PredictedEntity,
                                             PredictedEvidence, PredictedRelation,
                                             PredictionBundle)

__all__ = ["evaluate_document", "evaluate_corpus", "DocumentScore"]

_CONTRADICTION_TYPE = "contradiction_resolution"


@dataclass
class DocumentScore:
    document_id: str
    format: str
    doc_type: str
    predicted: bool
    entities: M.Tally = field(default_factory=M.Tally)
    relations: M.Tally = field(default_factory=M.Tally)
    evidence: M.Tally = field(default_factory=M.Tally)
    answers: M.Tally = field(default_factory=M.Tally)
    answerability: M.Tally = field(default_factory=M.Tally)
    contradictions: M.Tally = field(default_factory=M.Tally)
    values: M.Tally = field(default_factory=M.Tally)
    locators: M.Tally = field(default_factory=M.Tally)
    missing_questions: int = 0
    token_f1: float | None = None
    notes: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id, "format": self.format,
            "doc_type": self.doc_type, "predicted": self.predicted,
            "entities": self.entities.as_dict(),
            "relations": self.relations.as_dict(),
            "evidence": self.evidence.as_dict(),
            "answers": self.answers.as_dict(),
            "answerability": self.answerability.as_dict(),
            "contradictions": self.contradictions.as_dict(),
            "values": self.values.as_dict(),
            "locators": self.locators.as_dict(),
            "missing_questions": self.missing_questions,
            "token_f1": self.token_f1,
            "notes": dict(sorted(self.notes.items())),
        }


def _note(score: DocumentScore, key: str) -> None:
    score.notes[key] = score.notes.get(key, 0) + 1


# ---------------------------------------------------------------------------
def _entity_index(gold: GoldDocument) -> list[tuple[str, list[str]]]:
    """(entity id, normalised mention quotes) for every gold entity.

    Gold v2 has no display name, so identity is decided by the mention text: a
    predicted name matches an entity when its tokens occur in that entity's mentions
    and in no other entity's.  Two entities that share a name are therefore
    ambiguous by construction, which is what namesake detection needs.
    """
    return [(entity.entity_id, [M.normalise(quote) for quote in entity.quotes])
            for entity in gold.entities]


def _candidate_names(quote: str) -> set[str]:
    """Proper-noun runs in a quote: 'Dungate Industries GmbH'."""
    names: set[str] = set()
    run: list[str] = []
    for word in quote.split():
        stripped = word.strip("|,;:()[]{}\"'")
        if stripped[:1].isupper() and any(character.isalpha() for character in stripped):
            run.append(stripped)
        else:
            if len(run) >= 2:
                names.add(" ".join(run))
            run = []
    if len(run) >= 2:
        names.add(" ".join(run))
    return names


def _resolve(name: str, index: list[tuple[str, list[str]]],
             known_ids: set[str] | None = None) -> tuple[str | None, int]:
    """Gold entity id for a predicted name, and how many entities it could mean."""
    normalised = M.normalise(name)
    if not normalised:
        return None, 0
    if known_ids and normalised in {M.normalise(item) for item in known_ids}:
        for entity_id in known_ids:
            if M.normalise(entity_id) == normalised:
                return entity_id, 1
    exact = [entity_id for entity_id, quotes in index
             if any(normalised == _run for quote in quotes
                    for _run in _candidate_names(quote))]
    if exact:
        unique = sorted(set(exact))
        return unique[0], len(unique)
    # token containment: every token of the name occurs in the entity's mentions
    tokens = [token for token in normalised.split() if len(token) > 1]
    if not tokens:
        return None, 0
    contained = [entity_id for entity_id, quotes in index
                 if quotes and all(any(token in quote for quote in quotes)
                                   for token in tokens)]
    unique = sorted(set(contained))
    if unique:
        return unique[0], len(unique)
    return None, 0


def _match_entities(gold: GoldDocument, prediction: PredictedDocument | None,
                    score: DocumentScore) -> dict[str, str]:
    """Predicted entity id → gold entity id, for endpoints of relations."""
    index = _entity_index(gold)
    known_ids = {entity.entity_id for entity in gold.entities}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    if prediction is None:
        for _ in gold.entities:
            score.entities.miss()
        return mapping
    for entity in prediction.entities:
        gold_id, candidates = _resolve(entity.name, index, known_ids)
        if gold_id is None:
            for alias in entity.aliases:
                gold_id, candidates = _resolve(alias, index, known_ids)
                if gold_id is not None:
                    _note(score, "alias_match")
                    break
        if gold_id is None:
            score.entities.false_positive()
            continue
        if candidates > 1:
            _note(score, "namesake_ambiguous")
        if gold_id in used:
            score.entities.false_positive()
            _note(score, "duplicate_entity")
            continue
        used.add(gold_id)
        mapping[entity.entity_id] = gold_id
        score.entities.hit()
    score.entities.fn += len(gold.entities) - len(used)
    return mapping


def _gold_relation_key(relation: GoldRelation) -> tuple[str, str, str]:
    return (relation.subject, relation.relation, relation.object)


def _match_relations(gold: GoldDocument, prediction: PredictedDocument | None,
                     entity_map: dict[str, str], score: DocumentScore) -> dict[str, Any]:
    gold_by_key: dict[tuple[str, str, str], list[GoldRelation]] = {}
    for relation in gold.relations:
        gold_by_key.setdefault(_gold_relation_key(relation), []).append(relation)
    matched: dict[tuple[str, str, str], int] = {}
    evidence_pairs: list[tuple[GoldRelation, PredictedRelation]] = []
    if prediction is None:
        score.relations.fn += len(gold.relations)
        return {"matched": matched, "evidence_pairs": evidence_pairs}

    for relation in prediction.relations:
        subject = entity_map.get(relation.subject) or _lookup(relation.subject, entity_map)
        obj = entity_map.get(relation.object) or _lookup(relation.object, entity_map)
        if subject is None or obj is None:
            score.relations.false_positive()
            _note(score, "unresolved_endpoint")
            continue
        key = (subject, relation.relation, obj)
        candidates = gold_by_key.get(key, [])
        used_here = matched.get(key, 0)
        if used_here >= len(candidates):
            reversed_key = (obj, relation.relation, subject)
            score.relations.false_positive()
            if reversed_key in gold_by_key and key not in gold_by_key:
                _note(score, "reversed_direction")
            elif key in gold_by_key:
                _note(score, "duplicate_relation")
            continue
        gold_relation = candidates[used_here]
        agrees = True
        if relation.negated != gold_relation.negated:
            _note(score, "negation_mismatch")
            agrees = False
        if (relation.status or "") != (gold_relation.status or ""):
            _note(score, "status_mismatch")
            agrees = False
        if relation.effective and gold_relation.effective \
                and relation.effective != gold_relation.effective:
            _note(score, "temporal_mismatch")
            agrees = False
        matched[key] = used_here + 1
        if agrees:
            score.relations.hit()
        else:
            score.relations.false_positive()
            score.relations.miss()
        evidence_pairs.append((gold_relation, relation))
    score.relations.fn += len(gold.relations) - sum(matched.values())
    return {"matched": matched, "evidence_pairs": evidence_pairs}


def _lookup(endpoint: str, entity_map: dict[str, str]) -> str | None:
    for predicted_id, gold_id in entity_map.items():
        if endpoint == predicted_id:
            return gold_id
    return None


def _match_evidence(gold_items: Iterable[GoldEvidence],
                    predicted_items: Iterable[PredictedEvidence],
                    score: DocumentScore) -> None:
    gold_list = list(gold_items)
    predicted_list = list(predicted_items)
    if not gold_list and not predicted_list:
        return
    gold_quotes = [M.normalise(item.quote) for item in gold_list]
    used: set[int] = set()
    for predicted in predicted_list:
        quote = M.normalise(predicted.quote)
        hit = next((index for index, candidate in enumerate(gold_quotes)
                    if candidate == quote and index not in used), None)
        if hit is None:
            partial = next((index for index, candidate in enumerate(gold_quotes)
                            if quote and (quote in candidate or candidate in quote)
                            and index not in used), None)
            if partial is not None:
                # a paraphrase is not the parser's quote: it is a miss, recorded
                _note(score, "quote_partial")
            score.evidence.false_positive()
            continue
        used.add(hit)
        gold_item = gold_list[hit]
        score.evidence.hit()
        _check_locator(gold_item, predicted, score)
    score.evidence.fn += len(gold_list) - len(used)


def _check_locator(gold: GoldEvidence, predicted: PredictedEvidence,
                   score: DocumentScore) -> None:
    """Locators are scored only where the gold artifact has one to compare."""
    if predicted.sentence_id and gold.sentence_id:
        if predicted.sentence_id == gold.sentence_id:
            score.locators.hit()
        else:
            score.locators.false_positive()
            _note(score, "sentence_id_mismatch")
    if predicted.page is not None and gold.page is not None:
        if predicted.page == gold.page:
            score.locators.hit()
        else:
            score.locators.false_positive()
            _note(score, "page_mismatch")
    if predicted.cell and gold.cell:
        if predicted.cell == gold.cell:
            score.locators.hit()
        else:
            score.locators.false_positive()
            _note(score, "cell_mismatch")
    if predicted.section is not None and gold.section is not None:
        if predicted.section == gold.section:
            score.locators.hit()
        else:
            score.locators.false_positive()
            _note(score, "section_mismatch")


# ---------------------------------------------------------------------------
def _answer_matches(gold_answer, predicted_answer) -> tuple[bool, bool]:
    """``(normalized_match, exact_match)`` with kind-aware typed comparison."""
    predicted = predicted_answer.answer or ""
    gold = gold_answer.answer or ""
    exact = M.exact_match(predicted, gold)
    kind = gold_answer.answer_kind
    if kind == "money":
        left, right = M.parse_money(predicted), M.parse_money(gold)
        if left and right:
            equal, reason = M.money_equal(left[0], left[1], right[0], right[1])
            if not equal and reason == "currency":
                return False, False
            return equal, exact
    if kind == "percent":
        return M.percent_equal(predicted, gold), exact
    if kind == "date":
        return M.date_equal(predicted, gold), exact
    return M.normalized_match(predicted, gold), exact


def _gold_amount(relation: GoldRelation) -> tuple[int, str] | None:
    """A typed amount from the gold evidence.

    A prose quote carries its own currency (``GBP 12,000,000.00``); a workbook cell
    quote is a bare number and the currency comes from the evidence record, which is
    why the record's currency is used as a fallback rather than guessed.
    """
    for item in relation.evidence:
        found = M.find_money(item.quote)
        if found is not None:
            return found
        parsed = M.parse_money(item.quote)
        if parsed is None:
            continue
        minor, currency = parsed
        currency = currency or item.currency
        if currency:
            return minor, currency
    return None


def _score_values(gold: GoldDocument, prediction: PredictedDocument | None,
                  pairs: list[tuple[GoldRelation, PredictedRelation]],
                  score: DocumentScore) -> None:
    """Money and date accuracy on relations that were matched by identity."""
    if prediction is None:
        return
    for gold_relation, predicted in pairs:
        gold_amount = _gold_amount(gold_relation)
        if gold_amount is not None and predicted.amount_minor is not None:
            equal, reason = M.money_equal(predicted.amount_minor,
                                          predicted.currency, *gold_amount)
            if equal:
                score.values.hit()
            else:
                score.values.false_positive()
                _note(score, f"{reason}_mismatch")
        elif gold_amount is not None:
            score.values.miss()
            _note(score, "amount_missing")
        if gold_relation.effective and predicted.effective:
            if M.date_equal(predicted.effective, gold_relation.effective):
                score.values.hit()
            else:
                score.values.false_positive()
                _note(score, "date_mismatch")


def _score_answers(gold: GoldDocument, prediction: PredictedDocument | None,
                   score: DocumentScore) -> None:
    if not gold.answers:
        return
    predicted_by_id = {answer.question_id: answer
                       for answer in (prediction.answers if prediction else [])}
    f1_values: list[float] = []
    for gold_answer in gold.answers:
        predicted = predicted_by_id.get(gold_answer.question_id)
        if predicted is None:
            score.missing_questions += 1
            if gold_answer.answerable:
                score.answers.miss()
            else:
                score.answerability.miss()
            continue
        # answerability is scored for every question, answerable or not
        if predicted.answerable == gold_answer.answerable:
            score.answerability.hit()
        elif predicted.answerable and not gold_answer.answerable:
            score.answerability.false_positive()
            _note(score, "answered_unanswerable")
        else:
            score.answerability.miss()
            _note(score, "refused_answerable")

        if gold_answer.answerable:
            matched, exact = _answer_matches(gold_answer, predicted)
            if matched:
                score.answers.hit()
            else:
                score.answers.false_positive()
                score.answers.miss()
            f1_values.append(M.token_f1(predicted.answer or "",
                                        gold_answer.answer or ""))
            if exact:
                _note(score, "exact_match")
            _match_evidence(gold_answer.evidence, predicted.evidence, score)
        else:
            if not predicted.answerable:
                score.answers.hit()
                if gold_answer.question_type == _CONTRADICTION_TYPE:
                    score.contradictions.hit()
            else:
                score.answers.miss()

        if gold_answer.question_type == _CONTRADICTION_TYPE and gold_answer.answerable:
            if predicted.answerable and _answer_matches(gold_answer, predicted)[0]:
                score.contradictions.hit()
            else:
                score.contradictions.miss()

    if f1_values:
        score.token_f1 = sum(f1_values) / len(f1_values)


# ---------------------------------------------------------------------------
def evaluate_document(gold: GoldDocument, prediction: PredictedDocument | None,
                      ) -> DocumentScore:
    score = DocumentScore(document_id=gold.document_id, format=gold.format,
                          doc_type=gold.doc_type, predicted=prediction is not None)
    entity_map = _match_entities(gold, prediction, score)
    relations = _match_relations(gold, prediction, entity_map, score)
    # extraction evidence is scored on relations; answer evidence is scored with the
    # answers, and propositions have no prediction surface of their own
    gold_evidence = [item for relation in gold.relations for item in relation.evidence]
    predicted_evidence = []
    if prediction is not None:
        for entity in prediction.entities:
            predicted_evidence.extend(entity.mentions)
        for relation in prediction.relations:
            predicted_evidence.extend(relation.evidence)
    _match_evidence(gold_evidence, predicted_evidence, score)
    _score_values(gold, prediction, relations["evidence_pairs"], score)
    _score_answers(gold, prediction, score)
    return score


def _merge(scores: Iterable[DocumentScore]) -> dict[str, Any]:
    entity, relation, evidence = M.Tally(), M.Tally(), M.Tally()
    answers, answerability, contradictions = M.Tally(), M.Tally(), M.Tally()
    values, locators = M.Tally(), M.Tally()
    f1_values: list[float] = []
    missing = 0
    documents = 0
    for score in scores:
        documents += 1
        entity.merge(score.entities)
        relation.merge(score.relations)
        evidence.merge(score.evidence)
        answers.merge(score.answers)
        answerability.merge(score.answerability)
        contradictions.merge(score.contradictions)
        values.merge(score.values)
        locators.merge(score.locators)
        missing += score.missing_questions
        if score.token_f1 is not None:
            f1_values.append(score.token_f1)
    notes: dict[str, int] = {}
    for score in scores:
        for key, value in score.notes.items():
            notes[key] = notes.get(key, 0) + value
    return {"documents": documents,
            "notes": dict(sorted(notes.items())),
            "entities": entity.as_dict(),
            "relations": relation.as_dict(),
            "evidence": evidence.as_dict(),
            "answers": answers.as_dict(),
            "answerability": answerability.as_dict(),
            "contradictions": contradictions.as_dict(),
            "values": values.as_dict(),
            "locators": locators.as_dict(),
            "missing_questions": missing,
            "token_f1": M.mean(f1_values)}


def evaluate_corpus(gold: dict[tuple[str, str], GoldDocument], bundle: PredictionBundle,
                    *, label: str = "corpus") -> dict[str, Any]:
    """Score every gold document, then aggregate by format, type and document."""
    predicted = bundle.keyed()
    scores = [evaluate_document(document, predicted.get(key))
              for key, document in sorted(gold.items())]
    by_format: dict[str, list[DocumentScore]] = {}
    by_type: dict[str, list[DocumentScore]] = {}
    for score in scores:
        by_format.setdefault(score.format, []).append(score)
        by_type.setdefault(score.doc_type, []).append(score)

    extra_predictions = sorted(set(predicted) - set(gold))
    return {
        "label": label,
        "adapter": bundle.adapter,
        "schema_version": bundle.schema_version,
        "documents": [score.as_dict() for score in scores],
        "by_format": {name: _merge(items) for name, items in sorted(by_format.items())},
        "by_doc_type": {name: _merge(items) for name, items in sorted(by_type.items())},
        "corpus": _merge(scores),
        "unmatched_predictions": [f"{document_id}/{fmt}"
                                  for document_id, fmt in extra_predictions],
    }


def aggregate_by_question(gold: dict[tuple[str, str], GoldDocument],
                          bundle: PredictionBundle) -> dict[str, Any]:
    """Answer metrics split by question type and by answerability, per question."""
    predicted = bundle.keyed()
    by_type: dict[str, M.Tally] = {}
    f1_by_type: dict[str, list[float]] = {}
    answerable = M.Tally()
    unanswerable = M.Tally()
    for key, document in sorted(gold.items()):
        prediction = predicted.get(key)
        predicted_by_id = {answer.question_id: answer
                           for answer in (prediction.answers if prediction else [])}
        for gold_answer in document.answers:
            tally = by_type.setdefault(gold_answer.question_type, M.Tally())
            predicted_answer = predicted_by_id.get(gold_answer.question_id)
            bucket = answerable if gold_answer.answerable else unanswerable
            if predicted_answer is None:
                tally.miss()
                bucket.miss()
                continue
            if gold_answer.answerable:
                matched, _ = _answer_matches(gold_answer, predicted_answer)
                if matched:
                    tally.hit()
                    bucket.hit()
                else:
                    tally.false_positive()
                    tally.miss()
                    bucket.false_positive()
                    bucket.miss()
                f1_by_type.setdefault(gold_answer.question_type, []).append(
                    M.token_f1(predicted_answer.answer or "", gold_answer.answer or ""))
            else:
                if not predicted_answer.answerable:
                    tally.hit()
                    bucket.hit()
                else:
                    tally.miss()
                    bucket.false_positive()
    return {
        "by_question_type": {name: {**tally.as_dict(),
                                    "token_f1": M.mean(f1_by_type.get(name, []))}
                             for name, tally in sorted(by_type.items())},
        "answerable": answerable.as_dict(),
        "unanswerable": unanswerable.as_dict(),
    }
