"""The required end-to-end gold integration test and negative validation tests."""

from __future__ import annotations

import pytest

from synth.config import load_config
from synth.eval.gold import gold_filename, validate, write_gold_file
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.generator.annotations import (
    GoldGenerationError,
    assert_gold_valid,
    build_gold,
)
from synthetic_data.generator.documents import build_document_plan


def _ingest_and_build_gold(world, plan, tmp_path, store):
    config = load_config()
    path = tmp_path / "docs" / plan.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.render_text(), encoding="utf-8")
    records = ingest_path(store, config, path)
    assert len(records) == 1
    record = records[0]
    with store.reader() as reader:
        view = DocView.load(reader, record["doc_id"], config)
    gold = build_gold(world, plan, view)
    return path, record, view, gold


def test_generate_ingest_docview_gold_write_validate(world, tmp_path):
    """The integration path required by the project brief, step by step."""
    config = load_config()
    store = make_test_store()
    try:
        assert store.dialect == "sqlite"

        # 1. Generate a synthetic document.
        plan = build_document_plan(world, 0, 5)
        path = tmp_path / plan.filename
        path.write_text(plan.render_text(), encoding="utf-8")

        # 2. Ingest it through the real Synth parser.
        records = ingest_path(store, config, path)
        assert len(records) == 1
        record = records[0]
        assert record["status"] == "SEGMENTED"
        assert record["pages"] == 5
        assert record["sentences"] == len(plan.sentences)

        # 3. Load the canonical text and sentence index through DocView.
        with store.reader() as reader:
            view = DocView.load(reader, record["doc_id"], config)
        assert [view.sentence_text(s) for s in view.sentences] == plan.sentences

        # 4. Generate gold from the structured plan and the canonical DocView.
        gold = build_gold(world, plan, view)
        assert gold.mode == "generated"
        assert gold.doc.doc_id is None
        assert gold.doc.text_sha256
        assert gold.first_idx == 0
        assert gold.last_idx == len(view.sentences) - 1

        # 5. Write it with Synth's writer.
        gold_path = tmp_path / "gold" / gold_filename(
            plan.filename, gold.first_idx, gold.last_idx)
        write_gold_file(gold, gold_path)

        # 6. Validate with Synth's validator.
        report = validate(store, gold_path, ontology=world.ontology)

        # 7. Every quote is found, and every sentence reference is exact.
        assert report["ok"] is True
        assert report["sentence_mismatch"] == []
        assert report["not_found"] == []
        assert report["errors"] == []
        assert report["items"] > 0
        assert report["grounding"] == {"EXACT": report["items"]}
    finally:
        store.dispose()


def test_pilot_gold_is_fully_grounded(pilot):
    assert len(pilot.gold_results) == 5
    total_items = 0
    for result in pilot.gold_results:
        report = result.report
        assert report["ok"] is True
        assert report["sentence_mismatch"] == []
        assert report["not_found"] == []
        assert report["errors"] == []
        assert report["grounding"] == {"EXACT": report["items"]}
        total_items += report["items"]
    assert total_items > 300


def _unique_quote(gold, text: str) -> str | None:
    for entity in gold.entities:
        for mention in entity.mentions:
            if text.count(mention.quote) == 1:
                return mention.quote
    return None


def test_sentence_mismatch_is_explicitly_rejected(world, tmp_path):
    plan = build_document_plan(world, 0, 5)
    store = make_test_store()
    try:
        _path, _record, view, gold = _ingest_and_build_gold(world, plan, tmp_path, store)
        quote = _unique_quote(gold, view.text)
        assert quote is not None
        mention = next(m for entity in gold.entities for m in entity.mentions
                       if m.quote == quote)
        wrong_sentence = next(
            f"s{s['idx']}" for s in view.sentences
            if quote not in view.text[s["char_start"]:s["char_end"]])
        mention.sentence_id = wrong_sentence

        path = tmp_path / "gold" / gold_filename(plan.filename, gold.first_idx, gold.last_idx)
        write_gold_file(gold, path)
        report = validate(store, path, ontology=world.ontology)
        assert report["sentence_mismatch"], "Synth should report the soft mismatch"
        # Synth's validate() deliberately leaves ok True for this soft warning.
        assert report["ok"] is True
        with pytest.raises(GoldGenerationError):
            assert_gold_valid(store, path, ontology=world.ontology)
    finally:
        store.dispose()


def test_missing_quote_is_rejected(world, tmp_path):
    plan = build_document_plan(world, 1, 4)
    store = make_test_store()
    try:
        _path, _record, _view, gold = _ingest_and_build_gold(world, plan, tmp_path, store)
        gold.entities[0].mentions[0].quote = "This quote is not in any generated document."
        path = tmp_path / "gold" / gold_filename(plan.filename, gold.first_idx, gold.last_idx)
        write_gold_file(gold, path)
        report = validate(store, path, ontology=world.ontology)
        assert report["ok"] is False
        assert report["not_found"]
        with pytest.raises(GoldGenerationError):
            assert_gold_valid(store, path, ontology=world.ontology)
    finally:
        store.dispose()


def test_ontology_violation_is_rejected(world, tmp_path):
    plan = build_document_plan(world, 2, 3)
    store = make_test_store()
    try:
        _path, _record, _view, gold = _ingest_and_build_gold(world, plan, tmp_path, store)
        gold.entities[0].type = "not_a_real_ontology_type"
        path = tmp_path / "gold" / gold_filename(plan.filename, gold.first_idx, gold.last_idx)
        write_gold_file(gold, path)
        report = validate(store, path, ontology=world.ontology)
        assert report["ok"] is False
        assert any("unknown type" in error for error in report["errors"])
        with pytest.raises(GoldGenerationError):
            assert_gold_valid(store, path, ontology=world.ontology)
    finally:
        store.dispose()
