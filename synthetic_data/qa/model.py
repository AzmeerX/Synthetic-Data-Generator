"""QA artifacts (Phase H): questions, answers, evidence and insufficiency proofs.

A QA set is per **document**, not per file: the same `question_id` appears in the
TXT, PDF and DOCX set of one canonical document, with the same expected answer and
format-specific evidence.  That is what makes cross-format comparison meaningful
instead of tripling the benchmark.

Rules encoded here rather than left to callers:

* an answerable question must state an answer and cite at least one piece of
  evidence; the evidence quote is a parser substring and the sentence id is the
  parser's (canonical ids travel in their own field);
* an unanswerable question must carry a proof and must not carry an answer;
* both carry the canonical fact ids they are about, so a fact keeps one identity
  across formats and across documents.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from synthetic_data.qa.taxonomy import ANSWER_KINDS, QUESTION_TYPES, UNANSWERABLE_KINDS

__all__ = ["QAEvidence", "TextSearch", "AbsenceCheck", "UnanswerableProof",
           "QAQuestion", "QASet", "QA_VERSION"]

QA_VERSION = 1


class _QA(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QAEvidence(_QA):
    """One exact parser quote that supports an answer."""

    sentence_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    canonical_node_id: str | None = None
    canonical_sentence_ids: list[str] = Field(default_factory=list)
    render_format: str = Field(min_length=2)
    render_loc: dict[str, Any] = Field(default_factory=dict)
    #: canonical facts this quote states (may be several in one row)
    fact_ids: list[str] = Field(default_factory=list)


class TextSearch(_QA):
    """One corpus-wide search that came back empty — the basis of an absence proof."""

    needle: str = Field(min_length=1)
    hits: int = Field(ge=0)
    documents: list[str] = Field(default_factory=list)
    scope: str = "corpus"


class AbsenceCheck(_QA):
    """Two entities named together, with the relation never mentioned.

    A substring search alone cannot express "these two are named in the same
    document but never as director and company"; this check states exactly that,
    and the validator recomputes both counts from the parser text.
    """

    subject: str = Field(min_length=1)
    object: str | None = None
    relation_words: list[str] = Field(min_length=1)
    shared_sentences: int = Field(default=0, ge=0)
    relation_mentions: int = Field(default=0, ge=0)
    documents: list[str] = Field(default_factory=list)


class UnanswerableProof(_QA):
    """Why the documents cannot answer the question, with the search that shows it."""

    kind: str
    subject: str
    predicate: str
    documents_searched: list[str] = Field(min_length=1)
    text_searches: list[TextSearch] = Field(default_factory=list)
    absence_checks: list[AbsenceCheck] = Field(default_factory=list)
    facts_considered: list[str] = Field(default_factory=list)
    #: something nearby that must not be mistaken for the answer
    nearest_alias: str | None = None
    nearest_document: str | None = None
    contradicted_by: list[str] = Field(default_factory=list)
    proposed_only: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _kind(self) -> "UnanswerableProof":
        if self.kind not in UNANSWERABLE_KINDS:
            raise ValueError(f"unknown unanswerable kind {self.kind!r}")
        if not self.text_searches and not self.absence_checks:
            raise ValueError(f"proof for {self.kind!r} cites no search and no check")
        if any(search.hits for search in self.text_searches):
            raise ValueError(
                f"proof for {self.kind!r} cites a search that found something: "
                f"{[s.needle for s in self.text_searches if s.hits]}")
        if any(check.relation_mentions for check in self.absence_checks):
            raise ValueError(
                f"proof for {self.kind!r} cites a co-occurrence check that found the "
                f"relation: {[c.subject for c in self.absence_checks if c.relation_mentions]}")
        return self


class QAQuestion(_QA):
    question_id: str = Field(min_length=1)
    canonical_document_id: str = Field(min_length=1)
    doc_type: str
    format: str
    question_type: Literal[
        "entity_lookup", "relationship_lookup", "ownership_lookup", "financial",
        "temporal", "historical_change", "multi_hop", "identity_resolution",
        "negation", "proposed_vs_completed", "alleged_vs_established",
        "contradiction_resolution", "provenance", "workbook_derived",
        "workbook_cell", "unanswerable"]
    question: str = Field(min_length=8)
    answerable: bool
    expected_answer: str | None = None
    answer_kind: Literal["entity", "money", "percent", "date", "text", "boolean",
                         "count", "document", "location"] | None = None
    fact_ids: list[str] = Field(default_factory=list)
    evidence: list[QAEvidence] = Field(default_factory=list)
    answer_documents: list[str] = Field(default_factory=list)
    unanswerable_kind: Literal["absent_attribute", "absent_relationship",
                               "unsupported_historical_date",
                               "absent_financial_information",
                               "false_presupposition"] | None = None
    proof: UnanswerableProof | None = None

    @model_validator(mode="after")
    def _shape(self) -> "QAQuestion":
        if self.question_type not in QUESTION_TYPES:
            raise ValueError(f"unknown question type {self.question_type!r}")
        if self.answerable:
            if not self.expected_answer:
                raise ValueError(f"{self.question_id}: an answerable question needs an answer")
            if not self.evidence:
                raise ValueError(f"{self.question_id}: an answerable question needs evidence")
            if self.unanswerable_kind or self.proof:
                raise ValueError(f"{self.question_id}: answerable question carries a proof")
            if self.answer_kind is None:
                raise ValueError(f"{self.question_id}: answerable question needs a kind")
        else:
            if self.expected_answer:
                raise ValueError(f"{self.question_id}: an unanswerable question states an answer")
            if self.proof is None or self.unanswerable_kind is None:
                raise ValueError(f"{self.question_id}: an unanswerable question needs a proof")
            if self.evidence:
                raise ValueError(
                    f"{self.question_id}: an unanswerable question must not cite evidence")
            if self.proof.kind != self.unanswerable_kind:
                raise ValueError(f"{self.question_id}: proof kind disagrees with the question")
        if self.answer_kind is not None and self.answer_kind not in ANSWER_KINDS:
            raise ValueError(f"{self.question_id}: unknown answer kind {self.answer_kind!r}")
        return self


class QASet(_QA):
    qa_version: int = QA_VERSION
    corpus: str
    seed: int
    document_id: str
    doc_type: str
    format: str
    questions: list[QAQuestion] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    #: candidates the generator refused, by reason (cap, duplicate, no evidence)
    rejected: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts(self) -> "QASet":
        counts: dict[str, int] = {}
        for question in self.questions:
            key = question.question_type if question.answerable else \
                f"unanswerable:{question.unanswerable_kind}"
            counts[key] = counts.get(key, 0) + 1
        if self.counts and self.counts != counts:
            raise ValueError(f"declared counts {self.counts} do not match {counts}")
        self.counts = counts
        return self

    @property
    def answerable(self) -> list[QAQuestion]:
        return [question for question in self.questions if question.answerable]

    @property
    def unanswerable(self) -> list[QAQuestion]:
        return [question for question in self.questions if not question.answerable]
