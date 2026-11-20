"""J0 — Orchestrator: one process, stage-wise, resumable (P1 §2 execution order, §3 J0).

Purpose: drive every module in the spec's order so each model loads once per
stage, record the run manifest (L1), make every ``(run, stage, unit)``
idempotent (``unit_status``), retry a failing unit at most twice, and keep a
dead-letter list for the run report.

Contract (the CLI calls exactly these):

* :func:`ingest` — U1 over a file or folder (run-independent S1 rows).
* :func:`run` — one arm (``full`` | ``t2_only`` | ``t3_only``) over the ingested corpus.
* :func:`audit`, :func:`evaluate`, :func:`report`, :func:`view`, :func:`export`,
  :func:`gold`, :func:`serve` — thin wrappers over U12, J2, J3, J1.

Arms (P1 §1): ``full`` = U1 → U12. ``t2_only`` / ``t3_only`` = U1 → sections →
one extraction tier → U8 (escalation disabled) → commit. All arms share the
same deterministic code so differences come from the model tiers.
Stores written: ``runs`` (L1), ``unit_status``; everything else via modules.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from synth import states as S
from synth.config import REPO_ROOT, SynthConfig, load_config
from synth.gateway import Gateway, ScriptedModelClient
from synth.ids import new_id
from synth.modules.common import StageContext
from synth.ontology import Ontology, load_ontology
from synth.prompts import prompt_hashes
from synth.store import Store, open_store

log = logging.getLogger("synth.j0")

MAX_RETRIES = 2


# ------------------------------------------------------------------ helpers
def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # pragma: no cover - git missing
        return None


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def load_all(config_path: str | Path | None = None,
             overrides: dict[str, Any] | None = None) -> tuple[SynthConfig, Ontology]:
    config = load_config(config_path, overrides=overrides)
    return config, load_ontology(config.resolve(config.ontology))


def make_gliner(config: SynthConfig, mode: str | None = None):
    """``mode``: ``gliner2`` (real, needs the ``gliner`` extra), ``heuristic`` (regex
    stand-in for demo runs), or ``None`` = real if importable else error."""
    from synth.modules import gliner_backends as gb

    g = config.model_settings.gliner
    if mode == "heuristic":
        log.warning("U2 uses the HeuristicGlinerBackend (regex stand-in), not GLiNER2")
        return gb.HeuristicGlinerBackend()
    try:
        import torch  # noqa: F401  (gliner2's base package imports without its model stack)
        import gliner2  # noqa: F401
    except ImportError:
        if mode == "gliner2":
            raise
        raise RuntimeError("gliner2 is not installed (py -m pip install -e \".[gliner]\"); "
                           "pass --gliner heuristic for a demo run without it")
    return gb.Gliner2Backend(model=g.model, device=g.device)


def make_gateway(config: SynthConfig, store: Store, scripted: bool | ScriptedModelClient = False) -> Gateway:
    if scripted:
        client = scripted if isinstance(scripted, ScriptedModelClient) else ScriptedModelClient()
        return Gateway.scripted(config, store=store, client=client)
    return Gateway(config, store=store)


# ------------------------------------------------------------------ unit tracking
@dataclass
class StageReport:
    stage: str
    done: int = 0
    skipped: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "done": self.done, "skipped": self.skipped,
                "failed": self.failed}


def _progress(stage: str, unit_id: str, message: str) -> None:
    """One line per unit on stderr, so a long run shows where it is (stdout stays the JSON report)."""
    print(f"[{_dt.datetime.now():%H:%M:%S}] {stage:<11} {unit_id}  {message}", file=sys.stderr, flush=True)


def run_unit(ctx: StageContext, stage: str, unit_id: str, fn: Callable[[], Any],
             report: StageReport, input_hash: str | None = None) -> Any:
    """Run one ``(run, stage, unit)`` at most once to DONE; retry ≤ 2; then DEAD_LETTER."""
    with ctx.reader() as r:
        row = r.get("unit_status", run_id=ctx.run_id, stage=stage, unit_id=unit_id)
    if row and row["status"] == S.DONE:
        report.skipped += 1
        return None
    if row is None:
        with ctx.tx(stage, "J0") as tx:
            tx.insert("unit_status", dict(run_id=ctx.run_id, stage=stage, unit_id=unit_id,
                                          status=S.PENDING, attempts=0, input_hash=input_hash))
        attempts = 0
    else:
        attempts = row["attempts"] or 0
        if row["status"] == S.DEAD_LETTER:
            report.failed.append({"unit": unit_id, "error": row["error"], "status": S.DEAD_LETTER})
            return None
    last_error = None
    while attempts <= MAX_RETRIES:
        attempts += 1
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # unit failure: record, retry
            last_error = f"{type(exc).__name__}: {exc}"
            log.error("unit %s/%s attempt %d failed: %s", stage, unit_id, attempts, last_error)
            log.debug("%s", traceback.format_exc())
            _progress(stage, unit_id, f"attempt {attempts} FAILED: {last_error[:160]}")
            with ctx.tx(stage, "J0") as tx:
                tx.update("unit_status", {"run_id": ctx.run_id, "stage": stage, "unit_id": unit_id},
                          {"status": S.FAILED, "attempts": attempts, "error": last_error[:2000]})
            continue
        with ctx.tx(stage, "J0") as tx:
            tx.update("unit_status", {"run_id": ctx.run_id, "stage": stage, "unit_id": unit_id},
                      {"status": S.DONE, "attempts": attempts, "error": None})
        report.done += 1
        _progress(stage, unit_id, f"done in {time.monotonic() - started:.1f}s")
        if isinstance(result, dict):
            report.results.append({"unit": unit_id, **result})
        return result
    with ctx.tx(stage, "J0") as tx:
        tx.update("unit_status", {"run_id": ctx.run_id, "stage": stage, "unit_id": unit_id},
                  {"status": S.DEAD_LETTER, "attempts": attempts, "error": (last_error or "")[:2000]})
    report.failed.append({"unit": unit_id, "error": last_error, "status": S.DEAD_LETTER})
    return None


# ------------------------------------------------------------------ commands
def ingest(path: str | Path, config_path: str | Path | None = None, sovereign: bool = False,
           store: Store | None = None) -> dict[str, Any]:
    from synth.modules.u01_parser import ingest_path

    config, _ = load_all(config_path)
    store = store or open_store(config.db_url())
    results = ingest_path(store, config, Path(path), sovereign=sovereign)
    return {"documents": results, "count": len(results)}


def _corpus(store: Store, doc_ids: Sequence[str] | None) -> list[str]:
    with store.reader() as r:
        docs = r.select("documents", order_by="created_at")
    ids = [d["doc_id"] for d in docs if d["status"] == "SEGMENTED"]
    if doc_ids:
        wanted = set(doc_ids)
        missing = wanted - set(ids)
        if missing:
            raise KeyError(f"not ingested (or not SEGMENTED): {sorted(missing)}")
        ids = [d for d in ids if d in wanted]
    return ids


def start_run(store: Store, config: SynthConfig, ontology: Ontology, arm: str,
              doc_ids: Sequence[str], relation_mode: str, scripted: bool, ontology_name: str | None = None) -> str:
    run_id = new_id("run")
    manifest = {
        "arm": arm, "doc_ids": list(doc_ids), "relation_mode": relation_mode,
        "profile": os.environ.get("SYNTH_PROFILE") or "default",
        "scripted_models": bool(scripted), "config": config.as_manifest(),
        "ontology": {"pack": ontology.pack, "version": ontology.version, "sha256": ontology.fingerprint(),
                     "name": ontology_name or Path(config.ontology).stem},
        "prompts": prompt_hashes(),
        "models": {name: ep.model for name, ep in config.model_settings.endpoints.items()},
    }
    with store.begin(run_id=run_id, stage="RUN", module="J0") as tx:
        tx.insert("runs", dict(run_id=run_id, arm=arm, started_at=_now(), git_sha=git_sha(),
                               config_hash=config.config_hash({"ontology": ontology.fingerprint(),
                                                               "prompts": manifest["prompts"]}),
                               manifest=manifest, seed=config.seed, status="RUNNING"))
    return run_id


def run(arm: str = "minus0", config_path: str | Path | None = None,
        doc_ids: Sequence[str] | None = None, resume: str | None = None,
        relation_mode: str | None = None, scripted: bool | ScriptedModelClient = False,
        store: Store | None = None, gateway: Gateway | None = None, gliner=None,
        gliner_mode: str | None = None, overrides: dict[str, Any] | None = None,
        finalize: bool = True, ontology: str | None = None) -> dict[str, Any]:
    """Run one engine (``arm`` = engine id, see :mod:`synth.engines`). Returns the run report.
    ``ontology``: an ontology pack name (console/CLI choice); a resumed run keeps the one it started with."""
    from synth import engines
    from synth.config import ontology_path

    config, onto = load_all(config_path, overrides)
    store = store or open_store(config.db_url())
    relation_mode = relation_mode or config.model_settings.tier1.relation_mode
    if resume:
        with store.reader() as r:
            row = r.get("runs", run_id=resume)
        if row is None:
            raise KeyError(f"no run {resume}")
        arm = row["arm"]
        ontology = ((row.get("manifest") or {}).get("ontology") or {}).get("name") or ontology
    if ontology and Path(config.ontology).stem != ontology:
        config = config.model_copy(update={"ontology": str(ontology_path(ontology))})
        onto = load_ontology(config.resolve(config.ontology))
    if resume and row["manifest"].get("ontology", {}).get("sha256") not in (None, onto.fingerprint()):
        raise ValueError(f"run {resume} started with a different version of ontology {ontology!r}; "
                         "resume would mix two vocabularies: start a new run instead")
    try:
        engine = engines.get(arm)
    except KeyError as exc:
        raise ValueError(str(exc)) from None
    if not scripted and gateway is None:
        missing = engines.missing_slots(engine, config)
        if missing:
            raise ValueError(f"{engine.name} needs {', '.join(s.label for s in missing)}: assign "
                             f"{'it' if len(missing) == 1 else 'them'} in the console (Garage) or in the "
                             f"build's models file (endpoints: {', '.join(s.endpoint for s in missing)})")

    if resume:
        with store.reader() as r:
            row = r.get("runs", run_id=resume)
        if row is None:
            raise KeyError(f"no run {resume}")
        run_id, arm = resume, row["arm"]
        doc_ids = row["manifest"].get("doc_ids") or []
        relation_mode = row["manifest"].get("relation_mode", relation_mode)
    else:
        doc_ids = _corpus(store, doc_ids)
        run_id = start_run(store, config, onto, arm, doc_ids, relation_mode, bool(scripted), ontology)

    _progress("RUN", run_id, "resumed" if resume else f"started ({arm})")
    gateway = gateway or make_gateway(config, store, scripted)
    if gliner is None and any(sl.kind == "gliner" for sl in engine.slots):
        gliner = make_gliner(config, gliner_mode or ("heuristic" if scripted else None))
    ctx = StageContext(config=config, ontology=onto, store=store, gateway=gateway,
                       run_id=run_id, arm=arm, gliner=gliner,
                       extra={"relation_mode": relation_mode, "escalation": arm == "full", **engine.extra})
    stages: list[StageReport] = []
    try:
        stages = engine.run(ctx, doc_ids)
        if finalize:
            stages.append(_finalize(ctx))
        status = "DONE" if not any(s.failed for s in stages) else "DONE_WITH_FAILURES"
    except Exception:
        status = "FAILED"
        raise
    finally:
        with store.begin(run_id=run_id, stage="RUN", module="J0") as tx:
            tx.update("runs", {"run_id": run_id}, {"status": status, "finished_at": _now()})
    dead = [f for s in stages for f in s.failed]
    return {"run_id": run_id, "arm": arm, "status": status, "documents": len(doc_ids),
            "relation_mode": relation_mode, "stages": [s.as_dict() for s in stages],
            "dead_letter": dead}


# ------------------------------------------------------------------ arms
def _per_doc(ctx: StageContext, stage: str, doc_ids: Sequence[str], fn) -> StageReport:
    report = StageReport(stage)
    for doc_id in doc_ids:
        run_unit(ctx, stage, doc_id, lambda d=doc_id: fn(ctx, d), report)
    return report


def _chunks(ctx: StageContext, doc_id: str) -> list[dict[str, Any]]:
    with ctx.reader() as r:
        return r.select("micro_chunks", order_by="idx", doc_id=doc_id)


def _sections(ctx: StageContext, doc_id: str) -> list[dict[str, Any]]:
    with ctx.reader() as r:
        return r.select("sections", order_by="idx", doc_id=doc_id)


def _ingest_coverage(ctx: StageContext, doc_ids: Sequence[str]) -> StageReport:
    from synth.modules.u01_parser import emit_ingest_coverage

    return _per_doc(ctx, S.INGEST, doc_ids, emit_ingest_coverage)


def _run_full(ctx: StageContext, doc_ids: Sequence[str]) -> list[StageReport]:
    from synth.modules import u02_gliner, u03_time, u05_micro, u06_contract, u07_verify
    from synth.modules import u08_resolve, u09_gate, u10_frontier, u11_commit

    reports = [_ingest_coverage(ctx, doc_ids)]
    reports.append(_per_doc(ctx, S.GLINER, doc_ids, u02_gliner.run_document))   # GPU: GLiNER
    reports.append(_per_doc(ctx, S.TIME, doc_ids, u03_time.run_document))
    micro = StageReport(S.MICRO)                                                 # 3B model
    for doc_id in doc_ids:
        for chunk in _chunks(ctx, doc_id):
            run_unit(ctx, S.MICRO, chunk["chunk_id"],
                     lambda c=chunk["chunk_id"]: u05_micro.run_chunk(ctx, c), micro)
    reports.append(micro)
    reports.append(_per_doc(ctx, S.CONTRACT, doc_ids, u06_contract.run_document))
    t2 = StageReport(S.T2)                                                       # existing 14B
    for doc_id in doc_ids:
        for section in _sections(ctx, doc_id):
            run_unit(ctx, S.T2, section["section_id"],
                     lambda s=section["section_id"]: u07_verify.run_section(ctx, s), t2)
    reports.append(t2)
    reports.append(_whole_run(ctx, S.RESOLVE, u08_resolve.run))
    reports.append(_whole_run(ctx, S.GATE, u09_gate.run))
    reports.append(_whole_run(ctx, S.FRONTIER, u10_frontier.run))               # T3
    reports.append(_whole_run(ctx, S.COMMIT, u11_commit.run))
    return reports


def _run_minus0(ctx: StageContext, doc_ids: Sequence[str]) -> list[StageReport]:
    """Synth −0: GLiNER builds the entity table, one local model draws the standard graph
    (the original Datanaut prompt) guided by it, the frontier reviews and fixes each
    section against its source sentences, then resolve and commit. No gate: the frontier
    already saw everything."""
    from synth.modules import (m0_local, u02_gliner, u03_time, u06_contract, u07_verify, u08_resolve,
                               u11_commit)

    ctx.extra.setdefault("verify_endpoint", "t3")
    reports = [_ingest_coverage(ctx, doc_ids)]
    reports.append(_per_doc(ctx, S.GLINER, doc_ids, u02_gliner.run_document))   # entity table
    reports.append(_per_doc(ctx, S.TIME, doc_ids, u03_time.run_document))
    local = StageReport(S.LOCAL)                                                 # local model
    for doc_id in doc_ids:
        for section in _sections(ctx, doc_id):
            run_unit(ctx, S.LOCAL, section["section_id"],
                     lambda s=section["section_id"]: m0_local.run_section(ctx, s), local)
    reports.append(local)
    reports.append(_per_doc(ctx, S.CONTRACT, doc_ids, u06_contract.run_document))
    review = StageReport(S.T2)                                                   # frontier review
    for doc_id in doc_ids:
        for section in _sections(ctx, doc_id):
            run_unit(ctx, S.T2, section["section_id"],
                     lambda s=section["section_id"]: u07_verify.run_section(ctx, s), review)
    reports.append(review)
    reports.append(_whole_run(ctx, S.RESOLVE, u08_resolve.run))
    reports.append(_whole_run(ctx, S.COMMIT, u11_commit.run))
    return reports


def _run_single_tier(ctx: StageContext, doc_ids: Sequence[str], tier: str) -> list[StageReport]:
    from synth.modules import arm_extract, u08_resolve, u11_commit

    reports = [_ingest_coverage(ctx, doc_ids)]
    extract = StageReport(S.ARM_EXTRACT)
    for doc_id in doc_ids:
        for section in _sections(ctx, doc_id):
            run_unit(ctx, S.ARM_EXTRACT, section["section_id"],
                     lambda s=section["section_id"]: arm_extract.run_section_arm(ctx, s, tier), extract)
    reports.append(extract)
    reports.append(_whole_run(ctx, S.RESOLVE, u08_resolve.run))
    reports.append(_whole_run(ctx, S.COMMIT, u11_commit.run))   # no gate outside `full`
    return reports


def _whole_run(ctx: StageContext, stage: str, fn) -> StageReport:
    report = StageReport(stage)
    run_unit(ctx, stage, "run", lambda: fn(ctx), report)
    return report


def _finalize(ctx: StageContext) -> StageReport:
    from synth.audit.coverage import finalize

    report = StageReport(S.FINALIZE)
    run_unit(ctx, S.FINALIZE, "coverage", lambda: finalize(ctx), report)
    return report


# ------------------------------------------------------------------ wrappers
def _store_for(config_path: str | Path | None, store: Store | None = None) -> tuple[SynthConfig, Store]:
    config, _ = load_all(config_path)
    return config, store or open_store(config.db_url())


def audit(run_id: str, config_path: str | Path | None = None, store: Store | None = None) -> dict:
    from synth.audit.audits import audit_run

    _, store = _store_for(config_path, store)
    return audit_run(store, run_id)


def evaluate(run_id: str, config_path: str | Path | None = None, store: Store | None = None) -> dict:
    from synth.eval import metrics

    config, ontology = load_all(config_path)
    store = store or open_store(config.db_url())
    return metrics.evaluate_run(store, run_id, config=config, ontology=ontology)


def report(run_ids: Sequence[str], config_path: str | Path | None = None,
           out: str | Path | None = None, store: Store | None = None) -> dict:
    from synth.eval.scorecard import build_report

    config, ontology = load_all(config_path)
    store = store or open_store(config.db_url())
    out_dir = Path(out) if out else config.path("runs") / "report"
    md, html = build_report(store, list(run_ids), out_dir, config=config, ontology=ontology)
    return {"markdown": str(md), "html": str(html)}


def view(run_id: str, config_path: str | Path | None = None, out: str | Path | None = None,
         store: Store | None = None) -> Path:
    from synth.viewer.source_viewer import build_viewer

    config, store = _store_for(config_path, store)
    return build_viewer(store, run_id, Path(out) if out else config.path("runs") / run_id / "viewer")


def export(run_id: str, config_path: str | Path | None = None, out: str | Path | None = None,
           store: Store | None = None) -> dict:
    from synth.export.graph_export import export_graph

    config, ontology = load_all(config_path)
    store = store or open_store(config.db_url())
    paths = export_graph(store, run_id, Path(out) if out else config.path("runs") / run_id / "export",
                         ontology=ontology)
    return {k: str(v) for k, v in paths.items()}


def gold(action: str, config_path: str | Path | None = None, store: Store | None = None,
         **kwargs: Any) -> Any:
    from synth.eval import gold as gold_tools

    config, ontology = load_all(config_path)
    store = store or open_store(config.db_url())
    if action == "prefill":
        run_id = new_id("run")
        with store.begin(run_id=run_id, stage="RUN", module="J0") as tx:
            tx.insert("runs", dict(run_id=run_id, arm="gold_prefill", started_at=_now(),
                                   git_sha=git_sha(), manifest={"purpose": "gold prefill"},
                                   seed=config.seed, status="RUNNING"))
        ctx = StageContext(config=config, ontology=ontology, store=store,
                           gateway=make_gateway(config, store), run_id=run_id, arm="gold_prefill")
        return gold_tools.prefill(ctx, kwargs["doc_id"], kwargs["first"], kwargs["last"])
    if action == "validate":
        return gold_tools.validate(store, Path(kwargs["path"]), ontology=ontology)
    if action == "promote":
        return gold_tools.promote(store, Path(kwargs["path"]), kwargs.get("annotator", "unknown"),
                                  kwargs.get("blind", False))
    raise ValueError(f"unknown gold action {action!r}")


def serve(config_path: str | Path | None = None, host: str = "127.0.0.1", port: int = 8765,
          store: Store | None = None) -> None:  # pragma: no cover - blocking server
    import uvicorn

    from synth.api.app import create_app

    _, store = _store_for(config_path, store)
    uvicorn.run(create_app(store), host=host, port=port)
