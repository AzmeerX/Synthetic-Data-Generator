"""Schemas for the generated QA benchmark and for submitted predictions.

The dataset is intentionally conventional JSON: no model output is trusted as
ground truth, and every answer carries the document/sentence evidence it was
derived from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QueryType = Literal[
    "entity_lookup",
    "relationship_lookup",
    "financial",
    "temporal",
    "historical_change",
    "multi_hop",
    "identity_resolution",
    "negation",
    "proposed_vs_completed",
    "alleged_status",
    "contradiction",
    "provenance",
    "unanswerable",
]

AnswerType = Literal[
    "entity",
    "money",
    "date",
    "period",
    "boolean",
    "document",
    "contradiction",
    "text",
    "unanswerable",
]

UnanswerableKind = Literal[
    "absent_attribute",
    "absent_relationship",
    "unsupported_historical_date",
    "absent_financial_information",
    "false_presupposition",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnanswerableProof(_Strict):
    """Structured evidence that a question cannot be answered from the world.

    The proof is derived from the generator's fact index and entity/relation
    structure, not from keyword absence in the question.  ``checked_fact_ids``
    names the distractor facts that were inspected and found not to support the
    asked relation, attribute or date.
    """

    kind: UnanswerableKind
    subject_id: str
    relation: str | None = None
    object_id: str | None = None
    attribute: str | None = None
    reference_date: str | None = None
    checked_fact_ids: list[str] = Field(default_factory=list)
    checked_document_ids: list[str] = Field(default_factory=list)
    explanation: str


class QueryEvidence(_Strict):
    """One exact, verifiable piece of evidence in one generated document."""

    document_id: str = Field(description="Stable generator id, e.g. gen_doc_00000")
    filename: str
    text_sha256: str = Field(description="sha256 of the ingested canonical text")
    sentence_idx: int = Field(ge=0)
    sentence_id: str = Field(description="Portable Synth label, e.g. s7")
    quote: str


class QAQuery(_Strict):
    query_id: str
    question: str
    query_type: QueryType
    expected_answer: str
    answer_type: AnswerType
    answerable: bool
    supporting_evidence: list[QueryEvidence] = Field(default_factory=list)
    gold_fact_ids: list[str] = Field(default_factory=list)
    relevant_document_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    unanswerable_kind: UnanswerableKind | None = None
    unanswerable_proof: UnanswerableProof | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "QAQuery":
        if not self.question.strip().endswith("?"):
            raise ValueError(f"{self.query_id}: question must end with '?'")
        if not self.question.strip():
            raise ValueError(f"{self.query_id}: empty question")
        if self.answerable:
            if not self.expected_answer.strip():
                raise ValueError(f"{self.query_id}: answerable query without expected answer")
            if not self.supporting_evidence:
                raise ValueError(f"{self.query_id}: answerable query without evidence")
            evidence_docs = list(dict.fromkeys(e.document_id for e in self.supporting_evidence))
            if self.relevant_document_ids != evidence_docs:
                raise ValueError(
                    f"{self.query_id}: relevant_document_ids must match evidence documents")
            if self.answer_type == "unanswerable":
                raise ValueError(f"{self.query_id}: answerable query marked unanswerable")
        else:
            if self.expected_answer != "":
                raise ValueError(f"{self.query_id}: unanswerable query must have empty answer")
            if self.supporting_evidence:
                raise ValueError(f"{self.query_id}: unanswerable query must have no evidence")
            if self.gold_fact_ids:
                raise ValueError(f"{self.query_id}: unanswerable query must have no gold facts")
            if self.relevant_document_ids:
                raise ValueError(f"{self.query_id}: unanswerable query must name no documents")
            if self.answer_type != "unanswerable":
                raise ValueError(f"{self.query_id}: unanswerable query needs unanswerable type")
        if self.answerable:
            if self.unanswerable_kind is not None or self.unanswerable_proof is not None:
                raise ValueError(
                    f"{self.query_id}: answerable query carries an unanswerable proof")
        elif (self.unanswerable_kind is None) != (self.unanswerable_proof is None):
            raise ValueError(
                f"{self.query_id}: unanswerable kind and proof must be supplied together")
        elif self.unanswerable_proof is not None:
            if self.unanswerable_kind != self.unanswerable_proof.kind:
                raise ValueError(
                    f"{self.query_id}: unanswerable kind does not match proof kind")
        return self


class QADocument(_Strict):
    document_id: str
    filename: str
    text_sha256: str
    pages: int = Field(ge=1, le=5)
    doc_type: str
    doc_date: str
    sentence_count: int = Field(ge=1)


class QADataset(_Strict):
    version: int = 1
    seed: int
    target_pages: int
    actual_pages: int
    documents: list[QADocument]
    queries: list[QAQuery]
    notes: str = ""

    @model_validator(mode="after")
    def _document_consistency(self) -> "QADataset":
        ids = [d.document_id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate QADocument.document_id")
        known = set(ids)
        for query in self.queries:
            for ev in query.supporting_evidence:
                if ev.document_id not in known:
                    raise ValueError(
                        f"{query.query_id}: evidence document {ev.document_id!r} not in dataset")
        return self


class PredictionCitation(_Strict):
    """A citation submitted by a system under test.

    ``document_id`` and ``filename`` are alternative document identifiers.
    ``sentence_id`` is the portable ``sN`` label; ``sentence_idx`` is accepted
    as a fallback.
    """

    document_id: str | None = None
    filename: str | None = None
    sentence_id: str | None = None
    sentence_idx: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _identifiable(self) -> "PredictionCitation":
        if not self.document_id and not self.filename:
            raise ValueError("citation needs document_id or filename")
        if self.sentence_id is None and self.sentence_idx is None:
            raise ValueError("citation needs sentence_id or sentence_idx")
        return self


class QAPrediction(_Strict):
    query_id: str
    answer: str
    answerable: bool
    citations: list[PredictionCitation] = Field(default_factory=list)


class PredictionSet(_Strict):
    predictions: list[QAPrediction]
