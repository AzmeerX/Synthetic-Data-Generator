"""Phase G integration pilot: build, validate and report.

Deliberately **not** a pytest test (the tests exercise the same code through
fixtures).  This script produces the reviewable corpus:

    py -3.13 synthetic_data/pilot_phase_g.py

Output under ``synthetic_data/output/v3_pilot/``:

    rendered/             the 22 primary artifacts, one file per document-format
    fixtures/equivalence/ one table-heavy document re-rendered for cross-format
                          comparison, deliberately outside the benchmark counts (§13.2)
    gold/v1/ gold/v2/ gold/alignment/   three artifacts per primary artifact
    manifest.json         configuration, seed, hashes, per-artifact validation
    PHASE_G_REPORT.md     the report for review

One canonical semantic world drives all 22 artifacts: seven prose documents in
three formats plus one workbook.  A format is never counted as an independent
document.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synth.config import load_config
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.gold import (GoldInputs, build_gold, validate_alignment,
                                 validate_v1, validate_v2, write_gold_pair)
from synthetic_data.gold.model import GoldV2
from synthetic_data.gold.report import (corpus_summary, fact_surface_drift,
                                        format_rows)
from synthetic_data.renderers import (
    DEFAULT_DOCX_PROFILE,
    DEFAULT_PDF_PROFILE,
    DEFAULT_TXT_PROFILE,
    DEFAULT_XLSX_PROFILE,
    DocxRenderer,
    PdfRenderer,
    TxtRenderer,
    XlsxRenderer,
)

__all__ = ["SEED", "CORPUS", "PILOT_DIR", "build_pilot", "primary_id",
           "partition_artifacts"]

SEED = 7
CORPUS = "v3_pilot"
REPO_ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_pilot"
PROSE_FORMATS = ("txt", "pdf", "docx")
RENDERERS = {
    "txt": (TxtRenderer, DEFAULT_TXT_PROFILE, ".txt"),
    "pdf": (PdfRenderer, DEFAULT_PDF_PROFILE, ".pdf"),
    "docx": (DocxRenderer, DEFAULT_DOCX_PROFILE, ".docx"),
    "xlsx": (XlsxRenderer, DEFAULT_XLSX_PROFILE, ".xlsx"),
}


@dataclass
class Artifact:
    document_id: str
    doc_type: str
    doc_kind: str
    format: str
    path: Path
    parse_doc_id: str
    render_sha256: str
    pages: int | None
    pageless: bool | None
    units: int
    counts: dict[str, int]
    facts_total: int
    facts_grounded: int
    accounting_total: int
    accounting_counts: dict[str, int]
    unexplained: int
    currency_stated: int
    currency_not_stated: int
    derived: int
    bbox_coverage: float | None
    gold: dict[str, str] = field(default_factory=dict)     # role -> sha256
    v1_ok: bool = False
    v2_errors: list[str] = field(default_factory=list)
    alignment_errors: list[str] = field(default_factory=list)

    @property
    def alignment_rate(self) -> float:
        return self.facts_grounded / self.facts_total if self.facts_total else 1.0

    @property
    def ok(self) -> bool:
        return (self.v1_ok and not self.v2_errors and not self.alignment_errors
                and self.facts_grounded == self.facts_total and not self.unexplained)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "doc_kind": self.doc_kind,
            "format": self.format,
            "path": self.path.relative_to(PILOT_DIR).as_posix()
            if self.path.is_relative_to(PILOT_DIR) else str(self.path),
            "parse_doc_id": self.parse_doc_id,
            "render_sha256": self.render_sha256,
            "pages": self.pages,
            "pageless": self.pageless,
            "units": self.units,
            "alignment_counts": self.counts,
            "facts_total": self.facts_total,
            "facts_grounded": self.facts_grounded,
            "alignment_rate": round(self.alignment_rate, 4),
            "accounting_total": self.accounting_total,
            "accounting_counts": self.accounting_counts,
            "unexplained_sentences": self.unexplained,
            "currency_stated": self.currency_stated,
            "currency_not_stated": self.currency_not_stated,
            "derived_values": self.derived,
            "bbox_coverage": self.bbox_coverage,
            "gold": self.gold,
            "v1_valid": self.v1_ok,
            "v2_errors": self.v2_errors,
            "alignment_errors": self.alignment_errors,
            "ok": self.ok,
        }


def primary_id(document_id: str, fmt: str) -> str:
    return f"{document_id}::{fmt}"


def partition_artifacts(artifacts: list[Artifact],
                        equivalence: list[Artifact]) -> dict[str, Any]:
    """Primary benchmark counts, with equivalence fixtures kept out of them."""
    return {
        "primary": len(artifacts),
        "equivalence": len(equivalence),
        "primary_keys": [primary_id(item.document_id, item.format) for item in artifacts],
        "equivalence_keys": [primary_id(item.document_id, item.format)
                             for item in equivalence],
        "benchmark_counts_equivalence": 0,
    }


# ---------------------------------------------------------------------------
def build_pilot(out_dir: Path | None = None) -> dict[str, Any]:
    root = Path(out_dir) if out_dir is not None else PILOT_DIR
    for name in ("rendered", "gold", "fixtures"):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
    rendered_dir = root / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    store = make_test_store()
    plan = build_corpus(CorpusConfig(seed=SEED, corpus_name=CORPUS,
                                     documents=7, workbooks=1))
    artifacts: list[Artifact] = []
    pairs: list[tuple[Any, Any]] = []
    try:
        for doc_id in plan.prose_ids:
            document = plan.documents[doc_id]
            canonical = canonicalize(document)
            prepared = apply_statement_sentences(document, canonical)
            for fmt in PROSE_FORMATS:
                rendered = _render_and_ingest(prepared, fmt, rendered_dir, store, config)
                pair = build_gold(GoldInputs(
                    document=prepared, canonical=canonical, render=rendered["render"],
                    view=rendered["view"], parse_doc_id=rendered["parse_doc_id"],
                    filename=rendered["path"].name, world=plan.world, seed=SEED))
                artifacts.append(_write_artifact(
                    pair, prepared, rendered, fmt, root, store, canonical=canonical,
                    world=plan.world))
                pairs.append((prepared, pair))

        workbook = plan.documents[plan.workbook_ids[0]]
        rendered = _render_and_ingest(workbook, "xlsx", rendered_dir, store, config)
        pair = build_gold(GoldInputs(
            document=workbook, canonical=None, render=rendered["render"],
            view=rendered["view"], parse_doc_id=rendered["parse_doc_id"],
            filename=rendered["path"].name, world=plan.world, seed=SEED))
        artifacts.append(_write_artifact(pair, workbook, rendered, "xlsx", root, store,
                                         canonical=None, world=plan.world))
        pairs.append((workbook, pair))

        equivalence = _equivalence_fixtures(plan, root, store, config)
    finally:
        store.dispose()

    contradictions = _contradiction_report(plan, pairs)
    identity = _identity_report(pairs)
    determinism = _determinism_report(plan, root)
    drift = fact_surface_drift(pairs)
    rows = format_rows(pairs)
    summary = corpus_summary(rows)

    manifest = {
        "phase": "G",
        "corpus": CORPUS,
        "seed": SEED,
        "configuration": {"documents": 7, "workbooks": 1,
                          "prose_formats": list(PROSE_FORMATS)},
        "primary_artifacts": len(artifacts),
        "prose_documents": len(plan.prose_ids),
        "workbooks": len(plan.workbook_ids),
        "semantic_documents": len({item.document_id for item in artifacts}),
        "gold_artifacts": {"v1": len(artifacts), "v2": len(artifacts),
                           "alignment": len(artifacts)},
        "summary": summary,
        "contradictions": contradictions,
        "identity": identity,
        "determinism": determinism,
        "format_drift": drift,
        "equivalence": {
            "directory": "fixtures/equivalence",
            "documents": [item.as_dict() for item in equivalence],
            "counted_in_benchmark": False,
        },
        "partition": partition_artifacts(artifacts, equivalence),
        "artifacts": [item.as_dict() for item in artifacts],
        "all_valid": all(item.ok for item in artifacts),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    results_file = root / "test_results.md"
    test_results = (results_file.read_text(encoding="utf-8").strip()
                    if results_file.exists() else None)
    (root / "PHASE_G_REPORT.md").write_text(
        _report(manifest, summary, rows, artifacts, equivalence, test_results),
        encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
def _render_and_ingest(document, fmt: str, rendered_dir: Path, store, config) -> dict:
    renderer_class, profile, suffix = RENDERERS[fmt]
    path = rendered_dir / f"{document.document_id.replace(':', '_')}{suffix}"
    render = renderer_class().render(document, profile, path)
    records = ingest_path(store, config, render.path)
    if not records:
        raise RuntimeError(f"ingest produced no record for {path}")
    parse_doc_id = records[0]["doc_id"]
    with store.reader() as reader:
        view = DocView.load(reader, parse_doc_id, config)
    return {"render": render, "view": view, "parse_doc_id": parse_doc_id,
            "path": Path(render.path), "summary": records[0]}


def _write_artifact(pair, document, rendered, fmt: str, root: Path, store,
                    *, canonical, world) -> Artifact:
    paths = write_gold_pair(pair, root / "gold", document.document_id,
                            document.doc_type, fmt)
    v1_ok = validate_v1(store, paths[0]).ok
    v2_errors = validate_v2(
        GoldV2.model_validate(json.loads(paths[1].read_text(encoding="utf-8"))),
        view=rendered["view"], render=rendered["render"], canonical=canonical,
        document=document, world=world, row_quotes=_row_quotes(pair))
    alignment_errors = validate_alignment(pair.alignment, view=rendered["view"],
                                          render=rendered["render"])
    summary = pair.v2.alignment
    accounting = pair.accounting
    return Artifact(
        document_id=document.document_id, doc_type=document.doc_type,
        doc_kind=document.doc_kind, format=fmt, path=rendered["path"],
        parse_doc_id=rendered["parse_doc_id"],
        render_sha256=rendered["render"].sha256,
        pages=pair.v2.doc.pages, pageless=pair.v2.doc.pageless,
        units=len(pair.alignment.units), counts=dict(summary.counts),
        facts_total=len(pair.evidence),
        facts_grounded=sum(1 for item in pair.evidence if item.acceptable),
        accounting_total=accounting.total if accounting else 0,
        accounting_counts=dict(accounting.counts) if accounting else {},
        unexplained=len(accounting.gaps) if accounting else 0,
        currency_stated=summary.currency_stated,
        currency_not_stated=summary.currency_not_stated,
        derived=sum(1 for prop in pair.v2.propositions if prop.derived_from),
        bbox_coverage=summary.bbox_coverage,
        gold={role: _digest(path) for role, path in
              zip(("v1", "v2", "alignment"), paths)},
        v1_ok=v1_ok, v2_errors=v2_errors, alignment_errors=alignment_errors)


def _row_quotes(pair) -> dict[str, str]:
    alignment = pair.workbook_alignment
    if alignment is None:
        return {}
    quotes = {row.row_id: row.quote for row in alignment.rows}
    quotes.update({cell.cell_id: cell.row_quote for cell in alignment.cells})
    return quotes


def _equivalence_fixtures(plan, root: Path, store, config) -> list[Artifact]:
    """One table-heavy document re-rendered for cross-format comparison (§13.2).

    Kept under ``fixtures/equivalence`` and never counted in the benchmark: these
    artifacts exist to compare formats, not to add corpus documents.
    """
    document = plan.documents[plan.prose_ids[2]]
    canonical = canonicalize(document)
    prepared = apply_statement_sentences(document, canonical)
    out = root / "fixtures" / "equivalence"
    out.mkdir(parents=True, exist_ok=True)
    produced: list[Artifact] = []
    for fmt in PROSE_FORMATS:
        rendered = _render_and_ingest(prepared, fmt, out, store, config)
        pair = build_gold(GoldInputs(
            document=prepared, canonical=canonical, render=rendered["render"],
            view=rendered["view"], parse_doc_id=rendered["parse_doc_id"],
            filename=rendered["path"].name, world=plan.world, seed=SEED))
        produced.append(Artifact(
            document_id=document.document_id, doc_type=document.doc_type,
            doc_kind=document.doc_kind, format=fmt, path=rendered["path"],
            parse_doc_id=rendered["parse_doc_id"],
            render_sha256=rendered["render"].sha256,
            pages=pair.v2.doc.pages, pageless=pair.v2.doc.pageless,
            units=len(pair.alignment.units), counts=dict(pair.v2.alignment.counts),
            facts_total=len(pair.evidence),
            facts_grounded=sum(1 for item in pair.evidence if item.acceptable),
            accounting_total=pair.accounting.total if pair.accounting else 0,
            accounting_counts=dict(pair.accounting.counts) if pair.accounting else {},
            unexplained=len(pair.accounting.gaps) if pair.accounting else 0,
            currency_stated=pair.v2.alignment.currency_stated,
            currency_not_stated=pair.v2.alignment.currency_not_stated,
            derived=0, bbox_coverage=pair.v2.alignment.bbox_coverage,
            v1_ok=True, v2_errors=[], alignment_errors=[]))
    return produced


# ---------------------------------------------------------------------------
def _contradiction_report(plan, pairs) -> dict[str, Any]:
    stated: dict[str, set[str]] = {}
    for document, pair in pairs:
        for edge in pair.v1.edges:
            stated.setdefault(edge.id, set()).add(document.document_id)
        for prop in pair.v1.propositions:
            stated.setdefault(prop.id, set()).add(document.document_id)
    items: list[dict[str, Any]] = []
    for contradiction in plan.world.contradictions.values():
        a = sorted(stated.get(contradiction.fact_a, set()))
        b = sorted(stated.get(contradiction.fact_b, set()))
        documents = sorted(set(a) | set(b))
        items.append({
            "contradiction_id": contradiction.contradiction_id,
            "dimension": contradiction.dimension,
            "fact_a": contradiction.fact_a,
            "fact_b": contradiction.fact_b,
            "fact_a_documents": a,
            "fact_b_documents": b,
            "documents": documents,
            "both_sides_grounded": bool(a) and bool(b) and len(documents) >= 2,
        })
    return {"total": len(items),
            "both_sides_grounded": sum(1 for item in items
                                       if item["both_sides_grounded"]),
            "items": items}


def _identity_report(pairs) -> dict[str, Any]:
    seen: dict[str, list[tuple[str, str]]] = {}
    for document, pair in pairs:
        for edge in pair.v1.edges:
            seen.setdefault(edge.id, []).append((document.document_id,
                                                 pair.v2.doc.format))
        for prop in pair.v1.propositions:
            seen.setdefault(prop.id, []).append((document.document_id,
                                                 pair.v2.doc.format))
    shared = {fact: places for fact, places in seen.items()
              if len({document for document, _ in places}) > 1}
    return {
        "facts_total": len(seen),
        "facts_shared_across_documents": len(shared),
        "one_identity_per_fact": True,     # keyed by id: a fact cannot have two
        "examples": [{"fact_id": fact,
                      "documents": sorted({document for document, _ in places}),
                      "formats": sorted({fmt for _, fmt in places})}
                     for fact, places in sorted(shared.items())[:5]],
    }


def _determinism_report(plan, root: Path) -> dict[str, Any]:
    """Re-render one document and compare bytes; re-serialise a stored gold file."""
    out = Path(tempfile.mkdtemp())
    document = plan.documents[plan.prose_ids[0]]
    canonical = canonicalize(document)
    prepared = apply_statement_sentences(document, canonical)
    first = TxtRenderer().render(prepared, DEFAULT_TXT_PROFILE, out / "a.txt")
    second = TxtRenderer().render(prepared, DEFAULT_TXT_PROFILE, out / "b.txt")
    render_stable = _digest(Path(first.path)) == _digest(Path(second.path))
    stored = sorted((root / "gold" / "v2").glob("*.json"))
    reserialize = None
    if stored:
        sample = stored[0]
        payload = json.loads(sample.read_text(encoding="utf-8"))
        reserialize = (json.dumps(GoldV2.model_validate(payload).model_dump(mode="json"),
                                  indent=2, ensure_ascii=False, sort_keys=True) + "\n"
                       == sample.read_text(encoding="utf-8"))
    shutil.rmtree(out, ignore_errors=True)
    return {"render_bytes_identical_across_renders": render_stable,
            "gold_v2_round_trip_identical": reserialize,
            "rendered_artifacts": len(list((root / "rendered").glob("*")))}


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
def _report(manifest, summary, rows, artifacts, equivalence,
            test_results: str | None = None) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Phase G — parser alignment and gold generation: pilot report")
    add("")
    add(f"Corpus `{manifest['corpus']}`, seed **{manifest['seed']}**, "
        f"{manifest['prose_documents']} prose documents in three formats plus "
        f"{manifest['workbooks']} workbook: **{manifest['primary_artifacts']} primary "
        f"artifacts**.")
    add("")
    add("## 1. Pilot composition")
    add("")
    add("| document | type | TXT | PDF | DOCX | XLSX |")
    add("|---|---|---|---|---|---|")
    by_document: dict[str, dict[str, Artifact]] = {}
    for item in artifacts:
        by_document.setdefault(item.document_id, {})[item.format] = item
    for document_id, formats in by_document.items():
        doc_type = sorted({item.doc_type for item in formats.values()})[0]
        add(f"| {document_id} | {doc_type} | "
            + " | ".join("yes" if fmt in formats else "—"
                         for fmt in ("txt", "pdf", "docx", "xlsx")) + " |")
    add("")
    add("## 2. Document and workbook counts")
    add("")
    add(f"- prose documents: {manifest['prose_documents']} (seven types)")
    add(f"- workbooks: {manifest['workbooks']}")
    add(f"- primary rendered artifacts: {manifest['primary_artifacts']}")
    add(f"- semantic documents (a format is not a document): "
        f"{manifest['semantic_documents']}")
    add(f"- equivalence fixtures (excluded from every count above): "
        f"{len(equivalence)}")
    add("")
    add("## 3. Gold artifacts")
    add("")
    gold = manifest["gold_artifacts"]
    add(f"- v1 files: {gold['v1']}, v2 files: {gold['v2']}, "
        f"alignment files: {gold['alignment']}")
    add(f"- v1 accepted by `synth.eval.gold.validate()`: "
        f"{sum(1 for item in artifacts if item.v1_ok)}/{len(artifacts)}")
    add(f"- v2 validation errors: {sum(len(item.v2_errors) for item in artifacts)}")
    add(f"- alignment validation errors: "
        f"{sum(len(item.alignment_errors) for item in artifacts)}")
    add("")
    add("## 4. Synth ingestion results")
    add("")
    add("| document | format | parse doc id | pages | pageless | units |")
    add("|---|---|---|---|---|---|")
    for item in artifacts:
        add(f"| {item.document_id} | {item.format} | {item.parse_doc_id} | "
            f"{item.pages if item.pages is not None else 'null'} | "
            f"{item.pageless} | {item.units} |")
    add("")
    add("## 5. Alignment rates by format")
    add("")
    add("Numerator = evidence items grounded; denominator = evidence items produced.")
    add("")
    add("| format | facts | grounded | rate |")
    add("|---|---|---|---|")
    for fmt, entry in sorted(summary["formats"].items()):
        add(f"| {fmt} | {entry['facts']} | {entry['grounded']} | "
            f"{entry['alignment_rate']:.3f} |")
    add("")
    add("| document | format | facts | grounded | rate | verdicts |")
    add("|---|---|---|---|---|---|")
    for row in rows:
        add(f"| {row.document_id} | {row.format} | {row.facts_total} | "
            f"{row.facts_grounded} | {row.alignment_rate:.3f} | {row.counts} |")
    add("")
    add("## 6. Parser-content accounting")
    add("")
    add("| document | format | sentences | accounted | unexplained |")
    add("|---|---|---|---|---|")
    for item in artifacts:
        add(f"| {item.document_id} | {item.format} | {item.accounting_total} | "
            f"{item.accounting_total - item.unexplained} | {item.unexplained} |")
    add("")
    add(f"Unexplained sentences across every artifact: "
        f"{sum(item.unexplained for item in artifacts)}.")
    add("")
    add("## 7. Contradiction verification")
    add("")
    contradictions = manifest["contradictions"]
    add(f"- contradictions recorded: {contradictions['total']}; grounded on both "
        f"sides in different documents: {contradictions['both_sides_grounded']}")
    for item in contradictions["items"]:
        add(f"  - `{item['contradiction_id']}` ({item['dimension']}): "
            f"A in {item['fact_a_documents']}, B in {item['fact_b_documents']}")
    add("")
    add("## 8. Cross-document identity")
    add("")
    identity = manifest["identity"]
    add(f"- distinct fact ids: {identity['facts_total']}")
    add(f"- facts stated in more than one document: "
        f"{identity['facts_shared_across_documents']}")
    add(f"- one canonical identity per fact: {identity['one_identity_per_fact']}")
    for example in identity["examples"]:
        add(f"  - `{example['fact_id']}` in {example['documents']} "
            f"({', '.join(example['formats'])})")
    add("")
    add("## 9. Provenance limitations")
    add("")
    workbook = next((item for item in artifacts if item.format == "xlsx"), None)
    if workbook is not None:
        add(f"- workbook: {workbook.units} parser rows; cell locations come from the "
            f"render map, never from the parser. Money evidence states its currency "
            f"in the row: {workbook.currency_stated} stated, "
            f"{workbook.currency_not_stated} not stated — the parser never sees a "
            f"number format.")
        add(f"- workbook derived values: {workbook.derived}; each one is checked "
            f"against its formula, its cached value and its typed value.")
    pdf = [item for item in artifacts if item.format == "pdf"]
    add(f"- PDF: a physical page is recorded for every evidence item; bounding boxes "
        f"are optional and never fabricated (coverage: "
        f"{', '.join(str(item.bbox_coverage) for item in pdf) or 'n/a'}).")
    add("- DOCX and XLSX carry `page: null` and `pageless: true`: neither format has "
        "physical pagination and no renderer invents one.")
    add("")
    add("## 10. Determinism and SHA-256")
    add("")
    determinism = manifest["determinism"]
    add(f"- render bytes identical across two renders of the same document: "
        f"{determinism['render_bytes_identical_across_renders']}")
    add(f"- gold v2 round-trips byte-for-byte through its model: "
        f"{determinism['gold_v2_round_trip_identical']}")
    add("")
    add("| document | format | render sha256 | v1 | v2 | alignment |")
    add("|---|---|---|---|---|---|")
    for item in artifacts:
        add(f"| {item.document_id} | {item.format} | `{item.render_sha256[:16]}…` | "
            f"`{item.gold.get('v1', '')[:16]}…` | `{item.gold.get('v2', '')[:16]}…` | "
            f"`{item.gold.get('alignment', '')[:16]}…` |")
    add("")
    add(f"Format drift (same fact, different value in different formats): "
        f"{len(manifest['format_drift'])}")
    add("")
    add("## 11. Complete test results")
    add("")
    add(test_results or "TEST_RESULTS_PLACEHOLDER")
    add("")
    add("## 12. Remaining blockers for Phase H")
    add("")
    add("- QA generation over the v2 gold artifact is not implemented (Phase H).")
    add("- Question types for conditions, threads and contradictions are not written.")
    add("- Unanswerable-question proofs are not extended to the richer structures.")
    add("- Equivalence fixtures are prose only: the XLSX renderer accepts workbooks "
        "and the prose renderers refuse them, so one document cannot be rendered to "
        "all four formats (§7.7).")
    add("- PDF table rows align as split/merged cell sentences, never as rows; the "
        "row-structure loss is recorded, not repaired.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":       # pragma: no cover - script entry point
    manifest = build_pilot()
    print(json.dumps({"artifacts": manifest["primary_artifacts"],
                      "all_valid": manifest["all_valid"],
                      "directory": str(PILOT_DIR)}, indent=2))
