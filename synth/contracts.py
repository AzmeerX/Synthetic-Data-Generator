"""Contracts between modules and models: what each prompt must return.

Purpose: Pydantic v2 models for every structured model output (U5, U7, U10 and
the arm extractors), plus the GLiNER backend protocol. Models cite sentences by
label (``s41``) and quote verbatim; entities by prompt-local label (``E3``)
assigned by the who's-who index (U4); time references by ``T4``. Code maps
labels back to ids and grounds every quote (P1 §5). JSON schemas for the
gateway come from :func:`synth.gateway.strict_schema`.
Spec: P1 §3 (U5, U7, U10 out), §12 (gold).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    sentence_ids: list[str] = Field(default_factory=list)
    quote: str = ""


class TimeRole(_Strict):
    role: str          # occurred_at|valid_from|valid_to|as_of|deadline|reported_on
    ref: str           # prompt label T4


Stance = Literal["stated", "inferred"]


# ---------------------------------------------------------------- U5 micro LLM
class MicroEdgeOut(_Strict):
    src: str
    rel: str
    dst: str
    subclass: str | None = None
    desc: str = ""
    stance: Stance = "stated"
    negated: bool = False
    time: list[TimeRole] = Field(default_factory=list)
    conf: float = 0.5
    evidence: Evidence


class EntityFix(_Strict):
    entity: str
    type: str | None = None
    role: str | None = None
    role_org: str | None = None
    evidence: Evidence


class MissedEntity(_Strict):
    surface: str
    type: str
    evidence: Evidence


class U5Output(_Strict):
    edges: list[MicroEdgeOut] = Field(default_factory=list)
    entity_fixes: list[EntityFix] = Field(default_factory=list)
    missed_entities: list[MissedEntity] = Field(default_factory=list)
    fact_sentences: list[str] = Field(default_factory=list)
    nested_sentences: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- U7 T2 verifier
class EdgeBody(_Strict):
    src: str
    rel: str
    dst: str
    subclass: str | None = None
    desc: str = ""
    stance: Stance = "stated"
    negated: bool = False
    time: list[TimeRole] = Field(default_factory=list)


class VerifyOp(_Strict):
    op: Literal["ADD", "REMOVE", "CORRECT", "SPLIT_ENTITY", "MERGE_ENTITY"]
    target: str | None = None        # micro row id M17, or entity label E3, or null for ADD
    edge: EdgeBody | None = None
    conf: float = 0.5
    evidence: Evidence
    reason: str = ""


class PropositionOut(_Strict):
    pid: str
    text: str
    speaker: str | None = None
    status: str
    negated: bool = False
    evidence: Evidence


class LinkOut(_Strict):
    src: str
    dst: str
    type: str
    evidence: Evidence


class U7Output(_Strict):
    ops: list[VerifyOp] = Field(default_factory=list)
    propositions: list[PropositionOut] = Field(default_factory=list)
    links: list[LinkOut] = Field(default_factory=list)
    insufficient_context: bool = False
    context_request: str | None = None
    unexpressible_sentence_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- U10 frontier verdict
class ContextAsk(_Strict):
    kind: Literal["sentences", "entity", "edges", "timeline"]
    ids: list[str] = Field(default_factory=list)


class VerdictOut(_Strict):
    verdict: Literal["CONFIRM", "REJECT", "CORRECT", "MERGE", "KEEP_APART", "IRREDUCIBLE", "NEED_CONTEXT"]
    corrected_edge: EdgeBody | None = None
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.5
    context_request: ContextAsk | None = None


# ---------------------------------------------------------------- arm extraction (t2_only / t3_only)
class ArmEntity(_Strict):
    id: str                            # E1, E2, … local to the reply
    name: str
    type: str
    mentions: list[Evidence] = Field(default_factory=list)


class ArmOutput(_Strict):
    entities: list[ArmEntity] = Field(default_factory=list)
    edges: list[MicroEdgeOut] = Field(default_factory=list)
    propositions: list[PropositionOut] = Field(default_factory=list)
    links: list[LinkOut] = Field(default_factory=list)


# ---------------------------------------------------------------- GLiNER backend (U2)
@dataclass(frozen=True)
class GlinerSpan:
    start: int          # character offsets into the text passed in
    end: int
    text: str
    label: str
    score: float


@dataclass(frozen=True)
class GlinerRelation:
    head: GlinerSpan
    tail: GlinerSpan
    relation: str
    score: float


class GlinerBackend(Protocol):
    """GLiNER2 (live) or a deterministic fake (tests). Offsets are exact."""

    name: str

    def entities(self, text: str, labels: list[str], threshold: float) -> list[GlinerSpan]: ...

    def relations(self, text: str, relation_labels: list[str], entity_labels: list[str],
                  threshold: float) -> list[GlinerRelation]: ...
