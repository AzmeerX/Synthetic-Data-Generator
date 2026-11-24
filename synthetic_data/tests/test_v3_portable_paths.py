"""Portable artifact names and the archive round trip (Phase I).

The defect these tests exist for: two Phase G gold files were named with
``workbook:shareholder_register``, so Windows stored them as NTFS alternate data
streams on a zero-byte base file.  Every check here is one that would have caught it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthetic_data.paths import (InvalidComponent, PathCollision, PortablePaths,
                                  artifact_name, safe_component)
from synthetic_data.portable import (archive_portable, rebuild_portable,
                                     verify_archive)

PILOT = Path(__file__).resolve().parents[1] / "output" / "v3_pilot"
PORTABLE = Path(__file__).resolve().parents[1] / "output" / "v3_pilot_portable"


# ---------------------------------------------------------------------------
def test_a_colon_never_survives():
    assert ":" not in safe_component("workbook:shareholder_register")
    assert safe_component("workbook:shareholder_register") == "workbook_shareholder_register"


def test_every_platform_invalid_character_is_replaced():
    cleaned = safe_component('a<b>c:d"e/f\\g|h?i*j')
    assert cleaned == "a_b_c_d_e_f_g_h_i_j"


def test_trailing_spaces_and_dots_are_stripped():
    assert safe_component("report. ") == "report"
    assert not safe_component("name...").endswith(".")


def test_reserved_device_names_are_escaped():
    for reserved in ("CON", "nul", "COM1", "lpt9", "aux.txt"):
        cleaned = safe_component(reserved)
        stem = cleaned.split(".", 1)[0].lower()
        assert stem not in {"con", "nul", "com1", "lpt9", "aux"}, reserved


def test_an_empty_component_is_refused():
    with pytest.raises(InvalidComponent):
        safe_component("")
    with pytest.raises(InvalidComponent):
        safe_component(" . ")


def test_a_long_name_is_shortened_deterministically_and_stays_unique():
    long_name = "x" * 400
    first = safe_component(long_name)
    second = safe_component(long_name + "y")
    assert len(first) <= 180
    assert first != second
    assert safe_component(long_name) == first


def test_artifact_names_carry_no_colon_for_any_format():
    for fmt in ("txt", "pdf", "docx", "xlsx"):
        name = artifact_name("doc:00007:ndyafme64g7ly", "workbook:shareholder_register",
                             fmt, "alignment")
        assert ":" not in name
        assert name.endswith("__alignment.json")


def test_two_artifacts_that_differ_only_by_case_collide():
    paths = PortablePaths()
    paths.name("doc:1", "Case_Notes", "txt", "v1", directory="gold/v1")
    with pytest.raises(PathCollision):
        paths.name("doc:1", "case_notes", "txt", "v1", directory="gold/v1")


def test_the_same_artifact_maps_to_one_name():
    paths = PortablePaths()
    first = paths.name("doc:1", "case_notes", "txt", "v1", directory="gold/v1")
    second = paths.name("doc:1", "case_notes", "txt", "v1", directory="gold/v1")
    assert first == second


def test_gold_v1_and_v2_share_a_name_in_different_directories():
    paths = PortablePaths()
    one = paths.name("doc:1", "case_notes", "txt", "v1", directory="gold/v1")
    two = paths.name("doc:1", "case_notes", "txt", "v2", directory="gold/v2")
    assert one == two


# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rebuilt(tmp_path_factory):
    if not (PILOT / "manifest.json").exists():
        pytest.skip("Phase G pilot has not been generated")
    target = tmp_path_factory.mktemp("portable") / "v3_pilot_portable"
    result = rebuild_portable(PILOT, target)
    return result, target


def test_the_rebuild_is_clean_and_complete(rebuilt):
    result, target = rebuilt
    assert result.ok, result.problems
    assert len(result.by_role("v1")) == 22
    assert len(result.by_role("v2")) == 22
    assert len(result.by_role("alignment")) == 22
    assert len(result.by_role("rendered")) == 22


def test_no_file_keeps_a_colon_and_every_file_is_non_empty(rebuilt):
    result, target = rebuilt
    for entry in result.files:
        path = target / entry["path"]
        assert ":" not in entry["path"], entry["path"]
        assert path.is_file()
        assert path.stat().st_size > 0


def test_the_workbook_gold_files_are_regular_files_now(rebuilt):
    result, target = rebuilt
    workbook = [entry for entry in result.files
                if "workbook_shareholder_register" in entry["path"]]
    assert len(workbook) == 3          # v1, v2, alignment
    for entry in workbook:
        path = target / entry["path"]
        assert path.is_file() and path.stat().st_size > 0
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        if entry["role"] == "v2":
            # v2 carries the canonical ids; v1 is the vendored format, which uses a
            # ULID document id and no canonical document id at all
            assert "doc:00007:ndyafme64g7ly" in text
            assert "workbook:shareholder_register" in text


def test_content_is_byte_identical_to_the_source(rebuilt):
    result, target = rebuilt
    manifest = json.loads((PILOT / "manifest.json").read_text(encoding="utf-8"))
    expected = {digest for artifact in manifest["artifacts"]
                for digest in artifact["gold"].values()}
    produced = {entry["sha256"] for entry in result.files if entry["role"] in
                {"v1", "v2", "alignment"}}
    assert produced == expected


def test_the_manifest_uses_relative_paths(rebuilt):
    result, target = rebuilt
    manifest = json.loads((target / "manifest_portable.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        assert not Path(entry["path"]).is_absolute()
        assert ":" not in entry["path"]


def test_the_archive_round_trips(rebuilt, tmp_path):
    result, target = rebuilt
    manifest = json.loads((target / "manifest_portable.json").read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    archive = tmp_path / "portable.zip"
    info = archive_portable(target, archive)
    assert info["entries"] == len(expected) + 1      # plus the manifest itself
    check = verify_archive(archive, expected)
    assert check["ok"], check["problems"]
    assert check["checked"] == len(expected)


def test_the_archive_is_byte_deterministic(rebuilt, tmp_path):
    result, target = rebuilt
    first = archive_portable(target, tmp_path / "a.zip")
    second = archive_portable(target, tmp_path / "b.zip")
    assert first["sha256"] == second["sha256"]
