"""Build the v1/v2 gold pair and the alignment record (§8.4, DEC-3).

The build is deliberately **one-way**: the v1 items are constructed from the
canonical world and the parser evidence, and the v2 artifact repeats the same items
plus provenance.  Nothing enriches v1 afterwards, so a v2 field can never carry a
claim that v1 would have rejected.

Per document and format:

1. align the canonical document to the parser output (prose: sentences; workbook:
   rows and cells through the render map);
2. ground every stated fact and every derived value, and refuse the build if any is
   missing, ambiguous, unsupported, altered, duplicated or extra (§8.5);
3. emit v1 (vendored ``GoldSet``), v2 (superset) and the alignment record;
4. write all three, or none of them.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synth.eval.gold import (
    GoldDoc,
    GoldEdge,
    GoldEntity,
    GoldEvidence,
    GoldFlag,
    GoldProposition,
    GoldSet,
    GoldTime,
    GoldTimeRef,
)

from synthetic_data.accounting import (
    AccountingReport,
    account_parser_content,
    require_full_accounting,
)
from synthetic_data.alignment import (
    AlignmentError,
    EVIDENCE_KINDS,
    FactEvidence,
    align_document,
    align_facts,
    require_fact_evidence,
)
from synthetic_data.canonical import CanonicalDocument
from synthetic_data.gold.defects import Defect, detect_defects
from synthetic_data.gold.model import (
    AlignmentArtifact,
    AlignmentSummary,
    AlignmentUnit,
    GoldContradiction,
    GoldDocV2,
    GoldEdgeV2,
    GoldEntityV2,
    GoldEvidenceV2,
    GoldPropositionV2,
    GoldTimeRefV2,
    GoldV2,
)
from synthetic_data.model import (
    CdmDocument,
    CdmWorkbook,
    IsoDate,
    Money,
    Percentage,
    RenderResult,
    SemanticFact,
)
from synthetic_data.workbook_alignment import (
    WorkbookAlignment,
    align_workbook,
    currency_in_quote,
    workbook_derived_evidence,
    workbook_fact_evidence,
)

__all__ = [
    "GoldBuildError",
    "GoldInputs",
    "GoldPair",
    "build_gold",
    "write_gold_pair",
    "gold_paths",
    "value_kind_of",
]

ANNOTATOR = "synthetic_data.generator"
MODE = "generated"
NOTES = ("Generated from the canonical model and grounded in the parser's own text. "
         "Not blind, not human-verified: grounding is asserted, annotation quality "
         "is not.")
PAGE_KINDS = {"physical", "logical"}
_CURRENCIES = ("GBP", "USD", "EUR", "JPY", "CHF")


class GoldBuildError(RuntimeError):
    """The document cannot be annotated without inventing evidence."""


@dataclass(frozen=True)
class GoldInputs:
    document: CdmDocument
    canonical: CanonicalDocument | None
    render: RenderResult
    view: Any                      # synth.modules.common.DocView
    parse_doc_id: str
    filename: str
    world: Any | None = None       # CorpusWorld
    seed: int | None = None


@dataclass
class GoldPair:
    v1: GoldSet
    v2: GoldV2
    alignment: AlignmentArtifact
    evidence: tuple[FactEvidence, ...] = ()
    defects: tuple[Defect, ...] = ()
    workbook_alignment: WorkbookAlignment | None = None
    accounting: AccountingReport | None = None


@dataclass
class _FactItem:
    fact: SemanticFact
    evidence: FactEvidence


@dataclass
class _EntityItem:
    entity: Any
    entity_id: str


@dataclass
class _DerivedItem:
    value: Any
    cell: Any
    evidence: FactEvidence


# ---------------------------------------------------------------------------
def build_gold(inputs: GoldInputs) -> GoldPair:
    document, view, render = inputs.document, inputs.view, inputs.render
    sentences = _sentence_map(view)
    text = view.text

    if document.doc_kind == "prose":
        if inputs.canonical is None:
            raise GoldBuildError("a prose document needs its canonical sentence layer")
        alignments = align_document(inputs.canonical, view)
        evidence = align_facts(document, inputs.canonical, alignments,
                               render.fact_surfaces, text)
        _require_grounding(evidence)
        units = _prose_units(alignments, render, text)
        workbook_alignment = None
        row_quotes: dict[str, str] = {}
        accounting = account_parser_content(document, inputs.canonical, view,
                                            alignments, fmt=render.format)
        contexts = {item.statement_id: item.quote for item in evidence}
    else:
        if not isinstance(document, CdmWorkbook):
            raise GoldBuildError("a workbook document must be a CdmWorkbook")
        workbook_alignment = align_workbook(document, render, view)
        if workbook_alignment.problems:
            raise GoldBuildError("; ".join(workbook_alignment.problems))
        asserted = workbook_fact_evidence(document, workbook_alignment)
        derived = workbook_derived_evidence(document, workbook_alignment)
        _require_grounding(asserted + derived)
        evidence = asserted + derived
        units = _workbook_units(workbook_alignment, render)
        accounting = account_parser_content(
            document, None, view, [],
            workbook_row_ids=[row.row_id for row in workbook_alignment.rows
                              if row.sentence_id],
            claimed_sentence_ids=[row.sentence_id for row in workbook_alignment.rows
                                  if row.sentence_id],
            fmt=render.format)
        # every node that can appear in evidence — row or cell — maps to its row text
        row_quotes = {row.row_id: row.quote for row in workbook_alignment.rows}
        row_quotes.update({cell.cell_id: cell.row_quote
                           for cell in workbook_alignment.cells})
        contexts = {}
        for statement in document.statements:
            row = workbook_alignment.row(statement.node_id)
            contexts[statement.statement_id] = row.quote if row is not None else ""
        for item in derived:
            contexts[item.statement_id] = item.quote

    try:
        require_full_accounting(accounting)
    except AlignmentError as exc:
        raise GoldBuildError(str(exc)) from exc

    typed = _typed_values(document, inputs.world)
    known_money = {f"{value.currency}:{value.amount_minor}"
                   for value in typed.values() if isinstance(value, Money)}
    defects = detect_defects(evidence, typed, known_money)
    if defects:
        raise GoldBuildError("\n".join(str(defect) for defect in defects))
    problems = _alignment_problems(evidence, contexts)
    if problems:
        raise GoldBuildError("\n".join(problems))

    world_facts = getattr(inputs.world, "facts", {}) or {}
    world_entities = getattr(inputs.world, "entities", {}) or {}

    doc_v1 = GoldDoc(filename=inputs.filename, doc_id=inputs.parse_doc_id,
                     text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    first_idx = min((row["idx"] for row in view.sentences), default=0)
    last_idx = max((row["idx"] for row in view.sentences), default=0)

    node_by_sentence = _node_by_sentence(
        inputs.canonical, alignments if document.doc_kind == "prose" else [],
        workbook_alignment)
    fact_items = _fact_items(document, evidence, world_facts)
    entity_items = _entity_items(document, fact_items, world_entities)
    derived_values = (document.workbook.derived_values
                      if isinstance(document, CdmWorkbook) else [])
    derived_items = _derived_items(document, derived_values, evidence)
    flags, contradictions = _contradictions(document, inputs.world)

    gold_v1 = GoldSet(
        version=1, doc=doc_v1, first_idx=first_idx, last_idx=last_idx,
        annotator=ANNOTATOR, mode=MODE, notes=NOTES,
        entities=[_v1_entity(item, view, render) for item in entity_items],
        edges=[_v1_edge(item, view, sentences) for item in fact_items
               if item.fact.kind == "edge"],
        propositions=[_v1_proposition(item, view, sentences) for item in fact_items
                      if item.fact.kind == "proposition"]
        + [_v1_derived(item) for item in derived_items],
        links=[], expected_flags=flags, identity_traps=[],
        time_refs=_v1_time_refs(fact_items, view, sentences))

    summary = _summary(evidence, units, render, view, row_quotes)
    doc_v2 = GoldDocV2(
        filename=inputs.filename, doc_id=inputs.parse_doc_id,
        text_sha256=doc_v1.text_sha256, document_id=document.document_id,
        doc_type=document.doc_type, doc_kind=document.doc_kind,
        format=render.format, render_sha256=render.sha256,
        pages=summary.pages, pageless=summary.pageless)

    gold_v2 = GoldV2(
        doc=doc_v2, first_idx=first_idx, last_idx=last_idx,
        annotator=ANNOTATOR, mode=MODE, notes=NOTES, corpus_seed=inputs.seed,
        entities=[_v2_entity(item, view, render, node_by_sentence)
                  for item in entity_items],
        edges=[_v2_edge(item, view, sentences, render, typed, row_quotes)
               for item in fact_items if item.fact.kind == "edge"],
        propositions=[_v2_proposition(item, view, sentences, render, typed, row_quotes)
                      for item in fact_items if item.fact.kind == "proposition"]
        + [_v2_derived(item, view, sentences, render, row_quotes)
           for item in derived_items],
        links=[], expected_flags=flags, identity_traps=[],
        contradictions=contradictions,
        time_refs=_v2_time_refs(fact_items, render, view, sentences),
        alignment=summary)

    alignment = AlignmentArtifact(
        document_id=document.document_id, format=render.format,
        parse_doc_id=inputs.parse_doc_id, render_sha256=render.sha256,
        page_kind=getattr(getattr(render, "capabilities", None), "page_kind", None),
        counts=_counts(units), units=units, problems=[])

    return GoldPair(v1=gold_v1, v2=gold_v2, alignment=alignment,
                    evidence=tuple(evidence), defects=(),
                    workbook_alignment=workbook_alignment, accounting=accounting)


# ---------------------------------------------------------------------------
def _node_by_sentence(canonical, alignments, workbook_alignment) -> dict[str, str]:
    """parser sentence id -> the canonical node (block, row, cell) that carries it.

    Canonical ids and parser ids live in different namespaces, so the bridge is the
    alignment record: it is the only place that knows both.
    """
    mapping: dict[str, str] = {}
    if canonical is not None:
        blocks = {sentence.sentence_id: sentence.block_id
                  for sentence in canonical.sentences}
        for alignment in alignments:
            node = blocks.get(alignment.canonical_id)
            if node is None:
                continue
            for sentence_id in alignment.sentence_ids:
                mapping[sentence_id] = node
    if workbook_alignment is not None:
        for row in workbook_alignment.rows:
            if row.sentence_id:
                mapping[row.sentence_id] = row.row_id
    return mapping


def _require_grounding(evidence: list[FactEvidence]) -> None:
    """Ground every fact, reporting a failed build as ``GoldBuildError``.

    ``require_fact_evidence`` raises ``AlignmentError`` because it is a low-level
    gate; the builder has one failure type for "this document cannot be annotated",
    so the message is preserved and the class is promoted here.
    """
    try:
        require_fact_evidence(evidence)
    except AlignmentError as exc:
        raise GoldBuildError(str(exc)) from exc


def _sentence_map(view) -> dict:
    return {row["sentence_id"]: row for row in view.sentences}


def _cells(document: CdmWorkbook):
    for sheet in document.workbook.sheets:
        for block in sheet.blocks:
            if block.table is not None:
                for row in [*block.table.header_rows, *block.table.body_rows,
                            *block.table.total_rows]:
                    yield from row.cells
        for table in sheet.tables:
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                yield from row.cells


def _typed_values(document, world) -> dict[str, object]:
    typed: dict[str, object] = {}
    for fact in (getattr(world, "facts", {}) or {}).values():
        if fact.amount is not None:
            typed[fact.fact_id] = fact.amount
    if isinstance(document, CdmWorkbook):
        cells = {cell.cell_id: cell for cell in _cells(document)}
        for value in document.workbook.derived_values:
            cell = cells.get(value.cell_id)
            if cell is not None and cell.value is not None:
                typed[value.fact_id] = cell.value
    return typed


def _alignment_problems(evidence: list[FactEvidence],
                        contexts: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for item in evidence:
        if not item.acceptable:
            problems.append(f"{item.statement_id}: {'; '.join(item.problems)}")
            continue
        for kind in item.kinds:
            if kind not in EVIDENCE_KINDS:
                problems.append(f"{item.statement_id}: aligned as {kind}")
        context = contexts.get(item.statement_id, item.quote)
        if not item.quote or item.quote not in context:
            problems.append(
                f"{item.statement_id}: quote {item.quote!r} is not inside the "
                f"sentence it is cited from")
    return problems


# ---------------------------------------------------------------------------
def _fact_items(document, evidence: list[FactEvidence], world_facts) -> list[_FactItem]:
    by_statement = {item.statement_id: item for item in evidence}
    items: list[_FactItem] = []
    seen: set[str] = set()
    for statement in document.statements:
        fact = world_facts.get(statement.fact_id)
        item = by_statement.get(statement.statement_id)
        if fact is None or item is None or fact.fact_id in seen:
            continue
        items.append(_FactItem(fact=fact, evidence=item))
        seen.add(fact.fact_id)
    return items


def _entity_items(document, fact_items: list[_FactItem], world_entities
                  ) -> list[_EntityItem]:
    entity_ids: list[str] = []
    for statement in document.statements:
        for entity_id in statement.mention_ids:
            if entity_id not in entity_ids:
                entity_ids.append(entity_id)
    for item in fact_items:
        for ref in (item.fact.subject, item.fact.object, item.fact.speaker):
            if ref is not None and ref.entity_id not in entity_ids:
                entity_ids.append(ref.entity_id)
    return [_EntityItem(entity=world_entities.get(entity_id), entity_id=entity_id)
            for entity_id in entity_ids]


def _derived_items(document, derived_values, evidence: list[FactEvidence]
                   ) -> list[_DerivedItem]:
    by_statement = {item.statement_id: item for item in evidence}
    cells = ({cell.cell_id: cell for cell in _cells(document)}
             if isinstance(document, CdmWorkbook) else {})
    items: list[_DerivedItem] = []
    for value in derived_values:
        item = by_statement.get(f"derived:{value.cell_id}")
        cell = cells.get(value.cell_id)
        if item is None or cell is None:
            continue
        items.append(_DerivedItem(value=value, cell=cell, evidence=item))
    return items


def _contradictions(document, world) -> tuple[list[GoldFlag], list[GoldContradiction]]:
    """Within-document contradictions are v1 flags; cross-document ones are v2 only.

    The vendored flag model requires both object ids to exist in the same file, so a
    contradiction whose other side is in another document cannot be a v1 flag
    without inventing an entity.  It is recorded in v2 with both fact ids and both
    document ids instead of being dropped.
    """
    stated = {statement.fact_id for statement in document.statements}
    flags: list[GoldFlag] = []
    contradictions: list[GoldContradiction] = []
    for contradiction in getattr(world, "contradictions", {}).values():
        here = [fact for fact in (contradiction.fact_a, contradiction.fact_b)
                if fact in stated]
        if not here:
            continue
        record = GoldContradiction(
            contradiction_id=contradiction.contradiction_id,
            fact_a=contradiction.fact_a, fact_b=contradiction.fact_b,
            dimension=contradiction.dimension,
            documents=list(contradiction.documents), stated_here=here)
        contradictions.append(record)
        if len(here) == 2:
            flags.append(GoldFlag(kind="contradiction",
                                  object_ids=list(here),
                                  note=f"both sides grounded here ({contradiction.dimension})"))
    return flags, contradictions


def _date_surface(quote: str, value: IsoDate) -> str | None:
    for candidate in (value.value.isoformat(), value.value.strftime("%d %B %Y"),
                      value.value.strftime("%d %b %Y")):
        if candidate in quote:
            return candidate
    return None


def _time_refs(fact_items: list[_FactItem]) -> list[tuple[_FactItem, str]]:
    out: list[tuple[_FactItem, str]] = []
    for item in fact_items:
        scope = item.fact.effective
        if scope is None or scope.start is None:
            continue
        surface = _date_surface(item.evidence.quote, scope.start)
        if surface is not None:
            out.append((item, surface))
    return out


# ---------------------------------------------------------------------------
# evidence references
# ---------------------------------------------------------------------------
def _references(item: FactEvidence, view, sentences: dict
                ) -> list[tuple[str, str, int]]:
    """``(sentence_id, exact substring, occurrence)`` for each sentence spanned."""
    text = view.text
    out: list[tuple[str, str, int]] = []
    for sentence_id in item.sentence_ids:
        row = sentences.get(sentence_id)
        if row is None:
            continue
        start, end = row["char_start"], row["char_end"]
        lo = max(item.char_start, start) if item.char_start is not None else start
        hi = min(item.char_end, end) if item.char_end is not None else end
        if hi <= lo:
            continue
        quote = text[lo:hi]
        if not quote.strip():
            continue
        body = text[start:end]
        out.append((sentence_id, quote, body[:lo - start].count(quote)))
    if not out:
        for sentence_id in item.sentence_ids[:1]:
            row = sentences.get(sentence_id)
            if row is not None:
                out.append((sentence_id, text[row["char_start"]:row["char_end"]], 0))
    return out


def _evidence_v1(item: FactEvidence, view, sentences) -> list[GoldEvidence]:
    return [GoldEvidence(sentence_id=sid, quote=quote, occurrence=occurrence)
            for sid, quote, occurrence in _references(item, view, sentences)]


def _evidence_v2(item: FactEvidence, view, sentences, render, typed,
                 row_quotes: dict[str, str] | None = None,
                 *, derived_from: list[str] | None = None) -> list[GoldEvidenceV2]:
    value = typed.get(item.fact_id)
    loc = _render_loc(render, item.node_id)
    row_quote = (row_quotes or {}).get(item.node_id, "")
    out: list[GoldEvidenceV2] = []
    for sid, quote, occurrence in _references(item, view, sentences):
        out.append(GoldEvidenceV2(
            sentence_id=sid, quote=quote, occurrence=occurrence,
            canonical_node_id=item.node_id or None,
            canonical_sentence_ids=list(item.canonical_ids),
            alignment_kind=item.kinds[0] if item.kinds else None,
            render_format=render.format, render_loc=loc,
            value_kind=value_kind_of(value),
            typed_value=_typed_surface(value),
            currency=value.currency if isinstance(value, Money) else None,
            currency_in_quote=(currency_in_quote(value.currency, quote)
                               if isinstance(value, Money) else None),
            currency_in_row=(currency_in_quote(value.currency, row_quote)
                             if isinstance(value, Money) and row_quote else None),
            derived_from=list(derived_from or [])))
    return out


def value_kind_of(value) -> str | None:
    if isinstance(value, Money):
        return "money"
    if isinstance(value, Percentage):
        return "percent"
    if isinstance(value, IsoDate):
        return "date"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "text" if value is not None else None


def _typed_surface(value) -> str | None:
    if isinstance(value, (Money, Percentage)):
        return value.display()
    if isinstance(value, IsoDate):
        return value.value.isoformat()
    return None if value is None else str(value)


def _node_with(render, text: str) -> str:
    for span in getattr(render, "spans", []):
        if span.text and text in span.text:
            return span.canonical_id
    return ""


def _render_loc(render, node_id: str) -> dict[str, Any]:
    for span in getattr(render, "spans", []):
        if span.canonical_id == node_id:
            return span.loc.model_dump(mode="json")
    return {}


# ---------------------------------------------------------------------------
def _prose_units(alignments, render, text: str) -> list[AlignmentUnit]:
    units: list[AlignmentUnit] = []
    for alignment in alignments:
        body = (text[alignment.char_start:alignment.char_end]
                if alignment.char_start is not None
                and alignment.char_end is not None else "")
        units.append(AlignmentUnit(
            canonical_id=alignment.canonical_id, kind=alignment.kind,
            parser_sentence_ids=list(alignment.sentence_ids),
            char_start=alignment.char_start, char_end=alignment.char_end,
            page=alignment.page, render_loc=_render_loc(render, alignment.canonical_id),
            text_sha256=(hashlib.sha256(body.encode("utf-8")).hexdigest()
                         if body else None),
            missing_row_structure=alignment.missing_row_structure))
    return units


def _workbook_units(alignment: WorkbookAlignment, render) -> list[AlignmentUnit]:
    units: list[AlignmentUnit] = []
    for row in alignment.rows:
        body = row.quote or ""
        units.append(AlignmentUnit(
            canonical_id=row.row_id, kind=row.kind,
            parser_sentence_ids=[row.sentence_id] if row.sentence_id else [],
            char_start=row.char_start, char_end=row.char_end, page=None,
            render_loc=_render_loc(render, row.row_id),
            text_sha256=(hashlib.sha256(body.encode("utf-8")).hexdigest()
                         if body else None)))
    return units


def _counts(units: list[AlignmentUnit]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit.kind] = counts.get(unit.kind, 0) + 1
    return counts


def _summary(evidence: list[FactEvidence], units: list[AlignmentUnit],
             render, view, row_quotes: dict[str, str] | None = None
             ) -> AlignmentSummary:
    page_kind = getattr(getattr(render, "capabilities", None), "page_kind", None)
    pages = sorted({row.get("page") for row in view.sentences
                    if row.get("page") is not None})
    spans = list(getattr(render, "spans", []))
    with_bbox = sum(1 for span in spans if getattr(span.loc, "bbox", None) is not None)
    stated, not_stated = _currency_counts(evidence, row_quotes)
    return AlignmentSummary(
        counts=_counts(units),
        cells=sum(1 for span in spans if span.kind == "cell"),
        rows=sum(1 for span in spans if span.kind == "row"),
        pages=len(pages) if page_kind in PAGE_KINDS else None,
        pageless=(page_kind not in PAGE_KINDS) if page_kind else None,
        bbox_coverage=(with_bbox / len(spans)) if spans else None,
        currency_stated=stated, currency_not_stated=not_stated)


def _currency_counts(evidence: list[FactEvidence],
                     row_quotes: dict[str, str] | None = None) -> tuple[int, int]:
    """How often the currency is *stated in parser text*, cell quote or row quote.

    A workbook cell quote is the amount alone — the currency lives in the row's
    Currency column.  Counting only the cell quote would report "no currency
    extracted" for a sheet that states it on every line, which is the opposite error
    to claiming a number format was extracted.
    """
    stated = not_stated = 0
    for item in evidence:
        row_quote = (row_quotes or {}).get(item.node_id, "")
        haystacks = [item.quote] + ([row_quote] if row_quote else [])
        if any(currency_in_quote(currency, haystack)
               for currency in _CURRENCIES for haystack in haystacks):
            stated += 1
        elif re.search(r"\d[\d,]*(?:\.\d+)?", item.quote):
            not_stated += 1
    return stated, not_stated


# ---------------------------------------------------------------------------
# v1 emitters
# ---------------------------------------------------------------------------
def _entity_shape(item: _EntityItem) -> tuple[str, str, str | None]:
    entity = item.entity
    if entity is None:
        return item.entity_id, "organization", None
    return entity.canonical_name, entity.entity_type, entity.subtype


def _mentions(name: str, view, render) -> list[GoldEvidence]:
    """The entity name, cited from the first parser sentence that states it.

    A name that no rendered span carries is not offered as a mention: a mention
    without a location cannot be resolved through the render map, and an evidence
    item that cannot be resolved is not evidence.
    """
    for row in view.sentences:
        body = view.text[row["char_start"]:row["char_end"]]
        position = body.find(name)
        if position < 0:
            continue
        if not any(name in span.text for span in getattr(render, "spans", [])):
            continue
        return [GoldEvidence(sentence_id=row["sentence_id"], quote=name,
                             occurrence=body[:position].count(name))]
    return []


def _v1_entity(item: _EntityItem, view, render) -> GoldEntity:
    name, cls, subtype = _entity_shape(item)
    return GoldEntity(id=item.entity_id, name=name, type=cls, subtype=subtype,
                      mentions=_mentions(name, view, render))


def _v1_edge(item: _FactItem, view, sentences) -> GoldEdge:
    fact = item.fact
    return GoldEdge(
        id=fact.fact_id, subject=fact.subject.entity_id, rel=fact.relation,
        object=fact.object.entity_id, stance="stated", negated=fact.negated,
        nested=fact.nested, time=_gold_times(fact),
        evidence=_evidence_v1(item.evidence, view, sentences))


def _v1_proposition(item: _FactItem, view, sentences) -> GoldProposition:
    fact = item.fact
    return GoldProposition(
        id=fact.fact_id, text=fact.text,
        speaker=fact.speaker.entity_id if fact.speaker else None,
        status=fact.status, negated=fact.negated,
        evidence=_evidence_v1(item.evidence, view, sentences))


def _v1_derived(item: _DerivedItem) -> GoldProposition:
    return GoldProposition(
        id=item.value.fact_id,
        text=f"{item.value.label}: {item.cell.display}",
        status="stated",
        evidence=[GoldEvidence(sentence_id=item.evidence.sentence_ids[0],
                               quote=item.evidence.quote)])


def _gold_times(fact: SemanticFact) -> list[GoldTime]:
    scope = fact.effective
    if scope is None:
        return []
    return [GoldTime(role="occurred_at",
                     value_start=scope.start.value.isoformat() if scope.start else None,
                     value_end=scope.end.value.isoformat() if scope.end else None,
                     granularity=scope.granularity)]


def _v1_time_refs(fact_items: list[_FactItem], view, sentences) -> list[GoldTimeRef]:
    out: list[GoldTimeRef] = []
    for item, surface in _time_refs(fact_items):
        sentence_id = _sentence_with(item, surface, view, sentences)
        if sentence_id is None:
            continue
        out.append(GoldTimeRef(
            id=f"{item.fact.fact_id}:time", surface=surface, kind="INSTANT",
            value_start=item.fact.effective.start.value.isoformat(),
            granularity=item.fact.effective.granularity or "day",
            evidence=GoldEvidence(sentence_id=sentence_id, quote=surface)))
    return out


def _sentence_with(item: _FactItem, surface: str, view, sentences) -> str | None:
    """The cited sentence that actually contains the quote (a split span needs care)."""
    for sentence_id in item.evidence.sentence_ids:
        row = sentences.get(sentence_id)
        if row is not None and surface in view.text[row["char_start"]:row["char_end"]]:
            return sentence_id
    return None


# ---------------------------------------------------------------------------
# v2 emitters
# ---------------------------------------------------------------------------
def _v2_entity(item: _EntityItem, view, render, node_by_sentence: dict
               ) -> GoldEntityV2:
    name, cls, subtype = _entity_shape(item)
    mentions: list[GoldEvidenceV2] = []
    for mention in _mentions(name, view, render):
        node = node_by_sentence.get(mention.sentence_id, "") or _node_with(render, name)
        mentions.append(GoldEvidenceV2(
            sentence_id=mention.sentence_id, quote=mention.quote,
            occurrence=mention.occurrence, alignment_kind="exact",
            canonical_node_id=node or None,
            canonical_sentence_ids=[mention.sentence_id]
            if mention.sentence_id.startswith("snt:") else [],
            render_format=render.format,
            render_loc=_render_loc(render, node) if node else {}))
    return GoldEntityV2(id=item.entity_id, name=name, type=cls, subtype=subtype,
                        canonical_entity_id=item.entity_id, mentions=mentions)


def _v2_edge(item: _FactItem, view, sentences, render, typed, row_quotes
             ) -> GoldEdgeV2:
    fact = item.fact
    return GoldEdgeV2(
        id=fact.fact_id, subject=fact.subject.entity_id, rel=fact.relation,
        object=fact.object.entity_id, stance="stated", negated=fact.negated,
        nested=fact.nested, time=_gold_times(fact), canonical_fact_id=fact.fact_id,
        evidence=_evidence_v2(item.evidence, view, sentences, render, typed,
                              row_quotes))


def _v2_proposition(item: _FactItem, view, sentences, render, typed, row_quotes
                    ) -> GoldPropositionV2:
    fact = item.fact
    return GoldPropositionV2(
        id=fact.fact_id, text=fact.text,
        speaker=fact.speaker.entity_id if fact.speaker else None,
        status=fact.status, negated=fact.negated, canonical_fact_id=fact.fact_id,
        evidence=_evidence_v2(item.evidence, view, sentences, render, typed,
                              row_quotes))


def _v2_derived(item: _DerivedItem, view, sentences, render, row_quotes
                ) -> GoldPropositionV2:
    typed = {item.value.fact_id: item.cell.value}
    return GoldPropositionV2(
        id=item.value.fact_id,
        text=f"{item.value.label}: {item.cell.display}", status="stated",
        canonical_fact_id=item.value.fact_id,
        derived_from=list(item.value.inputs),
        evidence=_evidence_v2(item.evidence, view, sentences, render, typed,
                              row_quotes, derived_from=list(item.value.inputs)))


def _v2_time_refs(fact_items: list[_FactItem], render, view, sentences
                  ) -> list[GoldTimeRefV2]:
    out: list[GoldTimeRefV2] = []
    for item, surface in _time_refs(fact_items):
        sentence_id = _sentence_with(item, surface, view, sentences)
        if sentence_id is None:
            continue
        out.append(GoldTimeRefV2(
            id=f"{item.fact.fact_id}:time", surface=surface, kind="INSTANT",
            value_start=item.fact.effective.start.value.isoformat(),
            granularity=item.fact.effective.granularity or "day",
            evidence=GoldEvidenceV2(
                sentence_id=sentence_id, quote=surface,
                alignment_kind=item.evidence.kinds[0] if item.evidence.kinds else None,
                canonical_node_id=item.evidence.node_id or None,
                render_format=render.format, value_kind="date",
                typed_value=item.fact.effective.start.value.isoformat(),
                render_loc=_render_loc(render, item.evidence.node_id))))
    return out


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def gold_paths(root: str | Path, document_id: str, doc_type: str,
               fmt: str) -> tuple[Path, Path, Path]:
    root = Path(root)
    stem = f"{document_id.replace(':', '_')}__{doc_type}__{fmt}"
    return (root / "v1" / f"{stem}.json", root / "v2" / f"{stem}.json",
            root / "alignment" / f"{document_id.replace(':', '_')}__{fmt}.json")


def write_gold_pair(pair: GoldPair, root: str | Path, document_id: str,
                    doc_type: str, fmt: str) -> tuple[Path, Path, Path]:
    """Write v1, v2 and the alignment record atomically: all three, or none.

    A failure while *serialising* touches nothing on disk.  A failure while
    *publishing* restores the files that were already there: the previous artifacts
    are moved aside first and put back if any move fails, so a corpus can never end
    up with a v1 whose v2 was lost to a half-finished run.
    """
    v1_path, v2_path, alignment_path = gold_paths(root, document_id, doc_type, fmt)
    staging = Path(root) / ".staging" / f"{document_id.replace(':', '_')}__{fmt}"
    backup = staging / "previous"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    targets = (v1_path, v2_path, alignment_path)
    saved: list[tuple[Path, Path]] = []
    written: list[Path] = []
    try:
        _dump(pair.v1, staging / "v1.json")
        _dump(pair.v2, staging / "v2.json")
        _dump(pair.alignment, staging / "alignment.json")

        backup.mkdir(parents=True, exist_ok=True)
        for index, target in enumerate(targets):
            if target.exists():
                held = backup / f"{index}-{target.name}"
                shutil.move(str(target), str(held))
                saved.append((held, target))

        for source, target in ((staging / "v1.json", v1_path),
                               (staging / "v2.json", v2_path),
                               (staging / "alignment.json", alignment_path)):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            written.append(target)
    except Exception:
        for target in written:              # only what this run published
            target.unlink(missing_ok=True)
        for held, target in saved:          # put the previous corpus back
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(held), str(target))
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.parent.rmdir()          # keep the corpus free of empty staging dirs
        except OSError:
            pass
    return v1_path, v2_path, alignment_path


def _dump(model, path: Path) -> None:
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2,
                               ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
