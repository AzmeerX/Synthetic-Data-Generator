"""Depth, diversity and contradiction metrics computed from plans (§10.4, §11, §11.3).

No rendering and no parsing: every number here is derived from the canonical
documents and the semantic world, which is exactly what Phase B can measure.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from synthetic_data.model import CdmDocument, Table, TableCell

from .config import DEPTH_TARGETS, PROSE_TYPES
from .plan import CorpusPlan

__all__ = [
    "DocMetrics",
    "CorpusMetrics",
    "compute_metrics",
    "document_metrics",
    "diversity_metrics",
    "contradiction_metrics",
    "text_stats",
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MONTHS = ("January|February|March|April|May|June|July|August|September|"
           "October|November|December")
_DATE_LONG = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)? (?:{_MONTHS}) \d{{4}}\b")
_DATE_SHORT = re.compile(rf"\b\d{{1,2}} (?:{_MONTHS[:3]}|{_MONTHS}) \d{{4}}\b")
_MONEY = re.compile(r"(?:[£$€]\s?)?\b\d[\d,]*(?:\.\d+)?\b")
_PERCENT = re.compile(r"\b\d+(?:\.\d+)?%")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


@dataclass
class DocMetrics:
    document_id: str
    doc_type: str
    words: int
    sentences: int
    paragraphs: int
    sections: int
    tables: int
    table_rows: list[int]
    table_columns: list[int]
    facts: int
    entities: int
    transactions: int
    cross_refs: int
    fingerprint: str
    sentences_text: list[str] = field(default_factory=list)


@dataclass
class CorpusMetrics:
    documents: dict[str, DocMetrics]
    by_type: dict[str, dict[str, float]]
    diversity: dict[str, float]
    contradictions: dict[str, object]
    depth_failures: list[str]
    prose_count: int
    workbook_count: int


def text_stats(doc: CdmDocument) -> tuple[int, int, int, list[str]]:
    """Words, sentences, paragraph blocks and sentence strings for a document."""
    chunks: list[str] = []
    paragraphs = 0
    for section in doc.sections:
        for block in section.blocks:
            if block.kind == "paragraph":
                paragraphs += 1
            for run in block.runs or []:
                if run.text:
                    chunks.append(run.text)
            for item in block.items or []:
                for run in item.runs:
                    if run.text:
                        chunks.append(run.text)
            if block.table is not None:
                for row in [*block.table.header_rows, *block.table.body_rows,
                            *block.table.total_rows]:
                    for cell in row.cells:
                        if cell.text:
                            chunks.append(cell.text)
                        elif cell.display:
                            chunks.append(cell.display)
    sheets = getattr(getattr(doc, "workbook", None), "sheets", [])
    for sheet in sheets:
        for block in sheet.blocks:
            if block.kind == "paragraph":
                paragraphs += 1
            for run in block.runs or []:
                if run.text:
                    chunks.append(run.text)
        for table in sheet.tables:
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                for cell in row.cells:
                    if cell.text:
                        chunks.append(cell.text)
                    elif cell.display:
                        chunks.append(cell.display)
    text = " ".join(chunks)
    sentences = [piece for piece in _SENTENCE_SPLIT.split(text) if piece.strip()]
    words = len(_WORD.findall(text))
    return words, len(sentences), paragraphs, sentences


def _tables(doc: CdmDocument) -> list[Table]:
    out = [block.table for section in doc.sections for block in section.blocks
           if block.table is not None]
    sheets = getattr(getattr(doc, "workbook", None), "sheets", [])
    for sheet in sheets:
        out.extend(sheet.tables)
        out.extend(block.table for block in sheet.blocks if block.table is not None)
    return out


def _prose_sentences(doc: CdmDocument) -> list[str]:
    """Sentences from prose blocks only; a table cell is not a sentence (§10.4)."""
    chunks: list[str] = []
    for section in doc.sections:
        for block in section.blocks:
            for run in block.runs or []:
                if run.text:
                    chunks.append(run.text)
            for item in block.items or []:
                for run in item.runs:
                    if run.text:
                        chunks.append(run.text)
    text = " ".join(chunk for chunk in chunks if chunk)
    return [piece for piece in _SENTENCE_SPLIT.split(text) if piece.strip()]


def _entities(doc: CdmDocument) -> set[str]:
    seen: set[str] = set()
    for statement in doc.statements:
        seen.update(statement.mention_ids)
    for run in doc.iter_runs():
        if run.entity_ref is not None:
            seen.add(run.entity_ref.entity_id)
    for table in _tables(doc):
        for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
            for cell in row.cells:
                if cell.entity_ref is not None:
                    seen.add(cell.entity_ref.entity_id)
    return seen


def document_metrics(plan: CorpusPlan, doc: CdmDocument) -> DocMetrics:
    words, sentences, paragraphs, texts = text_stats(doc)
    tables = _tables(doc)
    fact_ids = {statement.fact_id for statement in doc.statements}
    txn_facts = {txn.fact_id for txn in plan.world.transactions.values()}
    return DocMetrics(
        document_id=doc.document_id, doc_type=doc.doc_type,
        words=words, sentences=sentences, paragraphs=paragraphs,
        sections=len(doc.sections), tables=len(tables),
        table_rows=[len(table.body_rows) + len(table.total_rows) for table in tables],
        table_columns=[len(table.columns) for table in tables],
        facts=len(fact_ids), entities=len(_entities(doc)),
        transactions=len(fact_ids & txn_facts),
        cross_refs=sum(1 for ref in plan.world.cross_references
                       if ref.src_document == doc.document_id),
        fingerprint=doc.plan_fingerprint, sentences_text=_prose_sentences(doc))


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _entropy(counter: Counter) -> float:
    total = sum(counter.values())
    if total <= 1:
        return 0.0
    entropy = 0.0
    for count in counter.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


def _mask(text: str, entity_names: list[str]) -> str:
    masked = text
    for name in entity_names:
        masked = masked.replace(name, "<ENT>")
    masked = _ISO_DATE.sub("<DATE>", masked)
    masked = _DATE_LONG.sub("<DATE>", masked)
    masked = _DATE_SHORT.sub("<DATE>", masked)
    masked = _PERCENT.sub("<PCT>", masked)
    masked = _MONEY.sub("<NUM>", masked)
    return " ".join(masked.lower().split())


def diversity_metrics(plan: CorpusPlan, rows: dict[str, DocMetrics]) -> dict[str, float]:
    prose = {doc_id: row for doc_id, row in rows.items() if doc_id in set(plan.prose_ids)}
    documents = [plan.documents[doc_id] for doc_id in prose]
    total_sentences = sum(row.sentences for row in prose.values())
    entity_names = sorted({entity.canonical_name for entity in plan.world.entities.values()},
                          key=len, reverse=True)

    masked: list[str] = []
    sentence_docs: dict[str, set[str]] = defaultdict(set)
    for doc_id, row in prose.items():
        for sentence in row.sentences_text:
            masked.append(_mask(sentence, entity_names))
            # boilerplate is an *exact* sentence in two documents; two different
            # amounts that mask to one template are not boilerplate
            sentence_docs[" ".join(sentence.lower().split())].add(doc_id)
    unique = len(set(masked))

    fingerprints = Counter(row.fingerprint for row in prose.values())
    duplicate_signature = sum(count for count in fingerprints.values() if count > 1)

    # information shape: relation/argument-type sequence per document
    shapes = []
    for doc in documents:
        shape = []
        for statement in doc.statements:
            fact = plan.world.facts[statement.fact_id]
            subject = (plan.world.entities[fact.subject.entity_id].subtype
                       if fact.subject and fact.subject.entity_id in plan.world.entities
                       else None)
            obj = (plan.world.entities[fact.object.entity_id].subtype
                   if fact.object and fact.object.entity_id in plan.world.entities
                   else None)
            shape.append((fact.relation or fact.status, subject, obj))
        shapes.append(tuple(shape))
    shape_unique = len(set(shapes))

    # section order entropy, per prose type
    orders: dict[str, Counter] = defaultdict(Counter)
    for doc in documents:
        orders[doc.doc_type][tuple(section.heading for section in doc.sections)] += 1
    entropies = [entropy for entropy in (_entropy(counter) for counter in orders.values())
                 if entropy > 0]
    section_entropy = statistics.mean(entropies) if entropies else 0.0

    # length spread per type
    spreads = []
    for doc_type in PROSE_TYPES:
        lengths = [row.words for row in prose.values() if row.doc_type == doc_type]
        if len(lengths) >= 2:
            median = statistics.median(lengths)
            spreads.append((_percentile(lengths, 0.95) - _percentile(lengths, 0.05)) / median
                           if median else 0.0)
    length_spread = min(spreads) if spreads else 0.0

    # table shape diversity, measured per type: how many documents of a type
    # present a table layout no sibling presents
    per_type_shapes: dict[str, list[tuple]] = defaultdict(list)
    for row in prose.values():
        tables = _tables(plan.documents[row.document_id])
        shape = tuple(sorted(
            (len(table.columns),
             tuple(column.type for column in table.columns),
             "small" if len(table.body_rows) + len(table.total_rows) < 5
             else "medium" if len(table.body_rows) + len(table.total_rows) <= 10
             else "large")
            for table in tables))
        per_type_shapes[row.doc_type].append(shape)
    ratios = [len(set(shapes)) / len(shapes) for shapes in per_type_shapes.values()
              if len(shapes) >= 2]
    table_diversity = statistics.mean(ratios) if ratios else 1.0

    # thread shape diversity
    thread_shapes = []
    for thread in plan.world.threads.values():
        metas = [plan.documents[doc_id].metadata.thread for doc_id in thread.messages]
        by_message = {meta.message_id: meta.sequence for meta in metas if meta}
        replies = tuple(by_message.get(meta.in_reply_to, 0)
                        for meta in metas if meta and meta.in_reply_to)
        citations = tuple(len(meta.references) for meta in metas if meta)
        if len(thread.messages) >= 2:      # a single letter carries no thread claims
            thread_shapes.append((len(thread.messages), replies, citations))
    thread_diversity = (len(set(thread_shapes)) / len(thread_shapes)) if thread_shapes else 1.0

    return {
        "documents": len(prose),
        "sentences": total_sentences,
        "unique_templates": unique,
        "template_duplication_pct": round(100 * (1 - unique / total_sentences), 2)
        if total_sentences else 0.0,
        "unique_templates_per_1000_docs": round(1000 * unique / len(prose), 1) if prose else 0.0,
        "templates_per_document": round(unique / len(prose), 2) if prose else 0.0,
        "boilerplate_pct": round(100 * sum(1 for key in masked if len(sentence_docs[key]) > 1)
                                 / len(masked), 2) if masked else 0.0,
        "max_documents_sharing_one_sentence": max(
            (len(docs) for docs in sentence_docs.values()), default=0),
        "structural_signature_duplication_pct": round(
            100 * duplicate_signature / len(prose), 2) if prose else 0.0,
        "information_shape_diversity": round(shape_unique / len(prose), 3) if prose else 0.0,
        "section_order_entropy_bits": round(section_entropy, 3),
        "length_spread_min": round(length_spread, 3),
        "table_shape_diversity": round(table_diversity, 3),
        "thread_shape_diversity": round(thread_diversity, 3),
        "threads": len(thread_shapes),
    }


def contradiction_metrics(plan: CorpusPlan) -> dict[str, object]:
    world = plan.world
    docs = plan.documents
    statements: dict[str, set[str]] = defaultdict(set)
    for doc in docs.values():
        for statement in doc.statements:
            statements[statement.fact_id].add(doc.document_id)
    rows = []
    for contradiction in world.contradictions.values():
        fact_a = world.facts[contradiction.fact_a]
        fact_b = world.facts[contradiction.fact_b]
        docs_a = statements.get(fact_a.fact_id, set())
        docs_b = statements.get(fact_b.fact_id, set())
        grounded = bool(docs_a and docs_b and docs_a.isdisjoint(docs_b))
        rows.append({
            "contradiction_id": contradiction.contradiction_id,
            "dimension": contradiction.dimension,
            "documents": list(contradiction.documents),
            "types": sorted({docs[doc_id].doc_type for doc_id in contradiction.documents
                             if doc_id in docs}),
            "grounded": grounded,
            "fact_a_stated_in": sorted(docs_a),
            "fact_b_stated_in": sorted(docs_b),
        })
    total_documents = len(docs)
    required = math.ceil(total_documents / 50) if total_documents else 0
    dimensions = Counter(row["dimension"] for row in rows)
    pairs = Counter(tuple(row["types"]) for row in rows)
    type_set = sorted({doc_type for row in rows for doc_type in row["types"]})
    share = (max(dimensions.values()) / len(rows)) if rows else 0.0
    return {
        "required": required,
        "planned": len(rows),
        "total_documents": total_documents,
        "density_satisfied": len(rows) >= required,
        "dimensions": dict(dimensions),
        "max_dimension_share": round(share, 3),
        "dimension_share_ok": share <= 0.4 or len(rows) < 3,
        "types": type_set,
        "types_ok": len(type_set) >= 3 or len(rows) < 3,
        "pairs": [list(pair) for pair in pairs],
        "no_repeated_pair": all(count == 1 for count in pairs.values()),
        "grounded": all(row["grounded"] for row in rows),
        "rows": rows,
    }


def compute_metrics(plan: CorpusPlan) -> CorpusMetrics:
    rows = {doc_id: document_metrics(plan, doc)
            for doc_id, doc in plan.documents.items()}
    by_type: dict[str, dict[str, float]] = {}
    failures: list[str] = []
    for doc_type in PROSE_TYPES:
        type_rows = [row for row in rows.values() if row.doc_type == doc_type]
        if not type_rows:
            continue
        target = DEPTH_TARGETS[doc_type]
        summary = {
            "documents": len(type_rows),
            "words_mean": round(statistics.mean(row.words for row in type_rows), 1),
            "words_p05": _percentile([row.words for row in type_rows], 0.05),
            "words_p95": _percentile([row.words for row in type_rows], 0.95),
            "words_min": min(row.words for row in type_rows),
            "words_max": max(row.words for row in type_rows),
            "sentences_mean": round(statistics.mean(row.sentences for row in type_rows), 1),
            "paragraphs_mean": round(statistics.mean(row.paragraphs for row in type_rows), 1),
            "sections_mean": round(statistics.mean(row.sections for row in type_rows), 1),
            "tables_mean": round(statistics.mean(row.tables for row in type_rows), 1),
            "facts_mean": round(statistics.mean(row.facts for row in type_rows), 1),
            "entities_mean": round(statistics.mean(row.entities for row in type_rows), 1),
            "transactions_mean": round(statistics.mean(row.transactions for row in type_rows), 1),
            "cross_refs_mean": round(statistics.mean(row.cross_refs for row in type_rows), 1),
            "median_table_rows": statistics.median(
                [value for row in type_rows for value in (row.table_rows or [0])]),
            "median_table_columns": statistics.median(
                [value for row in type_rows for value in (row.table_columns or [0])]),
        }
        by_type[doc_type] = summary
        for row in type_rows:
            checks = (
                ("words", row.words, target.words),
                ("sections", row.sections, target.sections),
                ("paragraphs", row.paragraphs, target.paragraphs),
                ("sentences", row.sentences, target.sentences),
                ("tables", row.tables, target.tables),
                ("entities", row.entities, target.entities),
                ("facts", row.facts, target.facts),
                ("transactions", row.transactions, target.transactions),
                ("cross_refs", row.cross_refs, target.cross_refs),
            )
            for name, value, (low, high) in checks:
                if not low <= value <= high:
                    failures.append(
                        f"{row.document_id} ({doc_type}) {name}={value} outside [{low}, {high}]")
    return CorpusMetrics(
        documents=rows, by_type=by_type,
        diversity=diversity_metrics(plan, rows),
        contradictions=contradiction_metrics(plan),
        depth_failures=failures,
        prose_count=len(plan.prose_ids),
        workbook_count=len(plan.workbook_ids))
