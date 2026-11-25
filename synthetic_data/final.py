"""Final V3 deliverable: batch orchestration for the four corpus sizes.

The Phase G/H/I/J scripts each build the *pilot* only: they hardcode
``CorpusConfig(seed=7, documents=7, workbooks=1)`` and write to ``v3_pilot``,
``v3_qa`` and ``v3_eval``.  Nothing in the repository built a corpus of any other
size, so this module supplies the missing orchestration.  It reuses every completed
component unchanged:

* :mod:`synthetic_data.generation`      — semantic world and canonical planning
* :mod:`synthetic_data.canonical`       — statement sentences on the canonical tree
* :mod:`synthetic_data.renderers`       — TXT, PDF, DOCX and XLSX writers
* :mod:`synthetic_data.gold`            — v1/v2 gold and the alignment record
* :mod:`synthetic_data.qa`              — QA generation and the independent validator
* :mod:`synthetic_data.benchmark`       — prediction schema, fixtures, evaluator

What this module adds is: page-target calibration, a resumable per-artifact build,
per-corpus manifests/checksums, the evaluator package, and deterministic archives.

Page definition
---------------
A corpus page is one **physical PDF page of a prose document**, counted from the
parser's own record of the ingested PDF.  DOCX and XLSX are pageless and the TXT
page boundaries are the writer's, so counting any of them as corpus pages would
inflate the corpus; they are reported separately instead.  The renderer's page count
is verified to equal the parser's page count for every PDF before a corpus is
accepted.

Usage
-----
    py -3.13 -m synthetic_data.final --list
    py -3.13 -m synthetic_data.final --corpus corpus_500
    py -3.13 -m synthetic_data.final --all
    py -3.13 -m synthetic_data.final --all --package
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from synth.config import load_config
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.benchmark.evaluate import aggregate_by_question, evaluate_corpus
from synthetic_data.benchmark.fixtures import empty_bundle, perfect_bundle
from synthetic_data.benchmark.gold import load_gold_corpus
from synthetic_data.benchmark.report import markdown_report
from synthetic_data.benchmark.schema import PREDICTION_SCHEMA_VERSION, PredictionBundle
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.generation import CorpusConfig, build_corpus, compute_metrics
from synthetic_data.gold import (GoldInputs, build_gold, validate_alignment,
                                 validate_v1, validate_v2, write_gold_pair)
from synthetic_data.gold.model import GoldV2
from synthetic_data.paths import PortablePaths, artifact_name
from synthetic_data.qa.build import CorpusTexts, QASource, generate, generate_unanswerable
from synthetic_data.qa.model import QASet
from synthetic_data.qa.taxonomy import QUESTION_TYPES, UNANSWERABLE_KINDS
from synthetic_data.qa.validate import (ValidationInput, coverage_report,
                                        validate_corpus, validate_set)
from synthetic_data.renderers import (DEFAULT_DOCX_PROFILE, DEFAULT_PDF_PROFILE,
                                      DEFAULT_TXT_PROFILE, DEFAULT_XLSX_PROFILE,
                                      DocxRenderer, PdfRenderer, TxtRenderer,
                                      XlsxRenderer)

__all__ = ["CorpusSpec", "SPECS", "FINAL_DIR", "build", "calibrate", "measure_pages",
           "package_corpora", "main", "PROSE_FORMATS", "SUFFIX"]

REPO_ROOT = Path(__file__).resolve().parents[1]
FINAL_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_final"
CALIBRATION = FINAL_DIR / "calibration.json"

PROSE_FORMATS = ("txt", "pdf", "docx")
SUFFIX = {"txt": ".txt", "pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx"}
RENDERERS = {
    "txt": (TxtRenderer, DEFAULT_TXT_PROFILE, ".txt"),
    "pdf": (PdfRenderer, DEFAULT_PDF_PROFILE, ".pdf"),
    "docx": (DocxRenderer, DEFAULT_DOCX_PROFILE, ".docx"),
    "xlsx": (XlsxRenderer, DEFAULT_XLSX_PROFILE, ".xlsx"),
}
PAGES_DEFINITION = ("one physical PDF page of a prose document, counted from the "
                    "parser's record of the ingested PDF; DOCX and XLSX are pageless "
                    "and TXT page boundaries are the writer's")

#: workbook share of the prose count (one workbook per nine prose documents)
WORKBOOK_RATIO = 9


@dataclass(frozen=True)
class CorpusSpec:
    corpus: str
    target_pages: int
    seed: int
    documents: int
    workbooks: int
    max_letters: int = 5
    note: str = ""

    @property
    def configuration(self) -> dict[str, Any]:
        return {"seed": self.seed, "documents": self.documents,
                "workbooks": self.workbooks, "max_letters": self.max_letters,
                "include_contradictions": True, "include_threads": True,
                "registers": ["formal", "neutral", "terse"],
                "corpus_name": self.corpus, "prose_formats": list(PROSE_FORMATS)}

    def config(self) -> CorpusConfig:
        return CorpusConfig(seed=self.seed, corpus_name=self.corpus,
                            documents=self.documents, workbooks=self.workbooks,
                            max_letters=self.max_letters)


#: ``corpus_20`` is the architecture's pilot: seven prose types and one workbook.
#: ``documents`` for the larger targets is calibrated by measurement (see
#: ``calibrate``) and recorded in ``calibration.json``; the numbers here are the
#: measured values at seed 7 for the prose mix that hits each page target exactly.
SPECS: dict[str, CorpusSpec] = {
    "corpus_20": CorpusSpec("corpus_20", 20, 7, 7, 1,
                            note="architecture §15.4 pilot: 7 prose x 3 formats + 1 workbook"),
}


# ---------------------------------------------------------------------------
# page calibration
# ---------------------------------------------------------------------------
def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(path: Path) -> str:
    return _digest_bytes(Path(path).read_bytes())


def _load_calibration() -> dict[str, Any]:
    if CALIBRATION.exists():
        return json.loads(CALIBRATION.read_text(encoding="utf-8"))
    return {"measurements": {}, "resolved": {}}


def _save_calibration(payload: dict[str, Any]) -> None:
    CALIBRATION.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")


def measure_pages(spec: CorpusSpec, scratch: Path | None = None) -> dict[str, Any]:
    """Plan one corpus and count physical PDF pages, without parsing anything.

    ``PdfRenderer`` records one span per canonical node, and every page must carry
    at least one span (the renderer asserts it), so the highest span page is the
    page count.  ``tests`` in the pilot verify that this equals the parser's own
    page count; the corpus build re-checks it per artifact before accepting.
    """
    scratch = Path(scratch or tempfile.mkdtemp(prefix="pages_"))
    started = time.time()
    plan = build_corpus(spec.config())
    per_type: dict[str, list[int]] = {}
    total = 0
    try:
        for doc_id in plan.prose_ids:
            document = plan.documents[doc_id]
            prepared = apply_statement_sentences(document, canonicalize(document))
            render = PdfRenderer().render(
                prepared, DEFAULT_PDF_PROFILE,
                scratch / f"{doc_id.replace(':', '_')}.pdf")
            pages = max((span.loc.page or 0) for span in render.spans)
            if pages <= 0:
                raise AssertionError(f"{doc_id} rendered with no page")
            per_type.setdefault(document.doc_type, []).append(pages)
            total += pages
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"documents": spec.documents, "workbooks": spec.workbooks,
            "total_pages": total, "prose_documents": len(plan.prose_ids),
            "per_type": {key: sorted(values) for key, values in sorted(per_type.items())},
            "seconds": round(time.time() - started, 1)}


def calibrate(target_pages: int, seed: int = 7, max_letters: int = 5,
              corpus: str | None = None, budget: int = 16, log=print) -> CorpusSpec:
    """Find the prose/workbook counts whose measured PDF pages equal the target.

    Page totals move in jumps of 4-11 pages (one document), so a single document
    count can step over the target.  The letter count per matter changes the mix of
    short correspondence against long agreements and is therefore a second, finer
    knob; the search tries a few values and keeps the closest result it measured.
    Every measurement is cached in ``calibration.json`` so a rerun is free.
    """
    corpus = corpus or f"corpus_{target_pages}"
    state = _load_calibration()
    measurements: dict[str, Any] = state["measurements"]
    resolved: dict[str, Any] = state["resolved"]

    def key(documents: int, workbooks: int, letters: int, seed_value: int) -> str:
        # v2: the PDF renderer paginates inside a block, so earlier page totals are
        # no longer comparable with the renderer that produces the corpora
        return f"v2|{corpus}|{seed_value}|{letters}|{documents}|{workbooks}"

    def measure(documents: int, workbooks: int, letters: int, *,
                seed_value: int | None = None, reason: str = "") -> int:
        seed_value = seed if seed_value is None else seed_value
        token = key(documents, workbooks, letters, seed_value)
        if token not in measurements:
            probe = CorpusSpec(corpus, target_pages, seed_value, documents, workbooks,
                               letters)
            result = measure_pages(probe)
            measurements[token] = {"documents": documents, "workbooks": workbooks,
                                   "max_letters": letters, "seed": seed_value,
                                   "pages": result["total_pages"],
                                   "seconds": result["seconds"]}
            state["measurements"] = measurements
            _save_calibration(state)
            log(f"  measured seed={seed_value} documents={documents} "
                f"workbooks={workbooks} max_letters={letters}: "
                f"{result['total_pages']} pages ({result['seconds']}s) {reason}")
        return int(measurements[token]["pages"])

    token = f"v2|{corpus}|{seed}"
    if token in resolved and resolved[token]["target_pages"] == target_pages:
        entry = resolved[token]
        return CorpusSpec(corpus, target_pages, entry["seed"], entry["documents"],
                          entry["workbooks"], entry["max_letters"])

    if target_pages == 20:
        # the pilot size is fixed by the architecture, not calibrated
        spec = SPECS["corpus_20"]
        result = measure_pages(spec)
        measurements[key(spec.documents, spec.workbooks, spec.max_letters,
                         spec.seed)] = {
            "documents": spec.documents, "workbooks": spec.workbooks,
            "max_letters": spec.max_letters, "seed": spec.seed,
            "pages": result["total_pages"], "seconds": result["seconds"]}
        state["measurements"] = measurements
        resolved[token] = {"target_pages": 20, "documents": spec.documents,
                           "workbooks": spec.workbooks, "max_letters": spec.max_letters,
                           "seed": spec.seed,
                           "measured_pages": result["total_pages"],
                           "exact": result["total_pages"] == 20}
        _save_calibration(state)
        return spec

    letters_order = [max_letters]
    for candidate in (4, 6, 3, 7, 2):
        if 1 <= candidate <= 8 and candidate not in letters_order:
            letters_order.append(candidate)

    #: pages, letters, documents, seed
    candidates: list[tuple[int, int, int, int]] = []
    new = 0

    def budget_left() -> bool:
        return new < budget

    probe_totals: dict[int, int] = {}
    for letters in letters_order:
        if not budget_left():
            break
        probe_docs = 70
        probe_books = max(1, probe_docs // WORKBOOK_RATIO)
        before = len(measurements)
        probe_pages = measure(probe_docs, probe_books, letters, reason="probe")
        new += len(measurements) - before
        if probe_pages in probe_totals.values():
            # this letter count changes nothing measurable; do not spend a search on it
            log(f"  max_letters={letters}: probe equals an earlier probe "
                f"({probe_pages} pages), skipping")
            continue
        probe_totals[letters] = probe_pages
        per_document = probe_pages / probe_docs
        estimate = max(7, int(round(target_pages / per_document)))
        log(f"  max_letters={letters}: probe {probe_docs} docs = {probe_pages} pages "
            f"({per_document:.2f}/doc), first guess {estimate} docs")
        for documents in (estimate, estimate + 1, estimate - 1,
                          estimate + 2, estimate - 2):
            if not budget_left() or documents < 7:
                continue
            workbooks = max(1, documents // WORKBOOK_RATIO)
            before = len(measurements)
            pages = measure(documents, workbooks, letters,
                            reason=f"(target {target_pages})")
            new += len(measurements) - before
            candidates.append((pages, letters, documents, seed))
            if pages == target_pages:
                break
        if any(pages == target_pages for pages, *_ in candidates):
            break

    if not candidates:                       # pragma: no cover - probe always adds one
        raise RuntimeError(f"calibration for {corpus} measured nothing")
    exact = [item for item in candidates if item[0] == target_pages]
    if exact:
        pages, letters, documents, used_seed = exact[0]
    else:
        # a page total moves by whole documents, so the target can fall between two
        # adjacent sizes; publish the smallest corpus that is at least the target,
        # and report the difference instead of trimming a document to fit
        above = sorted((item for item in candidates if item[0] > target_pages),
                       key=lambda item: (item[0], item[2]))
        below = sorted((item for item in candidates if item[0] < target_pages),
                       key=lambda item: (-item[0], item[2]))
        pages, letters, documents, used_seed = (above or below)[0]
    workbooks = max(1, documents // WORKBOOK_RATIO)

    resolved[token] = {"target_pages": target_pages, "documents": documents,
                       "workbooks": workbooks, "max_letters": letters,
                       "seed": used_seed, "measured_pages": pages,
                       "delta_pages": pages - target_pages,
                       "exact": pages == target_pages, "measurements": new,
                       "candidates": [{"pages": item[0], "max_letters": item[1],
                                        "documents": item[2], "seed": item[3]}
                                      for item in sorted(candidates)]}
    state["measurements"] = measurements
    state["resolved"] = resolved
    _save_calibration(state)
    if pages != target_pages:
        log(f"  NOTE {corpus}: target {target_pages} pages is not reachable by any "
            f"measured document count; publishing {pages} pages "
            f"(documents={documents}, max_letters={letters}, seed={used_seed}, "
            f"delta {pages - target_pages:+d})")
    return CorpusSpec(corpus, target_pages, used_seed, documents, workbooks, letters)


# ---------------------------------------------------------------------------
# per-artifact build
# ---------------------------------------------------------------------------
@dataclass
class Artifact:
    document_id: str
    doc_type: str
    doc_kind: str
    format: str
    path: str
    parse_doc_id: str
    render_sha256: str
    pages: int | None
    pageless: bool | None
    units: int
    facts_total: int
    facts_grounded: int
    accounting_total: int
    unexplained: int
    counts: dict[str, int] = field(default_factory=dict)
    gold: dict[str, str] = field(default_factory=dict)
    v1_ok: bool = False
    v2_errors: list[str] = field(default_factory=list)
    alignment_errors: list[str] = field(default_factory=list)
    text_sha256: str = ""
    qa_sha256: str = ""
    questions: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.document_id, self.format)

    @property
    def ok(self) -> bool:
        return (self.v1_ok and not self.v2_errors and not self.alignment_errors
                and self.facts_grounded == self.facts_total and not self.unexplained)

    def as_dict(self, root: Path) -> dict[str, Any]:
        return {
            "document_id": self.document_id, "doc_type": self.doc_type,
            "doc_kind": self.doc_kind, "format": self.format,
            "path": _relative(self.path, root),
            "parse_doc_id": self.parse_doc_id,
            "render_sha256": self.render_sha256,
            "pages": self.pages, "pageless": self.pageless,
            "units": self.units, "counts": self.counts,
            "facts_total": self.facts_total, "facts_grounded": self.facts_grounded,
            "accounting_total": self.accounting_total,
            "unexplained_sentences": self.unexplained,
            "gold": self.gold, "text_sha256": self.text_sha256,
            "qa_sha256": self.qa_sha256, "questions": self.questions,
            "v1_valid": self.v1_ok, "v2_errors": self.v2_errors,
            "alignment_errors": self.alignment_errors, "ok": self.ok,
        }


def _relative(path: str | Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                    sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_journal(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        entries[(record["document_id"], record["format"])] = record
    return entries


def _append_journal(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _names(document, fmt: str) -> dict[str, str]:
    from synthetic_data.paths import safe_component

    document_id, doc_type = document.document_id, document.doc_type
    document = safe_component(document_id.replace(":", "_"))
    kind = safe_component(doc_type.replace(":", "_"))
    stem = f"{document}__{kind}__{fmt}"
    return {
        "rendered": f"{stem}{SUFFIX[fmt]}",
        "v1": f"{stem}.json",
        "v2": f"{stem}.json",
        "alignment": f"{document}__{fmt}.json",
        "text": f"{stem}.txt",
        "accounting": f"{stem}__accounting.json",
        "qa": f"{document}__{fmt}.json",
    }


def _artifact(document, canonical, fmt: str, root: Path, store, config, world,
              seed: int, journal_path: Path, existing: dict) -> Artifact:
    names = _names(document, fmt)
    rendered_path = root / "rendered" / fmt / names["rendered"]
    v1_path = root / "gold" / "v1" / names["v1"]
    v2_path = root / "gold" / "v2" / names["v2"]
    alignment_path = root / "gold" / "alignment" / names["alignment"]
    text_path = root / "parsed" / names["text"]

    record = existing.get((document.document_id, fmt))
    if record is not None:
        files = (rendered_path, v1_path, v2_path, alignment_path, text_path)
        if all(path.exists() for path in files) and all(path.stat().st_size > 0
                                                        for path in files):
            return Artifact(**record["artifact"])

    renderer_class, profile, suffix = RENDERERS[fmt]
    rendered_path.parent.mkdir(parents=True, exist_ok=True)
    render = renderer_class().render(document, profile, rendered_path)
    records = ingest_path(store, config, render.path)
    if not records:
        raise RuntimeError(f"ingest produced no record for {rendered_path}")
    parse_doc_id = records[0]["doc_id"]
    with store.reader() as reader:
        view = DocView.load(reader, parse_doc_id, config)

    pair = build_gold(GoldInputs(document=document, canonical=canonical,
                                 render=render, view=view, parse_doc_id=parse_doc_id,
                                 filename=rendered_path.name, world=world, seed=seed))
    v1_path.parent.mkdir(parents=True, exist_ok=True)
    v2_path.parent.mkdir(parents=True, exist_ok=True)
    alignment_path.parent.mkdir(parents=True, exist_ok=True)
    write_gold_pair(pair, root / "gold", document.document_id, document.doc_type, fmt)

    v1_ok = validate_v1(store, v1_path).ok
    v2_errors = validate_v2(
        GoldV2.model_validate(json.loads(v2_path.read_text(encoding="utf-8"))),
        view=view, render=render, canonical=canonical, document=document,
        world=world, row_quotes=_row_quotes(pair))
    alignment_errors = validate_alignment(pair.alignment, view=view, render=render)

    # the renderer's page count must equal the parser's, or the corpus page count is
    # measuring the wrong thing
    if fmt == "pdf":
        render_pages = max((span.loc.page or 0) for span in render.spans)
        if pair.v2.doc.pages != render_pages:
            raise AssertionError(
                f"{document.document_id}: parser counted {pair.v2.doc.pages} pages, "
                f"renderer drew {render_pages}")

    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(view.text, encoding="utf-8")
    accounting = pair.accounting
    artifact = Artifact(
        document_id=document.document_id, doc_type=document.doc_type,
        doc_kind=document.doc_kind, format=fmt, path=str(rendered_path),
        parse_doc_id=parse_doc_id, render_sha256=render.sha256,
        pages=pair.v2.doc.pages, pageless=pair.v2.doc.pageless,
        units=len(pair.alignment.units), facts_total=len(pair.evidence),
        facts_grounded=sum(1 for item in pair.evidence if item.acceptable),
        accounting_total=accounting.total if accounting else 0,
        unexplained=len(accounting.gaps) if accounting else 0,
        counts=dict(pair.v2.alignment.counts),
        gold={"v1": _digest(v1_path), "v2": _digest(v2_path),
              "alignment": _digest(alignment_path)},
        v1_ok=v1_ok, v2_errors=v2_errors, alignment_errors=alignment_errors,
        text_sha256=_digest(text_path))

    _write_json(root / "accounting" / names["accounting"], {
        "document_id": document.document_id, "doc_type": document.doc_type,
        "format": fmt, "parse_doc_id": parse_doc_id,
        "sentences": accounting.total if accounting else 0,
        "counts": dict(accounting.counts) if accounting else {},
        "unexplained": len(accounting.gaps) if accounting else 0,
        "gaps": list(accounting.gaps) if accounting else [],
        "alignment_units": len(pair.alignment.units),
        "alignment_counts": dict(pair.alignment.counts),
    })
    _append_journal(journal_path, {"document_id": document.document_id,
                                   "format": fmt, "artifact": artifact.__dict__})
    return artifact


def _row_quotes(pair) -> dict[str, str]:
    alignment = pair.workbook_alignment
    if alignment is None:
        return {}
    quotes = {row.row_id: row.quote for row in alignment.rows}
    quotes.update({cell.cell_id: cell.row_quote for cell in alignment.cells})
    return quotes


# ---------------------------------------------------------------------------
# QA + validation pass
# ---------------------------------------------------------------------------
def _unanswerable_documents(plan) -> set[str]:
    prose = list(plan.prose_ids)
    count = max(1, len(prose) // 50)
    stride = max(1, len(prose) // count)
    picked = {prose[index] for index in range(0, len(prose), stride)} if prose else set()
    if len(picked) > 8:
        picked = set(sorted(picked)[:: max(1, len(picked) // 8)][:8])
    if plan.workbook_ids:
        picked.add(plan.workbook_ids[0])
    return picked


def _qa_pass(plan, artifacts: list[Artifact], root: Path, store, config,
             unanswerable_docs: set[str], journal_path: Path | None = None,
             log=print) -> tuple[list[QASet], dict[str, Any]]:
    started = time.time()
    corpus = CorpusTexts.build(
        (artifact.document_id, artifact.format,
         (root / "parsed" / _names(plan.documents[artifact.document_id],
                                   artifact.format)["text"]).read_text(encoding="utf-8"),
         plan.documents[artifact.document_id].doc_kind)
        for artifact in artifacts)
    stated = {artifact.document_id: sorted(
        {entry["id"] for section in ("edges", "propositions")
         for entry in json.loads((root / "gold" / "v2" / _names(
             plan.documents[artifact.document_id], artifact.format)["v2"])
             .read_text(encoding="utf-8")).get(section, [])})
        for artifact in artifacts}

    sets: list[QASet] = []
    reports: dict[str, Any] = {}
    scratch = Path(tempfile.mkdtemp(prefix="qa_final_"))
    try:
        for position, artifact in enumerate(artifacts):
            document = plan.documents[artifact.document_id]
            canonical = (canonicalize(document)
                         if document.doc_kind != "workbook" else None)
            names = _names(document, artifact.format)
            renderer_class, profile, suffix = RENDERERS[artifact.format]
            render = renderer_class().render(
                apply_statement_sentences(document, canonical)
                if canonical is not None else document,
                profile, scratch / names["rendered"])
            gold_v2 = json.loads((root / "gold" / "v2" / names["v2"])
                                 .read_text(encoding="utf-8"))
            alignment = json.loads((root / "gold" / "alignment" / names["alignment"])
                                   .read_text(encoding="utf-8"))
            text = (root / "parsed" / names["text"]).read_text(encoding="utf-8")
            pair = type("_GoldShim", (), {})()
            pair.v2 = GoldV2.model_validate(gold_v2)

            source = QASource(document=document, canonical=canonical, render=render,
                              view=None, pair=pair, world=plan.world,
                              corpus=plan.corpus_id, seed=plan.config.seed, plan=plan)
            qa_path = root / "qa" / names["qa"]
            qa = None
            if qa_path.exists() and artifact.qa_sha256 and _digest(qa_path) == artifact.qa_sha256:
                qa = QASet.model_validate(json.loads(qa_path.read_text(encoding="utf-8")))
            elif qa_path.exists():
                # a run that died after writing the set but before journalling it: the
                # file is adopted only if it parses and names this artifact, and the
                # validation below still has to accept every question
                try:
                    cached = QASet.model_validate(
                        json.loads(qa_path.read_text(encoding="utf-8")))
                except ValueError:
                    cached = None
                if (cached is not None and cached.document_id == artifact.document_id
                        and cached.format == artifact.format):
                    qa = cached
            if qa is None:
                qa = generate(source, corpus)
                if artifact.document_id in unanswerable_docs:
                    extra = generate_unanswerable(source, corpus)
                    qa = QASet(corpus=qa.corpus, seed=qa.seed,
                               document_id=qa.document_id, doc_type=qa.doc_type,
                               format=qa.format,
                               questions=[*qa.questions, *extra.questions],
                               rejected=qa.rejected)
                _write_json(qa_path, qa.model_dump(mode="json"))
            artifact.qa_sha256 = _digest(qa_path)
            artifact.questions = len(qa.questions)
            if journal_path is not None:
                # persist the QA hash so a resumed build reuses this set instead of
                # regenerating (and re-spending) the whole pass
                _append_journal(journal_path, {"document_id": artifact.document_id,
                                               "format": artifact.format,
                                               "artifact": artifact.__dict__})

            report = validate_set(ValidationInput(
                qa=qa, gold_v2=gold_v2, alignment=alignment, world=plan.world,
                plan=plan, fresh_text=text, corpus=corpus,
                workbook=document if document.doc_kind == "workbook" else None,
                render_map=render, stated_by_document=stated))
            sets.append(qa)
            reports[names["qa"]] = {
                "document_id": qa.document_id, "format": qa.format,
                "questions": len(qa.questions), "answerable": len(qa.answerable),
                "unanswerable": len(qa.unanswerable),
                "checked": report.checked, "errors": report.errors,
                "max_similarity": report.notes.get("max_question_similarity"),
            }
            if report.errors:
                log(f"  QA errors {artifact.document_id}/{artifact.format}: "
                    f"{report.errors[:2]}")
            if position % 100 == 99:
                log(f"  QA {position + 1}/{len(artifacts)} artifacts, "
                    f"{round(time.time() - started)}s")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    corpus_report = validate_corpus(sets, corpus, stated)
    summary = {
        "sets": len(sets), "questions": sum(len(qa.questions) for qa in sets),
        "answerable": sum(len(qa.answerable) for qa in sets),
        "unanswerable": sum(len(qa.unanswerable) for qa in sets),
        "evidence_items": sum(len(question.evidence) for qa in sets
                              for question in qa.answerable),
        "coverage": coverage_report(sets),
        "checked": sum(entry["checked"] for entry in reports.values()),
        "set_errors": sum(len(entry["errors"]) for entry in reports.values()),
        "corpus_checked": corpus_report.checked,
        "corpus_errors": corpus_report.errors,
        "corpus_notes": _jsonable(corpus_report.notes),
        "unanswerable_documents": sorted(unanswerable_docs),
        "per_set": reports,
        "seconds": round(time.time() - started, 1),
    }
    return sets, summary


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _metrics_summary(metrics) -> dict[str, Any]:
    """Depth, diversity and contradiction metrics, without the per-document table."""
    from dataclasses import asdict

    return _jsonable({
        "by_type": metrics.by_type,
        "diversity": metrics.diversity,
        "contradictions": metrics.contradictions,
        "depth_failures": metrics.depth_failures,
        "prose_count": metrics.prose_count,
        "workbook_count": metrics.workbook_count,
    })


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def _plan_digest(plan) -> str:
    digest = hashlib.sha256()
    for doc_id in sorted(plan.documents):
        payload = plan.documents[doc_id].model_dump(mode="json")
        digest.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
        digest.update(b"\0")
    for entity_id in sorted(plan.world.entities):
        digest.update(plan.world.entities[entity_id].model_dump_json().encode("utf-8"))
    return digest.hexdigest()


def _determinism(spec: CorpusSpec, plan, artifacts: list[Artifact], root: Path) -> dict:
    digest = _plan_digest(plan)
    rebuilt = build_corpus(spec.config())
    rebuilt_digest = _plan_digest(rebuilt)
    document_count = len(rebuilt.documents)
    del rebuilt
    # one artifact re-rendered from the same plan must be byte-identical
    sample = next((item for item in artifacts if item.format == "pdf"), None)
    render_identical = None
    if sample is not None:
        document = plan.documents[sample.document_id]
        canonical = (canonicalize(document) if document.doc_kind != "workbook" else None)
        prepared = (apply_statement_sentences(document, canonical)
                    if canonical is not None else document)
        scratch = Path(tempfile.mkdtemp(prefix="determinism_"))
        try:
            result = PdfRenderer().render(prepared, DEFAULT_PDF_PROFILE,
                                          scratch / "again.pdf")
            render_identical = result.sha256 == sample.render_sha256
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    qa_identical = None
    first = sorted((root / "qa").glob("*.json"))
    if first:
        payload = json.loads(first[0].read_text(encoding="utf-8"))
        qa_identical = QASet.model_validate(payload).model_dump(mode="json") == payload
    return {
        "plan_digest": digest,
        "identical_replan": digest == rebuilt_digest,
        "document_count": document_count,
        "render_identical": render_identical,
        "qa_round_trip_identical": qa_identical,
    }


# ---------------------------------------------------------------------------
# canonical content
# ---------------------------------------------------------------------------
def _write_canonical(plan, root: Path) -> dict[str, Any]:
    world_path = root / "canonical" / "world.json"
    _write_json(world_path, plan.world.model_dump(mode="json"))
    documents = 0
    for doc_id in sorted(plan.documents):
        document = plan.documents[doc_id]
        _write_json(root / "canonical" / "documents"
                    / artifact_name(doc_id, document.doc_type, "cdm", "canonical"),
                    document.model_dump(mode="json"))
        documents += 1
    return {"world": _relative(world_path, root), "world_sha256": _digest(world_path),
            "documents": documents,
            "directory": "canonical/documents"}


# ---------------------------------------------------------------------------
# evaluator package
# ---------------------------------------------------------------------------
def _evaluator_package(root: Path, plan, artifacts: list[Artifact],
                       sample: int = 30) -> dict[str, Any]:
    gold = load_gold_corpus(root, root)
    if not gold:
        raise RuntimeError("no gold documents to evaluate")
    keys = sorted(gold)
    stride = max(1, len(keys) // sample)
    chosen = {key: gold[key] for key in keys[::stride][:sample]}
    evaluator = root / "evaluator"
    evaluator.mkdir(parents=True, exist_ok=True)
    _write_json(evaluator / "prediction.schema.json",
                PredictionBundle.model_json_schema())

    runs: dict[str, Any] = {}
    bundles = {"perfect": perfect_bundle(chosen, corpus=plan.corpus_id),
               "empty": empty_bundle(plan.corpus_id)}
    for name, bundle in bundles.items():
        _write_json(evaluator / f"predictions_{name}.json",
                    bundle.model_dump(mode="json"))
        result = evaluate_corpus(gold, bundle, label=name)
        question_metrics = aggregate_by_question(gold, bundle)
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        (evaluator / f"results_{name}.json").write_text(payload, encoding="utf-8")
        (evaluator / f"results_{name}.md").write_text(
            markdown_report(result, question_metrics=question_metrics,
                            notes=[("Evaluator self-test — no extraction model was "
                                    "run. This scores a synthetic prediction bundle.")]),
            encoding="utf-8")
        runs[name] = {"documents_scored": len(chosen) if name == "perfect" else len(gold),
                      "sha256": _digest_bytes(payload.encode("utf-8")),
                      "summary": result.get("overall")}

    manifest = {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "label": "evaluator self-test; no extraction model was run",
        "gold_documents": len(gold),
        "evaluator_sample": len(chosen),
        "sample_stride": stride,
        "files": {"schema": "prediction.schema.json",
                  "gold": "gold v2 + qa sets under this corpus",
                  "perfect_bundle": "predictions_perfect.json",
                  "empty_bundle": "predictions_empty.json"},
        "runs": runs,
        "gold_index": [
            {"document_id": key[0], "format": key[1], "doc_type": gold[key].doc_type,
             "relations": len(gold[key].relations),
             "propositions": len(gold[key].propositions),
             "answers": len(gold[key].answers)}
            for key in sorted(gold)],
    }
    _write_json(evaluator / "manifest.json", manifest)
    return {"gold_documents": len(gold), "scored_sample": len(chosen),
            "stride": stride, "runs": runs}


# ---------------------------------------------------------------------------
# corpus build
# ---------------------------------------------------------------------------
def build(spec: CorpusSpec, root: Path | None = None, *, force: bool = False,
          resume: bool = True, log=print) -> dict[str, Any]:
    root = Path(root) if root is not None else FINAL_DIR / spec.corpus
    manifest_path = root / "manifest.json"
    marker = root / "_COMPLETE"
    if (manifest_path.exists() and marker.exists() and not force):
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (existing.get("configuration") == spec.configuration
                and existing.get("all_valid")):
            log(f"{spec.corpus}: already complete, skipping (use --force to rebuild)")
            return existing

    started = time.time()
    root.mkdir(parents=True, exist_ok=True)
    if force:
        for name in ("rendered", "gold", "qa", "parsed", "accounting", "canonical",
                     "evaluator"):
            shutil.rmtree(root / name, ignore_errors=True)
        marker.unlink(missing_ok=True)
        (root / "journal.jsonl").unlink(missing_ok=True)

    log(f"{spec.corpus}: planning {spec.documents} prose + {spec.workbooks} workbooks "
        f"(seed {spec.seed}, target {spec.target_pages} pages)")
    plan = build_corpus(spec.config())
    canonical_summary = _write_canonical(plan, root)

    journal_path = root / "journal.jsonl"
    existing = _read_journal(journal_path) if resume else {}
    config = load_config()
    store = make_test_store()
    artifacts: list[Artifact] = []
    try:
        for position, doc_id in enumerate(plan.prose_ids):
            document = plan.documents[doc_id]
            canonical = canonicalize(document)
            prepared = apply_statement_sentences(document, canonical)
            for fmt in PROSE_FORMATS:
                artifacts.append(_artifact(prepared, canonical, fmt, root, store,
                                           config, plan.world, spec.seed,
                                           journal_path, existing))
            if position % 50 == 49:
                log(f"  {position + 1}/{len(plan.prose_ids)} prose documents, "
                    f"{round(time.time() - started)}s")
        for doc_id in plan.workbook_ids:
            document = plan.documents[doc_id]
            artifacts.append(_artifact(document, None, "xlsx", root, store, config,
                                       plan.world, spec.seed, journal_path, existing))
    finally:
        store.dispose()

    log(f"{spec.corpus}: {len(artifacts)} artifacts built in "
        f"{round(time.time() - started)}s; running QA")
    sets, qa_summary = _qa_pass(plan, artifacts, root, store, config,
                                _unanswerable_documents(plan), journal_path, log=log)
    log(f"{spec.corpus}: {qa_summary['questions']} questions, "
        f"{len(qa_summary['corpus_errors'])} corpus errors, "
        f"{round(time.time() - started)}s")

    determinism = _determinism(spec, plan, artifacts, root)

    pages_total = sum(item.pages or 0 for item in artifacts if item.format == "pdf")
    documents = len({item.document_id for item in artifacts})
    by_format: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for item in artifacts:
        by_format[item.format] = by_format.get(item.format, 0) + 1
    seen_documents: set[str] = set()
    for item in artifacts:
        if item.document_id in seen_documents:
            continue
        seen_documents.add(item.document_id)
        by_type[item.doc_type] = by_type.get(item.doc_type, 0) + 1
        by_kind[item.doc_kind] = by_kind.get(item.doc_kind, 0) + 1

    failures = [item.as_dict(root) for item in artifacts if not item.ok]
    contradictions = _contradiction_report(plan, root, artifacts)
    metrics = compute_metrics(plan)
    manifest = {
        "phase": "final",
        "corpus": spec.corpus,
        "seed": spec.seed,
        "configuration": spec.configuration,
        "pages": {
            "target": spec.target_pages,
            "actual": pages_total,
            "definition": PAGES_DEFINITION,
            "exact": pages_total == spec.target_pages,
            "per_document_min": min((item.pages for item in artifacts
                                     if item.format == "pdf"), default=0),
            "per_document_max": max((item.pages for item in artifacts
                                     if item.format == "pdf"), default=0),
        },
        "documents": {
            "total": documents,
            "prose": len(plan.prose_ids),
            "workbooks": len(plan.workbook_ids),
            "by_type": dict(sorted(by_type.items())),
            "by_kind": dict(sorted(by_kind.items())),
        },
        "artifacts": [item.as_dict(root) for item in artifacts],
        "artifact_counts": {"total": len(artifacts),
                            "by_format": dict(sorted(by_format.items()))},
        "gold": {
            "v1": len(artifacts), "v2": len(artifacts), "alignment": len(artifacts),
            "v1_valid": sum(1 for item in artifacts if item.v1_ok),
            "v2_errors": sum(len(item.v2_errors) for item in artifacts),
            "alignment_errors": sum(len(item.alignment_errors) for item in artifacts),
        },
        "alignment": {
            "units": sum(item.units for item in artifacts),
            "facts_total": sum(item.facts_total for item in artifacts),
            "facts_grounded": sum(item.facts_grounded for item in artifacts),
            "accounting_sentences": sum(item.accounting_total for item in artifacts),
            "unexplained_sentences": sum(item.unexplained for item in artifacts),
        },
        "qa": qa_summary,
        "contradictions": contradictions,
        "canonical": canonical_summary,
        "evaluator": None,
        "determinism": determinism,
        "depth_and_diversity": _metrics_summary(metrics),
        "failures": failures,
        "all_valid": not failures and not qa_summary["corpus_errors"] and all(
            not entry["errors"] for entry in qa_summary["per_set"].values()),
        "build_seconds": round(time.time() - started, 1),
        "checksums": {"file": "checksums.sha256"},
    }
    # the evaluator reads the corpus through its own manifest, so the manifest is
    # published first and completed once the evaluation has run
    _write_json(manifest_path, manifest)
    manifest["evaluator"] = _evaluator_package(root, plan, artifacts)
    _write_json(manifest_path, manifest)
    _write_checksums(root)
    checksum_digest = _digest(root / "checksums.sha256")
    marker.write_text(json.dumps({"corpus": spec.corpus, "all_valid": manifest["all_valid"],
                                  "pages": pages_total, "artifacts": len(artifacts),
                                  "checksums_sha256": checksum_digest},
                                 sort_keys=True) + "\n", encoding="utf-8")
    log(f"{spec.corpus}: {'VALID' if manifest['all_valid'] else 'FAILED'} — "
        f"{pages_total} pages, {documents} documents, {len(artifacts)} artifacts, "
        f"{qa_summary['questions']} questions, {round(time.time() - started)}s")
    return manifest


def _contradiction_report(plan, root: Path, artifacts: list[Artifact]) -> dict[str, Any]:
    stated: dict[str, set[str]] = {}
    for artifact in artifacts:
        names = _names(plan.documents[artifact.document_id], artifact.format)
        payload = json.loads((root / "gold" / "v2" / names["v2"]).read_text(encoding="utf-8"))
        for section in ("edges", "propositions"):
            for entry in payload.get(section, []):
                stated.setdefault(entry["id"], set()).add(artifact.document_id)
    items = []
    for contradiction in plan.world.contradictions.values():
        a = sorted(stated.get(contradiction.fact_a, set()))
        b = sorted(stated.get(contradiction.fact_b, set()))
        items.append({"contradiction_id": contradiction.contradiction_id,
                      "dimension": contradiction.dimension,
                      "fact_a_documents": a, "fact_b_documents": b,
                      "both_sides_grounded": bool(a) and bool(b)
                      and len(set(a) | set(b)) >= 2})
    return {"planned": len(items),
            "both_sides_grounded": sum(1 for item in items
                                       if item["both_sides_grounded"]),
            "items": items}


# ---------------------------------------------------------------------------
# checksums and archives
# ---------------------------------------------------------------------------
def _corpus_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*")
                  if path.is_file() and path.name not in {"checksums.sha256",
                                                          "_COMPLETE"})


def _checksum_text(root: Path) -> str:
    lines = [f"{_digest(path)}  {path.relative_to(root).as_posix()}"
             for path in _corpus_files(root)]
    return "\n".join(lines) + "\n"


def _write_checksums(root: Path) -> dict[str, str]:
    entries = {path.relative_to(root).as_posix(): _digest(path)
               for path in _corpus_files(root)}
    (root / "checksums.sha256").write_text(
        "\n".join(f"{digest}  {name}" for name, digest in entries.items()) + "\n",
        encoding="utf-8")
    return entries


def _archive_tree(root: Path, archive: Path, prefix: str = "") -> dict[str, Any]:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive.unlink()
    entries = sorted(path for path in root.rglob("*") if path.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in entries:
            name = path.relative_to(root).as_posix()
            if prefix:
                name = f"{prefix}/{name}"
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes())
    return {"path": str(archive), "entries": len(entries),
            "sha256": _digest(archive), "bytes": archive.stat().st_size,
            "bytes_zipped": archive.stat().st_size}


def archive_corpus(root: Path, archive: Path) -> dict[str, Any]:
    return _archive_tree(root, archive, prefix=root.name)


def verify_archive(archive: Path, expected: dict[str, str]) -> dict[str, Any]:
    problems: list[str] = []
    checked = 0
    with tempfile.TemporaryDirectory(prefix="verify_zip_") as scratch:
        destination = Path(scratch)
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if ":" in name:
                    problems.append(f"{name} contains a colon")
                if name.endswith((" ", ".")):
                    problems.append(f"{name} ends with a space or dot")
            bundle.extractall(destination)
        base = destination / archive.stem
        if not base.exists():
            base = destination
        for name, digest in expected.items():
            path = base / name
            if not base.exists():
                path = destination / name
            if not path.exists():
                problems.append(f"{name} missing after extraction")
                continue
            if path.stat().st_size == 0:
                problems.append(f"{name} is empty after extraction")
                continue
            if _digest(path) != digest:
                problems.append(f"{name} hash differs after extraction")
                continue
            checked += 1
    return {"checked": checked, "expected": len(expected), "problems": problems,
            "ok": not problems}


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------
def package_corpora(specs: Iterable[CorpusSpec], log=print) -> dict[str, Any]:
    corpora: list[dict[str, Any]] = []
    for spec in specs:
        root = FINAL_DIR / spec.corpus
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        archive = FINAL_DIR / f"{spec.corpus}.zip"
        info = archive_corpus(root, archive)
        expected = _read_checksums(root)
        verified = verify_archive(archive, expected)
        log(f"{spec.corpus}: {info['entries']} entries, "
            f"{info['bytes'] / 2**20:.1f} MB, verify={verified['ok']}")
        corpora.append({"corpus": spec.corpus,
                        "target_pages": manifest["pages"]["target"],
                        "actual_pages": manifest["pages"]["actual"],
                        "documents": manifest["documents"]["total"],
                        "artifacts": manifest["artifact_counts"]["total"],
                        "questions": manifest["qa"]["questions"],
                        "all_valid": manifest["all_valid"],
                        "directory": str(root), "archive": info,
                        "archive_verified": verified})

    payload: dict[str, Any] = {"version": 1, "profile": "v3_final", "corpora": corpora,
                               "pages_definition": PAGES_DEFINITION}

    # independent verification of every published corpus (never imports the generator)
    from synthetic_data.final_verify import check_v2_checksums, verify_corpus

    v2_problems: list[str] = []
    v2 = check_v2_checksums(v2_problems)
    results = [verify_corpus(Path(entry["directory"])) for entry in corpora]
    verification = {"corpora": results, "v2_checksums": v2,
                    "v2_problems": v2_problems,
                    "ok": all(result["ok"] for result in results) and not v2_problems}
    _write_json(FINAL_DIR / "VERIFICATION.json", verification)
    payload["verification"] = {"ok": verification["ok"],
                               "problems": {result["corpus"]: result["problems"]
                                            for result in results if result["problems"]},
                               "v2_checksums": v2}
    log(f"independent verification: {'OK' if verification['ok'] else 'FAILED'}")

    # the combined archive cannot contain a report that describes the archive's own
    # digest, so the two summary documents are written after it and are not part of it
    combined = FINAL_DIR / "v3_final_all_corpora.zip"
    if combined.exists():
        combined.unlink()
    with zipfile.ZipFile(combined, "w", zipfile.ZIP_DEFLATED) as bundle:
        for entry in sorted(FINAL_DIR.rglob("*")):
            if not entry.is_file() or entry.name == combined.name:
                continue
            if entry.suffix == ".zip" and entry.parent == FINAL_DIR:
                continue
            if entry.name in {"FINAL_MANIFEST.json", "README.md",
                              "FINAL_DELIVERABLE_REPORT.md"}:
                continue
            name = f"v3_final/{entry.relative_to(FINAL_DIR).as_posix()}"
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, entry.read_bytes())
    combined_info = {"path": str(combined), "sha256": _digest(combined),
                     "bytes": combined.stat().st_size,
                     "entries": len(zipfile.ZipFile(combined).namelist())}
    payload["combined_archive"] = combined_info

    write_documents(payload)
    _write_json(FINAL_DIR / "FINAL_MANIFEST.json", payload)
    log(f"combined archive: {combined_info['entries']} entries, "
        f"{combined_info['bytes'] / 2**20:.1f} MB")
    return payload


def _read_checksums(root: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    path = root / "checksums.sha256"
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        entries[name] = digest
    return entries


README = """# V3 synthetic benchmark — final deliverable

Four independently generated corpora, each built from its own recorded
configuration with the V3 canonical document model, renderers, gold alignment,
QA generation and evaluator.

| corpus | target pages | pages | prose docs | workbooks | artifacts | questions |
|---|---|---|---|---|---|---|
{rows}

## What is in each corpus directory

```
corpus_N/
  canonical/            semantic world and the canonical document tree (JSON)
    world.json
    documents/          one canonical document per semantic document
  rendered/
    txt/  pdf/  docx/   prose documents, one file per format
    xlsx/               workbooks only
  parsed/               parser text of each rendered artifact (the evidence base)
  gold/
    v1/                 gold projection accepted by the vendored synth validator
    v2/                 V3 gold: canonical ids, parser sentence ids, render locations
    alignment/          canonical node <-> parser sentence alignment record
  accounting/           per-artifact parser-content accounting (no unexplained text)
  qa/                   one question set per artifact, twins share question ids
  evaluator/            prediction schema, perfect/empty bundles, scored results
  manifest.json         configuration, counts, validation, determinism, hashes
  checksums.sha256      SHA-256 of every file in the directory
  _COMPLETE             written last, only when the corpus validated
```

A document rendered in three formats is **one** semantic document: the three
artifacts share canonical ids and question ids, and each carries format-specific
evidence. Workbooks are XLSX only; prose documents are never rendered as XLSX and
workbooks are never rendered as prose.

## Page counts

A corpus page is one physical PDF page of a prose document, counted from the
parser's record of the ingested PDF. DOCX and XLSX are pageless; TXT page
boundaries belong to the writer and are reported per artifact, not counted as
corpus pages. Every PDF's parser page count is checked against the renderer's page
count during the build; a mismatch fails that document.

## Reproduce

```bash
py -3.13 -m synthetic_data.final --list
py -3.13 -m synthetic_data.final --all --package
py -3.13 -m synthetic_data.final_verify --all
```

`calibration.json` records every measured `(seed, documents, workbooks,
max_letters)` candidate and the page total it produced; a rebuilt corpus is
byte-identical because every input of the generator is recorded in its manifest's
`configuration` block.

## Validate

`synthetic_data.final_verify` reads only published files and the real parser. It
re-checks checksums, portability (no colon, no trailing space or dot, no zero-byte
file), counts, every evidence quote against the parsed text, every gold id for
duplicates, every question id for duplicates, answer equality across formats, and
re-parses a sample of artifacts to confirm the quotes and page counts survive a
fresh parse. It also verifies the pre-existing V2 dataset checksums.
"""


def write_documents(payload: dict[str, Any]) -> None:
    rows = []
    for entry in payload["corpora"]:
        manifest = json.loads((Path(entry["directory"]) / "manifest.json")
                              .read_text(encoding="utf-8"))
        formats = manifest["artifact_counts"]["by_format"]
        rows.append(
            f"| `{entry['corpus']}` | {entry['target_pages']} | {entry['actual_pages']} | "
            f"{manifest['documents']['prose']} | {manifest['documents']['workbooks']} | "
            f"{entry['artifacts']} ({', '.join(f'{k} {v}' for k, v in formats.items())}) | "
            f"{entry['questions']} |")
    (FINAL_DIR / "README.md").write_text(README.format(rows="\n".join(rows)),
                                         encoding="utf-8")

    lines: list[str] = []
    add = lines.append
    add("# V3 synthetic benchmark — final deliverable report")
    add("")
    total_pages = sum(entry["actual_pages"] for entry in payload["corpora"])
    total_docs = sum(entry["documents"] for entry in payload["corpora"])
    total_artifacts = sum(entry["artifacts"] for entry in payload["corpora"])
    total_questions = sum(entry["questions"] for entry in payload["corpora"])
    add(f"{len(payload['corpora'])} corpora · {total_pages:,} pages · "
        f"{total_docs:,} semantic documents · {total_artifacts:,} rendered "
        f"artifacts · {total_questions:,} questions.")
    add("")
    add("Every number below is read from the published manifest of the corpus it "
        "describes; nothing is estimated.")
    add("")
    add("## 1. Size")
    add("")
    add("| corpus | target pages | actual pages | exact | prose docs | workbooks | "
        "artifacts | questions |")
    add("|---|---|---|---|---|---|---|---|")
    for entry in payload["corpora"]:
        manifest = json.loads((Path(entry["directory"]) / "manifest.json")
                              .read_text(encoding="utf-8"))
        add(f"| `{entry['corpus']}` | {entry['target_pages']} | {entry['actual_pages']} | "
            f"{'yes' if manifest['pages']['exact'] else 'no'} | "
            f"{manifest['documents']['prose']} | {manifest['documents']['workbooks']} | "
            f"{entry['artifacts']} | {entry['questions']} |")
    add("")
    add("## 2. Formats and document types")
    add("")
    add("| corpus | TXT | PDF | DOCX | XLSX | document types |")
    add("|---|---|---|---|---|---|")
    for entry in payload["corpora"]:
        manifest = json.loads((Path(entry["directory"]) / "manifest.json")
                              .read_text(encoding="utf-8"))
        formats = manifest["artifact_counts"]["by_format"]
        add(f"| `{entry['corpus']}` | {formats.get('txt', 0)} | {formats.get('pdf', 0)} | "
            f"{formats.get('docx', 0)} | {formats.get('xlsx', 0)} | "
            f"{len(manifest['documents']['by_type'])} |")
    add("")
    add("## 3. Gold, alignment and QA")
    add("")
    add("| corpus | gold v1 | gold v2 | alignment | facts | grounded | unexplained "
        "sentences | answerable | unanswerable |")
    add("|---|---|---|---|---|---|---|---|---|")
    for entry in payload["corpora"]:
        manifest = json.loads((Path(entry["directory"]) / "manifest.json")
                              .read_text(encoding="utf-8"))
        alignment = manifest["alignment"]
        qa = manifest["qa"]
        add(f"| `{entry['corpus']}` | {manifest['gold']['v1']} | {manifest['gold']['v2']} | "
            f"{manifest['gold']['alignment']} | {alignment['facts_total']:,} | "
            f"{alignment['facts_grounded']:,} | {alignment['unexplained_sentences']} | "
            f"{qa['answerable']:,} | {qa['unanswerable']:,} |")
    add("")
    add("## 4. Validation")
    add("")
    add("| corpus | valid | v1 accepted | v2 errors | alignment errors | QA set errors | "
        "corpus QA errors |")
    add("|---|---|---|---|---|---|---|")
    for entry in payload["corpora"]:
        manifest = json.loads((Path(entry["directory"]) / "manifest.json")
                              .read_text(encoding="utf-8"))
        add(f"| `{entry['corpus']}` | {'yes' if manifest['all_valid'] else 'NO'} | "
            f"{manifest['gold']['v1_valid']}/{manifest['gold']['v1']} | "
            f"{manifest['gold']['v2_errors']} | {manifest['gold']['alignment_errors']} | "
            f"{manifest['qa']['set_errors']} | {len(manifest['qa']['corpus_errors'])} |")
    add("")
    add("Each corpus is also checked by `synthetic_data.final_verify`, which does not "
        "import the generator: checksums, portability, counts, evidence quotes against "
        "the parsed text, duplicate ids, cross-format answer equality and a fresh parser "
        "pass. See `VERIFICATION.json` for that report.")
    add("")
    add("## 5. Archives")
    add("")
    add("| archive | entries | size (MB) | SHA-256 | verified |")
    add("|---|---|---|---|---|")
    for entry in payload["corpora"]:
        archive = entry["archive"]
        add(f"| `{Path(archive['path']).name}` | {archive['entries']:,} | "
            f"{archive['bytes'] / 2**20:.1f} | `{archive['sha256'][:32]}…` | "
            f"{'yes' if entry['archive_verified']['ok'] else 'NO'} |")
    combined = payload["combined_archive"]
    add(f"| `{Path(combined['path']).name}` | {combined['entries']:,} | "
        f"{combined['bytes'] / 2**20:.1f} | `{combined['sha256'][:32]}…` | — |")
    add("")
    add("## 6. Limitations")
    add("")
    for entry in payload["corpora"]:
        manifest = json.loads((Path(entry["directory"]) / "manifest.json")
                              .read_text(encoding="utf-8"))
        if not manifest["pages"]["exact"]:
            add(f"- `{entry['corpus']}`: target {manifest['pages']['target']} pages, "
                f"actual {manifest['pages']['actual']}. "
                + ("The pilot carries seven mandated prose types at V3 depth; its "
                   "smallest document is four pages, so a seven-document corpus cannot "
                   "render as few as twenty pages."
                   if manifest["pages"]["target"] == 20 else
                   "Page totals move by whole documents (4-11 pages each), so the exact "
                   "target can fall between two adjacent corpus sizes; the closest "
                   "measured size is published and recorded in `calibration.json`."))
    add("- Unanswerable questions are generated for a deterministic sample of documents "
        "per corpus (recorded in each manifest under `qa.unanswerable_documents`) and for "
        "the first workbook; the five insufficiency kinds are all represented.")
    add("- PDF table rows are drawn as a real grid but Docling's PDF pipeline returns "
        "cells as separate passages, so PDF table row structure is not recovered. Table "
        "gold is graded on DOCX and XLSX; no PDF table fidelity is claimed.")
    add("- DOCX and XLSX have no physical pagination: `pages` is null and `pageless` is "
        "true for those artifacts, by design.")
    add("- The evaluator package scores evaluator fixtures (gold, empty). No extraction "
        "model was run, so no model accuracy is claimed anywhere in this deliverable.")
    add("")
    (FINAL_DIR / "FINAL_DELIVERABLE_REPORT.md").write_text("\n".join(lines) + "\n",
                                                           encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve(targets: list[int], seed: int = 7, log=print) -> list[CorpusSpec]:
    specs: list[CorpusSpec] = []
    for target in targets:
        specs.append(calibrate(target, seed=seed, log=log))
    return specs


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Build the final V3 corpora")
    parser.add_argument("--corpus", action="append", default=[],
                        help="corpus name, e.g. corpus_500 (repeatable)")
    parser.add_argument("--pages", action="append", type=int, default=[],
                        help="page target to calibrate and build (repeatable)")
    parser.add_argument("--all", action="store_true", help="20, 500, 1000 and 5000")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--package", action="store_true",
                        help="write the per-corpus and combined archives")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        state = _load_calibration()
        print(json.dumps({"specs": {name: spec.configuration
                                    for name, spec in SPECS.items()},
                          "resolved": state.get("resolved", {})}, indent=2))
        return 0

    targets: list[int] = []
    if args.all:
        targets = [20, 500, 1000, 5000]
    else:
        targets = list(args.pages)
        for name in args.corpus:
            targets.append(int(name.rsplit("_", 1)[-1]))
    if not targets:
        parser.error("give --all, --pages or --corpus")

    specs = _resolve(sorted(set(targets), key=targets.index), seed=args.seed)
    built = [build(spec, force=args.force) for spec in specs]
    if args.package:
        package_corpora(specs)
    print(json.dumps([{key: manifest[key] for key in
                       ("corpus", "seed", "pages", "documents", "all_valid")}
                      for manifest in built], indent=2, default=str))
    return 0 if all(manifest["all_valid"] for manifest in built) else 1


if __name__ == "__main__":       # pragma: no cover - entry point
    raise SystemExit(main())
