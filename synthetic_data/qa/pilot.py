"""Phase H pilot: QA benchmark over the Phase G corpus, plus independent validation.

    py -3.13 -m synthetic_data.qa.pilot

Reads the Phase G artifacts (never modifies them), re-ingests every rendered file
through the real parser, generates the question set from the gold v2 artifact and
the canonical world, then validates the result with a separately written checker.

Output under ``synthetic_data/output/v3_qa/``:

    qa/<document>__<format>.json   one set per document-format (twins share ids)
    manifest.json                  counts, coverage, validation, hashes
    PHASE_H_REPORT.md              the report for review
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synth.config import load_config
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.gold.model import GoldV2
from synthetic_data.qa.build import CorpusTexts, QASource, generate, \
    generate_unanswerable
from synthetic_data.qa.model import QASet
from synthetic_data.qa.taxonomy import QUESTION_TYPES, UNANSWERABLE_KINDS
from synthetic_data.qa.validate import ValidationInput, coverage_report, \
    validate_corpus, validate_set
from synthetic_data.renderers import (DEFAULT_DOCX_PROFILE, DEFAULT_PDF_PROFILE,
                                      DEFAULT_TXT_PROFILE, DEFAULT_XLSX_PROFILE,
                                      DocxRenderer, PdfRenderer, TxtRenderer,
                                      XlsxRenderer)

__all__ = ["SEED", "CORPUS", "QA_DIR", "PILOT_DIR", "build_qa", "build_one",
           "qa_filename"]

SEED = 7
CORPUS = "v3_pilot"
REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_pilot"
QA_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_qa"
PROSE_FORMATS = ("txt", "pdf", "docx")
RENDERERS = {
    "txt": (TxtRenderer, DEFAULT_TXT_PROFILE, ".txt"),
    "pdf": (PdfRenderer, DEFAULT_PDF_PROFILE, ".pdf"),
    "docx": (DocxRenderer, DEFAULT_DOCX_PROFILE, ".docx"),
    "xlsx": (XlsxRenderer, DEFAULT_XLSX_PROFILE, ".xlsx"),
}
#: the two documents that carry the unanswerable sets (one prose, one workbook)
UNANSWERABLE_DOCUMENTS = 2


def qa_filename(document_id: str, fmt: str) -> str:
    return f"{document_id.replace(':', '_')}__{fmt}.json"


@dataclass
class Loaded:
    document: Any
    canonical: Any
    render: Any
    view: Any
    gold_v2: dict[str, Any]
    alignment: dict[str, Any]
    v2: GoldV2
    path: Path
    format: str


def build_qa(out_dir: Path | None = None) -> dict[str, Any]:
    root = Path(out_dir) if out_dir is not None else QA_DIR
    for name in ("qa",):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
    (root / "qa").mkdir(parents=True, exist_ok=True)

    config = load_config()
    store = make_test_store()
    plan = build_corpus(CorpusConfig(seed=SEED, corpus_name=CORPUS,
                                     documents=7, workbooks=1))
    scratch = Path(tempfile.mkdtemp(prefix="qa_h_"))
    loaded: list[Loaded] = []
    try:
        for doc_id in plan.prose_ids:
            document = plan.documents[doc_id]
            canonical = canonicalize(document)
            prepared = apply_statement_sentences(document, canonical)
            for fmt in PROSE_FORMATS:
                loaded.append(_load(prepared, canonical, fmt, store, config, scratch))
        workbook_id = plan.workbook_ids[0]
        workbook = plan.documents[workbook_id]
        loaded.append(_load(workbook, None, "xlsx", store, config, scratch))

        corpus = CorpusTexts.build(
            (item.document.document_id, item.format, item.view.text,
             item.document.doc_kind) for item in loaded)
        stated = {item.document.document_id: sorted(
            {entry["id"] for section in ("edges", "propositions")
             for entry in item.gold_v2.get(section, [])}) for item in loaded}

        sets: list[QASet] = []
        reports: dict[str, Any] = {}
        for item in loaded:
            source = QASource(
                document=item.document, canonical=item.canonical,
                render=item.render, view=item.view,
                pair=item, world=plan.world, corpus=CORPUS, seed=SEED, plan=plan)
            qa = generate(source, corpus)
            if item.document.document_id in _unanswerable_ids(plan):
                extra = generate_unanswerable(source, corpus)
                qa = QASet(corpus=qa.corpus, seed=qa.seed,
                           document_id=qa.document_id, doc_type=qa.doc_type,
                           format=qa.format,
                           questions=[*qa.questions, *extra.questions])
            path = root / "qa" / qa_filename(qa.document_id, qa.format)
            path.write_text(_dump(qa), encoding="utf-8")

            report = validate_set(ValidationInput(
                qa=qa, gold_v2=item.gold_v2, alignment=item.alignment,
                world=plan.world, plan=plan, fresh_text=item.view.text,
                corpus=corpus, workbook=item.document
                if item.document.doc_kind == "workbook" else None,
                render_map=item.render,
                stated_by_document=stated))
            sets.append(qa)
            reports[path.name] = {
                "document_id": qa.document_id, "format": qa.format,
                "questions": len(qa.questions),
                "answerable": len(qa.answerable),
                "unanswerable": len(qa.unanswerable),
                "counts": dict(qa.counts), "rejected": dict(qa.rejected),
                "validation": {"checked": report.checked,
                               "errors": report.errors,
                               "notes": _jsonable(report.notes)},
                "sha256": _digest(path), "path": str(path.relative_to(REPO_ROOT)),
            }

        corpus_report = validate_corpus(sets, corpus, stated)
        proofs = [question for qa in sets for question in qa.unanswerable]
        rejected: Counter = Counter()
        for qa in sets:
            rejected.update(qa.rejected)
        evidence_total = sum(len(question.evidence) for qa in sets
                             for question in qa.answerable)
        provenance = sum(1 for qa in sets for question in qa.answerable
                         if question.question_type == "provenance")
    finally:
        store.dispose()
        shutil.rmtree(scratch, ignore_errors=True)

    manifest = {
        "phase_g_integrity": _phase_g_integrity(),
        "phase": "H",
        "corpus": CORPUS, "seed": SEED,
        "source_corpus": str(PILOT_DIR.relative_to(REPO_ROOT)),
        "documents": len({item.document.document_id for item in loaded}),
        "artifacts": len(loaded),
        "sets": reports,
        "coverage": coverage_report(sets),
        "types_required": list(QUESTION_TYPES),
        "kinds_required": list(UNANSWERABLE_KINDS),
        "corpus_validation": {
            "checked": corpus_report.checked,
            "errors": corpus_report.errors,
            "notes": _jsonable(corpus_report.notes),
        },
        "evidence": {
            "items": evidence_total,
            "provenance_questions": provenance,
            "proofs": len(proofs),
            "proof_text_searches": sum(len(question.proof.text_searches)
                                       for question in proofs),
            "proof_absence_checks": sum(len(question.proof.absence_checks)
                                        for question in proofs),
        },
        "rejected_candidates": dict(rejected),
        "cross_format": {
            "twins_compared": corpus_report.checked,
            "format_only": corpus_report.notes.get("format_only", {}),
            "format_specific_locations":
                corpus_report.notes.get("format_specific_locations", 0),
        },
        "all_valid": not corpus_report.errors and all(
            not entry["validation"]["errors"] for entry in reports.values()),
        "determinism": _determinism(loaded, corpus, plan),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "PHASE_H_REPORT.md").write_text(
        _report(manifest, sets), encoding="utf-8")
    return manifest


def build_one(document_id: str, fmt: str) -> QASet:
    """Generate one set in isolation (used by the fixture script and its tests).

    The corpus is this document only, so absence proofs are not generated here;
    the answerable questions are identical to the ones the pilot writes.
    """
    config = load_config()
    store = make_test_store()
    plan = build_corpus(CorpusConfig(seed=SEED, corpus_name=CORPUS,
                                     documents=7, workbooks=1))
    scratch = Path(tempfile.mkdtemp(prefix="qa_one_"))
    try:
        document = plan.documents[document_id]
        canonical = None
        prepared = document
        if document.doc_kind != "workbook":
            canonical = canonicalize(document)
            prepared = apply_statement_sentences(document, canonical)
        item = _load(prepared, canonical, fmt, store, config, scratch)
        corpus = CorpusTexts.build([(document_id, fmt, item.view.text,
                                     document.doc_kind)])
        source = QASource(document=prepared, canonical=canonical, render=item.render,
                          view=item.view, pair=item, world=plan.world, corpus=CORPUS,
                          seed=SEED, plan=plan)
        return generate(source, corpus)
    finally:
        store.dispose()
        shutil.rmtree(scratch, ignore_errors=True)


def _phase_g_integrity() -> dict[str, Any]:
    """Verify the Phase G artifacts against their own manifest, without writing."""
    manifest = json.loads((PILOT_DIR / "manifest.json").read_text(encoding="utf-8"))
    checked = 0
    problems: list[str] = []
    portable_risk: list[str] = []
    for artifact in manifest["artifacts"]:
        base = artifact["document_id"].replace(":", "_")
        names = {
            "v1": f"{base}__{artifact['doc_type']}__{artifact['format']}.json",
            "v2": f"{base}__{artifact['doc_type']}__{artifact['format']}.json",
            "alignment": f"{base}__{artifact['format']}.json",
        }
        for role, name in names.items():
            path = PILOT_DIR / "gold" / role / name
            if not path.exists():
                problems.append(f"missing {role}/{name}")
                continue
            if _digest(path) != artifact["gold"][role]:
                problems.append(f"changed {role}/{name}")
                continue
            checked += 1
            if ":" in name:
                portable_risk.append(f"{role}/{name}")
    return {"gold_verified": checked, "gold_expected": len(manifest["artifacts"]) * 3,
            "rendered": len(list((PILOT_DIR / "rendered").iterdir())),
            "problems": problems, "non_portable_names": portable_risk}


def _unanswerable_ids(plan) -> set[str]:
    prose = list(plan.prose_ids)
    picked = {prose[min(UNANSWERABLE_DOCUMENTS, len(prose) - 1)]}
    picked.add(plan.workbook_ids[0])
    return picked


def _load(document, canonical, fmt: str, store, config, scratch: Path) -> Loaded:
    renderer_class, profile, suffix = RENDERERS[fmt]
    name = f"{document.document_id.replace(':', '_')}{suffix}"
    published = PILOT_DIR / "rendered" / name
    if not published.exists():
        raise RuntimeError(f"Phase G artifact missing: {published}")
    base = document.document_id.replace(':', '_')
    gold_path = PILOT_DIR / "gold" / "v2" / f"{base}__{document.doc_type}__{fmt}.json"
    alignment_path = PILOT_DIR / "gold" / "alignment" / f"{base}__{fmt}.json"
    gold_v2 = json.loads(gold_path.read_text(encoding="utf-8"))
    alignment = json.loads(alignment_path.read_text(encoding="utf-8"))

    render = renderer_class().render(document, profile, scratch / name)
    records = ingest_path(store, config, published)
    if not records:
        raise RuntimeError(f"ingest produced no record for {published}")
    with store.reader() as reader:
        view = DocView.load(reader, records[0]["doc_id"], config)
    return Loaded(document=document, canonical=canonical, render=render, view=view,
                  gold_v2=gold_v2, alignment=alignment,
                  v2=GoldV2.model_validate(gold_v2), path=published, format=fmt)


def _dump(qa: QASet) -> str:
    return json.dumps(qa.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _determinism(loaded: list[Loaded], corpus: CorpusTexts, plan) -> dict[str, Any]:
    """Generate twice from the same inputs: the QA JSON must be byte-identical."""
    first: dict[str, str] = {}
    second: dict[str, str] = {}
    for item in loaded:
        source = QASource(document=item.document, canonical=item.canonical,
                          render=item.render, view=item.view, pair=item,
                          world=plan.world, corpus=CORPUS, seed=SEED, plan=plan)
        for bucket in (first, second):
            qa = generate(source, corpus)
            bucket[qa_filename(qa.document_id, qa.format)] = hashlib.sha256(
                _dump(qa).encode()).hexdigest()
    return {"identical": first == second, "sets": len(first)}


# ---------------------------------------------------------------------------
def _report(manifest: dict[str, Any], sets: list[QASet]) -> str:
    lines: list[str] = []
    add = lines.append
    coverage = manifest["coverage"]
    answerable = sum(coverage["types"].values())
    unanswerable = sum(coverage["kinds"].values())
    total = answerable + unanswerable

    add("# Phase H — QA benchmark over the Phase G pilot corpus")
    add("")
    add(f"Source corpus: `{manifest['source_corpus']}` (unchanged) · seed "
        f"{manifest['seed']} · {manifest['documents']} semantic documents · "
        f"{manifest['artifacts']} rendered artifacts · {len(sets)} QA sets")
    add("")
    add("## 1. Taxonomy and counts")
    add("")
    add("| question type | questions |")
    add("|---|---|")
    for question_type in QUESTION_TYPES:
        if question_type == "unanswerable":
            continue
        add(f"| `{question_type}` | {coverage['types'].get(question_type, 0)} |")
    add("")
    add("| unanswerable kind | questions |")
    add("|---|---|")
    for kind in UNANSWERABLE_KINDS:
        add(f"| `{kind}` | {coverage['kinds'].get(kind, 0)} |")
    add("")
    add(f"Total {total} questions: {answerable} answerable "
        f"({answerable / total:.1%}), {unanswerable} unanswerable "
        f"({unanswerable / total:.1%}).")
    add("")
    add("## 2. Per document")
    add("")
    add("| document | sets | questions |")
    add("|---|---|---|")
    for document_id, count in sorted(coverage["per_document"].items()):
        sets_here = sum(1 for entry in manifest["sets"].values()
                        if entry["document_id"] == document_id)
        add(f"| `{document_id}` | {sets_here} | {count} |")
    add("")
    add("A document rendered in three formats is one semantic document: the same "
        "question id appears in each format's set with the same answer and "
        "format-specific evidence.")
    add("")
    add("## 3. Independent validation")
    add("")
    per_set = manifest["sets"]
    checked = sum(entry["validation"]["checked"] for entry in per_set.values())
    errors = sum(len(entry["validation"]["errors"]) for entry in per_set.values())
    add(f"{checked} question checks over {len(per_set)} sets; {errors} errors. "
        f"Corpus-level checks: {manifest['corpus_validation']['checked']}, "
        f"{len(manifest['corpus_validation']['errors'])} errors.")
    add("")
    add("Checks performed by a module that does not import the generator:")
    add("")
    add("- every evidence quote occurs in a **fresh parse** of the same file;")
    add("- every cited sentence id is the sentence the gold v2 artifact uses for "
        "that fact;")
    add("- answers are recomputed from the canonical world (entities, relations, "
        "amounts, dates, statuses), not compared with the generator's own text;")
    add("- derived workbook answers are recomputed with the formula evaluator;")
    add("- provenance answers are compared with the render location in gold v2;")
    add("- every unanswerable proof re-runs its searches over every parser text;")
    add("- duplicates and near-duplicates are detected per set;")
    add("- twins are compared question id by question id across formats.")
    add("")
    if manifest["corpus_validation"]["errors"]:
        add("```")
        for error in manifest["corpus_validation"]["errors"][:20]:
            add(error)
        add("```")
        add("")
    evidence = manifest["evidence"]
    add("### 3a. Evidence and provenance")
    add("")
    add(f"{evidence['items']} evidence items across {answerable} answerable questions; "
        f"every quote occurs in a fresh parse of its own file and every sentence id "
        f"matches the gold v2 artifact. {evidence['provenance_questions']} questions "
        f"ask for a location: {manifest['cross_format']['format_specific_locations']} "
        f"of them name a different location in a different format, which is expected — "
        f"the same fact sits on another page, in another section, or in a cell.")
    add("")
    add("### 3b. Unanswerable proofs")
    add("")
    add(f"{evidence['proofs']} unanswerable questions, each with a proof: "
        f"{evidence['proof_text_searches']} corpus-wide text searches and "
        f"{evidence['proof_absence_checks']} sentence-level co-occurrence checks, all "
        f"re-run by the validator, all empty. A proof is refused at construction time "
        f"if any cited search found something, so a false negative cannot be written.")
    add("")
    add("### 3c. Duplicates")
    add("")
    rejected = manifest["rejected_candidates"]
    add(f"Candidates dropped before publication: {rejected.get('duplicate', 0)} "
        f"duplicates (same wording, or the same words with the pair reversed) and "
        f"{rejected.get('cap', 0)} over the per-type cap.")
    similarities = [entry["validation"]["notes"].get("max_question_similarity", 0.0)
                    for entry in per_set.values()]
    add(f"Highest token overlap between two questions in one set: "
        f"{max(similarities):.3f} (near-duplicates are refused at 0.95).")
    add("")
    add("### 3d. Cross-format consistency")
    add("")
    cross = manifest["cross_format"]
    add(f"{cross['twins_compared']} question ids compared across TXT, PDF and DOCX of "
        f"the same document: answers identical except for provenance locations. "
        f"Where a format states fewer facts, the difference is reported rather than "
        f"hidden:")
    add("")
    for key, entry in sorted(cross["format_only"].items()):
        add(f"- `{key}`: {entry['overlap']:.1%} of ids shared "
            f"({len(entry['missing'])} only in TXT, {len(entry['extra'])} only here)")
    add("")
    add("## 4. Fact coverage")
    add("")
    fact_coverage = manifest["corpus_validation"]["notes"].get("fact_coverage", {})
    add(f"{fact_coverage.get('covered', 0)} of {fact_coverage.get('facts', 0)} facts "
        f"stated by the corpus are the subject of at least one question "
        f"({fact_coverage.get('covered', 0) / max(fact_coverage.get('facts', 1), 1):.1%}).")
    add("")
    add("## 5. Determinism")
    add("")
    add(f"Two independent generations of every set from identical inputs: "
        f"byte-identical = {manifest['determinism']['identical']} "
        f"({manifest['determinism']['sets']} sets).")
    add("")
    add("## 6. What this pilot does not measure")
    add("")
    add("- Extraction accuracy of the FYP pipeline: the extraction system has not "
        "been benchmarked, so no model score is claimed here.")
    add("- Answer quality of any model: the benchmark states expected answers and "
        "their evidence; it does not evaluate a reader.")
    add("- Gold is `mode: generated`, not blind or human-adjudicated.")
    add("")
    integrity = manifest["phase_g_integrity"]
    add("## 7. Preflight: the Phase G corpus is untouched")
    add("")
    add(f"{integrity['gold_verified']} of {integrity['gold_expected']} gold artifacts and "
        f"{integrity['rendered']} rendered files verify against the Phase G manifest. "
        f"Phase H only read them.")
    if integrity["non_portable_names"]:
        add("")
        add(f"Defect found in the Phase G output: {len(integrity['non_portable_names'])} "
            f"gold file(s) embed the document type `workbook:shareholder_register` in "
            f"their name, so Windows stores them as NTFS alternate data streams on a "
            f"zero-byte base file. They read back through the same path here, but they "
            f"will not survive a copy to a non-NTFS filesystem. Sanitising the filename "
            f"is a Phase I fix; Phase H does not rewrite Phase G artifacts.")
    add("")
    add("## 8. Paths")
    add("")
    add(f"- questions: `synthetic_data/output/v3_qa/qa/` ({len(per_set)} files)")
    add("- manifest: `synthetic_data/output/v3_qa/manifest.json`")
    add(f"- source artifacts: `{manifest['source_corpus']}` (read-only)")
    add("")
    add("## 9. Remaining blockers for Phase I")
    add("")
    add("- No reader/evaluator yet: Phase I needs an answer-checking harness to "
        "score model output against these questions.")
    add("- Unanswerable questions are attached to one prose document and the "
        "workbook; broadening them to every document needs absence checks that "
        "scale with corpus size.")
    add("- The pilot is one scenario with 7 documents; question diversity per type "
        "is capped by the number of distinct facts in that scenario.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    result = build_qa()
    print(json.dumps({
        "sets": len(result["sets"]),
        "questions": sum(entry["questions"] for entry in result["sets"].values()),
        "all_valid": result["all_valid"],
        "directory": str(QA_DIR),
    }, indent=2))
