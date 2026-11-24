"""Adapter: Datanaut run export → benchmark prediction bundle (Phase J).

Datanaut's export writer produces one dictionary per run:

    {"run": {...}, "nodes": [...], "edges": [...], "conflicts": [...],
     "rejected": [...], "coverage": {...}, "documents": [...], "passages": [...]}

with every node and edge carrying anchors (document id, page, char span, verbatim
quote, match kind).  This module maps that shape onto the versioned prediction
contract, so a run can be scored without touching the extraction code and without the
evaluator learning anything about Datanaut's internals.

What the export cannot supply is stated rather than guessed:

* there are no QA answers — Datanaut does not answer questions, so the `answers`
  array is empty and the QA metrics score as missing;
* amounts live in edge attributes under names the frame defines; the adapter reads a
  small, documented set and leaves the rest out;
* conflicts carry competing claims; they map to contradictions, not to answers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from synthetic_data.benchmark.schema import (PREDICTION_SCHEMA_VERSION,
                                             PredictedContradiction,
                                             PredictedDocument, PredictedEntity,
                                             PredictedEvidence, PredictedRelation,
                                             PredictionBundle)

__all__ = ["DatanautPredictionAdapter", "bundle_from_export", "document_from_export"]

_AMOUNT_KEYS = ("amount_minor", "amount_pence", "amount")
_CURRENCY_KEYS = ("currency", "ccy")
_STATUS_KEYS = ("status", "stance", "modality")
_DATE_KEYS = ("effective", "date", "date_start", "start")


def _evidence(anchors: list[dict[str, Any]]) -> list[PredictedEvidence]:
    evidence: list[PredictedEvidence] = []
    for anchor in anchors or []:
        quote = (anchor.get("quote") or "").strip()
        if not quote:
            continue
        page = anchor.get("page_no")
        evidence.append(PredictedEvidence(
            quote=quote,
            page=int(page) if isinstance(page, int) and page > 0 else None,
            char_start=anchor.get("char_start"),
            char_end=anchor.get("char_end")))
    return evidence


def _amount(attributes: dict[str, Any], currency: str | None
            ) -> tuple[int | None, str | None]:
    for key in _AMOUNT_KEYS:
        if key not in attributes:
            continue
        value = attributes[key]
        if isinstance(value, dict):                    # {"minor": ..., "currency": ...}
            minor = value.get("minor") or value.get("amount_minor")
            currency = value.get("currency") or currency
            if isinstance(minor, int):
                return minor, currency
            continue
        if isinstance(value, (int, float)):
            minor = int(round(float(value) * 100)) if key == "amount" else int(value)
            return minor, currency
        if isinstance(value, str):
            from synthetic_data.benchmark.metrics import find_money
            found = find_money(value)
            if found is not None:
                return found
    return None, currency


def _currency(attributes: dict[str, Any]) -> str | None:
    for key in _CURRENCY_KEYS:
        value = attributes.get(key)
        if isinstance(value, str) and len(value) == 3:
            return value.upper()
    return None


def _status(attributes: dict[str, Any]) -> str | None:
    for key in _STATUS_KEYS:
        value = attributes.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def document_from_export(export: dict[str, Any], document_id: str,
                         fmt: str) -> PredictedDocument:
    """One export dictionary → the predictions for one canonical document."""
    nodes = export.get("nodes") or []
    entities = [PredictedEntity(
        entity_id=str(node["id"]),
        name=str(node.get("canonical") or node.get("id")),
        entity_type=node.get("type"),
        aliases=[str(alias) for alias in (node.get("variants") or [])],
        mentions=_evidence(node.get("anchors") or []),
        confidence=node.get("confidence")) for node in nodes]

    known = {entity.entity_id for entity in entities}
    relations: list[PredictedRelation] = []
    for edge in export.get("edges") or []:
        subject, target = str(edge.get("source")), str(edge.get("target"))
        if subject not in known or target not in known:
            # an edge whose endpoint was rejected is not a prediction we can score
            continue
        attributes = edge.get("attributes") or {}
        currency = _currency(attributes)
        amount, currency = _amount(attributes, currency)
        if amount is not None and currency is None:
            amount = None                              # untyped without a currency
        dates = edge.get("dates") or {}
        effective = next((dates.get(key) for key in _DATE_KEYS
                          if isinstance(dates.get(key), str)), None)
        relations.append(PredictedRelation(
            subject=subject, object=target, relation=str(edge.get("type")),
            status=_status(attributes), negated=bool(attributes.get("negated")),
            amount_minor=amount, currency=currency, effective=effective,
            evidence=_evidence(edge.get("anchors") or []),
            confidence=edge.get("confidence")))

    contradictions = [PredictedContradiction(
        subject=str(conflict.get("subject") or conflict.get("subject_id") or ""),
        dimension=str(conflict.get("field") or "unknown"),
        values=[str(claim.get("value")) for claim in (conflict.get("claims") or [])
                if isinstance(claim, dict) and claim.get("value") is not None],
        documents=sorted({str(anchor.get("document_id"))
                          for claim in (conflict.get("claims") or [])
                          if isinstance(claim, dict)
                          for anchor in (claim.get("anchors") or [])}))
        for conflict in export.get("conflicts") or []]

    return PredictedDocument(document_id=document_id, format=fmt,
                             doc_type=None, entities=entities,
                             relations=relations, contradictions=contradictions,
                             answers=[])


class DatanautPredictionAdapter:
    """Reads Datanaut's export JSON and emits a prediction bundle.

    One run covers one document in an evaluation run (separate stores per format), so
    the canonical document id and format are supplied by the caller rather than
    guessed from the export.
    """

    def __init__(self, source: str | Path | dict[str, Any], document_id: str,
                 format: str, name: str | None = None) -> None:
        self.source = source
        self.document_id = document_id
        self.format = format
        if name:
            self.name = name
        elif isinstance(source, (str, Path)):
            self.name = f"datanaut:{Path(source).name}"
        else:
            self.name = "datanaut:payload"

    def export(self) -> dict[str, Any]:
        if isinstance(self.source, dict):
            return self.source
        return json.loads(Path(self.source).read_text(encoding="utf-8"))

    def load(self) -> PredictionBundle:
        return bundle_from_export(self.export(), self.document_id, self.format,
                                  adapter=self.name)


def bundle_from_export(export: dict[str, Any], document_id: str, format: str,
                       *, adapter: str = "datanaut",
                       corpus: str = "v3_pilot") -> PredictionBundle:
    document = document_from_export(export, document_id, format)
    return PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION,
                            corpus=corpus, adapter=adapter, documents=[document])
