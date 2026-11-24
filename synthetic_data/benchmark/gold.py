"""The gold side of the benchmark, read from published artifacts only.

Nothing here imports the generator: a gold document is built from the gold v2 JSON
and the QA JSON that a corpus directory already contains.  That keeps the evaluator
honest — it scores predictions against the same bytes a third party would read.

A note on entity names: gold v2 stores entity ids and the quotes their mentions
appear in, not a display name.  An entity is therefore identified by the text of its
mentions, which is exactly what a prediction can be matched against.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["GoldEvidence", "GoldEntity", "GoldRelation", "GoldProposition",
           "GoldContradiction", "GoldAnswer", "GoldDocument", "load_gold_corpus",
           "load_gold_document"]


@dataclass(frozen=True)
class GoldEvidence:
    quote: str
    sentence_id: str | None
    loc: dict[str, Any]
    fact_id: str
    alignment_kind: str | None = None
    currency: str | None = None

    @property
    def page(self) -> int | None:
        return self.loc.get("page")

    @property
    def sheet(self) -> str | None:
        return self.loc.get("sheet_name")

    @property
    def cell(self) -> str | None:
        return self.loc.get("cell_ref")

    @property
    def section(self) -> int | None:
        index = self.loc.get("section_index")
        return None if index is None else int(index) + 1

    @property
    def paragraph(self) -> int | None:
        return self.loc.get("paragraph_index")


@dataclass(frozen=True)
class GoldEntity:
    entity_id: str
    quotes: tuple[str, ...]

    def mentions(self, name: str) -> int:
        return sum(1 for quote in self.quotes if name in quote)


@dataclass(frozen=True)
class GoldRelation:
    fact_id: str
    subject: str
    relation: str
    object: str
    negated: bool
    status: str | None
    evidence: tuple[GoldEvidence, ...]
    #: ISO date the fact is anchored to, when the gold artifact records one
    effective: str | None = None


@dataclass(frozen=True)
class GoldProposition:
    fact_id: str
    text: str
    status: str | None
    negated: bool
    derived_from: tuple[str, ...]
    evidence: tuple[GoldEvidence, ...]


@dataclass(frozen=True)
class GoldContradiction:
    contradiction_id: str
    dimension: str
    documents: tuple[str, ...]
    fact_a: str
    fact_b: str


@dataclass(frozen=True)
class GoldAnswer:
    question_id: str
    document_id: str
    format: str
    question_type: str
    answerable: bool
    answer: str | None
    answer_kind: str | None
    fact_ids: tuple[str, ...]
    evidence: tuple[GoldEvidence, ...]
    unanswerable_kind: str | None = None


@dataclass
class GoldDocument:
    document_id: str
    format: str
    doc_type: str
    entities: list[GoldEntity] = field(default_factory=list)
    relations: list[GoldRelation] = field(default_factory=list)
    propositions: list[GoldProposition] = field(default_factory=list)
    contradictions: list[GoldContradiction] = field(default_factory=list)
    answers: list[GoldAnswer] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.document_id, self.format)


def _evidence(entries: Iterable[dict[str, Any]], fact_id: str) -> tuple[GoldEvidence, ...]:
    return tuple(GoldEvidence(
        quote=entry.get("quote", ""), sentence_id=entry.get("sentence_id"),
        loc=entry.get("render_loc") or {}, fact_id=fact_id,
        alignment_kind=entry.get("alignment_kind"),
        currency=entry.get("currency")) for entry in entries)


def _effective(time_refs: Any) -> str | None:
    """The anchor date of a fact, from the gold time references."""
    for item in time_refs or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value_start")
        if value and item.get("role") in {None, "occurred_at", "effective"}:
            return str(value)
    return None


def load_gold_document(payload: dict[str, Any]) -> GoldDocument:
    document = payload["doc"]
    entities = [GoldEntity(entity_id=entity["id"],
                           quotes=tuple(mention.get("quote", "")
                                        for mention in entity.get("mentions", [])))
                for entity in payload.get("entities", [])]
    relations = []
    for edge in payload.get("edges", []):
        if not edge.get("rel"):
            continue
        relations.append(GoldRelation(
            fact_id=edge.get("canonical_fact_id") or edge["id"],
            subject=edge["subject"], relation=edge["rel"], object=edge["object"],
            negated=bool(edge.get("negated")),
            status=edge.get("stance"),
            evidence=_evidence(edge.get("evidence", []),
                               edge.get("canonical_fact_id") or edge["id"]),
            effective=_effective(edge.get("time"))))
    propositions = [GoldProposition(
        fact_id=item.get("canonical_fact_id") or item["id"],
        text=item.get("text", ""), status=item.get("status"),
        negated=bool(item.get("negated")),
        derived_from=tuple(item.get("derived_from", [])),
        evidence=_evidence(item.get("evidence", []),
                           item.get("canonical_fact_id") or item["id"]))
        for item in payload.get("propositions", [])]
    contradictions = [GoldContradiction(
        contradiction_id=item["contradiction_id"], dimension=item["dimension"],
        documents=tuple(item.get("documents", [])), fact_a=item["fact_a"],
        fact_b=item["fact_b"]) for item in payload.get("contradictions", [])]
    return GoldDocument(document_id=document["document_id"], format=document["format"],
                        doc_type=document.get("doc_type", ""), entities=entities,
                        relations=relations, propositions=propositions,
                        contradictions=contradictions)


def load_gold_corpus(root: Path, qa_root: Path | None = None
                     ) -> dict[tuple[str, str], GoldDocument]:
    """Every gold document of a corpus, keyed by (document id, format)."""
    root = Path(root)
    documents: dict[tuple[str, str], GoldDocument] = {}
    portable = root / "manifest_portable.json"
    if portable.exists():
        manifest = json.loads(portable.read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            if entry["role"] != "v2":
                continue
            payload = json.loads((root / entry["path"]).read_text(encoding="utf-8"))
            document = load_gold_document(payload)
            documents[document.key] = document
    else:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            base = artifact["document_id"].replace(":", "_")
            name = f"{base}__{artifact['doc_type']}__{artifact['format']}.json"
            payload = json.loads((root / "gold" / "v2" / name).read_text(encoding="utf-8"))
            document = load_gold_document(payload)
            documents[document.key] = document
    if qa_root is not None:
        for path in sorted(Path(qa_root).glob("qa/*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = (payload["document_id"], payload["format"])
            document = documents.get(key)
            if document is None:
                continue
            document.answers = [_answer(item) for item in payload["questions"]]
    return documents


def _answer(item: dict[str, Any]) -> GoldAnswer:
    return GoldAnswer(
        question_id=item["question_id"], document_id=item["canonical_document_id"],
        format=item["format"], question_type=item["question_type"],
        answerable=item["answerable"], answer=item.get("expected_answer"),
        answer_kind=item.get("answer_kind"),
        fact_ids=tuple(item.get("fact_ids", [])),
        evidence=_evidence([{**evidence, "sentence_id": evidence.get("sentence_id")}
                            for evidence in item.get("evidence", [])],
                           item.get("fact_ids", [""])[0] if item.get("fact_ids") else ""),
        unanswerable_kind=item.get("unanswerable_kind"))
