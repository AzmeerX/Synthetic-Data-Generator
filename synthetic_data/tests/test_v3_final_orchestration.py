"""The final orchestration, its portability rules and the independent verifier.

These tests do not build a corpus: they pin the two defects found while scaling and
the naming/checksum contracts the deliverable depends on.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from synthetic_data.canonical import CanonicalDocument

from synthetic_data.final import (CorpusSpec, _archive_tree, _names, _read_checksums,
                                 _write_checksums)
from synthetic_data.final_verify import FINAL_DIR, verify_corpus
from synthetic_data.generation.config import matter_sizes
from synthetic_data.gold.build import gold_paths
from synthetic_data.alignment import align_document, normalise


# -- matter decomposition --------------------------------------------------
def test_matter_sizes_decomposes_a_remainder_the_cycle_cannot_hold():
    """66 documents with at most 8 per matter used to raise AssertionError."""
    sizes = matter_sizes(66, 2)
    assert sum(sizes) == 66
    assert all(7 <= size <= 8 for size in sizes)
    assert len(sizes) == 9


@pytest.mark.parametrize("total", [7, 14, 63, 68, 70, 100, 135, 500, 630])
def test_matter_sizes_still_uses_the_published_cycle(total):
    sizes = matter_sizes(total, 5)
    assert sum(sizes) == total
    assert all(7 <= size <= 11 for size in sizes)


# -- portable names --------------------------------------------------------
def test_gold_paths_never_contain_a_colon():
    v1, v2, alignment = gold_paths(Path("gold"), "doc:00007:ndyafme64g7ly",
                                   "workbook:shareholder_register", "xlsx")
    for path in (v1, v2, alignment):
        assert ":" not in str(path)
        assert path.parent.name in {"v1", "v2", "alignment"}


def test_artifact_names_are_portable_and_distinct():
    document = SimpleNamespace(document_id="doc:00007:ndyafme64g7ly",
                               doc_type="workbook:shareholder_register")
    names = _names(document, "xlsx")
    for value in names.values():
        assert ":" not in value
        assert not value.endswith((" ", "."))
    # gold v1 and v2 legitimately share a basename in different directories; the
    # names that sit in one directory must differ
    assert len({names["rendered"], names["text"], names["accounting"], names["qa"]}) == 4


# -- checksums and archives ------------------------------------------------
def test_checksums_cover_every_file_and_round_trip(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
    _write_checksums(tmp_path)
    entries = _read_checksums(tmp_path)
    assert set(entries) == {"a/one.json", "two.txt"}
    assert "checksums.sha256" not in entries


def test_archive_is_deterministic_and_colon_free(tmp_path):
    root = tmp_path / "corpus_20"
    (root / "rendered").mkdir(parents=True)
    (root / "rendered" / "doc.txt").write_text("hello\n", encoding="utf-8")
    first = _archive_tree(root, tmp_path / "a.zip", prefix=root.name)
    second = _archive_tree(root, tmp_path / "b.zip", prefix=root.name)
    assert first["sha256"] == second["sha256"]
    assert first["entries"] == 1


# -- alignment of a sentence cut by a line break --------------------------
class _View:
    """The parts of DocView the aligner reads, over hand-built parser rows."""

    def __init__(self, texts: list[str]):
        self.text = ""
        self.sentences: list[dict] = []
        for index, text in enumerate(texts):
            start = len(self.text)
            self.text += text
            self.sentences.append({"sentence_id": f"snt{index + 1}", "idx": index,
                                   "char_start": start, "char_end": len(self.text),
                                   "filtered": False, "page": 4})
            self.text += "\n"

    def sentence_text(self, row):
        return self.text[row["char_start"]:row["char_end"]]


def _canonical(*texts: str) -> CanonicalDocument:
    """The aligner reads only ``sentences``; a namespace keeps the test cheap."""
    return cast(CanonicalDocument, SimpleNamespace(sentences=[
        SimpleNamespace(sentence_id=f"cs{index + 1}", block_id="blk:1", text=text,
                        kind="sentence", index=index)
        for index, text in enumerate(texts)]))


def test_a_line_break_inside_a_sentence_leaves_no_unclaimed_parser_text():
    """doc:00313: the parser cut "...in section | 6. The analysis ... and |
    recommend a side letter." and the tail sentence was left unexplained."""
    view = _View([
        "For these reasons, The analysis note that EBITDA needs adjustment subject "
        "to the qualifications in section",
        "6. The analysis consider the amendment mechanism workable assuming "
        "continued solvency, and",
        "recommend a side letter.",
        "In our view identify tension with the disposal covenant in the light of "
        "the lender's policy.",
    ])
    canonical = _canonical(
        "For these reasons, The analysis note that EBITDA needs adjustment subject "
        "to the qualifications in section 6.",
        "The analysis consider the amendment mechanism workable assuming continued "
        "solvency, and recommend a side letter.",
        "In our view identify tension with the disposal covenant in the light of "
        "the lender's policy.")
    units = align_document(canonical, view)
    first, second, third = units
    assert first.kind == "split" and first.sentence_ids == ("snt1", "snt2")
    # the first sentence ends inside snt2, so snt2 also opens the second one
    assert second.kind == "split" and second.sentence_ids == ("snt2", "snt3")
    assert third.kind == "exact" and third.sentence_ids == ("snt4",)
    claimed = {sid for unit in units for sid in unit.sentence_ids}
    assert claimed == {"snt1", "snt2", "snt3", "snt4"}


def test_a_repeated_sentence_is_not_a_wrapped_split():
    """The same text twice must not be explained by claiming the first copy twice."""
    text = "The analysis consider the amendment mechanism workable."
    view = _View([text, text])
    units = align_document(_canonical(text, text), view)
    assert [unit.kind for unit in units] == ["exact", "exact"]
    assert units[1].sentence_ids == ("snt2",)


# -- configuration contract ------------------------------------------------
def test_configuration_records_every_generation_input():
    spec = CorpusSpec("corpus_500", 500, 7, 68, 7, 5)
    configuration = spec.configuration
    assert configuration["seed"] == 7
    assert configuration["documents"] == 68
    assert configuration["workbooks"] == 7
    assert configuration["max_letters"] == 5
    assert configuration["corpus_name"] == "corpus_500"
    assert spec.config().corpus_name == "corpus_500"


# -- the published pilot, when it exists -----------------------------------
@pytest.mark.skipif(not (FINAL_DIR / "corpus_20" / "manifest.json").exists(),
                    reason="final corpus_20 has not been built")
def test_published_corpus_verifies_without_importing_the_generator():
    result = verify_corpus(FINAL_DIR / "corpus_20", fresh=False)
    assert result["problems"] == []
    assert result["ok"] is True
    manifest = json.loads((FINAL_DIR / "corpus_20" / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert result["pages"]["actual"] == manifest["pages"]["actual"]
