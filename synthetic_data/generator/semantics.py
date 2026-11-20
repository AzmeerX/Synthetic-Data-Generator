"""Independent semantic validation for generated corpora.

This layer deliberately re-reads the generated document text instead of trusting
the generator's fact objects.  For every relationship, date, amount, status and
entity identity it checks both the structured artifact and the sentence that is
cited as evidence.  Synth's gold validator remains responsible for quote
location; this module checks whether the located statement actually means what
the gold/QA artifact claims.

The checks are intentionally separable so tests can inject a known defect and
assert that the corresponding validator rejects it.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from synth.eval.gold import GoldSet

from synthetic_data.generator.documents import (
    DocumentPlan,
    add_months,
    format_date,
)
from synthetic_data.generator.queries import FactIndex
from synthetic_data.generator.world import (
    FACILITY_CAPACITY_INCREASE,
    FACILITY_CAPACITY_ORIGINAL,
    Fact,
    World,
)
from synthetic_data.schemas.qa import QADataset, QAQuery


class SemanticValidationError(RuntimeError):
    """Raised when an artifact is grounded but semantically wrong."""


@dataclass(frozen=True)
class SemanticReport:
    checks: dict[str, int]
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": dict(sorted(self.checks.items())),
            "failure_count": len(self.failures),
            "failures": list(self.failures),
        }


# --------------------------------------------------------------------------- text model
@dataclass(frozen=True)
class Statement:
    """A relationship independently recovered from one canonical sentence."""

    relation: str
    subject_id: str
    object_id: str
    sentence: str
    subject_surface: str
    object_surface: str
    amount_gbp: int | None = None
    date_text: str | None = None
    negated: bool = False
    kind: str = "edge"


_CUES: tuple[tuple[str, str, bool], ...] = (
    ("family_of", " is a family member of ", False),
    ("shareholder_of", " is a shareholder of ", False),
    ("chair_of", " served as chair of ", False),
    ("chair_of", " became chair of ", False),
    ("chair_of", " is the chair of ", False),
    ("guarantor_for", " guaranteed the loan to ", False),
    ("guarantor_for", " did not guarantee the facility for ", True),
    ("represents", " represents ", False),
    ("owns", " owns ", False),
    ("lent_to", " lent ", False),
    ("owes", " owes ", False),
    ("signed", " signed the ", False),
    ("party_to", " is a party to the ", False),
    ("located_in", " is located in ", False),
)

_FACILITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^(?P<bank>.+?) provided a facility of GBP (?P<amount>[\d,]+) "
            r"million to (?P<company>.+?) on (?P<date>.+?)\.$"),
        "facility_original",
    ),
    (
        re.compile(
            r"^(?P<bank>.+?) increased the facility available to "
            r"(?P<company>.+?) to GBP (?P<amount>[\d,]+) million on "
            r"(?P<date>.+?)\.$"),
        "facility_increase",
    ),
)

_AMOUNT_RE = re.compile(r"GBP ([\d,]+)(?: (million|thousand))?")
_DATE_RE = re.compile(r"(?:on|as at) (\d{1,2} [A-Z][a-z]+ \d{4})")
_FY_RE = re.compile(r"FY\d{4}")


@dataclass(frozen=True)
class _SurfaceIndex:
    """One world's surface forms, resolved once instead of per lookup.

    Tiers preserve the original resolution order: qualified display name,
    canonical name, then alias.  Lists keep ``world.entities`` iteration order
    so ambiguous surfaces return the same ids as a full scan.
    """

    display_names: dict[str, str]
    by_display: dict[str, tuple[str, ...]]
    by_name: dict[str, tuple[str, ...]]
    by_alias: dict[str, tuple[str, ...]]
    ordered: tuple[tuple[str, str], ...]

    @classmethod
    def build(cls, world: World) -> "_SurfaceIndex":
        display_names: dict[str, str] = {}
        by_display: dict[str, list[str]] = defaultdict(list)
        by_name: dict[str, list[str]] = defaultdict(list)
        by_alias: dict[str, list[str]] = defaultdict(list)
        pairs: list[tuple[str, str]] = []
        for entity_id, entity in world.entities.items():
            display = world.display_name(entity_id)
            display_names[entity_id] = display
            by_display[display].append(entity_id)
            by_name[entity.name].append(entity_id)
            for alias in dict.fromkeys(entity.aliases):
                by_alias[alias].append(entity_id)
            pairs.append((display, entity_id))
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        return cls(
            display_names=display_names,
            by_display={key: tuple(value) for key, value in by_display.items()},
            by_name={key: tuple(value) for key, value in by_name.items()},
            by_alias={key: tuple(value) for key, value in by_alias.items()},
            ordered=tuple(pairs),
        )

    def resolve(self, name: str) -> list[str]:
        name = name.strip()
        for tier in (self.by_display, self.by_name, self.by_alias):
            matches = tier.get(name)
            if matches:
                return list(matches)
        return []

    def name_to_id(self, name: str) -> str | None:
        matches = self.by_display.get(name.strip(), ())
        return matches[0] if len(matches) == 1 else None


def _display_surfaces(world: World,
                      surfaces: _SurfaceIndex | None = None
                      ) -> Sequence[tuple[str, str]]:
    if surfaces is not None:
        return surfaces.ordered
    return _SurfaceIndex.build(world).ordered


def _match_end(text: str, surfaces: Sequence[tuple[str, str]]) -> tuple[str | None, str | None]:
    text = text.rstrip()
    for surface, entity_id in surfaces:
        if text.endswith(surface):
            return entity_id, surface
    return None, None


def _match_start(text: str, surfaces: Sequence[tuple[str, str]]) -> tuple[str | None, str | None]:
    text = text.lstrip()
    for surface, entity_id in surfaces:
        if text.startswith(surface):
            return entity_id, surface
    if text.startswith("the "):
        rest = text[4:]
        for surface, entity_id in surfaces:
            if rest.startswith(surface):
                return entity_id, surface
    return None, None


def _parse_amount(text: str) -> int | None:
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    value = int(match.group(1).replace(",", ""))
    unit = match.group(2)
    if unit == "million":
        value *= 1_000_000
    elif unit == "thousand":
        value *= 1_000
    return value


def _parse_date_text(text: str) -> str | None:
    match = _DATE_RE.search(text)
    return match.group(1) if match else None


def _name_to_id(world: World, name: str,
                surfaces: _SurfaceIndex | None = None) -> str | None:
    if surfaces is not None:
        return surfaces.name_to_id(name)
    matches = [entity_id for entity_id in world.entities
               if world.display_name(entity_id) == name.strip()]
    return matches[0] if len(matches) == 1 else None


def extract_statements(plan: DocumentPlan, world: World,
                       surfaces: _SurfaceIndex | None = None) -> list[Statement]:
    """Recover relationships from canonical text without reading stated facts."""
    index = surfaces if surfaces is not None else _SurfaceIndex.build(world)
    ordered = index.ordered
    out: list[Statement] = []
    for sentence in plan.sentences:
        for relation, cue, negated in _CUES:
            if cue not in sentence:
                continue
            before, after = sentence.split(cue, 1)
            subject_id, subject_surface = _match_end(before, ordered)
            object_id, object_surface = _match_start(after, ordered)
            if subject_id is None or object_id is None:
                continue
            out.append(Statement(
                relation=relation,
                subject_id=subject_id,
                object_id=object_id,
                sentence=sentence,
                subject_surface=subject_surface or "",
                object_surface=object_surface or "",
                amount_gbp=_parse_amount(after),
                date_text=_parse_date_text(after),
                negated=negated,
            ))
        for pattern, relation in _FACILITY_PATTERNS:
            match = pattern.match(sentence)
            if not match:
                continue
            bank = _name_to_id(world, match.group("bank"), index)
            company = _name_to_id(world, match.group("company"), index)
            if bank is None or company is None:
                continue
            value = int(match.group("amount").replace(",", "")) * 1_000_000
            out.append(Statement(
                relation=relation,
                subject_id=bank,
                object_id=company,
                sentence=sentence,
                subject_surface=match.group("bank").strip(),
                object_surface=match.group("company").strip(),
                amount_gbp=value,
                date_text=match.group("date").strip(),
                kind="proposition",
            ))
    return out


def relationship_statement_matches(sentence: str, relation: str, subject: str,
                                   object_: str) -> bool:
    """Independent expected-case helper used by tests.

    It deliberately does not consult the world or the generator facts.
    """
    phrases = {
        "family_of": f"{subject} is a family member of {object_}",
        "shareholder_of": f"{subject} is a shareholder of {object_}",
        "represents": f"{subject} represents {object_}",
        "owns": f"{subject} owns {object_}",
        "lent_to": f"{subject} lent {object_}",
        "owes": f"{subject} owes {object_}",
        "guarantor_for": f"{subject} guaranteed the loan to {object_}",
        "located_in": f"{subject} is located in {object_}",
    }
    if relation in phrases:
        return phrases[relation] in sentence
    if relation == "chair_of":
        return any(f"{subject} {verb} chair of {object_}" in sentence
                   for verb in ("served as", "became", "is the"))
    if relation == "signed":
        return f"{subject} signed the {object_}" in sentence
    if relation == "party_to":
        return f"{subject} is a party to the {object_}" in sentence
    if relation == "facility_original":
        return "provided a facility of" in sentence and subject in sentence and object_ in sentence
    if relation == "facility_increase":
        return "increased the facility available to" in sentence and subject in sentence and object_ in sentence
    return False


def temporal_bounds_supported(sentence: str, expected: str) -> bool:
    """Independent expected-case helper: both boundaries must be in the text."""
    parts = expected.split(" to ")
    return len(parts) == 2 and all(part and part in sentence for part in parts)


# --------------------------------------------------------------------------- shared helpers
def _plans_by_id(plans: Iterable[DocumentPlan]) -> dict[str, DocumentPlan]:
    return {plan.document_id: plan for plan in plans}


def _facts_by_id(plans: Iterable[DocumentPlan]) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    for plan in plans:
        for stated in plan.stated_facts:
            facts[stated.fact.fact_id] = stated.fact
    return facts


def _statements_by_doc(plans: Iterable[DocumentPlan], world: World,
                       surfaces: _SurfaceIndex | None = None
                       ) -> dict[str, list[Statement]]:
    index = surfaces if surfaces is not None else _SurfaceIndex.build(world)
    return {plan.document_id: extract_statements(plan, world, index)
            for plan in plans}


def _statement_docs(query: QAQuery, statements: dict[str, list[Statement]]) -> list[Statement]:
    out: list[Statement] = []
    for document_id in dict.fromkeys(ev.document_id for ev in query.supporting_evidence):
        out.extend(statements.get(document_id, []))
    return out


def _money(amount: int) -> str:
    if amount % 1_000_000 == 0:
        return f"GBP {amount // 1_000_000} million"
    if amount % 1_000 == 0:
        return f"GBP {amount // 1_000:,} thousand"
    return f"GBP {amount:,}"


def _resolve_entity_ids(world: World, name: str,
                        surfaces: _SurfaceIndex | None = None) -> list[str]:
    """Resolve a surface form to entity ids, preferring the most specific tier.

    Alias resolution is part of entity identity: ``Petra Alden`` must resolve to
    the same person as ``Petra Hale``.  Tiers are tried in order so an alias can
    never shadow a canonical or qualified name.
    """
    index = surfaces if surfaces is not None else _SurfaceIndex.build(world)
    return index.resolve(name)


def _unqualified_namesake(world: World, text: str) -> str | None:
    """Return a namesake id whose bare name remains after removing qualifiers."""
    stripped = text
    for entity_id in world.duplicate_names:
        stripped = stripped.replace(world.display_name(entity_id), "")
    for entity_id in world.duplicate_names:
        if world.entity(entity_id).name in stripped:
            return entity_id
    return None


def _gold_list(golds: Iterable[Any] | None) -> list[GoldSet]:
    out: list[GoldSet] = []
    for item in golds or []:
        out.append(item.gold if hasattr(item, "gold") else item)
    return out


def _gold_by_filename(golds: Iterable[Any] | None) -> dict[str, GoldSet]:
    return {gold.doc.filename or "": gold for gold in _gold_list(golds)}


def _gold_evidence(gold: GoldSet):
    for entity in gold.entities:
        for index, mention in enumerate(entity.mentions):
            yield f"entity {entity.id} mention {index}", mention
    for edge in gold.edges:
        for index, evidence in enumerate(edge.evidence):
            yield f"edge {edge.id} evidence {index}", evidence
    for proposition in gold.propositions:
        for index, evidence in enumerate(proposition.evidence):
            yield f"proposition {proposition.id} evidence {index}", evidence
    for index, link in enumerate(gold.links):
        for evidence_index, evidence in enumerate(link.evidence):
            yield f"link {link.id or index} evidence {evidence_index}", evidence
    for time_ref in gold.time_refs:
        yield f"time_ref {time_ref.id}", time_ref.evidence


def _sentence_for_evidence(plan: DocumentPlan, sentence_id: str) -> str | None:
    if not sentence_id.startswith("s"):
        return None
    try:
        index = int(sentence_id[1:])
    except ValueError:
        return None
    if 0 <= index < len(plan.sentences):
        return plan.sentences[index]
    return None


def _fact_by_id(fact_id: str, plans: Iterable[DocumentPlan]) -> Fact | None:
    for plan in plans:
        for stated in plan.stated_facts:
            if stated.fact.fact_id == fact_id:
                return stated.fact
    return None


def _clause_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _supported_money_in_evidence(query: QAQuery, expected: str) -> bool:
    if not query.supporting_evidence:
        return False
    return any(expected in evidence.quote for evidence in query.supporting_evidence)


# --------------------------------------------------------------------------- checks
def check_evidence_quotes(world: World, plans: Sequence[DocumentPlan],
                          dataset: QADataset, golds=None) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    by_id = _plans_by_id(plans)
    docs = {document.document_id: document for document in dataset.documents}
    facts = _facts_by_id(plans)

    for query in dataset.queries:
        for evidence in query.supporting_evidence:
            checked += 1
            plan = by_id.get(evidence.document_id)
            if plan is None:
                failures.append(f"{query.query_id}: evidence cites unknown document "
                                f"{evidence.document_id!r}")
                continue
            document = docs.get(evidence.document_id)
            if document is None:
                failures.append(f"{query.query_id}: evidence document is not in dataset")
                continue
            if evidence.filename != plan.filename:
                failures.append(f"{query.query_id}: evidence filename mismatch")
            if evidence.text_sha256 != document.text_sha256:
                failures.append(f"{query.query_id}: evidence text hash mismatch")
            if not 0 <= evidence.sentence_idx < len(plan.sentences):
                failures.append(f"{query.query_id}: evidence sentence index out of range")
                continue
            if evidence.sentence_id != f"s{evidence.sentence_idx}":
                failures.append(f"{query.query_id}: evidence sentence label mismatch")
            sentence = plan.sentences[evidence.sentence_idx]
            if not evidence.quote or evidence.quote not in sentence:
                failures.append(
                    f"{query.query_id}: evidence quote is not in the cited sentence: "
                    f"{evidence.quote!r}")
        for fact_id in query.gold_fact_ids:
            if fact_id not in facts:
                failures.append(f"{query.query_id}: gold fact {fact_id!r} is not stated")

    plan_by_filename = {plan.filename: plan for plan in plans}
    for gold in _gold_list(golds):
        plan = plan_by_filename.get(gold.doc.filename or "")
        if plan is None:
            continue
        for label, evidence in _gold_evidence(gold):
            checked += 1
            sentence = _sentence_for_evidence(plan, evidence.sentence_id)
            if sentence is None:
                failures.append(f"{gold.doc.filename}: {label} cites missing sentence "
                                f"{evidence.sentence_id!r}")
                continue
            if evidence.quote not in sentence:
                failures.append(f"{gold.doc.filename}: {label} quote is not in the sentence")
                continue
            if sentence.count(evidence.quote) <= evidence.occurrence:
                failures.append(f"{gold.doc.filename}: {label} occurrence is out of range")
    return checked, failures


def check_relationship_direction(world: World, plans: Sequence[DocumentPlan],
                                 dataset: QADataset, golds=None, *,
                                 surfaces: _SurfaceIndex | None = None,
                                 statements: dict[str, list[Statement]] | None = None,
                                 ) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    index = surfaces if surfaces is not None else _SurfaceIndex.build(world)
    if statements is None:
        statements = _statements_by_doc(plans, world, index)
    facts = _facts_by_id(plans)

    # Every stated edge must be independently recoverable from its sentence.
    for plan in plans:
        for stated in plan.stated_facts:
            fact = stated.fact
            if fact.kind != "edge":
                continue
            checked += 1
            matches = [statement for statement in statements.get(plan.document_id, [])
                       if statement.sentence == stated.sentence_text
                       and statement.relation == fact.relation
                       and statement.subject_id == fact.subject_id
                       and statement.object_id == fact.object_id
                       and statement.negated == fact.negated]
            if not matches:
                failures.append(
                    f"{plan.document_id}: {fact.relation} edge {fact.fact_id} is not "
                    "recoverable from its cited sentence with the same direction")

    for query in dataset.queries:
        if query.query_type not in ("relationship_lookup", "historical_change",
                                    "financial", "negation", "multi_hop"):
            continue
        query_facts = [facts[fact_id] for fact_id in query.gold_fact_ids if fact_id in facts]
        if query.query_type in ("relationship_lookup", "historical_change", "negation"):
            if len(query_facts) != 1:
                failures.append(f"{query.query_id}: expected exactly one gold fact")
                continue
            fact = query_facts[0]
            if query.query_type == "negation":
                checked += 1
                if not fact.negated:
                    failures.append(f"{query.query_id}: negation cites a positive fact")
                if query.expected_answer != "No":
                    failures.append(f"{query.query_id}: negation answer is not No")
                if not any(statement.negated and statement.relation == fact.relation
                           and statement.subject_id == fact.subject_id
                           and statement.object_id == fact.object_id
                           for statement in _statement_docs(query, statements)):
                    failures.append(f"{query.query_id}: negated statement is not in evidence")
                continue
            if fact.kind != "edge":
                failures.append(f"{query.query_id}: relationship query cites a proposition")
                continue
            checked += 1
            doc_statements = _statement_docs(query, statements)
            matches = [statement for statement in doc_statements
                       if statement.relation == fact.relation
                       and statement.subject_id == fact.subject_id
                       and statement.object_id == fact.object_id
                       and not statement.negated]
            if not matches:
                failures.append(
                    f"{query.query_id}: no evidence statement has "
                    f"{fact.subject_id} -{fact.relation}-> {fact.object_id}")
                continue
            expected_ids = _resolve_entity_ids(world, query.expected_answer, index)
            if len(expected_ids) != 1:
                failures.append(
                    f"{query.query_id}: expected answer {query.expected_answer!r} does "
                    "not identify exactly one entity")
                continue
            expected_id = expected_ids[0]
            if fact.relation == "family_of":
                if expected_id != fact.object_id:
                    failures.append(
                        f"{query.query_id}: family_of direction is reversed; expected "
                        "the object of the edge")
                subject_name = world.display_name(fact.subject_id)
                object_name = world.display_name(fact.object_id)
                if subject_name not in query.question or "family member" not in query.question:
                    failures.append(f"{query.query_id}: question does not name the subject")
                if not any(relationship_statement_matches(
                        statement.sentence, "family_of", subject_name, object_name)
                        for statement in matches):
                    failures.append(f"{query.query_id}: family sentence is not in the "
                                    "subject -> object direction")
            elif fact.relation == "shareholder_of":
                if expected_id != fact.subject_id:
                    failures.append(f"{query.query_id}: shareholder direction is reversed")
                if world.entity(fact.object_id).name not in query.question:
                    failures.append(f"{query.query_id}: question does not name the company")
            elif fact.relation == "owns":
                if expected_id != fact.object_id:
                    failures.append(f"{query.query_id}: ownership direction is reversed")
                if world.entity(fact.subject_id).name not in query.question:
                    failures.append(f"{query.query_id}: question does not name the owner")
                lowered = query.question.casefold()
                qualified_kind = ("asset" if "which asset" in lowered else
                                  "company" if "which company" in lowered else None)
                if (qualified_kind is not None
                        and world.entity(fact.object_id).kind != qualified_kind):
                    failures.append(
                        f"{query.query_id}: ownership target is not a {qualified_kind}")
                subject_name = world.display_name(fact.subject_id)
                object_name = world.display_name(fact.object_id)
                if not any(relationship_statement_matches(
                        statement.sentence, "owns", subject_name, object_name)
                        for statement in matches):
                    failures.append(f"{query.query_id}: ownership sentence is not in the "
                                    "subject -> object direction")
            elif fact.relation == "represents":
                if expected_id != fact.subject_id:
                    failures.append(f"{query.query_id}: representation direction is reversed")
                if world.entity(fact.object_id).name not in query.question:
                    failures.append(f"{query.query_id}: question does not name the client")
            elif fact.relation == "chair_of":
                if expected_id != fact.subject_id:
                    failures.append(f"{query.query_id}: chair direction is reversed")
                if query.query_type == "historical_change" and fact.end_date is None:
                    failures.append(f"{query.query_id}: historical chair has no end date")
                if fact.end_date is not None and "until" not in query.question:
                    failures.append(f"{query.query_id}: historical question lacks 'until'")
            elif fact.relation == "guarantor_for":
                if expected_id != fact.subject_id:
                    failures.append(f"{query.query_id}: guarantee direction is reversed")
                if world.entity(fact.object_id).name not in query.question:
                    failures.append(f"{query.query_id}: question does not name the borrower")
            else:
                failures.append(f"{query.query_id}: unchecked relation {fact.relation!r}")

        elif query.query_type == "financial":
            if len(query_facts) != 1:
                failures.append(f"{query.query_id}: financial query needs one gold fact")
                continue
            fact = query_facts[0]
            checked += 1
            lowered = query.question.casefold()
            if "facility" in lowered:
                if fact.kind != "proposition" or fact.note not in (
                        FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
                    failures.append(f"{query.query_id}: facility question cites a non-facility fact")
                    continue
                if fact.amount_gbp is None:
                    failures.append(f"{query.query_id}: facility fact has no amount")
                    continue
                if query.expected_answer != _money(fact.amount_gbp):
                    failures.append(f"{query.query_id}: facility amount mismatch")
                if not any(statement.relation in ("facility_original", "facility_increase")
                           and statement.amount_gbp == fact.amount_gbp
                           for statement in _statement_docs(query, statements)):
                    failures.append(f"{query.query_id}: facility amount is not in the text")
                if any(statement.relation == "lent_to"
                       and statement.amount_gbp == fact.amount_gbp
                       for statement in _statement_docs(query, statements)):
                    failures.append(
                        f"{query.query_id}: facility amount is also described as lent")
                if not _supported_money_in_evidence(query, query.expected_answer):
                    failures.append(f"{query.query_id}: expected money is not in evidence")
            elif "lend" in lowered:
                if fact.kind != "edge" or fact.relation != "lent_to":
                    failures.append(f"{query.query_id}: lending question cites a non-loan fact")
                    continue
                if fact.amount_gbp is None or fact.date is None:
                    failures.append(f"{query.query_id}: loan fact has no amount/date")
                    continue
                if query.expected_answer != _money(fact.amount_gbp):
                    failures.append(f"{query.query_id}: loan amount mismatch")
                if not any(statement.relation == "lent_to"
                           and statement.subject_id == fact.subject_id
                           and statement.object_id == fact.object_id
                           and statement.amount_gbp == fact.amount_gbp
                           for statement in _statement_docs(query, statements)):
                    failures.append(f"{query.query_id}: loan is not described as lent")
                if not _supported_money_in_evidence(query, query.expected_answer):
                    failures.append(f"{query.query_id}: expected money is not in evidence")
            elif "owe" in lowered:
                if fact.kind != "edge" or fact.relation != "owes":
                    failures.append(f"{query.query_id}: balance question cites a non-owes fact")
                    continue
                if fact.amount_gbp is None:
                    failures.append(f"{query.query_id}: owes fact has no amount")
                    continue
                if query.expected_answer != _money(fact.amount_gbp):
                    failures.append(f"{query.query_id}: balance amount mismatch")
                if not any(statement.relation == "owes"
                           and statement.subject_id == fact.subject_id
                           and statement.object_id == fact.object_id
                           and statement.amount_gbp == fact.amount_gbp
                           for statement in _statement_docs(query, statements)):
                    failures.append(f"{query.query_id}: balance is not in the text")
            else:
                failures.append(f"{query.query_id}: unrecognised financial question")

        elif query.query_type == "multi_hop":
            if len(query_facts) != 2:
                failures.append(f"{query.query_id}: multi-hop query needs two facts")
                continue
            checked += 1
            ownership = next((fact for fact in query_facts if fact.relation == "owns"), None)
            other = next((fact for fact in query_facts if fact is not ownership), None)
            if ownership is None or other is None:
                failures.append(f"{query.query_id}: multi-hop facts are not ownership + join")
                continue
            if other.relation == "represents":
                if world.entity(other.subject_id or "").name != query.expected_answer:
                    failures.append(f"{query.query_id}: law-firm answer mismatch")
            elif other.relation == "lent_to":
                if world.entity(other.subject_id or "").name != query.expected_answer:
                    failures.append(f"{query.query_id}: bank answer mismatch")
            else:
                failures.append(f"{query.query_id}: unsupported multi-hop join")
    return checked, failures


def check_unique_answers(world: World, plans: Sequence[DocumentPlan],
                         dataset: QADataset, golds=None, *,
                         surfaces: _SurfaceIndex | None = None,
                         statements: dict[str, list[Statement]] | None = None,
                         ) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    index = surfaces if surfaces is not None else _SurfaceIndex.build(world)
    if statements is None:
        statements = _statements_by_doc(plans, world, index)
    facts = _facts_by_id(plans)
    plans_by_id = _plans_by_id(plans)

    for query in dataset.queries:
        if query.answer_type == "entity":
            checked += 1
            matches = _resolve_entity_ids(world, query.expected_answer, index)
            if len(matches) != 1:
                failures.append(
                    f"{query.query_id}: answer {query.expected_answer!r} resolves to "
                    f"{len(matches)} entities, not one")

        if query.query_type not in ("relationship_lookup", "historical_change",
                                    "financial", "multi_hop"):
            continue
        query_facts = [facts[fact_id] for fact_id in query.gold_fact_ids if fact_id in facts]
        if not query_facts:
            continue
        fact = query_facts[0]

        if query.query_type in ("relationship_lookup", "historical_change"):
            checked += 1
            if fact.kind != "edge":
                failures.append(f"{query.query_id}: relationship fact is not an edge")
                continue
            doc_statements = _statement_docs(query, statements)
            if fact.relation == "family_of":
                candidates = {s.object_id for s in doc_statements
                              if s.relation == "family_of" and s.subject_id == fact.subject_id}
            elif fact.relation == "shareholder_of":
                candidates = {s.subject_id for s in doc_statements
                              if s.relation == "shareholder_of" and s.object_id == fact.object_id}
            elif fact.relation == "represents":
                candidates = {s.subject_id for s in doc_statements
                              if s.relation == "represents" and s.object_id == fact.object_id}
            elif fact.relation == "guarantor_for":
                candidates = {s.subject_id for s in doc_statements
                              if s.relation == "guarantor_for" and not s.negated
                              and s.object_id == fact.object_id}
            elif fact.relation == "chair_of":
                historical = query.query_type == "historical_change" or fact.end_date is not None
                candidates = {s.subject_id for s in doc_statements
                              if s.relation == "chair_of"
                              and s.object_id == fact.object_id
                              and ("served as chair" in s.sentence) == historical}
            elif fact.relation == "owns":
                lowered = query.question.casefold()
                targets = [s.object_id for s in doc_statements
                           if s.relation == "owns" and not s.negated
                           and s.subject_id == fact.subject_id]
                if "which asset" in lowered:
                    targets = [target for target in targets
                               if world.entity(target).kind == "asset"]
                elif "which company" in lowered:
                    targets = [target for target in targets
                               if world.entity(target).kind == "company"]
                candidates = set(targets)
            else:
                failures.append(f"{query.query_id}: unchecked relation {fact.relation!r}")
                continue
            if len(candidates) != 1:
                failures.append(
                    f"{query.query_id}: {len(candidates)} valid answers in the document "
                    "for an unqualified question")
                continue
            candidate = next(iter(candidates))
            if query.expected_answer != world.display_name(candidate):
                failures.append(f"{query.query_id}: unique candidate does not match answer")

        elif query.query_type == "financial":
            checked += 1
            if len(query_facts) != 1:
                failures.append(f"{query.query_id}: financial query needs one fact")
                continue
            doc_statements = _statement_docs(query, statements)
            if fact.note in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
                relation = ("facility_increase"
                            if fact.note == FACILITY_CAPACITY_INCREASE
                            else "facility_original")
                # A facility proposition is about the borrower's capacity.  The
                # sentence names the bank as the provider, so match the company
                # on the object side and the stated date.
                amounts = {s.amount_gbp for s in doc_statements
                           if s.relation == relation
                           and s.object_id == fact.subject_id
                           and s.date_text == (format_date(fact.date)
                                               if fact.date else None)
                           and s.amount_gbp is not None}
            elif fact.relation == "lent_to":
                amounts = {s.amount_gbp for s in doc_statements
                           if s.relation == "lent_to"
                           and s.subject_id == fact.subject_id
                           and s.object_id == fact.object_id
                           and s.date_text == (format_date(fact.date)
                                               if fact.date else None)
                           and s.amount_gbp is not None}
            elif fact.relation == "owes":
                amounts = {s.amount_gbp for s in doc_statements
                           if s.relation == "owes"
                           and s.subject_id == fact.subject_id
                           and s.object_id == fact.object_id
                           and s.date_text == (format_date(fact.date)
                                               if fact.date else None)
                           and s.amount_gbp is not None}
            else:
                failures.append(f"{query.query_id}: unrecognised financial fact")
                continue
            if len(amounts) != 1:
                failures.append(f"{query.query_id}: {len(amounts)} valid money answers")
                continue
            amount = next(iter(amounts))
            if amount is None or query.expected_answer != _money(amount):
                failures.append(f"{query.query_id}: money answer does not match the text")

        elif query.query_type == "multi_hop":
            checked += 1
            ownership = next((f for f in query_facts if f.relation == "owns"), None)
            other = next((f for f in query_facts if f is not ownership), None)
            if ownership is None or other is None or ownership.object_id is None:
                failures.append(f"{query.query_id}: malformed multi-hop facts")
                continue
            target = ownership.object_id
            all_statements = [s for items in statements.values() for s in items]
            # The question says "the company owned by X": only company targets
            # are candidates.  Asset ownership does not make the hop ambiguous.
            ownership_candidates = {s.object_id for s in all_statements
                                    if s.relation == "owns" and not s.negated
                                    and s.subject_id == ownership.subject_id
                                    and s.object_id is not None
                                    and world.entity(s.object_id).kind == "company"}
            if len(ownership_candidates) != 1:
                failures.append(f"{query.query_id}: ambiguous ownership hop")
            if other.relation == "represents":
                candidates = {s.subject_id for s in all_statements
                              if s.relation == "represents" and s.object_id == target}
            elif other.relation == "lent_to":
                candidates = {s.subject_id for s in all_statements
                              if s.relation == "lent_to" and s.object_id == target}
            else:
                candidates = set()
            if len(candidates) != 1:
                failures.append(f"{query.query_id}: ambiguous join answer")
                continue
            candidate = next(iter(candidates))
            if query.expected_answer != world.entity(candidate).name:
                failures.append(f"{query.query_id}: join answer does not match the text")
    return checked, failures


def check_temporal_support(world: World, plans: Sequence[DocumentPlan],
                           dataset: QADataset, golds=None) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    plans_by_id = _plans_by_id(plans)
    docs = {document.document_id: document for document in dataset.documents}
    start_month = world.ontology.fiscal_year_start_month

    for query in dataset.queries:
        if query.query_type != "temporal":
            continue
        checked += 1
        if not query.supporting_evidence:
            failures.append(f"{query.query_id}: temporal query has no evidence")
            continue
        evidence_docs = [plans_by_id[ev.document_id] for ev in query.supporting_evidence
                         if ev.document_id in plans_by_id]
        sentences = [plans_by_id[ev.document_id].sentences[ev.sentence_idx]
                     for ev in query.supporting_evidence
                     if ev.document_id in plans_by_id
                     and 0 <= ev.sentence_idx < len(plans_by_id[ev.document_id].sentences)]
        if not sentences:
            failures.append(f"{query.query_id}: temporal evidence is not locatable")
            continue
        joined = " ".join(sentences)

        if query.answer_type == "period":
            if not temporal_bounds_supported(joined, query.expected_answer):
                failures.append(
                    f"{query.query_id}: period answer is not stated explicitly in the text")
                continue
            labels = _FY_RE.findall(query.question)
            if not labels:
                failures.append(f"{query.query_id}: period question does not name a fiscal year")
                continue
            if labels[0] not in joined:
                failures.append(f"{query.query_id}: fiscal-year label is not in the evidence")
            if "runs from" not in joined and "period from" not in joined:
                failures.append(f"{query.query_id}: evidence does not establish the period")
            document = docs.get(query.supporting_evidence[0].document_id)
            if document is None:
                failures.append(f"{query.query_id}: missing temporal document metadata")
                continue
            doc_date = _clause_date(document.doc_date)
            if doc_date.month >= start_month:
                start_year, end_year = doc_date.year, doc_date.year + 1
            else:
                start_year, end_year = doc_date.year - 1, doc_date.year
            fiscal_start = dt.date(start_year, start_month, 1)
            fiscal_end = dt.date(end_year, start_month, 1) - dt.timedelta(days=1)
            expected = f"{format_date(fiscal_start)} to {format_date(fiscal_end)}"
            if query.expected_answer != expected:
                failures.append(
                    f"{query.query_id}: period answer disagrees with the stated fiscal year")
        elif query.answer_type == "date":
            if "reviewed" in query.question.casefold():
                document = docs.get(query.supporting_evidence[0].document_id)
                if document is None:
                    failures.append(f"{query.query_id}: missing document metadata")
                    continue
                doc_date = _clause_date(document.doc_date)
                expected = format_date(add_months(doc_date, 6))
                if query.expected_answer != expected:
                    failures.append(f"{query.query_id}: relative date arithmetic is wrong")
                if "six months after the date of this note" not in joined:
                    failures.append(f"{query.query_id}: relative-date phrase is not in evidence")
                if not any(sentence.startswith("Date:") and format_date(doc_date) in sentence
                           for sentence in sentences):
                    failures.append(f"{query.query_id}: document date anchor is not in evidence")
            elif query.expected_answer not in joined:
                failures.append(
                    f"{query.query_id}: absolute date is not stated explicitly in the text")
        else:
            failures.append(f"{query.query_id}: unsupported temporal answer type")
    return checked, failures


def check_proposition_statuses(world: World, plans: Sequence[DocumentPlan],
                               dataset: QADataset, golds=None) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    facts = _facts_by_id(plans)
    plans_by_id = _plans_by_id(plans)
    gold_by_filename = _gold_by_filename(golds)

    for plan in plans:
        for stated in plan.stated_facts:
            fact = stated.fact
            if fact.kind != "proposition":
                continue
            checked += 1
            sentence = stated.sentence_text
            if fact.status not in world.ontology.proposition_status:
                failures.append(f"{fact.fact_id}: unsupported proposition status")
            if fact.negated and fact.status in {
                    "held", "found", "resolved", "ordered", "declared", "admitted"}:
                failures.append(
                    f"{fact.fact_id}: negated proposition has positive status "
                    f"{fact.status!r}")
            if fact.status == "alleged":
                if fact.speaker_id is not None:
                    failures.append(
                        f"{fact.fact_id}: alleged speaker must be the unnamed former "
                        "employee, not a named reporter")
                if fact.reporter_id is None:
                    failures.append(f"{fact.fact_id}: allegation has no reporter")
                elif world.display_name(fact.reporter_id) not in sentence:
                    failures.append(f"{fact.fact_id}: reporter is not named in the text")
                if "former employee alleged" not in sentence:
                    failures.append(f"{fact.fact_id}: text does not attribute the allegation "
                                    "to a former employee")
            elif fact.status == "proposed":
                if fact.speaker_id is None:
                    failures.append(f"{fact.fact_id}: proposal has no speaker")
                elif world.display_name(fact.speaker_id) not in sentence:
                    failures.append(f"{fact.fact_id}: proposer is not named in the text")
                if "proposed" not in sentence:
                    failures.append(f"{fact.fact_id}: proposal is not stated as proposed")
                if "completed" in sentence.casefold():
                    failures.append(f"{fact.fact_id}: proposal sentence claims completion")
            elif fact.status == "stated":
                if fact.note in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
                    if "facility" not in sentence.casefold():
                        failures.append(f"{fact.fact_id}: facility statement lacks 'facility'")
                    if fact.note == FACILITY_CAPACITY_INCREASE and "increased" not in sentence:
                        failures.append(f"{fact.fact_id}: increase is not stated as an increase")
                    if "lent" in sentence.casefold():
                        failures.append(f"{fact.fact_id}: facility capacity is described as lent")
                if fact.negated and not re.search(r"\b(not|never)\b", sentence):
                    failures.append(f"{fact.fact_id}: negated proposition lacks negation")
                if fact.speaker_id is not None and world.display_name(
                        fact.speaker_id) not in sentence:
                    failures.append(f"{fact.fact_id}: speaker is not named in the text")
            if fact.speaker_id is not None and fact.speaker_id not in world.entities:
                failures.append(f"{fact.fact_id}: unknown speaker")

    for plan in plans:
        gold = gold_by_filename.get(plan.filename)
        if gold is None:
            continue
        for proposition in gold.propositions:
            checked += 1
            fact = facts.get(proposition.id)
            if fact is None:
                failures.append(f"{plan.filename}: gold proposition {proposition.id} "
                                "has no source fact")
                continue
            if proposition.status != fact.status:
                failures.append(f"{plan.filename}: proposition {proposition.id} status "
                                "does not match the source fact")
            if proposition.speaker != fact.speaker_id:
                failures.append(f"{plan.filename}: proposition {proposition.id} speaker "
                                "does not match the source fact")

    for query in dataset.queries:
        if query.query_type == "alleged_status":
            checked += 1
            facts_for_query = [facts[fid] for fid in query.gold_fact_ids if fid in facts]
            if len(facts_for_query) != 1 or facts_for_query[0].status != "alleged":
                failures.append(f"{query.query_id}: alleged-status query cites the wrong status")
                continue
            if query.expected_answer != "Alleged":
                failures.append(f"{query.query_id}: alleged-status answer is not Alleged")
            if "unnamed" not in query.notes.casefold():
                failures.append(f"{query.query_id}: notes do not distinguish reporter from speaker")
            if not any("alleged" in ev.quote for ev in query.supporting_evidence):
                failures.append(f"{query.query_id}: evidence does not say alleged")
        elif query.query_type == "proposed_vs_completed":
            checked += 1
            facts_for_query = [facts[fid] for fid in query.gold_fact_ids if fid in facts]
            if len(facts_for_query) != 1 or facts_for_query[0].status != "proposed":
                failures.append(f"{query.query_id}: proposal query cites the wrong status")
                continue
            fact = facts_for_query[0]
            if query.expected_answer == "No":
                if "closed-world" not in query.notes.casefold():
                    failures.append(f"{query.query_id}: closed-world assumption is not documented")
                match = re.match(r"The acquisition of (.+?) by (.+)$", fact.text or "")
                if not match:
                    failures.append(f"{query.query_id}: malformed proposal text")
                    continue
                target, acquirer = match.groups()
                for plan in plans:
                    for sentence in plan.sentences:
                        lowered = sentence.casefold()
                        if (f"acquisition of {target}" in sentence
                                and f"by {acquirer}" in sentence
                                and ("completed" in lowered or "completion" in lowered
                                     or "acquired" in lowered)):
                            failures.append(
                                f"{query.query_id}: corpus contains completion language for "
                                f"{target}")
            elif not any("proposed" in ev.quote for ev in query.supporting_evidence):
                failures.append(f"{query.query_id}: proposal evidence lacks 'proposed'")

    for plan in plans:
        for link in plan.links:
            if link.type != "conditional_on":
                continue
            checked += 1
            if link.src not in facts or link.dst not in facts:
                failures.append(f"{plan.document_id}: conditional link references missing facts")
                continue
            if facts[link.src].status != "proposed":
                failures.append(f"{plan.document_id}: conditional source is not proposed")
            if not facts[link.dst].negated:
                failures.append(f"{plan.document_id}: conditional target is not negated")
    return checked, failures


def check_entity_identity(world: World, plans: Sequence[DocumentPlan],
                          dataset: QADataset, golds=None, *,
                          surfaces: _SurfaceIndex | None = None
                          ) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    index = surfaces if surfaces is not None else _SurfaceIndex.build(world)
    namesakes = set(world.duplicate_names)
    other_surface = {
        world.duplicate_names[0]: world.display_name(world.duplicate_names[1]),
        world.duplicate_names[1]: world.display_name(world.duplicate_names[0]),
    }

    for plan in plans:
        for spec in plan.mentions:
            checked += 1
            if spec.entity_id in namesakes:
                expected_surface = world.display_name(spec.entity_id)
                if spec.surface != expected_surface:
                    failures.append(
                        f"{plan.document_id}: namesake {spec.entity_id} mention "
                        f"{spec.surface!r} is not qualified as {expected_surface!r}")
                if other_surface[spec.entity_id] in spec.surface:
                    failures.append(
                        f"{plan.document_id}: namesake {spec.entity_id} uses the other "
                        "person's qualifier")
            else:
                resolved = _resolve_entity_ids(world, spec.surface, index)
                if resolved != [spec.entity_id]:
                    failures.append(
                        f"{plan.document_id}: mention {spec.surface!r} does not resolve "
                        f"uniquely to {spec.entity_id}")
        # Check the raw sentence text, not just the mention list.  Qualified
        # surfaces contain the bare name, so remove all qualifiers first.
        for sentence in plan.sentences:
            if _unqualified_namesake(world, sentence) is not None:
                failures.append(
                    f"{plan.document_id}: unqualified namesake mention in {sentence!r}")
        if plan.identity_traps:
            a, b, _ = plan.identity_traps[0]
            if {a, b} != namesakes:
                failures.append(f"{plan.document_id}: identity trap does not pair the namesakes")
            if not any(flag.kind == "identity_ambiguity"
                       and set(flag.object_ids) == namesakes for flag in plan.flags):
                failures.append(f"{plan.document_id}: namesake identity flag is missing")

    gold_by_filename = _gold_by_filename(golds)
    for plan in plans:
        gold = gold_by_filename.get(plan.filename)
        if gold is None:
            continue
        # Only documents that carry the identity trap mention the namesakes.
        # Requiring both names in every v2 document would be a false failure:
        # the trap is deliberately document-specific.
        if not plan.identity_traps:
            continue
        gold_entities = {entity.id: entity for entity in gold.entities}
        for entity_id in namesakes:
            checked += 1
            entity = gold_entities.get(entity_id)
            if entity is None:
                failures.append(f"{plan.filename}: namesake {entity_id} is missing from gold")
                continue
            if entity.name != world.entity(entity_id).name:
                failures.append(f"{plan.filename}: namesake {entity_id} name mismatch")
            qualified = world.display_name(entity_id)
            for mention in entity.mentions:
                if qualified not in mention.quote:
                    failures.append(
                        f"{plan.filename}: namesake {entity_id} gold mention is unqualified")
                if other_surface[entity_id] in mention.quote:
                    failures.append(
                        f"{plan.filename}: namesake {entity_id} gold mention uses the "
                        "other qualifier")
        if not any({trap.a, trap.b} == namesakes for trap in gold.identity_traps):
            failures.append(f"{plan.filename}: gold identity trap is missing")
        if not any(flag.kind == "identity_ambiguity"
                   and set(flag.object_ids) == namesakes for flag in gold.expected_flags):
            failures.append(f"{plan.filename}: gold identity flag is missing or unpaired")

    for query in dataset.queries:
        if query.answer_type == "entity":
            checked += 1
            matches = _resolve_entity_ids(world, query.expected_answer, index)
            if len(matches) != 1:
                failures.append(
                    f"{query.query_id}: entity answer {query.expected_answer!r} is ambiguous")
        checked += 1
        if _unqualified_namesake(world, query.question) is not None:
            failures.append(
                f"{query.query_id}: question uses an unqualified namesake mention")
    return checked, failures


def check_transactions(world: World, plans: Sequence[DocumentPlan],
                       dataset: QADataset, golds=None, *,
                       statements: dict[str, list[Statement]] | None = None,
                       ) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    facts = _facts_by_id(plans)
    if statements is None:
        statements = _statements_by_doc(plans, world)
    occurrence_by_fact: dict[str, list[DocumentPlan]] = defaultdict(list)
    for plan in plans:
        for stated in plan.stated_facts:
            occurrence_by_fact[stated.fact.fact_id].append(plan)
    occurrences = occurrence_by_fact

    world_facts = {fact.fact_id: fact for fact in world.facts}
    eligible_loans = {
        fact.fact_id for fact in world.facts
        if fact.kind == "edge" and fact.relation == "lent_to"
        and fact.amount_gbp is not None and fact.date is not None and not fact.negated
        and fact.note not in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE)
    }
    transaction_facts = {transaction.fact_id for transaction in world.transactions}

    for transaction in world.transactions:
        checked += 1
        fact = world_facts.get(transaction.fact_id)
        if fact is None:
            failures.append(f"{transaction.transaction_id}: unknown fact")
            continue
        if transaction.kind != "loan" or transaction.status != "completed":
            failures.append(f"{transaction.transaction_id}: non-completed or non-loan")
        if fact.kind != "edge" or fact.relation != "lent_to" or fact.negated:
            failures.append(f"{transaction.transaction_id}: fact is not a completed loan")
        if fact.note in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
            failures.append(f"{transaction.transaction_id}: facility is treated as a loan")
        for plan in occurrences.get(fact.fact_id, []):
            if not any(statement.relation == "lent_to"
                       and statement.subject_id == fact.subject_id
                       and statement.object_id == fact.object_id
                       and statement.amount_gbp == fact.amount_gbp
                       for statement in statements.get(plan.document_id, [])):
                failures.append(
                    f"{transaction.transaction_id}: {plan.document_id} does not describe "
                    "a completed loan")

    missing = eligible_loans - transaction_facts
    if missing:
        failures.append(f"loans without completed transactions: {sorted(missing)}")
    extra = transaction_facts - eligible_loans
    if extra:
        failures.append(f"transactions without eligible loans: {sorted(extra)}")

    for fact in world.facts:
        if fact.kind == "proposition" and fact.note in (
                FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
            checked += 1
            if fact.fact_id in transaction_facts:
                failures.append(f"{fact.fact_id}: facility capacity has a completed transaction")
        if fact.kind == "edge" and fact.relation == "owes" and fact.amount_gbp is not None:
            checked += 1
            if fact.fact_id in transaction_facts:
                failures.append(f"{fact.fact_id}: outstanding balance is treated as a payment")
            for plan in occurrences.get(fact.fact_id, []):
                if not any(statement.relation == "owes"
                           and statement.subject_id == fact.subject_id
                           and statement.object_id == fact.object_id
                           and statement.amount_gbp == fact.amount_gbp
                           for statement in statements.get(plan.document_id, [])):
                    failures.append(f"{fact.fact_id}: balance is not described as owed")

    for query in dataset.queries:
        if query.query_type not in ("financial", "provenance"):
            continue
        lowered = query.question.casefold()
        if "lend" not in lowered:
            continue
        checked += 1
        facts_for_query = [facts[fid] for fid in query.gold_fact_ids if fid in facts]
        if len(facts_for_query) != 1:
            failures.append(f"{query.query_id}: lending question needs one fact")
            continue
        fact = facts_for_query[0]
        if fact.fact_id not in transaction_facts:
            failures.append(f"{query.query_id}: lending answer has no completed transaction")
        if fact.note in (FACILITY_CAPACITY_ORIGINAL, FACILITY_CAPACITY_INCREASE):
            failures.append(f"{query.query_id}: facility is described as a loan")
    return checked, failures


def check_contradictions(world: World, plans: Sequence[DocumentPlan],
                         dataset: QADataset, golds=None, *,
                         statements: dict[str, list[Statement]] | None = None,
                         ) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    facts = _facts_by_id(plans)
    if statements is None:
        statements = _statements_by_doc(plans, world)
    plans_by_filename = {plan.filename: plan for plan in plans}
    gold_by_filename = _gold_by_filename(golds)

    for contradiction in world.contradictions:
        checked += 1
        fact_a = world.fact(contradiction.fact_a_id)
        fact_b = world.fact(contradiction.fact_b_id)
        if (fact_a.subject_id, fact_a.relation, fact_a.object_id, fact_a.date) != (
                fact_b.subject_id, fact_b.relation, fact_b.object_id, fact_b.date):
            failures.append(f"{contradiction.contradiction_id}: not a same-event conflict")
        if fact_a.amount_gbp == fact_b.amount_gbp:
            failures.append(f"{contradiction.contradiction_id}: amounts do not conflict")

        docs_a = [plan for plan in plans if any(
            s.fact.fact_id == fact_a.fact_id for s in plan.stated_facts)]
        docs_b = [plan for plan in plans if any(
            s.fact.fact_id == fact_b.fact_id for s in plan.stated_facts)]
        if not docs_a or not docs_b:
            failures.append(f"{contradiction.contradiction_id}: one side is not documented")
            continue
        if {plan.document_id for plan in docs_a} & {plan.document_id for plan in docs_b}:
            failures.append(f"{contradiction.contradiction_id}: both sides in one document")

        for fact, docs in ((fact_a, docs_a), (fact_b, docs_b)):
            for plan in docs:
                if not any(statement.relation == "lent_to"
                           and statement.subject_id == fact.subject_id
                           and statement.object_id == fact.object_id
                           and statement.amount_gbp == fact.amount_gbp
                           for statement in statements.get(plan.document_id, [])):
                    failures.append(
                        f"{contradiction.contradiction_id}: {plan.document_id} does not "
                        "state the conflicting amount")

        queries = [query for query in dataset.queries
                   if query.query_type == "contradiction"
                   and set(query.gold_fact_ids) == {fact_a.fact_id, fact_b.fact_id}]
        if len(queries) != 1:
            failures.append(f"{contradiction.contradiction_id}: expected one contradiction query")
        else:
            query = queries[0]
            evidence_docs = {evidence.document_id for evidence in query.supporting_evidence}
            if len(evidence_docs) != 2:
                failures.append(f"{query.query_id}: contradiction needs two source documents")
            for amount in (fact_a.amount_gbp or 0, fact_b.amount_gbp or 0):
                if _money(amount) not in query.expected_answer:
                    failures.append(f"{query.query_id}: answer omits one conflicting amount")
            bank = world.entity(fact_a.subject_id or "")
            company = world.entity(fact_a.object_id or "")
            if bank.name not in query.question or company.name not in query.question:
                failures.append(f"{query.query_id}: question does not identify the parties")
            if format_date(fact_a.date or dt.date.min) not in query.question:
                failures.append(f"{query.query_id}: question does not identify the date")

        # Paired flags: each side names its counterpart and the counterpart's file.
        for fact, docs in ((fact_a, docs_a), (fact_b, docs_b)):
            companion = fact_b if fact is fact_a else fact_a
            for plan in docs:
                checked += 1
                gold = gold_by_filename.get(plan.filename)
                if gold is None:
                    continue
                flags = [flag for flag in gold.expected_flags
                         if flag.kind == "conflict"
                         and flag.object_ids == [fact.fact_id]]
                if len(flags) != 1:
                    failures.append(
                        f"{plan.filename}: missing paired conflict flag for {fact.fact_id}")
                    continue
                note = flags[0].note
                if companion.fact_id not in note:
                    failures.append(
                        f"{plan.filename}: conflict flag does not name companion "
                        f"{companion.fact_id}")
                companion_plan = next(
                    (candidate for candidate in plans
                     if candidate.filename in note and candidate.document_id != plan.document_id),
                    None)
                if companion_plan is None:
                    failures.append(f"{plan.filename}: conflict flag does not name the "
                                    "companion document")
                    continue
                companion_gold = gold_by_filename.get(companion_plan.filename)
                if companion_gold is None:
                    continue
                reciprocal = [flag for flag in companion_gold.expected_flags
                              if flag.kind == "conflict"
                              and flag.object_ids == [companion.fact_id]]
                if len(reciprocal) != 1:
                    failures.append(f"{companion_plan.filename}: conflict flag is not reciprocal")
                elif fact.fact_id not in reciprocal[0].note:
                    failures.append(f"{companion_plan.filename}: reciprocal flag is unpaired")
                elif plan.filename not in reciprocal[0].note:
                    failures.append(f"{companion_plan.filename}: reciprocal flag names the "
                                    "wrong document")
    return checked, failures


# --------------------------------------------------------------------------- unanswerable proofs
def _proof_attribute_mentioned(fact: Fact, attribute: str) -> bool:
    tokens = {
        "auditor": ("auditor", "audited"),
        "vat_registration_number": ("vat", "registration number"),
    }.get(attribute, (attribute.replace("_", " "),))
    haystack = f"{fact.note} {fact.text or ''}".casefold()
    return any(re.search(rf"\b{re.escape(token)}\b", haystack) for token in tokens)


def check_unanswerable_proofs(world: World, plans: Sequence[DocumentPlan],
                              dataset: QADataset, golds=None) -> tuple[int, list[str]]:
    """Validate every unanswerable claim against the structural fact index."""
    failures: list[str] = []
    checked = 0
    fact_index = FactIndex.build(world, plans)
    known_documents = {document.document_id for document in dataset.documents}
    attribute_mentions: dict[tuple[str, str], bool] = {}

    for query in dataset.queries:
        if query.answerable:
            if (query.unanswerable_proof is not None
                    or query.unanswerable_kind is not None):
                failures.append(
                    f"{query.query_id}: answerable query carries an unanswerable proof")
            continue
        checked += 1
        proof = query.unanswerable_proof
        if proof is None:
            failures.append(f"{query.query_id}: unanswerable query has no structured proof")
            continue
        if proof.kind != query.unanswerable_kind:
            failures.append(f"{query.query_id}: proof kind does not match query kind")
        if proof.subject_id not in world.entities:
            failures.append(f"{query.query_id}: proof cites unknown subject")
            continue
        missing_facts = [fact_id for fact_id in proof.checked_fact_ids
                         if fact_id not in fact_index.facts]
        if missing_facts:
            failures.append(
                f"{query.query_id}: proof cites unknown facts {missing_facts}")
        unknown_documents = [doc_id for doc_id in proof.checked_document_ids
                             if doc_id not in known_documents]
        if unknown_documents:
            failures.append(
                f"{query.query_id}: proof cites unknown documents {unknown_documents}")

        subject = proof.subject_id
        if proof.kind == "absent_attribute":
            if not proof.attribute:
                failures.append(f"{query.query_id}: absent-attribute proof has no attribute")
                continue
            if proof.attribute in fact_index.attributes.get(subject, {}):
                failures.append(
                    f"{query.query_id}: attribute {proof.attribute!r} is present on the subject")
            cache_key = (subject, proof.attribute)
            if cache_key not in attribute_mentions:
                attribute_mentions[cache_key] = any(
                    _proof_attribute_mentioned(fact, proof.attribute)
                    for fact in fact_index.facts_for_entity(subject))
            if attribute_mentions[cache_key]:
                failures.append(
                    f"{query.query_id}: a fact mentions attribute {proof.attribute!r}")
            if not proof.checked_fact_ids:
                failures.append(
                    f"{query.query_id}: absent-attribute proof has no distractor facts")

        elif proof.kind == "absent_relationship":
            if not proof.relation:
                failures.append(f"{query.query_id}: absent-relation proof has no relation")
                continue
            if fact_index.has_relation(subject, proof.relation, include_negated=True):
                failures.append(
                    f"{query.query_id}: relation {proof.relation!r} is present on the subject")
            distractors = [fact_index.facts[fid] for fid in proof.checked_fact_ids
                           if fid in fact_index.facts]
            if not any(fact.relation != proof.relation for fact in distractors):
                failures.append(
                    f"{query.query_id}: absent-relation proof has no plausible distractor")

        elif proof.kind == "unsupported_historical_date":
            if not proof.reference_date:
                failures.append(f"{query.query_id}: historical proof has no reference date")
                continue
            try:
                reference = dt.date.fromisoformat(proof.reference_date)
            except ValueError:
                failures.append(f"{query.query_id}: historical proof has a bad reference date")
                continue
            chairs = [fact for fact in fact_index.facts.values()
                      if fact.kind == "edge" and fact.relation == "chair_of"
                      and fact.object_id == subject and not fact.negated]
            if not chairs:
                failures.append(
                    f"{query.query_id}: historical proof subject has no chair facts")
                continue
            if not any(fact.fact_id in proof.checked_fact_ids for fact in chairs):
                failures.append(
                    f"{query.query_id}: historical proof omits the chair facts it checked")
            for fact in chairs:
                if (fact.date is not None and fact.date <= reference
                        and (fact.end_date is None or fact.end_date >= reference)):
                    failures.append(
                        f"{query.query_id}: chair fact {fact.fact_id} covers the asked date")

        elif proof.kind == "absent_financial_information":
            if fact_index.has_dividend(subject):
                failures.append(
                    f"{query.query_id}: a dividend fact exists for the subject")
            financial = [fact for fact in fact_index.financial_facts(subject)
                         if fact.amount_gbp is not None]
            if not financial:
                failures.append(
                    f"{query.query_id}: absent-financial proof has no distractor amounts")
            elif not any(fact.fact_id in proof.checked_fact_ids for fact in financial):
                failures.append(
                    f"{query.query_id}: absent-financial proof omits its distractor facts")

        elif proof.kind == "false_presupposition":
            if not proof.object_id:
                failures.append(f"{query.query_id}: false-presupposition proof has no target")
                continue
            if proof.object_id not in world.entities:
                failures.append(f"{query.query_id}: false-presupposition target is unknown")
                continue
            if fact_index.has_relation(subject, "owns", proof.object_id,
                                       include_negated=True):
                failures.append(
                    f"{query.query_id}: target is actually owned by the subject")
            if fact_index.has_relation(proof.object_id, "subsidiary_of", subject,
                                       include_negated=True):
                failures.append(
                    f"{query.query_id}: target is actually a subsidiary of the subject")
            if any(fact.relation in ("acquired", "merged_with")
                   and fact.subject_id == subject and fact.object_id == proof.object_id
                   for fact in fact_index.facts.values()):
                failures.append(
                    f"{query.query_id}: an acquisition edge supports the presupposition")
            if not proof.checked_fact_ids:
                failures.append(
                    f"{query.query_id}: false-presupposition proof has no checked facts")
        else:  # pragma: no cover - schema Literal rejects unknown kinds
            failures.append(f"{query.query_id}: unknown unanswerable kind {proof.kind!r}")
    return checked, failures


# --------------------------------------------------------------------------- entry point
def assert_semantics(world: World, plans: Sequence[DocumentPlan],
                     dataset: QADataset, golds=None) -> SemanticReport:
    """Run every semantic check and raise on the first non-empty failure set."""
    checks: dict[str, int] = {}
    failures: list[str] = []
    # Build the surface index and recovered statements once: five validators
    # need them, and both are pure functions of the world and plans.
    surfaces = _SurfaceIndex.build(world)
    statements = _statements_by_doc(plans, world, surfaces)
    validator_kwargs: dict[str, dict[str, object]] = {
        "relationship_direction": {"surfaces": surfaces, "statements": statements},
        "unique_answers": {"surfaces": surfaces, "statements": statements},
        "entity_identity": {"surfaces": surfaces},
        "transactions": {"statements": statements},
        "contradictions": {"statements": statements},
    }
    validators = [
        ("evidence_quotes", check_evidence_quotes),
        ("relationship_direction", check_relationship_direction),
        ("unique_answers", check_unique_answers),
        ("temporal_support", check_temporal_support),
        ("proposition_statuses", check_proposition_statuses),
        ("entity_identity", check_entity_identity),
        ("transactions", check_transactions),
        ("contradictions", check_contradictions),
    ]
    # V1 has no structured proofs and must keep its frozen manifest contract.
    # V2 always carries proofs, so the independent proof validator runs.
    if any(query.unanswerable_proof is not None for query in dataset.queries):
        validators.append(("unanswerable_proofs", check_unanswerable_proofs))
    for name, validator in validators:
        checked, found = validator(world, plans, dataset, golds,
                                   **validator_kwargs.get(name, {}))
        checks[name] = checked
        failures.extend(found)
    report = SemanticReport(checks=checks, failures=failures)
    if not report.ok:
        preview = "; ".join(report.failures[:8])
        raise SemanticValidationError(
            f"semantic validation failed with {len(report.failures)} issue(s): {preview}")
    return report
