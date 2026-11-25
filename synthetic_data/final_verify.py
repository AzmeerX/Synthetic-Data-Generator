"""Independent verification of a published V3 corpus.

This module deliberately imports **none** of the corpus generator: no
``synthetic_data.generation``, no ``synthetic_data.gold.build``, no
``synthetic_data.qa.build``.  It reads the published files and asks three questions:

1. Is every file the manifest claims present, non-empty, portable and hashing to the
   digest in ``checksums.sha256``?
2. Do the annotations agree with the document text — is every evidence quote actually
   in the parsed text, is every page within the document, is every referenced fact
   declared, is every question id unique?
3. Does a **fresh parse** of the rendered file still produce those quotes and that
   page count?  (The parser is the ingestion system, not the generator.)

    py -3.13 -m synthetic_data.final_verify --corpus corpus_500
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

__all__ = ["verify_corpus", "verify_all", "main"]

REPO_ROOT = Path(__file__).resolve().parents[1]
FINAL_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_final"
_WHITESPACE = re.compile(r"\s+")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _normal(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _read_checksums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        entries[name] = digest
    return entries


def _portable_problem(name: str) -> str | None:
    if ":" in name:
        return f"colon in path: {name}"
    if name.endswith((" ", ".")):
        return f"trailing space or dot: {name}"
    reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
        | {f"lpt{i}" for i in range(1, 10)}
    for part in name.split("/"):
        if part.split(".", 1)[0].lower() in reserved:
            return f"reserved device name: {name}"
    return None


def _check_files(root: Path, problems: list[str]) -> dict[str, Any]:
    checksums_path = root / "checksums.sha256"
    if not checksums_path.exists():
        problems.append("checksums.sha256 is missing")
        return {"listed": 0, "verified": 0}
    listed = _read_checksums(checksums_path)
    verified = 0
    for name, expected in listed.items():
        trouble = _portable_problem(name)
        if trouble:
            problems.append(trouble)
        path = root / name
        if not path.exists():
            problems.append(f"listed file missing: {name}")
            continue
        if not path.is_file():
            problems.append(f"not a regular file (alternate data stream?): {name}")
            continue
        if path.stat().st_size == 0:
            problems.append(f"zero-byte file: {name}")
            continue
        if _digest(path) != expected:
            problems.append(f"checksum mismatch: {name}")
            continue
        verified += 1
    on_disk = {path.relative_to(root).as_posix() for path in root.rglob("*")
               if path.is_file()
               and path.name not in {"checksums.sha256", "_COMPLETE"}}
    unlisted = sorted(on_disk - set(listed))
    if unlisted:
        problems.append(f"files not in checksums.sha256: {unlisted[:5]}")
    return {"listed": len(listed), "verified": verified,
            "on_disk": len(on_disk), "unlisted": len(unlisted)}


def _check_manifest(root: Path, manifest: dict[str, Any],
                    problems: list[str]) -> dict[str, Any]:
    items = manifest["artifacts"]
    by_format: dict[str, int] = {}
    pages = 0
    documents = set()
    for item in items:
        by_format[item["format"]] = by_format.get(item["format"], 0) + 1
        documents.add(item["document_id"])
        if item["format"] == "pdf":
            pages += item["pages"] or 0
    counts = manifest.get("artifact_counts") or {}
    if counts.get("total") != len(items):
        problems.append("artifact_counts.total does not match the item list")
    if dict(sorted(by_format.items())) != counts.get("by_format"):
        problems.append("artifact_counts.by_format does not match the item list")
    if manifest["documents"]["total"] != len(documents):
        problems.append("documents.total does not match the distinct ids")
    if manifest["pages"]["actual"] != pages:
        problems.append(f"pages.actual {manifest['pages']['actual']} != sum of "
                        f"artifact pages {pages}")
    if not items:
        problems.append("no artifacts listed")
    return {"artifacts": len(items), "documents": len(documents), "pages": pages,
            "by_format": dict(sorted(by_format.items()))}


def _gold_path(root: Path, item: dict[str, Any], role: str) -> Path:
    stem = (Path(item["path"]).name.rsplit(".", 1)[0])
    if role == "alignment":
        base = stem.rsplit("__", 2)[0]
        return root / "gold" / "alignment" / f"{base}__{item['format']}.json"
    return root / "gold" / role / f"{stem}.json"


def _text_path(root: Path, item: dict[str, Any]) -> Path:
    stem = Path(item["path"]).name.rsplit(".", 1)[0]
    return root / "parsed" / f"{stem}.txt"


def _check_gold_and_qa(root: Path, manifest: dict[str, Any],
                       problems: list[str]) -> dict[str, Any]:
    items = manifest["artifacts"]
    evidence_checked = 0
    questions_checked = 0
    duplicates = 0
    twin_answers = 0
    twin_problems = 0
    per_document_questions: dict[str, dict[str, dict[str, Any]]] = {}

    for item in items:
        stem = Path(item["path"]).name.rsplit(".", 1)[0]
        text_path = _text_path(root, item)
        if not text_path.exists():
            problems.append(f"parsed text missing: {text_path.name}")
            continue
        text = _normal(text_path.read_text(encoding="utf-8"))
        v2_path = _gold_path(root, item, "v2")
        alignment_path = _gold_path(root, item, "alignment")
        for path in (v2_path, alignment_path, _gold_path(root, item, "v1")):
            if not path.exists():
                problems.append(f"gold artifact missing: {path.name}")
        if not v2_path.exists() or not alignment_path.exists():
            continue
        gold = json.loads(v2_path.read_text(encoding="utf-8"))
        alignment = json.loads(alignment_path.read_text(encoding="utf-8"))

        if gold["doc"]["format"] != item["format"]:
            problems.append(f"{item['path']}: gold format mismatch")
        if (gold["doc"]["pages"] or 0) != (item["pages"] or 0):
            problems.append(f"{item['path']}: gold pages {gold['doc']['pages']} != "
                            f"manifest {item['pages']}")
        ids: list[str] = []
        for entry in gold.get("entities", []):
            ids.append(entry["id"])
            for mention in entry.get("mentions", []):
                evidence_checked += 1
                if _normal(mention.get("quote", "")) not in text:
                    problems.append(f"{item['path']}: entity quote not in parsed text: "
                                    f"{mention.get('quote', '')[:50]!r}")
        for section in ("edges", "propositions"):
            for entry in gold.get(section, []):
                ids.append(entry["id"])
                for evidence in entry.get("evidence", []):
                    evidence_checked += 1
                    if _normal(evidence.get("quote", "")) not in text:
                        problems.append(f"{item['path']}: evidence quote not in parsed "
                                        f"text: {evidence.get('quote', '')[:50]!r}")
                    page = (evidence.get("render_loc") or {}).get("page")
                    if page is not None and item["pages"] is not None:
                        if not 1 <= int(page) <= int(item["pages"]):
                            problems.append(f"{item['path']}: evidence page {page} "
                                            f"outside 1..{item['pages']}")
                    if evidence.get("render_format") not in {None, item["format"]}:
                        problems.append(f"{item['path']}: evidence format "
                                        f"{evidence.get('render_format')}")
        if len(ids) != len(set(ids)):
            duplicates += len(ids) - len(set(ids))
            problems.append(f"{item['path']}: duplicate canonical ids in gold v2")
        units = alignment.get("units", [])
        if not units:
            problems.append(f"{item['path']}: alignment has no units")
        counts = gold.get("alignment", {}).get("counts", {})
        if counts and sum(counts.values()) != len(units):
            problems.append(f"{item['path']}: alignment counts do not sum to units")

        qa_path = root / "qa" / f"{stem.split('__')[0]}__{item['format']}.json"
        if not qa_path.exists():
            base = item["document_id"].replace(":", "_")
            qa_path = root / "qa" / f"{base}__{item['format']}.json"
        if not qa_path.exists():
            problems.append(f"QA set missing for {item['path']}")
            continue
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        declared = set(ids) | {value["id"] for value in
                               gold.get("propositions", []) if value.get("derived_from")}
        seen: set[str] = set()
        answers: dict[str, Any] = {}
        for question in qa["questions"]:
            questions_checked += 1
            qid = question["question_id"]
            if qid in seen:
                duplicates += 1
                problems.append(f"{qa_path.name}: duplicate question id {qid}")
            seen.add(qid)
            if question.get("format") != item["format"]:
                problems.append(f"{qa_path.name}: question format mismatch")
            if question["answerable"]:
                if not question.get("evidence"):
                    problems.append(f"{qa_path.name}: {qid} has no evidence")
                for evidence in question.get("evidence", []):
                    if _normal(evidence.get("quote", "")) not in text:
                        problems.append(f"{qa_path.name}: {qid} quote not in parsed text")
                for fact_id in question.get("fact_ids", []):
                    if fact_id not in declared:
                        problems.append(f"{qa_path.name}: {qid} references undeclared "
                                        f"fact {fact_id}")
                if not (question.get("expected_answer") or "").strip():
                    problems.append(f"{qa_path.name}: {qid} has an empty answer")
                answers[qid] = (question.get("question_type"),
                                question.get("expected_answer"))
            else:
                proof = question.get("proof") or {}
                if not proof.get("documents_searched"):
                    problems.append(f"{qa_path.name}: {qid} proof names no documents")
                for search in proof.get("text_searches", []) or []:
                    needle = search.get("needle", "")
                    if needle and needle.lower() in text.lower():
                        problems.append(f"{qa_path.name}: {qid} proof needle "
                                        f"{needle!r} occurs in its own text")
        per_document_questions.setdefault(item["document_id"], {})[item["format"]] = answers

    for document_id, formats in per_document_questions.items():
        if len(formats) < 2:
            continue
        reference_format = "txt" if "txt" in formats else sorted(formats)[0]
        reference = formats[reference_format]
        for fmt, answers in formats.items():
            if fmt == reference_format:
                continue
            shared = set(answers) & set(reference)
            if reference and len(shared) / len(reference) < 0.75:
                twin_problems += 1
                problems.append(f"{document_id}/{fmt}: only {len(shared)} of "
                                f"{len(reference)} question ids shared with "
                                f"{reference_format}")
            for qid in shared:
                twin_answers += 1
                kind, answer = answers[qid]
                if kind == "provenance":
                    continue
                if answer != reference[qid][1]:
                    twin_problems += 1
                    problems.append(f"{document_id}/{fmt}: {qid} answer "
                                    f"{answer!r} != {reference_format} "
                                    f"{reference[qid][1]!r}")
    return {"evidence_checked": evidence_checked, "questions_checked": questions_checked,
            "duplicates": duplicates, "twin_answers_compared": twin_answers,
            "twin_problems": twin_problems}


def _fresh_parse(root: Path, manifest: dict[str, Any],
                 problems: list[str]) -> dict[str, Any]:
    """Re-ingest a deterministic sample with the real parser and re-check quotes."""
    from synth.config import load_config
    from synth.modules.common import DocView
    from synth.modules.u01_parser import ingest_path
    from synth.store import test_store as make_test_store

    items = manifest["artifacts"]
    sample: list[dict[str, Any]] = []
    seen_formats: set[str] = set()
    for item in items:
        if item["format"] not in seen_formats:
            sample.append(item)
            seen_formats.add(item["format"])
    for item in items:
        if len(sample) >= 12:
            break
        if item not in sample and item["format"] == "pdf":
            sample.append(item)
    config = load_config()
    store = make_test_store()
    checked_quotes = 0
    pages_checked = 0
    try:
        for item in sample:
            path = root / item["path"]
            records = ingest_path(store, config, path)
            if not records:
                problems.append(f"fresh parse produced no record for {item['path']}")
                continue
            with store.reader() as reader:
                view = DocView.load(reader, records[0]["doc_id"], config)
            text = _normal(view.text)
            if item["format"] == "pdf":
                fresh_pages = max((sentence.get("page") or 0) for sentence in view.sentences)
                if fresh_pages != item["pages"]:
                    problems.append(f"{item['path']}: fresh parse has {fresh_pages} "
                                    f"pages, manifest says {item['pages']}")
                else:
                    pages_checked += 1
            v2_path = _gold_path(root, item, "v2")
            gold = json.loads(v2_path.read_text(encoding="utf-8"))
            for section in ("edges", "propositions"):
                for entry in gold.get(section, []):
                    for evidence in entry.get("evidence", []):
                        checked_quotes += 1
                        if _normal(evidence.get("quote", "")) not in text:
                            problems.append(f"{item['path']}: fresh parse lost the quote "
                                            f"{evidence.get('quote', '')[:40]!r}")
    finally:
        store.dispose()
    return {"artifacts_parsed": len(sample), "quotes_checked": checked_quotes,
            "pdf_pages_checked": pages_checked}


def _check_contradictions(manifest: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    contradictions = manifest.get("contradictions") or {}
    if contradictions.get("planned") and not contradictions.get("both_sides_grounded"):
        problems.append("no contradiction is grounded on both sides")
    ungrounded = [item["contradiction_id"] for item in contradictions.get("items", [])
                  if not item["both_sides_grounded"]]
    return {"planned": contradictions.get("planned", 0),
            "both_sides_grounded": contradictions.get("both_sides_grounded", 0),
            "ungrounded": ungrounded}


def verify_corpus(root: Path, *, fresh: bool = True) -> dict[str, Any]:
    root = Path(root)
    problems: list[str] = []
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"corpus": root.name, "ok": False,
                "problems": ["manifest.json is missing"]}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = _check_files(root, problems)
    counts = _check_manifest(root, manifest, problems)
    gold = _check_gold_and_qa(root, manifest, problems)
    parsed = _fresh_parse(root, manifest, problems) if fresh else {}
    contradictions = _check_contradictions(manifest, problems)
    determinism = manifest.get("determinism") or {}
    if not determinism.get("identical_replan"):
        problems.append("determinism: replan was not identical")
    if determinism.get("render_identical") is False:
        problems.append("determinism: a re-render differed")
    if not manifest.get("all_valid"):
        problems.append("manifest marks the corpus invalid")
    return {
        "corpus": manifest.get("corpus", root.name),
        "ok": not problems,
        "pages": {"target": manifest["pages"]["target"],
                  "actual": manifest["pages"]["actual"],
                  "exact": manifest["pages"]["exact"]},
        "documents": manifest["documents"],
        "artifacts": counts,
        "files": files,
        "gold_and_qa": gold,
        "fresh_parse": parsed,
        "contradictions": contradictions,
        "determinism": determinism,
        "problems": problems,
    }


def verify_all(*, fresh: bool = True) -> dict[str, Any]:
    corpora = sorted(path for path in FINAL_DIR.glob("corpus_*") if path.is_dir())
    results = [verify_corpus(path, fresh=fresh) for path in corpora]
    return {"corpora": results, "ok": all(result["ok"] for result in results),
            "problems": {result["corpus"]: result["problems"] for result in results
                         if result["problems"]}}


def check_v2_checksums(problems: list[str]) -> dict[str, Any]:
    """The pre-existing V2 datasets must still match the repository's manifest."""
    manifest = REPO_ROOT / "SHA256SUMS.txt"
    if not manifest.exists():
        return {"checked": 0, "skipped": "SHA256SUMS.txt is absent"}
    checked = missing = mismatched = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        if "_v2/" not in name:
            continue
        path = REPO_ROOT / name
        if not path.exists():
            missing += 1
            continue
        if _digest(path) != digest:
            mismatched += 1
            problems.append(f"V2 checksum changed: {name}")
        else:
            checked += 1
    return {"checked": checked, "missing": missing, "mismatched": mismatched}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Verify published V3 corpora")
    parser.add_argument("--corpus", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--no-fresh", action="store_true",
                        help="skip the fresh parser pass")
    parser.add_argument("--json", default=None, help="write the report here")
    args = parser.parse_args(argv)
    targets = ([FINAL_DIR / name for name in args.corpus] if args.corpus
               else sorted(path for path in FINAL_DIR.glob("corpus_*") if path.is_dir()))
    if not targets:
        print("no corpora found under", FINAL_DIR)
        return 1
    results = [verify_corpus(path, fresh=not args.no_fresh) for path in targets]
    v2_problems: list[str] = []
    v2 = check_v2_checksums(v2_problems)
    payload = {"corpora": results, "ok": all(result["ok"] for result in results)
               and not v2_problems,
               "v2_checksums": v2, "v2_problems": v2_problems}
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
    for result in results:
        print(f"{result['corpus']}: {'OK' if result['ok'] else 'FAILED'} — "
              f"{result['pages']['actual']} pages, "
              f"{result['artifacts']['artifacts']} artifacts, "
              f"{len(result['problems'])} problems")
        for problem in result["problems"][:10]:
            print("   -", problem)
    print("v2 checksums:", v2, "problems:", len(v2_problems))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":       # pragma: no cover - entry point
    raise SystemExit(main())
