"""Corpus assembly: world -> documents -> validated :class:`Corpus`.

Planning is the only stage that mutates anything: the world is built once, every
document is planned from it, and then each fact's ``statement_ids`` are filled in
from the statements the documents actually produced.  A fact that no document
states is a planning defect, and the planner raises rather than emitting it.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from synthetic_data.model import Corpus, CorpusWorld, CdmDocument

from .config import CorpusConfig, matter_sizes
from .planner import plan_document
from .workbook import WORKBOOK_FAMILIES, plan_workbook
from .world import Matter, build_world, contradiction_schedule

__all__ = ["CorpusPlan", "build_corpus", "plan_contradictions"]


@dataclass
class CorpusPlan:
    config: CorpusConfig
    corpus_id: str
    world: CorpusWorld
    documents: dict[str, CdmDocument]
    matters: list[Matter]
    document_types: dict[str, str]
    workbook_ids: list[str] = field(default_factory=list)

    # -- access ------------------------------------------------------------
    @property
    def prose_ids(self) -> list[str]:
        return [doc_id for doc_id, doc_type in self.document_types.items()
                if doc_id not in set(self.workbook_ids)]

    def by_type(self, doc_type: str) -> list[CdmDocument]:
        return [doc for doc in self.documents.values() if doc.doc_type == doc_type]

    def corpus(self) -> Corpus:
        return Corpus(corpus_id=self.corpus_id, seed=self.config.seed,
                      world=self.world, documents=self.documents)


def build_corpus(config: CorpusConfig) -> CorpusPlan:
    draft = build_world(config)
    documents: dict[str, CdmDocument] = {}
    document_types: dict[str, str] = {}
    counts: dict[str, int] = {}
    for _doc_id, _doc_type, _ in draft.document_slots:
        counts[_doc_type] = counts.get(_doc_type, 0) + 1
    seen: dict[str, int] = {}
    for doc_id, doc_type, _matter_index in draft.document_slots:
        matter = draft.matters[_matter_index]
        index = seen.get(doc_type, 0)
        seen[doc_type] = index + 1
        documents[doc_id] = plan_document(matter, doc_id, doc_type,
                                          draft.alloc, config,
                                          type_index=index, type_count=counts[doc_type])
        document_types[doc_id] = doc_type

    workbook_ids: list[str] = []
    if config.workbooks:
        ordinal = config.documents
        for index in range(config.workbooks):
            matter = draft.matters[index % len(draft.matters)]
            doc_id = draft.alloc.alloc("doc", f"{ordinal:05d}")
            ordinal += 1
            family = WORKBOOK_FAMILIES[index % len(WORKBOOK_FAMILIES)]
            documents[doc_id] = plan_workbook(matter, doc_id, draft.alloc,
                                              config, family)
            document_types[doc_id] = f"workbook:{family}"
            workbook_ids.append(doc_id)

    world = draft.world()
    statements: dict[str, list[str]] = defaultdict(list)
    for doc in documents.values():
        for statement in doc.statements:
            statements[statement.fact_id].append(statement.statement_id)
    facts = {fact_id: fact.model_copy(
        update={"statement_ids": statements.get(fact_id, [])})
        for fact_id, fact in world.facts.items()}
    orphaned = [fact_id for fact_id, fact in facts.items() if not fact.statement_ids]
    if orphaned:  # pragma: no cover - the planner asserts before this point
        raise AssertionError(f"{len(orphaned)} fact(s) are never stated: {orphaned[:3]}")
    world = world.model_copy(update={"facts": facts})

    return CorpusPlan(config=config, corpus_id=draft.corpus_id, world=world,
                      documents=documents, matters=draft.matters,
                      document_types=document_types, workbook_ids=workbook_ids)


def plan_contradictions(config: CorpusConfig) -> list[dict[str, object]]:
    """The contradiction schedule with the corpus facts it depends on.

    Computed from the configuration alone so the distribution rule can be checked
    at sizes (500 documents) where planning every document would be wasteful.
    """
    sizes = matter_sizes(config.documents, config.max_letters)
    schedule = contradiction_schedule(config, sizes)
    total = config.documents + config.workbooks
    required = 0 if not config.include_contradictions else math.ceil(total / 50)
    if not schedule:
        return []
    rows = []
    for index, (matter_index, type_a, type_b, dimension) in enumerate(schedule):
        rows.append({
            "index": index,
            "matter": matter_index,
            "type_a": type_a,
            "type_b": type_b,
            "dimension": dimension,
            "pair": (type_a, type_b),
        })
    return [{
        "required": required,
        "planned": len(rows),
        "documents": total,
        "matters": len(sizes),
        "schedule": rows,
        "types": sorted({row["type_a"] for row in rows} | {row["type_b"] for row in rows}),
        "dimensions": {dimension: sum(1 for row in rows if row["dimension"] == dimension)
                       for dimension in ("amount", "date", "status", "relationship")},
        "pairs": [row["pair"] for row in rows],
    }]
