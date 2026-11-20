"""Tests for the end-to-end corpus pipeline, manifest and safety guards."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from synth.eval.gold import load_gold_file
from synth.store import test_store as make_test_store

from synthetic_data.generator.cli import main
from synthetic_data.generator.documents import build_document_plan, page_counts_v2
from synthetic_data.generator.pipeline import (
    CorpusGenerationError,
    _check_plans,
    _prepare_output_dir,
    generate_corpus,
)
from synthetic_data.schemas.qa import QADataset


def _files(directory):
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*"))
            if path.is_file()}


def test_pilot_artifacts_and_manifest(pilot):
    out = pilot.output_dir
    assert (out / "documents").is_dir()
    assert (out / "gold").is_dir()
    assert (out / "manifest.json").is_file()
    assert (out / "qa_dataset.json").is_file()
    assert (out / "qa_dataset.schema.json").is_file()
    assert len(list((out / "documents").glob("*.txt"))) == 5
    assert len(list((out / "gold").glob("*.json"))) == 5

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["batch"] == {
        "seed": 7,
        "target_pages": 20,
        "actual_pages": 20,
        "document_count": 5,
        "page_pattern": [5, 4, 3, 4, 4],
    }

    validation = manifest["validation"]
    assert validation["documents"] == validation["ok"] == 5
    assert validation["failed"] == 0
    assert validation["sentence_mismatch"] == 0
    assert validation["not_found"] == 0
    assert validation["errors"] == 0
    assert validation["grounding"] == {"EXACT": validation["items"]}
    assert validation["items"] == sum(
        result.report["items"] for result in pilot.gold_results)

    semantics = manifest["semantics"]
    assert semantics["ok"] is True
    assert semantics["failure_count"] == 0
    assert semantics["failures"] == []
    assert semantics["checks"] == pilot.semantics.checks
    assert set(semantics["checks"]) == {
        "evidence_quotes", "relationship_direction", "unique_answers",
        "temporal_support", "proposition_statuses", "entity_identity",
        "transactions", "contradictions",
    }
    assert all(value > 0 for value in semantics["checks"].values())

    rows = manifest["documents"]
    assert len(rows) == 5
    assert sum(row["pages"] for row in rows) == 20
    for row, context in zip(rows, pilot.documents):
        assert row["document_id"] == context.document_id
        assert row["synth_doc_id"] == context.synth_doc_id
        assert row["filename"] == context.filename
        assert row["text_sha256"] == context.text_sha256
        assert 1 <= row["pages"] <= 5
        assert len(row["trap_kinds"]) >= 3
        assert row["gold_ok"] is True
        assert row["gold_sentence_mismatch"] == 0
        gold = load_gold_file(out / "gold" / row["gold_file"])
        assert gold.mode == "generated"
        assert gold.doc.doc_id is None
        assert gold.doc.text_sha256 == row["text_sha256"]

    reproducibility = manifest["reproducibility"]
    assert reproducibility["gold_mode"] == "generated"
    assert reproducibility["blind"] is False
    assert reproducibility["promoted"] is False

    dataset = QADataset.model_validate_json(
        (out / "qa_dataset.json").read_text(encoding="utf-8"))
    assert dataset == pilot.dataset


def test_v1_dataset_and_schema_keep_the_frozen_contract(pilot):
    raw = json.loads(
        (pilot.output_dir / "qa_dataset.json").read_text(encoding="utf-8"))
    assert len(raw["queries"]) == 95
    for query in raw["queries"]:
        assert "unanswerable_kind" not in query
        assert "unanswerable_proof" not in query
    assert "null" not in (
        pilot.output_dir / "qa_dataset.json").read_text(encoding="utf-8").casefold()

    schema = json.loads(
        (pilot.output_dir / "qa_dataset.schema.json").read_text(encoding="utf-8"))
    properties = schema["$defs"]["QAQuery"]["properties"]
    assert "unanswerable_kind" not in properties
    assert "unanswerable_proof" not in properties
    assert "UnanswerableProof" not in schema["$defs"]


def test_manifest_counts_match_the_world(pilot):
    manifest = pilot.manifest
    world = pilot.world

    assert manifest["entities"]["world_total"] == len(world.entities)
    assert manifest["relationships"]["world_edge_facts"] == sum(
        1 for fact in world.facts if fact.kind == "edge")
    assert manifest["events"] == len(world.events)
    assert manifest["transactions"] == len(world.transactions)
    assert manifest["contradictions"] == len(world.contradictions)
    assert set(manifest["relationships"]["critical_relations"]) == {
        "owns", "shareholder_of", "family_of", "lent_to", "owes",
        "guarantor_for", "represents",
    }
    assert manifest["traps"]["minimum_per_document"] >= 3
    assert manifest["queries"]["total"] == len(pilot.dataset.queries)
    assert manifest["queries"]["answerable"] == sum(
        query.answerable for query in pilot.dataset.queries)


def test_document_files_have_the_planned_page_counts(pilot):
    documents = pilot.output_dir / "documents"
    total = 0
    for plan in pilot.plans:
        text = (documents / plan.filename).read_text(encoding="utf-8")
        pages = text.split("\f")
        assert len(pages) == plan.pages
        assert all(page.strip() for page in pages)
        total += len(pages)
    assert total == 20


def test_same_seed_reproduces_documents_gold_and_qa(pilot, tmp_path):
    store = make_test_store()
    try:
        rebuilt = generate_corpus(
            seed=7, pages=20, out_dir=tmp_path / "rebuilt", store=store)
        # The pipeline must never promote generated gold.
        with store.reader() as reader:
            assert reader.select("gold_sets") == []
    finally:
        store.dispose()

    assert _files(rebuilt.output_dir / "documents") == _files(
        pilot.output_dir / "documents")
    assert _files(rebuilt.output_dir / "gold") == _files(pilot.output_dir / "gold")
    assert (rebuilt.output_dir / "qa_dataset.json").read_bytes() == (
        pilot.output_dir / "qa_dataset.json").read_bytes()

    left = pilot.manifest["reproducibility"]
    right = rebuilt.manifest["reproducibility"]
    assert right["document_hashes"] == left["document_hashes"]
    assert right["prefix_sha256"] == left["prefix_sha256"]
    assert right["world_signature_sha256"] == left["world_signature_sha256"]
    assert rebuilt.manifest["batch"] == pilot.manifest["batch"]
    assert rebuilt.manifest["validation"] == pilot.manifest["validation"]
    assert rebuilt.manifest["semantics"] == pilot.manifest["semantics"]

    # Synth document ids are ULIDs and intentionally differ between ingests.
    assert [row["synth_doc_id"] for row in rebuilt.manifest["documents"]] != [
        row["synth_doc_id"] for row in pilot.manifest["documents"]]


def test_different_seed_changes_the_corpus(pilot, tmp_path):
    rebuilt = generate_corpus(seed=8, pages=20, out_dir=tmp_path / "seed8")

    assert _files(rebuilt.output_dir / "documents").keys() == _files(
        pilot.output_dir / "documents").keys()
    assert _files(rebuilt.output_dir / "documents") != _files(
        pilot.output_dir / "documents")
    assert rebuilt.manifest["reproducibility"]["world_signature_sha256"] != (
        pilot.manifest["reproducibility"]["world_signature_sha256"])
    assert rebuilt.manifest["validation"]["ok"] == 5


@pytest.mark.parametrize("pages", [0, 10, -20, 30])
def test_page_counts_must_be_positive_multiples_of_twenty(pages, tmp_path):
    with pytest.raises(ValueError, match="positive multiple of 20"):
        generate_corpus(seed=7, pages=pages, out_dir=tmp_path / "refused")


def test_non_sqlite_store_is_refused(tmp_path):
    with pytest.raises(CorpusGenerationError, match="non-SQLite"):
        generate_corpus(
            seed=7,
            pages=20,
            out_dir=tmp_path / "refused",
            store=SimpleNamespace(dialect="postgresql"),
        )


def test_existing_artifacts_require_overwrite(tmp_path):
    out = tmp_path / "out"
    _prepare_output_dir(out, False)
    (out / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already contains generated artifacts"):
        _prepare_output_dir(out, False)

    _prepare_output_dir(out, True)
    assert not (out / "manifest.json").exists()
    assert (out / "documents").is_dir()
    assert (out / "gold").is_dir()


def test_cli_refuses_output_outside_the_output_root(tmp_path):
    outside = tmp_path / "outside"
    with pytest.raises(SystemExit):
        main(["generate", "--pages", "20", "--seed", "7", "--out", str(outside)])
    assert not outside.exists()


# --------------------------------------------------------------------------- v2 pipeline
def test_v2_pilot_artifacts_and_manifest(pilot_v2):
    out = pilot_v2.output_dir
    assert len(list((out / "documents").glob("*.txt"))) == 7
    assert len(list((out / "gold").glob("*.json"))) == 7

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["batch"] == {
        "seed": 7,
        "target_pages": 20,
        "actual_pages": 20,
        "document_count": 7,
        "page_pattern": [3, 3, 3, 3, 3, 3, 2],
        "layout": "v2",
    }

    validation = manifest["validation"]
    assert validation["documents"] == validation["ok"] == 7
    assert validation["failed"] == 0
    assert validation["sentence_mismatch"] == 0
    assert validation["not_found"] == 0
    assert validation["errors"] == 0
    assert validation["grounding"] == {"EXACT": validation["items"]}

    traps = manifest["traps"]
    assert traps["minimum_per_document"] >= 3
    assert traps["maximum_per_document"] <= 6
    assert traps["documents_outside_three_to_six"] == 0
    assert len(traps["by_document"]) == 7
    assert all(3 <= row["trap_count"] <= 6 for row in traps["by_document"])
    assert sum(row["trap_count"] for row in traps["by_document"]) == (
        traps["total_statements"])

    queries = manifest["queries"]
    assert queries["total"] == 95
    assert queries["answerable"] == 70
    assert queries["unanswerable"] == 25
    assert queries["unanswerable_by_kind"] == {
        "absent_attribute": 5,
        "absent_relationship": 5,
        "unsupported_historical_date": 5,
        "absent_financial_information": 5,
        "false_presupposition": 5,
    }
    assert queries["duplicates"]["duplicate_normalized_percent"] < 10.0
    assert queries["duplicates"]["max_pair_occurrences"] <= 2

    semantics = manifest["semantics"]
    assert semantics["ok"] is True
    assert semantics["failure_count"] == 0
    assert semantics["failures"] == []
    assert "unanswerable_proofs" in semantics["checks"]
    assert all(value > 0 for value in semantics["checks"].values())

    rows = manifest["documents"]
    assert len(rows) == 7
    assert sum(row["pages"] for row in rows) == 20
    assert len({row["doc_type"] for row in rows}) == 7
    for row, context in zip(rows, pilot_v2.documents):
        assert row["document_id"] == context.document_id
        assert row["synth_doc_id"] == context.synth_doc_id
        assert row["filename"] == context.filename
        assert row["text_sha256"] == context.text_sha256
        assert 1 <= row["pages"] <= 5
        assert 3 <= row["trap_count"] <= 6
        assert row["trap_count"] == len(row["trap_kinds"])
        assert row["gold_ok"] is True
        assert row["gold_sentence_mismatch"] == 0
        gold = load_gold_file(out / "gold" / row["gold_file"])
        assert gold.mode == "generated"
        assert gold.doc.doc_id is None
        assert gold.doc.text_sha256 == row["text_sha256"]

    diversity = manifest["diversity"]
    assert diversity["document_subjects"] == 7
    assert diversity["unique_document_subjects"] == 7
    assert diversity["reused_document_subjects"] == 0
    assert diversity["world_companies"] == 24

    performance = manifest["performance"]
    assert performance["measurement"] == "tracemalloc"
    assert performance["peak_memory_mb"] > 0
    assert performance["tracemalloc_started_by_pipeline"] is True
    assert set(performance["timings_ms"]) == {
        "world_generation", "document_generation", "ingestion",
        "gold_validation", "qa_generation", "semantic_validation", "total",
    }
    assert all(value >= 0 for value in performance["timings_ms"].values())

    reproducibility = manifest["reproducibility"]
    assert reproducibility["gold_mode"] == "generated"
    assert reproducibility["blind"] is False
    assert reproducibility["promoted"] is False

    dataset = QADataset.model_validate_json(
        (out / "qa_dataset.json").read_text(encoding="utf-8"))
    assert dataset.version == 2
    assert dataset == pilot_v2.dataset


def test_v2_document_files_have_the_planned_page_counts(pilot_v2):
    documents = pilot_v2.output_dir / "documents"
    total = 0
    for plan in pilot_v2.plans:
        text = (documents / plan.filename).read_text(encoding="utf-8")
        pages = text.split("\f")
        assert len(pages) == plan.pages
        assert all(page.strip() for page in pages)
        total += len(pages)
    assert total == 20


def test_v2_same_seed_reproduces_documents_gold_and_qa(pilot_v2, tmp_path):
    store = make_test_store()
    try:
        rebuilt = generate_corpus(
            seed=7, pages=20, out_dir=tmp_path / "rebuilt_v2",
            store=store, layout="v2")
        with store.reader() as reader:
            assert reader.select("gold_sets") == []
    finally:
        store.dispose()

    assert _files(rebuilt.output_dir / "documents") == _files(
        pilot_v2.output_dir / "documents")
    assert _files(rebuilt.output_dir / "gold") == _files(
        pilot_v2.output_dir / "gold")
    assert (rebuilt.output_dir / "qa_dataset.json").read_bytes() == (
        pilot_v2.output_dir / "qa_dataset.json").read_bytes()

    left = pilot_v2.manifest["reproducibility"]
    right = rebuilt.manifest["reproducibility"]
    assert right["document_hashes"] == left["document_hashes"]
    assert right["prefix_sha256"] == left["prefix_sha256"]
    assert right["world_signature_sha256"] == left["world_signature_sha256"]
    assert rebuilt.manifest["batch"] == pilot_v2.manifest["batch"]
    assert rebuilt.manifest["validation"] == pilot_v2.manifest["validation"]
    assert rebuilt.manifest["semantics"] == pilot_v2.manifest["semantics"]
    assert rebuilt.manifest["queries"] == pilot_v2.manifest["queries"]
    assert rebuilt.manifest["traps"] == pilot_v2.manifest["traps"]


def test_v2_plan_rejects_trap_counts_outside_three_to_six(world):
    counts = page_counts_v2(20)
    plans = [build_document_plan(world, index, counts[index], layout="v2")
             for index in range(7)]
    _check_plans(plans, layout="v2")
    plans[0].trap_kinds = plans[0].trap_kinds + ["extra"] * 2
    with pytest.raises(CorpusGenerationError, match="v2 requires 3-6"):
        _check_plans(plans, layout="v2")


def test_v2_rejects_unknown_layout_and_non_sqlite_store(tmp_path):
    with pytest.raises(ValueError, match="layout must be 'v1' or 'v2'"):
        generate_corpus(seed=7, pages=20, out_dir=tmp_path / "bad",
                        layout="v3")
    with pytest.raises(CorpusGenerationError, match="non-SQLite"):
        generate_corpus(
            seed=7, pages=20, out_dir=tmp_path / "refused_v2",
            store=SimpleNamespace(dialect="postgresql"), layout="v2")
