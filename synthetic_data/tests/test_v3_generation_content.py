"""Phase B — canonical content: rich structures, tables, threads, statements."""
from __future__ import annotations

from collections import Counter

import pytest

from synthetic_data.generation import build_corpus
from synthetic_data.model import (
    Block,
    CdmWorkbook,
    CellFormula,
    Money,
    Percentage,
    Table,
    collect_violations,
)
from synthetic_data.model.values import IsoDate

from synthetic_data.tests.v3_generation import pilot_plan

@pytest.fixture(scope="module")
def v3_pilot_plan():
    return pilot_plan()



@pytest.fixture(scope="module")
def plan(v3_pilot_plan):
    return v3_pilot_plan


def _blocks(document):
    for section in document.sections:
        yield from section.blocks


def _tables(document):
    for block in _blocks(document):
        if block.table is not None:
            yield block.table


def test_corpus_validates_through_the_phase_a_invariants(plan):
    corpus = plan.corpus()                      # runs every cross-layer invariant
    assert not collect_violations(corpus)
    assert set(corpus.documents) == set(plan.documents)


def test_every_prose_type_is_planned(plan):
    types = Counter(document.doc_type for document in plan.documents.values())
    for doc_type in ("board_minutes", "due_diligence", "financial_agreement",
                     "case_notes", "memorandum", "press_release", "correspondence"):
        assert types[doc_type] >= 1


def test_documents_have_metadata_and_sections(plan):
    for document in plan.documents.values():
        assert document.metadata.title
        assert document.metadata.document_date is not None
        assert document.metadata.jurisdiction
        assert document.plan_fingerprint
        if not isinstance(document, CdmWorkbook):
            assert document.sections, f"{document.document_id} has no sections"
            assert all(section.heading for section in document.sections)
            assert all(section.blocks for section in document.sections)


def test_section_tree_is_well_formed(plan):
    for document in plan.documents.values():
        by_id = {section.section_id: section for section in document.sections}
        assert len(by_id) == len(document.sections)
        for section in document.sections:
            if section.parent_id is not None:
                parent = by_id[section.parent_id]
                assert section.depth == parent.depth + 1
                assert parent.section_id in by_id


def test_rich_block_kinds_appear(plan):
    kinds = Counter(block.kind for document in plan.documents.values()
                    for block in _blocks(document))
    for kind in ("paragraph", "list", "table", "signature", "exhibit", "note"):
        assert kinds[kind] > 0, f"no {kind} block in the pilot"


def test_tables_carry_typed_cells_and_formats(plan):
    typed = Counter()
    formats = Counter()
    for document in plan.documents.values():
        for table in _tables(document):
            assert table.columns and table.body_rows
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                for cell in row.cells:
                    if isinstance(cell.value, Money):
                        typed["money"] += 1
                    elif isinstance(cell.value, Percentage):
                        typed["percent"] += 1
                    elif isinstance(cell.value, IsoDate):
                        typed["date"] += 1
                    elif isinstance(cell.value, int):
                        typed["int"] += 1
                    if cell.number_format:
                        formats[cell.number_format] += 1
    assert set(typed) == {"money", "percent", "date", "int"}
    assert "#,##0.00" in formats and "0.00%" in formats


def test_cell_references_resolve_and_match_values(plan):
    for document in plan.documents.values():
        for table in _tables(document):
            for key, fact_id in table.cell_refs.items():
                assert fact_id in plan.world.facts
                row = int(key[1:3])
                column = int(key[4:6])
                rows = [*table.header_rows, *table.body_rows, *table.total_rows]
                cell = rows[row].cells[column]
                fact = plan.world.facts[fact_id]
                if isinstance(cell.value, Money) and fact.amount is not None:
                    assert cell.value == fact.amount


def test_formulas_have_cached_values(plan):
    for document in plan.documents.values():
        for table in _tables(document):
            for row in [*table.header_rows, *table.body_rows, *table.total_rows]:
                for cell in row.cells:
                    if cell.formula is not None:
                        assert isinstance(cell.formula, CellFormula)
                        assert cell.formula.cached_value == cell.value


def test_every_statement_is_anchored_and_resolves(plan):
    for document in plan.documents.values():
        node_ids = document.node_ids()
        assert document.statements, f"{document.document_id} states no facts"
        for statement in document.statements:
            assert statement.node_id in node_ids
            assert statement.surface.strip()
            fact = plan.world.facts[statement.fact_id]
            assert statement.statement_id in fact.statement_ids


def test_every_world_fact_is_stated(plan):
    stated = {statement.fact_id for document in plan.documents.values()
              for statement in document.statements}
    assert stated == set(plan.world.facts)


def test_a_fact_is_shared_across_documents(plan):
    """One repayment is the same fact in the agreement, the report and the workbook."""
    shared = []
    for matter in plan.matters:
        for fact_id, fact in matter.facts.items():
            docs = [doc_id for doc_id, document in plan.documents.items()
                    if any(statement.fact_id == fact_id
                           for statement in document.statements)]
            if len(docs) > 1:
                shared.append((fact_id, docs))
    assert shared, "no fact is stated in more than one document"

    fact_id, docs = max(shared, key=lambda item: len(item[1]))
    assert len(docs) >= 3
    # every document's statement names the *same* fact id — identity does not
    # change with location, which is the whole point of §3.2
    for doc_id in docs:
        matching = [statement for statement in plan.documents[doc_id].statements
                    if statement.fact_id == fact_id]
        assert matching
        assert {statement.fact_id for statement in matching} == {fact_id}
    # a transaction must be one fact even when a prose paragraph and a workbook
    # row both state it
    transaction_facts = {txn.fact_id for txn in plan.world.transactions.values()}
    if fact_id in transaction_facts:
        anchors = {statement.node_id.split(":")[0]
                   for doc_id in docs
                   for statement in plan.documents[doc_id].statements
                   if statement.fact_id == fact_id}
        assert "tbl" in anchors and "blk" in anchors


def test_correspondence_threads_are_coherent(plan):
    threads = plan.world.threads
    assert threads
    for thread_id, thread in threads.items():
        assert thread.messages, "a thread has no messages"
        sequences = []
        for position, doc_id in enumerate(thread.messages, start=1):
            document = plan.documents[doc_id]
            meta = document.metadata.thread
            assert meta is not None
            assert meta.thread_id == thread_id
            assert meta.sequence == position
            sequences.append(meta.sequence)
            for attachment in meta.attachments:
                assert attachment in {exhibit.exhibit_id for exhibit in document.exhibits}
            if meta.in_reply_to is not None:
                assert meta.in_reply_to in {plan.documents[earlier].metadata.thread.message_id
                                            for earlier in thread.messages[:position - 1]}
        assert sequences == sorted(sequences)
        assert thread.latest_document == thread.messages[-1]


def test_cross_references_resolve(plan):
    assert plan.world.cross_references
    for reference in plan.world.cross_references:
        assert reference.src_document in plan.documents
        assert reference.dst_document in plan.documents
        if reference.src_fact:
            assert reference.src_fact in plan.world.facts


def test_pilot_has_a_contradiction_scenario(plan):
    assert len(plan.world.contradictions) == 1
    contradiction = next(iter(plan.world.contradictions.values()))
    assert len(contradiction.documents) == 2
    assert contradiction.documents[0] != contradiction.documents[1]


def test_no_nested_tables(plan):
    for document in plan.documents.values():
        for table in _tables(document):
            for row in table.body_rows:
                for cell in row.cells:
                    assert isinstance(cell.value, (Money, Percentage, IsoDate, int, type(None)))
