"""The benchmark prediction contract (Phase I).

This is the *only* interface between an extraction system and the evaluator.  A
prediction bundle is plain JSON with a version number; the evaluator never imports
the generator, the semantic world or the parser.  That separation is the point: a
scorer that shares code with the thing it scores cannot catch its bugs.

Version 1 fields, all optional except where noted:

``document_id``     the canonical id from the corpus manifest (required)
``format``          txt / pdf / docx / xlsx (required)
``entities``        id, name, type, aliases, mentions, confidence
``relations``       subject, object, relation, status, negated, amount, effective
``attributes``      entity, name, typed value (text/money/percent/date/count)
``contradictions``  subject, dimension, competing values, documents
``answers``         question id, answer, answerable, evidence, confidence
``evidence``        quote, parser sentence id, page / section / paragraph / cell

Anything the system cannot produce is simply absent; absent is scored as *missing*,
never skipped.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["PREDICTION_SCHEMA_VERSION", "PredictedEvidence", "PredictedEntity",
           "PredictedRelation", "PredictedAttribute", "PredictedContradiction",
           "PredictedAnswer", "PredictedDocument", "PredictionBundle",
           "ANSWER_KINDS", "VALUE_KINDS"]

PREDICTION_SCHEMA_VERSION = 1

ANSWER_KINDS = ("entity", "money", "percent", "date", "text", "boolean", "count",
                "document", "location")
VALUE_KINDS = ("text", "money", "percent", "date", "count")


class _Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PredictedEvidence(_Prediction):
    quote: str = Field(min_length=1, max_length=4000)
    sentence_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    section: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class PredictedEntity(_Prediction):
    entity_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=400)
    entity_type: str | None = None
    aliases: list[str] = Field(default_factory=list)
    mentions: list[PredictedEvidence] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PredictedRelation(_Prediction):
    subject: str = Field(min_length=1, max_length=400)
    object: str = Field(min_length=1, max_length=400)
    relation: str = Field(min_length=1, max_length=80)
    status: str | None = None
    negated: bool = False
    amount_minor: int | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    effective: str | None = None
    evidence: list[PredictedEvidence] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _amount(self) -> "PredictedRelation":
        if self.amount_minor is not None and self.currency is None:
            raise ValueError(
                "a predicted amount must name its currency: an amount without one "
                "cannot be compared with a typed gold value")
        if self.effective is not None:
            import datetime
            try:
                datetime.date.fromisoformat(self.effective)
            except ValueError as error:
                raise ValueError(f"effective date {self.effective!r} is not ISO") from error
        return self


class PredictedAttribute(_Prediction):
    entity: str = Field(min_length=1, max_length=400)
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=400)
    value_kind: Literal["text", "money", "percent", "date", "count"]
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    evidence: list[PredictedEvidence] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _currency(self) -> "PredictedAttribute":
        if self.value_kind == "money" and self.currency is None:
            raise ValueError("a money attribute must name its currency")
        return self


class PredictedContradiction(_Prediction):
    subject: str = Field(min_length=1, max_length=400)
    dimension: str = Field(min_length=1, max_length=80)
    values: list[str] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)


class PredictedAnswer(_Prediction):
    question_id: str = Field(min_length=1, max_length=200)
    answer: str | None = Field(default=None, max_length=2000)
    answerable: bool = True
    evidence: list[PredictedEvidence] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _shape(self) -> "PredictedAnswer":
        if self.answerable and not self.answer:
            raise ValueError(
                f"{self.question_id}: an answerable prediction must carry an answer")
        if not self.answerable and self.answer:
            raise ValueError(
                f"{self.question_id}: an unanswerable prediction must not answer")
        return self


class PredictedDocument(_Prediction):
    document_id: str = Field(min_length=1, max_length=200)
    format: str = Field(min_length=2, max_length=8)
    doc_type: str | None = None
    entities: list[PredictedEntity] = Field(default_factory=list)
    relations: list[PredictedRelation] = Field(default_factory=list)
    attributes: list[PredictedAttribute] = Field(default_factory=list)
    contradictions: list[PredictedContradiction] = Field(default_factory=list)
    answers: list[PredictedAnswer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> "PredictedDocument":
        known = {entity.entity_id for entity in self.entities}
        known |= {entity.name for entity in self.entities}
        for relation in self.relations:
            for endpoint in (relation.subject, relation.object):
                if known and endpoint not in known:
                    raise ValueError(
                        f"{self.document_id}: relation endpoint {endpoint!r} is not a "
                        f"declared entity id or name")
        ids = [entity.entity_id for entity in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.document_id}: duplicate entity ids")
        return self


class PredictionBundle(_Prediction):
    schema_version: int
    corpus: str = ""
    adapter: str = "unknown"
    documents: list[PredictedDocument] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _version(self) -> "PredictionBundle":
        if self.schema_version != PREDICTION_SCHEMA_VERSION:
            raise ValueError(
                f"prediction schema {self.schema_version} is not supported; this "
                f"evaluator reads version {PREDICTION_SCHEMA_VERSION}")
        seen = set()
        for document in self.documents:
            key = (document.document_id, document.format)
            if key in seen:
                raise ValueError(f"duplicate prediction for {key}")
            seen.add(key)
        return self

    def keyed(self) -> dict[tuple[str, str], PredictedDocument]:
        return {(document.document_id, document.format): document
                for document in self.documents}


def empty_bundle(corpus: str = "", adapter: str = "empty") -> PredictionBundle:
    return PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION, corpus=corpus,
                            adapter=adapter, documents=[])
