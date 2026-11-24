"""Phase C closure — table surface fidelity.

The board-minutes defect was ``(fact.text or "").capitalize()``: Python's
``capitalize`` lower-cases every later character, so "Meridian Finance Limited"
became "meridian finance limited".  These tests keep the cell text, the typed
value and the fact reference agreeing, and prove that a table statement is
grounded by *its own* quote rather than by a prose sentence elsewhere.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from synthetic_data.alignment import (AlignmentError, align_document, align_facts,
                                      require_fact_evidence)
from synthetic_data.canonical import canonicalize, row_text
from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.model import Money
from synthetic_data.renderers import DEFAULT_TXT_PROFILE, TxtRenderer
from synthetic_data.surfaces import money_key, money_surface_key
from synthetic_data.tests.v3_txt import pilot_plan, txt_ingested, txt_rendered


@pytest.fixture(scope="module")
def plan():
    return pilot_plan()


def _cells(document):
    for section in document.sections:
        for block in section.blocks:
            if block.table is None:
                continue
            for row in [*block.table.header_rows, *block.table.body_rows,
                        *block.table.total_rows]:
                for cell in row.cells:
                    yield block, row, cell


def _statements_by_fact(document):
    out = {}
    for statement in document.statements:
        out.setdefault(statement.fact_id, []).append(statement)
    return out


def test_case_is_capitalised_without_lowercasing_the_rest():
    """The regression, in isolation: no later character may change case."""
    from synthetic_data.generation.planner import _first_upper

    source = "the facility agreement with Meridian Finance Limited be approved"
    assert _first_upper(source) == "The facility agreement with Meridian Finance Limited be approved"
    assert _first_upper("Pikestead Properties GmbH") == "Pikestead Properties GmbH"
    assert source.capitalize() != _first_upper(source)


def test_resolution_table_cells_preserve_entity_casing(plan):
    checked = 0
    for doc_id in plan.prose_ids:
        document = plan.documents[doc_id]
        facts = plan.world.facts
        for block, row, cell in _cells(document):
            if not cell.fact_ref:
                continue
            fact = facts.get(cell.fact_ref)
            if fact is None or not fact.text:
                continue
            if cell.text is None:
                continue
            checked += 1
            # every capitalised word of the fact survives in the cell
            for word in fact.text.split():
                if word[:1].isupper():
                    assert word in cell.text, (doc_id, cell.cell_id, word, cell.text)
            assert cell.text[0].isupper()
            assert cell.text == fact.text or cell.text[1:] == fact.text[1:]
    assert checked > 5


def test_every_table_fact_reference_resolves_to_a_statement(plan):
    for doc_id in plan.prose_ids:
        document = plan.documents[doc_id]
        referenced = {cell.fact_ref for _, _, cell in _cells(document) if cell.fact_ref}
        stated = {statement.fact_id for statement in document.statements}
        assert referenced <= stated, doc_id


def test_typed_cells_agree_with_their_display_string(plan):
    checked = 0
    for doc_id in plan.prose_ids:
        for _, _, cell in _cells(plan.documents[doc_id]):
            if not isinstance(cell.value, Money):
                continue
            checked += 1
            key = money_surface_key(cell.display)
            assert key == money_key(cell.value), (doc_id, cell.cell_id, cell.display)
            assert cell.number_format == "#,##0.00"
    assert checked > 5


def test_cells_with_a_fact_reference_carry_the_same_value_as_the_statement(plan):
    for doc_id in plan.prose_ids:
        document = plan.documents[doc_id]
        statements = {statement.fact_id: statement for statement in document.statements}
        for _, _, cell in _cells(document):
            if not cell.fact_ref or cell.value is None:
                continue
            statement = statements.get(cell.fact_ref)
            if statement is None:
                continue
            slot = statement.value_slots.get("amount")
            if slot:
                assert money_surface_key(cell.display) == money_surface_key(slot) or \
                    "amount" not in statement.value_slots


def test_one_fact_keeps_one_identity_across_prose_and_table(plan):
    shared = 0
    for doc_id in plan.prose_ids:
        document = plan.documents[doc_id]
        for fact_id, statements in _statements_by_fact(document).items():
            nodes = {statement.node_id for statement in statements}
            has_table = any(node.startswith("tbl:") for node in nodes)
            has_prose = any(node.startswith("blk:") for node in nodes)
            if not (has_table and has_prose):
                continue
            shared += 1
            # one fact id, one SemanticFact, several statements — never a copy
            assert len({statement.fact_id for statement in statements}) == 1
            assert fact_id in plan.world.facts
            fact = plan.world.facts[fact_id]
            assert set(fact.statement_ids) >= {s.statement_id for s in statements}
    assert shared, "the pilot states at least one fact in prose and in a table"


def test_table_row_projection_matches_the_rendered_row(plan):
    with tempfile.TemporaryDirectory() as tmp:
        for doc_id in plan.prose_ids:
            document = plan.documents[doc_id]
            result = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                          Path(tmp) / f"{doc_id.split(':')[1]}.txt")
            text = Path(result.path).read_text(encoding="utf-8")
            canonical = canonicalize(document)
            rows = [sentence for sentence in canonical.sentences
                    if sentence.kind == "table_row"]
            for sentence in rows:
                assert sentence.text in text


def test_grounding_refuses_a_table_statement_supported_only_by_prose(plan):
    """Corrupt the table cell; the prose sentence must not rescue the row."""
    import synth.config
    from synth.modules.common import DocView
    from synth.modules.u01_parser import ingest_path
    from synth.store import test_store as make_test_store

    config = synth.config.load_config()
    store = make_test_store()
    try:
        doc_id = next(d for d in plan.prose_ids
                      if plan.documents[d].doc_type == "board_minutes")
        document = plan.documents[doc_id]
        tampered = []
        for section in document.sections:
            for block in section.blocks:
                if block.table is None:
                    continue
                rows = []
                for row in block.table.body_rows:
                    cells = []
                    for cell in row.cells:
                        if isinstance(cell.value, Money):
                            cells.append(cell.model_copy(update={
                                "display": "GBP 1.00", "text": "GBP 1.00"}))
                        else:
                            cells.append(cell)
                    rows.append(row.model_copy(update={"cells": cells}))
                table = block.table.model_copy(update={"body_rows": rows})
                tampered.append(block.model_copy(update={"table": table}))
                break
            if tampered:
                break
        assert tampered, "no money cell to tamper with"
        sections = [section.model_copy(update={
            "blocks": [tampered[0] if block.block_id == tampered[0].block_id else block
                       for block in section.blocks]})
            for section in document.sections]
        corrupted = document.model_copy(update={"sections": sections})

        with tempfile.TemporaryDirectory() as tmp:
            result = TxtRenderer().render(corrupted, DEFAULT_TXT_PROFILE,
                                          Path(tmp) / "tampered.txt")
            records = ingest_path(store, config, result.path)
            with store.reader() as reader:
                view = DocView.load(reader, records[0]["doc_id"], config)
            canonical = canonicalize(corrupted)
            alignments = align_document(canonical, view)
            evidence = align_facts(corrupted, canonical, alignments,
                                   result.fact_surfaces, view.text)
            with pytest.raises(AlignmentError):
                require_fact_evidence(evidence)
    finally:
        store.dispose()
