"""Question taxonomy and caps for the V3 QA benchmark (Phase H).

The answerable types are the capabilities the architecture requires a question
generator to demonstrate over rich documents; the five unanswerable kinds are the
established V2 categories, kept because a benchmark without defensible negatives
measures nothing but template recall.

Every type names the evidence shape it needs, which is what stops a generator from
asking something the documents cannot support: a type whose required shape is
absent from a document is simply not generated there.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["QUESTION_TYPES", "UNANSWERABLE_KINDS", "ANSWER_KINDS", "Caps",
           "QuestionSpec", "SPECS", "cap_for", "relation_phrase", "relation_base",
           "COPULAR_RELATIONS"]


@dataclass(frozen=True)
class QuestionSpec:
    question_type: str
    answer_kind: str
    requires: tuple[str, ...]
    per_document_cap: int
    multi_document: bool = False


QUESTION_TYPES = (
    "entity_lookup",
    "relationship_lookup",
    "ownership_lookup",
    "financial",
    "temporal",
    "historical_change",
    "multi_hop",
    "identity_resolution",
    "negation",
    "proposed_vs_completed",
    "alleged_vs_established",
    "contradiction_resolution",
    "provenance",
    "workbook_derived",
    "workbook_cell",
    "unanswerable",
)

UNANSWERABLE_KINDS = (
    "absent_attribute",
    "absent_relationship",
    "unsupported_historical_date",
    "absent_financial_information",
    "false_presupposition",
)

ANSWER_KINDS = ("entity", "money", "percent", "date", "text", "boolean", "count",
                "document", "location")

SPECS: dict[str, QuestionSpec] = {
    "entity_lookup": QuestionSpec("entity_lookup", "entity", ("edge",), 3),
    "relationship_lookup": QuestionSpec("relationship_lookup", "text", ("edge",), 4),
    "ownership_lookup": QuestionSpec("ownership_lookup", "entity",
                                     ("edge", "percent"), 2),
    "financial": QuestionSpec("financial", "money", ("amount",), 4),
    "temporal": QuestionSpec("temporal", "date", ("date",), 4),
    "historical_change": QuestionSpec("historical_change", "text",
                                      ("amount", "sequence"), 1),
    "multi_hop": QuestionSpec("multi_hop", "entity", ("edge", "edge"), 2,
                              multi_document=True),
    "identity_resolution": QuestionSpec("identity_resolution", "entity",
                                        ("edge", "names"), 1),
    "negation": QuestionSpec("negation", "boolean", ("negative_status",), 2),
    "proposed_vs_completed": QuestionSpec("proposed_vs_completed", "text",
                                          ("status",), 2),
    "alleged_vs_established": QuestionSpec("alleged_vs_established", "text",
                                           ("status",), 2),
    "contradiction_resolution": QuestionSpec("contradiction_resolution", "text",
                                             ("contradiction",), 1,
                                             multi_document=True),
    "provenance": QuestionSpec("provenance", "location", ("render_loc",), 2),
    "workbook_derived": QuestionSpec("workbook_derived", "money", ("derived",), 4),
    "workbook_cell": QuestionSpec("workbook_cell", "money", ("cell",), 4),
    "unanswerable": QuestionSpec("unanswerable", "text", ("absence",), 6),
}

#: hard cap per document-format set, so a long agreement cannot dominate the corpus
DEFAULT_CAP = 12


@dataclass(frozen=True)
class Caps:
    per_type: dict[str, int]

    def for_type(self, question_type: str) -> int:
        return self.per_type.get(question_type,
                                 SPECS.get(question_type,
                                           QuestionSpec(question_type, "text", (), 1))
                                 .per_document_cap)


def cap_for(question_type: str) -> int:
    return SPECS[question_type].per_document_cap if question_type in SPECS else 1


_RELATION_PHRASES = {
    "owns": "owns",
    "is_subsidiary_of": "is a subsidiary of",
    "is_director_of": "is a director of",
    "chairs": "chairs",
    "advises": "advises",
    "represents": "represents",
    "lent_to": "lent to",
    "disbursed": "disbursed funds to",
    "repaid": "repaid",
    "owes": "owes",
    "guarantees": "guarantees",
    "grants_security_over": "grants security over",
    "acquired": "acquired",
    "attends": "attends",
    "served": "served",
    "filed": "filed",
    "settled": "settled",
    "ordered": "ordered",
}


#: base (infinitive) form, for questions built with do-support
_RELATION_BASE = {
    "owns": "own",
    "is_subsidiary_of": "be a subsidiary of",
    "is_director_of": "be a director of",
    "chairs": "chair",
    "advises": "advise",
    "represents": "represent",
    "lent_to": "lend to",
    "disbursed": "disburse funds to",
    "repaid": "repay",
    "owes": "owe",
    "guarantees": "guarantee",
    "grants_security_over": "grant security over",
    "acquired": "acquire",
    "attends": "attend",
    "served": "serve",
    "filed": "file",
    "settled": "settle",
    "ordered": "order",
}

#: relations whose phrase is copular ("is a subsidiary of") and take "is" questions
COPULAR_RELATIONS = frozenset({"is_subsidiary_of", "is_director_of"})


def relation_base(relation: str) -> str:
    return _RELATION_BASE.get(relation, relation.replace("_", " "))


def relation_phrase(relation: str) -> str:
    """A readable verb phrase for a relation, never a raw token in a question."""
    return _RELATION_PHRASES.get(relation, relation.replace("_", " "))
