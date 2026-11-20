"""End-to-end corpus pipeline: plans -> files -> Synth ingest -> gold -> QA.

Safety properties:
* the pilot uses ``synth.store.test_store()`` (in-memory SQLite), never the
  team's configured PostgreSQL database;
* one store is reused for every document in the corpus;
* gold is written and validated but never promoted;
* no model/API pipeline is started.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from synth.config import SynthConfig, load_config
from synth.eval.gold import gold_filename, write_gold_file
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store

from synthetic_data import __version__
from synthetic_data.generator.annotations import (
    GoldBuildResult,
    assert_gold_valid,
    build_gold,
)
from synthetic_data.generator.documents import (
    DOC_TYPES,
    DocumentPlan,
    build_document_plan,
    page_counts,
    page_counts_v2,
)
from synthetic_data.generator.queries import (
    CorpusIndex,
    DocumentContext,
    duplicate_metrics,
    generate_qa_dataset,
    generate_qa_dataset_v2,
)
from synthetic_data.generator.semantics import (
    SemanticReport,
    assert_semantics,
)
from synthetic_data.generator.world import World, build_world
from synthetic_data.schemas.qa import QADataset


class CorpusGenerationError(RuntimeError):
    """Raised when generated artifacts fail ingestion, grounding or checks."""


class DuplicateDocumentError(CorpusGenerationError):
    """Raised when two planned documents have the same name or bytes."""


@dataclass
class CorpusResult:
    output_dir: Path
    world: World
    plans: list[DocumentPlan]
    documents: list[DocumentContext]
    gold_results: list[GoldBuildResult]
    dataset: QADataset
    semantics: SemanticReport
    manifest: dict[str, Any]
    layout: str = "v1"

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / "manifest.json"

    @property
    def dataset_path(self) -> Path:
        return self.output_dir / "qa_dataset.json"


_GENERATED_ENTRIES = (
    "documents",
    "gold",
    "qa_dataset.json",
    "qa_dataset.schema.json",
    "manifest.json",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _prepare_output_dir(out_dir: Path, overwrite: bool) -> None:
    if out_dir.exists() and not out_dir.is_dir():
        raise NotADirectoryError(f"output path is not a directory: {out_dir}")
    existing = [name for name in _GENERATED_ENTRIES if (out_dir / name).exists()]
    if existing and not overwrite:
        raise FileExistsError(
            f"{out_dir} already contains generated artifacts {existing}; "
            "pass overwrite=True (CLI: --overwrite) to replace them")
    if overwrite:
        for name in _GENERATED_ENTRIES:
            target = out_dir / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
    (out_dir / "documents").mkdir(parents=True, exist_ok=True)
    (out_dir / "gold").mkdir(parents=True, exist_ok=True)


def _check_plans(plans: list[DocumentPlan], *, layout: str = "v1",
                 texts: dict[str, str] | None = None) -> dict[str, str]:
    """Reject duplicate filenames, duplicate bytes and invalid page/trap plans."""
    if len({plan.filename for plan in plans}) != len(plans):
        raise DuplicateDocumentError("duplicate generated document filename")
    hashes: dict[str, str] = {}
    for plan in plans:
        if not 1 <= plan.pages <= 5:
            raise CorpusGenerationError(f"{plan.document_id}: pages must be 1-5")
        trap_count = len(plan.trap_kinds)
        if layout == "v2":
            if not 3 <= trap_count <= 6:
                raise CorpusGenerationError(
                    f"{plan.document_id}: {trap_count} traps; v2 requires 3-6")
        elif trap_count < 3:
            raise CorpusGenerationError(
                f"{plan.document_id}: only {trap_count} traps")
        text = texts[plan.document_id] if texts is not None else plan.render_text()
        text_hash = _sha256_text(text)
        if text_hash in hashes:
            raise DuplicateDocumentError(
                f"{plan.document_id} duplicates content of {hashes[text_hash]}")
        hashes[text_hash] = plan.document_id
    if layout == "v2":
        missing = sorted(set(DOC_TYPES) - {plan.doc_type for plan in plans})
        if missing:
            raise CorpusGenerationError(
                f"v2 plan is missing document types {missing}")
    return hashes


def _validation_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    grounding: Counter[str] = Counter()
    sentence_mismatch = 0
    not_found = 0
    errors = 0
    items = 0
    ok = 0
    for report in reports:
        items += int(report.get("items", 0))
        grounding.update(report.get("grounding", {}))
        sentence_mismatch += len(report.get("sentence_mismatch", []))
        not_found += len(report.get("not_found", []))
        errors += len(report.get("errors", []))
        ok += 1 if report.get("ok") else 0
    return {
        "documents": len(reports),
        "ok": ok,
        "failed": len(reports) - ok,
        "items": items,
        "grounding": dict(sorted(grounding.items())),
        "sentence_mismatch": sentence_mismatch,
        "not_found": not_found,
        "errors": errors,
    }


def _world_counts(world: World, plans: list[DocumentPlan]) -> dict[str, Any]:
    entity_ids = {eid for plan in plans for eid in plan.entity_ids}
    edge_statements = [s.fact for plan in plans for s in plan.stated_facts
                       if s.fact.kind == "edge"]
    unique_edges = {fact.fact_id: fact for fact in edge_statements}
    by_relation: dict[str, int] = Counter(
        fact.relation or "" for fact in unique_edges.values())
    critical = sorted(
        {fact.relation for fact in unique_edges.values() if fact.relation}
        & world.ontology.critical_relations)
    by_kind: dict[str, int] = Counter(entity.kind for entity in world.entities.values())
    return {
        "entities": {
            "world_total": len(world.entities),
            "by_kind": dict(sorted(by_kind.items())),
            "mentioned_unique": len(entity_ids),
        },
        "relationships": {
            "world_edge_facts": sum(1 for fact in world.facts if fact.kind == "edge"),
            "documented_unique": len(unique_edges),
            "statements": len(edge_statements),
            "by_relation": dict(sorted(by_relation.items())),
            "critical_relations": critical,
        },
        "events": len(world.events),
        "transactions": len(world.transactions),
        "contradictions": len(world.contradictions),
    }


def _trap_counts(plans: list[DocumentPlan], *, layout: str = "v1") -> dict[str, Any]:
    by_kind: Counter[str] = Counter()
    total = 0
    for plan in plans:
        by_kind.update(plan.trap_kinds)
        total += len(plan.trap_kinds)
    counts = {
        "total_statements": total,
        "by_kind": dict(sorted(by_kind.items())),
        "minimum_per_document": min((len(p.trap_kinds) for p in plans), default=0),
        "documents_with_fewer_than_three": sum(
            1 for plan in plans if len(plan.trap_kinds) < 3),
    }
    if layout == "v2":
        counts["maximum_per_document"] = max(
            (len(p.trap_kinds) for p in plans), default=0)
        counts["documents_outside_three_to_six"] = sum(
            1 for plan in plans if not 3 <= len(plan.trap_kinds) <= 6)
        counts["by_document"] = [
            {
                "document_id": plan.document_id,
                "filename": plan.filename,
                "doc_type": plan.doc_type,
                "trap_count": len(plan.trap_kinds),
                "by_kind": dict(sorted(Counter(plan.trap_kinds).items())),
            }
            for plan in plans
        ]
    return counts


def _query_counts(dataset: QADataset, *, layout: str = "v1") -> dict[str, Any]:
    by_type = Counter(query.query_type for query in dataset.queries)
    answerable = sum(1 for query in dataset.queries if query.answerable)
    counts = {
        "total": len(dataset.queries),
        "answerable": answerable,
        "unanswerable": len(dataset.queries) - answerable,
        "by_type": dict(sorted(by_type.items())),
    }
    if layout == "v2":
        total = len(dataset.queries)
        counts["answerable_percent"] = round(100.0 * answerable / total, 2) if total else 0.0
        counts["unanswerable_percent"] = round(
            100.0 * counts["unanswerable"] / total, 2) if total else 0.0
        counts["unanswerable_by_kind"] = dict(sorted(Counter(
            query.unanswerable_kind for query in dataset.queries
            if not query.answerable).items()))
        counts["duplicates"] = duplicate_metrics(dataset)
    return counts


def _qa_dataset_text(dataset: QADataset, layout: str) -> str:
    """Serialize the dataset with the exact contract of the selected layout.

    V1 predates the v2 proof fields.  Omitting ``None`` values keeps the
    frozen Phase 1 ``qa_dataset.json`` byte-identical while v2 retains the
    explicit nulls that document its stricter query contract.
    """
    if layout == "v1":
        return dataset.model_dump_json(indent=2, exclude_none=True) + "\n"
    return dataset.model_dump_json(indent=2) + "\n"


def _qa_schema(layout: str) -> dict[str, Any]:
    """JSON Schema for the selected layout.

    The v1 data contract has no unanswerable-proof fields, so the published
    v1 schema is the v2 schema minus those additions.  This keeps the frozen
    Phase 1 artifact set byte-stable without weakening v2 validation.
    """
    schema = QADataset.model_json_schema()
    if layout == "v1":
        query = schema["$defs"]["QAQuery"]
        for field_name in ("unanswerable_kind", "unanswerable_proof"):
            query["properties"].pop(field_name, None)
        schema["$defs"].pop("UnanswerableProof", None)
    return schema


def _diversity_counts(world: World, plans: list[DocumentPlan]) -> dict[str, Any]:
    """Entity pools and document-subject reuse for the improved layout."""
    subjects = [plan.subject_entity_id for plan in plans]
    unique_subjects = set(subjects)
    return {
        "document_subjects": len(subjects),
        "unique_document_subjects": len(unique_subjects),
        "reused_document_subjects": len(subjects) - len(unique_subjects),
        "world_companies": len(world.companies()),
        "world_people": len(world.people()),
        "world_banks": len(world.banks()),
        "world_law_firms": len(world.law_firms()),
        "world_entities": len(world.entities),
    }


def _manifest_documents(
    plans: list[DocumentPlan],
    contexts: list[DocumentContext],
    gold_results: list[GoldBuildResult],
    *,
    layout: str = "v1",
) -> list[dict[str, Any]]:
    rows = []
    for plan, ctx, gold in zip(plans, contexts, gold_results):
        row = {
            "index": plan.index,
            "document_id": plan.document_id,
            "synth_doc_id": ctx.synth_doc_id,
            "filename": plan.filename,
            "doc_type": plan.doc_type,
            "doc_date": plan.doc_date.isoformat(),
            "pages": plan.pages,
            "sentences": len(plan.sentences),
            "text_sha256": ctx.text_sha256,
            "gold_file": str(gold.path.name),
            "gold_ok": bool(gold.report.get("ok")),
            "gold_items": gold.report.get("items", 0),
            "gold_sentence_mismatch": len(gold.report.get("sentence_mismatch", [])),
            "trap_kinds": list(plan.trap_kinds),
            "fact_ids": plan.fact_ids,
            "entity_ids": plan.entity_ids,
        }
        if layout == "v2":
            row["trap_count"] = len(plan.trap_kinds)
            row["trap_counts_by_kind"] = dict(
                sorted(Counter(plan.trap_kinds).items()))
        rows.append(row)
    return rows


def generate_corpus(
    *,
    seed: int = 7,
    pages: int = 20,
    out_dir: str | Path = "synthetic_data/output/pilot_20",
    overwrite: bool = False,
    config: SynthConfig | None = None,
    store=None,
    layout: str = "v1",
) -> CorpusResult:
    """Generate, ingest, annotate and validate one cumulative corpus checkpoint.

    ``layout="v1"`` preserves the frozen Phase 1 stream.  ``layout="v2"`` uses
    the seven-document plan, cohort-expanded world, structured unanswerable
    proofs and the improved QA mix.  Both layouts refuse non-SQLite stores and
    never promote generated gold.
    """
    if layout not in ("v1", "v2"):
        raise ValueError("layout must be 'v1' or 'v2'")
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if pages <= 0 or pages % 20 != 0:
        raise ValueError("pages must be a positive multiple of 20")

    out_path = Path(out_dir)
    _prepare_output_dir(out_path, overwrite)
    config = config or load_config()
    owns_store = store is None
    store = store or test_store()
    if getattr(store, "dialect", None) != "sqlite":
        raise CorpusGenerationError(
            f"refusing to use non-SQLite store (dialect={getattr(store, 'dialect', None)!r})")

    owns_trace = not tracemalloc.is_tracing()
    if owns_trace:
        tracemalloc.start()
    timings: dict[str, float] = {}
    started = time.perf_counter()

    def mark(name: str, since: float) -> None:
        timings[name] = round((time.perf_counter() - since) * 1000.0, 3)

    try:
        t_world = time.perf_counter()
        world = (build_world(seed, target_pages=pages) if layout == "v2"
                 else build_world(seed))
        mark("world_generation", t_world)

        t_docs = time.perf_counter()
        page_plan = page_counts_v2(pages) if layout == "v2" else page_counts(pages)
        plans = [
            build_document_plan(world, index, page_count, layout=layout)
            for index, page_count in enumerate(page_plan)
        ]
        # Render once; reuse the exact text for hashing and ingestion.
        plan_texts = {plan.document_id: plan.render_text() for plan in plans}
        _check_plans(plans, layout=layout, texts=plan_texts)
        mark("document_generation", t_docs)

        documents: list[DocumentContext] = []
        gold_results: list[GoldBuildResult] = []
        reports: list[dict[str, Any]] = []
        actual_pages = 0
        documents_dir = out_path / "documents"
        gold_dir = out_path / "gold"
        ingest_seconds = 0.0
        gold_seconds = 0.0

        for plan in plans:
            t_ingest = time.perf_counter()
            text = plan_texts[plan.document_id]
            path = documents_dir / plan.filename
            path.write_text(text, encoding="utf-8")
            try:
                ingested = ingest_path(store, config, path)
            except Exception as exc:  # ingestion failures must identify the document
                raise CorpusGenerationError(
                    f"ingestion raised for {plan.filename}: {exc}") from exc
            if not ingested:
                raise CorpusGenerationError(f"ingestion returned no result for {plan.filename}")
            record = ingested[0]
            if record.get("status") != "SEGMENTED":
                raise CorpusGenerationError(
                    f"{plan.filename}: ingested status {record.get('status')!r}, "
                    f"notes={record.get('notes')!r}")
            if record.get("existing"):
                raise DuplicateDocumentError(
                    f"{plan.filename}: store already contained identical bytes")
            if int(record.get("pages") or 0) != plan.pages:
                raise CorpusGenerationError(
                    f"{plan.filename}: ingested {record.get('pages')} pages, "
                    f"planned {plan.pages}")
            if int(record.get("sentences") or 0) != len(plan.sentences):
                raise CorpusGenerationError(
                    f"{plan.filename}: ingested {record.get('sentences')} sentences, "
                    f"planned {len(plan.sentences)}")
            actual_pages += int(record["pages"])

            with store.reader() as reader:
                view = DocView.load(reader, record["doc_id"], config)
            try:
                ctx = DocumentContext.from_view(plan, record["doc_id"], view)
            except Exception as exc:
                raise CorpusGenerationError(
                    f"{plan.filename}: canonical sentence mapping failed: {exc}") from exc
            documents.append(ctx)
            ingest_seconds += time.perf_counter() - t_ingest

            t_gold = time.perf_counter()
            gold = build_gold(world, plan, view)
            gold_path = gold_dir / gold_filename(plan.filename, gold.first_idx, gold.last_idx)
            write_gold_file(gold, gold_path)
            try:
                report = assert_gold_valid(store, gold_path, ontology=world.ontology)
            except Exception as exc:
                raise CorpusGenerationError(
                    f"{plan.filename}: gold validation failed: {exc}") from exc
            gold_results.append(GoldBuildResult(gold=gold, path=gold_path, report=report))
            reports.append(report)
            gold_seconds += time.perf_counter() - t_gold

        timings["ingestion"] = round(ingest_seconds * 1000.0, 3)
        timings["gold_validation"] = round(gold_seconds * 1000.0, 3)
        # Renderings are no longer needed; DocumentContext holds the canonical
        # sentences used by every later stage.
        plan_texts.clear()

        if actual_pages != pages:
            raise CorpusGenerationError(
                f"actual page total {actual_pages} != requested {pages}")
        if sum(plan.pages for plan in plans) != pages:
            raise CorpusGenerationError(
                f"page plan total {sum(plan.pages for plan in plans)} != requested {pages}")

        t_qa = time.perf_counter()
        corpus_index = CorpusIndex.build(documents)
        if layout == "v2":
            dataset = generate_qa_dataset_v2(
                corpus_index, world, target_pages=pages)
        else:
            dataset = generate_qa_dataset(corpus_index, world, target_pages=pages)
        mark("qa_generation", t_qa)

        t_semantics = time.perf_counter()
        try:
            semantics = assert_semantics(world, plans, dataset, gold_results)
        except Exception as exc:
            raise CorpusGenerationError(f"semantic validation failed: {exc}") from exc
        mark("semantic_validation", t_semantics)

        dataset_path = out_path / "qa_dataset.json"
        dataset_path.write_text(_qa_dataset_text(dataset, layout), encoding="utf-8")
        schema_path = out_path / "qa_dataset.schema.json"
        schema_path.write_text(
            json.dumps(_qa_schema(layout), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

        doc_hashes = [ctx.text_sha256 for ctx in documents]
        batch = {
            "seed": seed,
            "target_pages": pages,
            "actual_pages": actual_pages,
            "document_count": len(plans),
            "page_pattern": page_plan,
        }
        if layout == "v2":
            batch["layout"] = "v2"
        manifest = {
            "version": 2 if layout == "v2" else 1,
            "generator_version": __version__,
            "batch": batch,
            **_world_counts(world, plans),
            "traps": _trap_counts(plans, layout=layout),
            "queries": _query_counts(dataset, layout=layout),
            "validation": _validation_summary(reports),
            "semantics": semantics.as_dict(),
            "documents": _manifest_documents(
                plans, documents, gold_results, layout=layout),
            "reproducibility": {
                "document_hashes": doc_hashes,
                "prefix_sha256": _sha256_text("\n".join(doc_hashes)),
                "world_signature_sha256": _sha256_text(
                    _canonical_json(world.content_signature())),
                "gold_mode": "generated",
                "blind": False,
                "promoted": False,
            },
        }
        if layout == "v2":
            mark("total", started)
            peak = tracemalloc.get_traced_memory()[1] if tracemalloc.is_tracing() else 0
            manifest["diversity"] = _diversity_counts(world, plans)
            manifest["performance"] = {
                "timings_ms": dict(timings),
                "peak_memory_mb": round(peak / (1024.0 * 1024.0), 3),
                "measurement": "tracemalloc",
                "tracemalloc_started_by_pipeline": owns_trace,
            }
        manifest_path = out_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return CorpusResult(
            output_dir=out_path,
            world=world,
            plans=plans,
            documents=documents,
            gold_results=gold_results,
            dataset=dataset,
            semantics=semantics,
            manifest=manifest,
            layout=layout,
        )
    finally:
        if owns_trace and tracemalloc.is_tracing():
            tracemalloc.stop()
        if owns_store:
            store.dispose()


def load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
