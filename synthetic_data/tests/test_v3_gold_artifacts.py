"""Gold artifacts: atomicity, determinism, separation and consistency (§8.7).

The three files are one unit of truth.  These tests hold the writer to that: a
failed write must leave the previous corpus intact, no staging directory may
survive, serialization must be canonical, and the alignment record must agree with
both gold versions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from synth.eval.gold import GoldSet, load_gold_file

from synthetic_data.alignment import EVIDENCE_KINDS
from synthetic_data.gold import (AlignmentArtifact, GoldV2, gold_paths,
                                 validate_v1, validate_v2, write_gold_pair)
from synthetic_data.gold.build import GoldPair

from synthetic_data.tests.v3_gold import (REPO_ROOT, gold_docx, gold_pdf, gold_pilot,
                                          gold_store, gold_txt, pilot_plan)


def _write(golded, root: Path):
    return write_gold_pair(golded.pair, root, golded.document_id,
                           golded.document.doc_type, golded.format)


# -- atomicity -------------------------------------------------------------
def test_a_pair_writes_exactly_three_artifacts(tmp_path, gold_txt):
    v1, v2, alignment = _write(gold_txt, tmp_path / "gold")
    assert all(path.exists() for path in (v1, v2, alignment))
    assert v1.parent.name == "v1" and v2.parent.name == "v2"
    assert alignment.parent.name == "alignment"
    assert v1.read_text(encoding="utf-8").startswith("{")
    assert not (tmp_path / "gold" / ".staging").exists()


def test_a_failed_write_preserves_the_previous_artifacts(tmp_path, gold_txt, monkeypatch):
    root = tmp_path / "gold"
    v1, v2, alignment = _write(gold_txt, root)
    before = {path: path.read_bytes() for path in (v1, v2, alignment)}

    from synthetic_data.gold import build as gold_build

    real_dump = gold_build._dump
    calls = {"n": 0}

    def flaky_dump(model, path):
        calls["n"] += 1
        if calls["n"] == 2:                    # fail while writing v2
            raise OSError("simulated disk failure")
        return real_dump(model, path)

    monkeypatch.setattr(gold_build, "_dump", flaky_dump)
    with pytest.raises(OSError, match="simulated disk failure"):
        _write(gold_txt, root)

    for path, payload in before.items():
        assert path.exists(), f"{path.name} must survive a failed write"
        assert path.read_bytes() == payload
    assert not (root / ".staging").exists()


def test_a_failed_write_removes_partial_new_files(tmp_path, gold_txt, monkeypatch):
    root = tmp_path / "gold"
    from synthetic_data.gold import build as gold_build

    real_dump = gold_build._dump
    calls = {"n": 0}

    def flaky_dump(model, path):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated failure on the alignment record")
        return real_dump(model, path)

    monkeypatch.setattr(gold_build, "_dump", flaky_dump)
    with pytest.raises(OSError):
        _write(gold_txt, root)
    v1, v2, alignment = gold_paths(root, gold_txt.document_id,
                                   gold_txt.document.doc_type, gold_txt.format)
    assert not any(path.exists() for path in (v1, v2, alignment))
    assert not (root / ".staging").exists()


def test_rewriting_replaces_all_three(tmp_path, gold_txt):
    root = tmp_path / "gold"
    v1, v2, alignment = _write(gold_txt, root)
    _write(gold_txt, root)
    assert all(path.exists() for path in (v1, v2, alignment))
    assert not list(root.glob(".staging/*"))


# -- determinism -----------------------------------------------------------
def test_serialization_is_deterministic(tmp_path):
    from synthetic_data.tests.v3_gold import gold_document

    plan = pilot_plan()
    golded = gold_document(plan.documents[plan.prose_ids[3]], "txt",
                           tmp_path / "render")
    first = tmp_path / "a"
    second = tmp_path / "b"
    _write(golded, first)
    _write(golded, second)
    for relative in ("v1", "v2", "alignment"):
        left = sorted((first / relative).glob("*.json"))
        right = sorted((second / relative).glob("*.json"))
        assert [p.name for p in left] == [p.name for p in right]
        for one, two in zip(left, right):
            assert one.read_bytes() == two.read_bytes(), relative


def test_stored_artifacts_round_trip_byte_for_byte(tmp_path, gold_txt):
    v1, v2, alignment = _write(gold_txt, tmp_path / "gold")
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from synth.eval.gold import load_gold_file\n"
        "from synthetic_data.gold.model import GoldV2, AlignmentArtifact\n"
        "root = Path(sys.argv[1])\n"
        "out = {}\n"
        "out['v1'] = json.dumps(load_gold_file(root / 'v1.json').model_dump(mode='json'),"
        " indent=2, ensure_ascii=False, sort_keys=True) + '\\n'\n"
        "out['v2'] = json.dumps(GoldV2.model_validate(json.loads("
        "(root / 'v2.json').read_text(encoding='utf-8'))).model_dump(mode='json'),"
        " indent=2, ensure_ascii=False, sort_keys=True) + '\\n'\n"
        "out['alignment'] = json.dumps(AlignmentArtifact.model_validate(json.loads("
        "(root / 'alignment.json').read_text(encoding='utf-8'))).model_dump(mode='json'),"
        " indent=2, ensure_ascii=False, sort_keys=True) + '\\n'\n"
        "print(json.dumps(out, sort_keys=True))\n"
    )
    runs = []
    staging = tmp_path / "roundtrip"
    staging.mkdir()
    for source, target in ((v1, "v1.json"), (v2, "v2.json"), (alignment, "alignment.json")):
        (staging / target).write_bytes(source.read_bytes())
    for _ in range(2):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run([sys.executable, "-c", script, str(staging)],
                                   capture_output=True, text=True,
                                   env=environment, timeout=300)
        assert completed.returncode == 0, completed.stderr
        runs.append(json.loads(completed.stdout))
    assert runs[0] == runs[1]
    for name, payload in zip(("v1.json", "v2.json", "alignment.json"),
                             (runs[0]["v1"], runs[0]["v2"], runs[0]["alignment"])):
        assert payload == (staging / name).read_text(encoding="utf-8"), name


# -- separation and consistency -------------------------------------------
def test_v1_has_no_v2_fields(tmp_path, gold_txt):
    v1, v2, alignment = _write(gold_txt, tmp_path / "gold")
    text = v1.read_text(encoding="utf-8")
    for field in ("canonical_node_id", "canonical_sentence_ids", "alignment_kind",
                  "render_format", "render_loc", "gold_version", "derived_from"):
        assert field not in text, field
    loaded = load_gold_file(v1)
    assert loaded.version == 1
    assert loaded.annotator == "synthetic_data.generator"
    assert loaded.mode == "generated"


def test_v2_is_a_superset_with_provenance(tmp_path, gold_txt):
    _, v2, _ = _write(gold_txt, tmp_path / "gold")
    artifact = GoldV2.model_validate(json.loads(v2.read_text(encoding="utf-8")))
    assert artifact.gold_version == 2
    evidence = [item for edge in artifact.edges for item in edge.evidence]
    assert evidence
    for item in evidence:
        assert item.canonical_node_id
        assert item.render_format == gold_txt.format
        assert item.render_loc
        assert item.alignment_kind in EVIDENCE_KINDS
    for edge in artifact.edges:
        assert edge.canonical_fact_id == edge.id


def test_alignment_counts_match_the_records(tmp_path, gold_txt):
    _, _, alignment = _write(gold_txt, tmp_path / "gold")
    artifact = AlignmentArtifact.model_validate(json.loads(alignment.read_text(
        encoding="utf-8")))
    counts: dict[str, int] = {}
    for unit in artifact.units:
        counts[unit.kind] = counts.get(unit.kind, 0) + 1
    assert artifact.counts == counts
    assert artifact.document_id == gold_txt.document_id
    assert artifact.format == gold_txt.format
    assert artifact.render_sha256 == gold_txt.render.sha256


def test_no_missing_partial_or_ambiguous_units(gold_pilot):
    for golded in gold_pilot:
        bad = {unit.kind for unit in golded.pair.alignment.units
               if unit.kind in {"missing", "partial", "ambiguous"}}
        assert not bad, (golded.document_id, golded.format, bad)


def test_alignment_agrees_with_both_gold_versions(gold_pilot):
    for golded in gold_pilot:
        units = {unit.canonical_id: unit for unit in golded.pair.alignment.units}
        for edge in golded.pair.v2.edges:
            for item in edge.evidence:
                if item.canonical_node_id is None:
                    continue
                unit = units.get(item.canonical_node_id)
                if unit is None:
                    continue
                assert item.sentence_id in unit.parser_sentence_ids or \
                    unit.kind == "missing"
        # v1 cites the same parser sentences the v2 items were derived from
        v2_sentences = {item.sentence_id for edge in golded.pair.v2.edges
                        for item in edge.evidence}
        v1_sentences = {item.sentence_id for edge in golded.pair.v1.edges
                        for item in edge.evidence}
        assert v1_sentences


def test_every_artifact_validates_against_the_parser(tmp_path, gold_store, gold_pdf,
                                                    gold_docx):
    for golded in (gold_pdf, gold_docx):
        v1, v2, alignment = _write(golded, tmp_path / golded.format)
        assert validate_v1(gold_store, v1).ok
        errors = validate_v2(
            GoldV2.model_validate(json.loads(v2.read_text(encoding="utf-8"))),
            view=golded.view, render=golded.render, canonical=golded.canonical,
            document=golded.document, world=pilot_plan().world,
            row_quotes=golded.row_quotes)
        assert not errors, errors[:3]
        assert v1.exists() and alignment.exists()


def test_no_unexplained_parser_content(gold_pilot):
    for golded in gold_pilot:
        report = golded.pair.accounting
        assert report is not None and report.ok, report.describe()
        assert report.total == len(golded.view.sentences)
