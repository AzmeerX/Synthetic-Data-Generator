"""Independent validation of a QA benchmark (Phase H).

This module deliberately does **not** import the generator's helpers.  It reads the
QA file, the gold v2 artifact and the canonical world from disk, re-ingests the
rendered document through the real parser, and recomputes what the answer should be
from the semantic layer.  Where the generator said "the answer is GBP 12,000,000.00"
the validator asks the world what the fact's amount is; where the generator said
"page 3" it asks the gold artifact what render location that evidence carries.

What it can prove and what it cannot is stated per check, because a validator that
claims more than it checks is worse than no validator:

* answer correctness — recomputed from the world, not from the generator;
* quote accuracy — the quote must occur in a fresh parse of the same file;
* sentence identity — the cited parser sentence must be one the gold artifact uses
  for that fact;
* arithmetic — derived answers are recomputed with the formula evaluator;
* unanswerable proofs — every cited search is re-run over every parser text;
* cross-format consistency — the same question id must carry the same answer.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from synthetic_data.alignment import normalise
from synthetic_data.model import CdmWorkbook, Money, Percentage
from synthetic_data.qa.model import QASet, QAQuestion
from synthetic_data.qa.taxonomy import QUESTION_TYPES, UNANSWERABLE_KINDS

__all__ = ["ValidationInput", "ValidationReport", "validate_set", "validate_corpus",
           "coverage_report"]

_RELATION_WORDS = {
    "owns": {"owns", "own"},
    "is_subsidiary_of": {"subsidiary", "subsidiaries"},
    "is_director_of": {"director", "directors"},
    "chairs": {"chairs", "chair", "chairman", "chairperson"},
    "advises": {"advises", "advised", "advisor", "adviser"},
    "represents": {"represents", "represented", "representative"},
    "lent_to": {"lent", "lends", "lend"},
    "disbursed": {"disbursed", "disburse"},
    "repaid": {"repaid", "repay", "repayment"},
    "owes": {"owes", "owed"},
    "guarantees": {"guarantees", "guarantee", "guarantor"},
    "grants_security_over": {"security", "charge", "mortgage"},
    "acquired": {"acquired", "acquisition", "acquires"},
    "attends": {"attends", "attended", "attendance"},
    "served": {"served", "serves"},
    "filed": {"filed", "files"},
    "settled": {"settled", "settles", "settlement"},
    "ordered": {"ordered", "orders", "order"},
}


@dataclass
class ValidationInput:
    qa: QASet
    gold_v2: dict[str, Any]
    alignment: dict[str, Any] | None
    world: Any
    plan: Any | None
    fresh_text: str
    corpus: Any
    workbook: CdmWorkbook | None = None
    render_map: Any | None = None
    #: document id -> fact ids that document states (rebuilt from every gold file)
    stated_by_document: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ValidationReport:
    checked: int = 0
    errors: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, message: str) -> None:
        self.errors.append(message)

    def summary(self) -> str:
        return (f"{self.checked} questions checked, {len(self.errors)} errors"
                + (f": {self.errors[:3]}" if self.errors else ""))


# ---------------------------------------------------------------------------
def _facts(world) -> dict[str, Any]:
    return getattr(world, "facts", {}) or {}


def _v2_items(gold_v2: dict) -> dict[str, dict]:
    items = {}
    for section in ("edges", "propositions"):
        for item in gold_v2.get(section, []):
            items[item["id"]] = item
    return items


def _sentence_ids(item: dict) -> set[str]:
    return {evidence["sentence_id"] for evidence in item.get("evidence", [])}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", normalise(text)).strip().lower()


def _money_equal(answer: str, value: Money) -> bool:
    digits = re.sub(r"[^0-9.]", "", answer)
    if not digits:
        return False
    try:
        return abs(float(digits) - value.amount_minor / 100) < 0.005
    except ValueError:
        return False


def _percent_equal(answer: str, value: Percentage) -> bool:
    digits = re.sub(r"[^0-9.]", "", answer)
    if not digits:
        return False
    try:
        return abs(float(digits) - value.basis_points / 100) < 0.005
    except ValueError:
        return False


def _relation_matches(answer: str, relation: str) -> bool:
    words = set(re.findall(r"[a-z]+", _normalise(answer)))
    return bool(words & _RELATION_WORDS.get(relation, {relation.replace("_", " ")}))


# ---------------------------------------------------------------------------
def validate_set(source: ValidationInput) -> ValidationReport:
    report = ValidationReport()
    qa = source.qa
    facts = _facts(source.world)
    items = _v2_items(source.gold_v2)
    if qa.document_id in source.stated_by_document:
        stated_here = set(source.stated_by_document[qa.document_id])
    else:
        stated_here = set(items)
    text = source.fresh_text

    for question in qa.questions:
        report.checked += 1
        _check_common(question, text, items, source, report)
        if question.answerable:
            _check_answer(question, facts, items, source, stated_here, report)
        else:
            _check_proof(question, source, report)
    _check_duplicates(qa, report)
    report.notes["counts"] = dict(qa.counts)
    report.notes["answerable"] = len(qa.answerable)
    report.notes["unanswerable"] = len(qa.unanswerable)
    return report


def _check_common(question: QAQuestion, text: str, items: dict[str, dict],
                  source: ValidationInput, report: ValidationReport) -> None:
    for evidence in question.evidence:
        if evidence.quote not in text:
            report.add(f"{question.question_id}: quote not found in a fresh parse: "
                       f"{evidence.quote[:70]!r}")
        item = items.get(question.fact_ids[0]) if question.fact_ids else None
        if item is not None:
            if evidence.sentence_id not in _sentence_ids(item):
                report.add(f"{question.question_id}: sentence {evidence.sentence_id} is "
                           f"not the sentence the gold artifact cites for {item['id']}")
        elif question.answerable:
            report.add(f"{question.question_id}: no gold item for "
                       f"{question.fact_ids[0] if question.fact_ids else '<none>'}")
        if evidence.render_format != question.format:
            report.add(f"{question.question_id}: evidence format {evidence.render_format} "
                       f"is not the question format {question.format}")


def _check_answer(question: QAQuestion, facts: dict[str, Any], items: dict[str, dict],
                  source: ValidationInput, stated_here: set[str],
                  report: ValidationReport) -> None:
    answer = question.expected_answer or ""
    # derived values are facts of the workbook, not of the semantic world
    derived = {item["id"] for item in source.gold_v2.get("propositions", [])
               if item.get("derived_from")}
    for fact_id in question.fact_ids:
        if fact_id not in stated_here:
            report.add(f"{question.question_id}: fact {fact_id} is not stated in "
                       f"{question.canonical_document_id}")
        if fact_id not in facts and fact_id not in derived:
            report.add(f"{question.question_id}: fact {fact_id} is not in the world")

    primary = facts.get(question.fact_ids[0]) if question.fact_ids else None
    kind = question.question_type

    if kind in {"relationship_lookup"} and primary is not None:
        if not _relation_matches(answer, primary.relation or ""):
            report.add(f"{question.question_id}: answer {answer!r} does not state the "
                       f"world relation {primary.relation!r}")

    elif kind in {"entity_lookup", "ownership_lookup", "multi_hop"} and primary is not None:
        expected = _expected_object(question, primary, facts, source.world)
        if expected and _normalise(answer) != _normalise(expected):
            report.add(f"{question.question_id}: answer {answer!r} != world {expected!r}")

    elif kind in {"financial", "workbook_cell", "contradiction_resolution"} and primary:
        if primary.amount is not None and not _money_equal(answer, primary.amount):
            report.add(f"{question.question_id}: answer {answer!r} != world amount "
                       f"{primary.amount.display()!r}")

    elif kind == "workbook_derived":
        _check_derived(question, source, report)

    elif kind == "temporal" and primary is not None and primary.effective is not None:
        start = primary.effective.start
        if start is not None and answer != start.value.isoformat():
            report.add(f"{question.question_id}: answer {answer!r} != world date "
                       f"{start.value.isoformat()!r}")
        if start is not None and not _quote_states_date(question, start):
            report.add(f"{question.question_id}: evidence quote does not state the date "
                       f"{start.value.isoformat()}")

    elif kind in {"negation", "proposed_vs_completed", "alleged_vs_established"}:
        if primary is not None:
            _check_status(question, answer, primary, report)

    elif kind == "provenance":
        _check_provenance(question, items, report)

    elif kind == "historical_change":
        if not any(word in answer.lower() for word in ("increase", "decrease")):
            report.add(f"{question.question_id}: historical answer does not name a "
                       f"direction: {answer!r}")
        if not re.search(r"\d", answer):
            report.add(f"{question.question_id}: historical answer states no magnitude")

    elif kind == "identity_resolution":
        if not any(entity.canonical_name == answer
                   for entity in getattr(source.world, "entities", {}).values()):
            report.add(f"{question.question_id}: identity answer {answer!r} is not an "
                       f"entity name")


def _entity_name_by_id(world, entity_id: str | None) -> str | None:
    if entity_id is None:
        return None
    entity = getattr(world, "entities", {}).get(entity_id)
    return entity.canonical_name if entity is not None else None


def _expected_object(question, fact, facts: dict[str, Any], world) -> str | None:
    """The answer recomputed from the world: the object of the last fact in the hop."""
    if question.question_type == "multi_hop" and len(question.fact_ids) > 1:
        last = facts.get(question.fact_ids[-1])
        if last is None or last.object is None:
            return None
        return _entity_name_by_id(world, last.object.entity_id)
    if fact.object is None:
        return None
    return _entity_name_by_id(world, fact.object.entity_id)


def _check_derived(question: QAQuestion, source: ValidationInput,
                   report: ValidationReport) -> None:
    """Recompute a derived cell from its formula and compare with the answer."""
    if source.workbook is None or source.render_map is None:
        report.add(f"{question.question_id}: no workbook available for arithmetic")
        return
    from synthetic_data.model import workbook_cells
    from synthetic_data.workbook import evaluate, sheet_index

    index = sheet_index(source.render_map)
    fact_id = question.fact_ids[0]
    for cell in workbook_cells(source.workbook.workbook):
        if cell.formula is None:
            continue
        if cell.fact_ref != fact_id and cell.formula.derived_fact_id != fact_id:
            continue
        sheet_name = _sheet_of(source.workbook, cell.cell_id)
        if sheet_name is None:
            continue
        computed = evaluate(cell.formula, sheet=sheet_name, index=index,
                            workbook=source.workbook.workbook)
        answer = question.expected_answer or ""
        if isinstance(cell.value, Percentage):
            if not _percent_equal(answer, Percentage(basis_points=int(round(computed * 10000)))):
                report.add(f"{question.question_id}: answer {answer!r} != recomputed "
                           f"{computed:.4f}")
        elif not _money_equal(answer, Money(
                amount_minor=int(round(computed * 100)),
                currency=cell.value.currency if isinstance(cell.value, Money) else "GBP")):
            report.add(f"{question.question_id}: answer {answer!r} != recomputed "
                       f"{computed}")
        return
    report.add(f"{question.question_id}: no formula cell for {fact_id}")


def _sheet_of(document: CdmWorkbook, cell_id: str) -> str | None:
    for sheet in document.workbook.sheets:
        for table in [*(b.table for b in sheet.blocks if b.table is not None),
                      *sheet.tables]:
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                if any(cell.cell_id == cell_id for cell in row.cells):
                    return sheet.name
    return None


def _quote_states_date(question: QAQuestion, start) -> bool:
    surfaces = {start.value.isoformat(), start.value.strftime("%d %B %Y"),
                start.value.strftime("%d %b %Y")}
    return any(surface in evidence.quote for evidence in question.evidence
               for surface in surfaces)


def _check_status(question: QAQuestion, answer: str, fact, report: ValidationReport) -> None:
    status = fact.status or ""
    folded = _normalise(answer)
    if question.question_type == "negation":
        if status not in {"rejected", "deferred", "denied"}:
            report.add(f"{question.question_id}: negation question about status {status!r}")
        if not folded.startswith("no"):
            report.add(f"{question.question_id}: negation answer does not deny: {answer!r}")
        if status not in folded:
            report.add(f"{question.question_id}: negation answer does not state {status!r}")
    elif question.question_type == "proposed_vs_completed":
        if status != "proposed":
            report.add(f"{question.question_id}: proposed question about {status!r}")
        if "proposed" not in folded:
            report.add(f"{question.question_id}: answer does not state 'proposed'")
    else:
        if status not in {"alleged", "found", "held"}:
            report.add(f"{question.question_id}: alleged question about {status!r}")
        if status not in folded and not (status == "found" and "established" in folded):
            report.add(f"{question.question_id}: answer does not state {status!r}")


def _check_provenance(question: QAQuestion, items: dict[str, dict],
                      report: ValidationReport) -> None:
    item = items.get(question.fact_ids[0]) if question.fact_ids else None
    if item is None:
        return
    loc = (item.get("evidence") or [{}])[0].get("render_loc") or {}
    answer = question.expected_answer or ""
    if "page" in answer:
        page = loc.get("page")
        if page is None:
            report.add(f"{question.question_id}: claims a page but the artifact has none")
        elif str(page) not in answer:
            report.add(f"{question.question_id}: answer {answer!r} != page {page}")
    elif "cell" in answer:
        ref = loc.get("cell_ref")
        if ref is None or ref not in answer:
            report.add(f"{question.question_id}: answer {answer!r} != cell {ref}")
    elif "section" in answer:
        index = loc.get("section_index")
        if index is None or str(index + 1) not in answer:
            report.add(f"{question.question_id}: answer {answer!r} != section {index}")


def _check_proof(question: QAQuestion, source: ValidationInput,
                 report: ValidationReport) -> None:
    proof = question.proof
    if proof is None:
        report.add(f"{question.question_id}: no proof")
        return
    if proof.kind != question.unanswerable_kind:
        report.add(f"{question.question_id}: proof kind {proof.kind} != "
                   f"{question.unanswerable_kind}")
    # every cited search is re-run from scratch over every parser text
    for search in proof.text_searches:
        fresh = source.corpus.search(search.needle)
        if fresh.hits:
            report.add(f"{question.question_id}: proof needle {search.needle!r} occurs "
                       f"in {fresh.documents}")
    # co-occurrence checks are recomputed, including the counts the proof states
    for check in proof.absence_checks:
        fresh = source.corpus.co_occurrence(check.subject, check.object,
                                            check.relation_words)
        if fresh.relation_mentions:
            report.add(f"{question.question_id}: {check.subject!r} and "
                       f"{check.object!r} are stated together with "
                       f"{check.relation_words} in {fresh.relation_mentions} sentence(s)")
        if fresh.shared_sentences != check.shared_sentences:
            report.add(f"{question.question_id}: proof claims "
                       f"{check.shared_sentences} shared sentences, corpus has "
                       f"{fresh.shared_sentences}")
    if not proof.documents_searched:
        report.add(f"{question.question_id}: proof names no searched scope")
    # the semantic layer must agree that nothing answers it
    facts = _facts(source.world)
    if proof.kind == "absent_relationship":
        target = proof.predicate.split(" ", 1)[-1].strip()
        for fact in facts.values():
            if fact.kind != "edge" or fact.relation != "is_director_of":
                continue
            names = {_entity_name(source.world, fact.subject),
                     _entity_name(source.world, fact.object)}
            if {proof.subject, target} <= names:
                report.add(f"{question.question_id}: the world states "
                           f"{fact.relation} for {sorted(names)}")
    if proof.kind == "absent_financial_information":
        if any("dividend" in (fact.text or "").lower() for fact in facts.values()):
            report.add(f"{question.question_id}: a dividend fact exists")
    if proof.kind == "false_presupposition":
        subject = proof.subject
        target_id = next((entity_id for entity_id, entity
                          in getattr(source.world, "entities", {}).items()
                          if entity.canonical_name == subject), None)
        presupposed = proof.predicate.split("acquired ", 1)[-1].strip()
        object_id = next((entity_id for entity_id, entity
                          in getattr(source.world, "entities", {}).items()
                          if entity.canonical_name == presupposed), None)
        edges = {(fact.subject.entity_id, fact.object.entity_id)
                 for fact in facts.values()
                 if fact.kind == "edge" and fact.relation == "acquired"
                 and fact.subject and fact.object}
        if (target_id, object_id) in edges:
            report.add(f"{question.question_id}: the presupposed acquisition is a "
                       f"real edge; the question is answerable")


def _entity_name(world, reference) -> str:
    if reference is None or reference.entity_id is None:
        return ""
    entity = getattr(world, "entities", {}).get(reference.entity_id)
    return entity.canonical_name if entity is not None else ""


def _check_duplicates(qa: QASet, report: ValidationReport) -> None:
    seen: dict[str, str] = {}
    for question in qa.questions:
        key = _normalise(question.question)
        if key in seen:
            report.add(f"{question.question_id}: duplicate of {seen[key]}: "
                       f"{question.question[:60]!r}")
        seen[key] = question.question_id
    tokens = [(question, set(_normalise(question.question).split()))
              for question in qa.questions]
    closest = 0.0
    for index, (left, left_tokens) in enumerate(tokens):
        for right, right_tokens in tokens[index + 1:]:
            if not left_tokens or not right_tokens:
                continue
            overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
            closest = max(closest, overlap)
            if overlap >= 0.95 and left.question != right.question:
                report.add(f"{left.question_id}: near-duplicate of {right.question_id} "
                           f"(jaccard {overlap:.2f})")
    report.notes["max_question_similarity"] = round(closest, 3)


# ---------------------------------------------------------------------------
def validate_corpus(sets: list[QASet], corpus, stated_by_document: dict[str, list[str]]
                    ) -> ValidationReport:
    """Checks that need more than one QA set: twins, coverage, cross-document facts."""
    report = ValidationReport()
    by_document: dict[str, dict[str, dict[str, QAQuestion]]] = {}
    for qa in sets:
        by_document.setdefault(qa.document_id, {})[qa.format] = {
            question.question_id: question for question in qa.questions}

    for document_id, formats in by_document.items():
        if len(formats) < 2:
            continue
        reference_format = "txt" if "txt" in formats else sorted(formats)[0]
        reference = formats[reference_format]
        for fmt, questions in formats.items():
            if fmt == reference_format:
                continue
            if set(questions) != set(reference):
                # a fact can be stated in one format and not another; that is a
                # provenance difference to report, not a broken twin
                missing = sorted(set(reference) - set(questions))
                extra = sorted(set(questions) - set(reference))
                overlap = len(set(questions) & set(reference)) / max(len(reference), 1)
                report.notes.setdefault("format_only", {})[f"{document_id}/{fmt}"] = {
                    "missing": missing, "extra": extra, "overlap": round(overlap, 4)}
                if overlap < 0.75:
                    report.add(f"{document_id}/{fmt}: only {overlap:.0%} of the "
                               f"{reference_format} question ids are shared")
            for question_id, question in questions.items():
                twin = reference.get(question_id)
                if twin is None:
                    continue
                report.checked += 1
                if twin.expected_answer != question.expected_answer:
                    if question.question_type == "provenance":
                        # the same fact sits on a different page in a different
                        # format; the location is expected to differ and is checked
                        # per set against that format's own render map
                        report.notes.setdefault("format_specific_locations", 0)
                        report.notes["format_specific_locations"] += 1
                    else:
                        report.add(f"{document_id}/{fmt}: {question_id} answers "
                                   f"{question.expected_answer!r} vs "
                                   f"{reference_format} {twin.expected_answer!r}")
                if twin.answerable != question.answerable:
                    report.add(f"{document_id}/{fmt}: {question_id} answerability differs")
                if question.answerable and not question.evidence:
                    report.add(f"{document_id}/{fmt}: {question_id} has no evidence")

    types = Counter()
    kinds = Counter()
    for qa in sets:
        for question in qa.questions:
            if question.answerable:
                types[question.question_type] += 1
            else:
                kinds[question.unanswerable_kind] += 1
    for question_type in QUESTION_TYPES:
        if question_type in {"unanswerable", "historical_change"}:
            continue
        if types[question_type] == 0:
            report.add(f"corpus: no {question_type} questions")
    for kind in UNANSWERABLE_KINDS:
        if kinds[kind] == 0:
            report.add(f"corpus: no unanswerable question of kind {kind}")
    report.notes["types"] = dict(types)
    report.notes["kinds"] = dict(kinds)

    covered = set()
    for qa in sets:
        for question in qa.questions:
            covered.update(question.fact_ids)
    all_facts = set()
    for fact_ids in stated_by_document.values():
        all_facts.update(fact_ids)
    report.notes["fact_coverage"] = {
        "covered": len(covered & all_facts), "facts": len(all_facts)}
    return report


def coverage_report(sets: Iterable[QASet]) -> dict[str, Any]:
    types = Counter()
    kinds = Counter()
    per_document: dict[str, int] = {}
    for qa in sets:
        per_document[qa.document_id] = per_document.get(qa.document_id, 0) + len(qa.questions)
        for question in qa.questions:
            if question.answerable:
                types[question.question_type] += 1
            else:
                kinds[question.unanswerable_kind] += 1
    return {"types": dict(types), "kinds": dict(kinds),
            "per_document": per_document}
