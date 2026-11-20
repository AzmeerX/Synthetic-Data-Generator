"""Engines: the pipelines Synth can run, registered in one place.

Purpose: an engine is a complete extraction pipeline: its stages (shown as an
inverted tree), the model slots it needs (each mapped onto a gateway endpoint of
a build: ``t1_micro``, ``t2``, ``t3`` or ``gliner``) and the function J0 calls to
run it. The console's workshop lists, configures, runs and compares engines from
this registry, so adding a pipeline is one :func:`register` call.
Contract: :data:`ENGINES` (id → :class:`Engine`), :func:`get`, :func:`register`,
:func:`default_engine`, :func:`missing_slots`. An engine's id is the ``arm``
stored on its runs. Stores written: none. Token caps: none. Spec: P1 §1 (arms),
§3 J0.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from synth import states as S


@dataclass(frozen=True)
class Slot:
    id: str                 # the engine's own name for it: "local", "frontier", "gliner", "micro", "verifier"
    endpoint: str           # where a build keeps its model: t1_micro | t2 | t3 | gliner
    label: str
    kind: str = "llm"       # "llm" (an OpenAI-compatible endpoint) | "gliner" (in-process)
    required: bool = True
    hint: str = ""


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    stage: str | None       # J0 stage whose unit progress this node shows
    kind: str               # rules | model | gliner | store
    slot: str | None        # the engine slot id, for model/gliner nodes
    detail: str
    children: tuple[str, ...]
    lane: str


@dataclass(frozen=True)
class Engine:
    id: str
    name: str
    status: str             # current | classic | legacy | baseline | experimental
    tagline: str
    description: str
    slots: tuple[Slot, ...]
    lanes: tuple[tuple[str, str, str], ...]          # (id, label, sub)
    nodes: tuple[Node, ...]
    run: Callable[[Any, Sequence[str]], list]        # (StageContext, doc_ids) -> list[StageReport]
    default: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # run options put in ctx.extra

    def slot(self, slot_id: str) -> Slot | None:
        return next((s for s in self.slots if s.id == slot_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "status": self.status, "default": self.default,
                "tagline": self.tagline, "description": self.description,
                "slots": [vars(s) for s in self.slots],
                "lanes": [{"id": i, "label": label, "sub": sub} for i, label, sub in self.lanes],
                "nodes": [{**vars(n), "children": list(n.children)} for n in self.nodes]}


ENGINES: dict[str, Engine] = {}


def register(engine: Engine) -> Engine:
    if engine.default:
        for other in ENGINES.values():
            if other.default and other.id != engine.id:
                raise ValueError(f"two default engines: {other.id} and {engine.id}")
    ENGINES[engine.id] = engine
    return engine


def get(engine_id: str) -> Engine:
    try:
        return ENGINES[engine_id]
    except KeyError:
        raise KeyError(f"unknown engine {engine_id!r} (engines: {', '.join(ENGINES)})") from None


def default_engine() -> Engine:
    return next(e for e in ENGINES.values() if e.default)


def missing_slots(engine: Engine, config) -> list[Slot]:
    """Required LLM slots whose endpoint still holds the template placeholder."""
    out = []
    for s in engine.slots:
        if s.kind != "llm" or not s.required:
            continue
        ep = config.endpoint(s.endpoint)
        if "<" in ep.base_url or "<" in ep.model:
            out.append(s)
    return out


# ------------------------------------------------------------------ the built-in engines
def _runner():
    from synth.orchestrator import runner
    return runner


DOCS = Node("DOCS", "Documents", None, "store", None, "the ingested corpus (S1)", ("U1",), "IN")
PARSE = "Docling/pypdf text, pysbd sentences, chunks and sections"
GLINER_DETAIL = "every entity and mention in the whole text (S2)"
TIME_DETAIL = "dates, intervals, fiscal years, relative times (S3)"
CONTRACT_DETAIL = "fold duplicates, type-check, identity and value flags"
RESOLVE_DETAIL = "entity resolution, conflicts over time (L5)"
AUDIT = Node("U12", "Eval & audit", S.FINALIZE, "rules", None, "coverage finalizer, provenance/coverage/budget audits",
             (), "OUT")

register(Engine(
    id="minus0", name="Synth −0", status="current", default=True,
    tagline="GLiNER → local model → frontier review",
    description=("GLiNER builds the entity table; one local model draws the standard Datanaut graph per section "
                 "with that table; the frontier checks every local fact against its source sentences and fixes it. "
                 "Every final fact is frontier-verified and grounded to its sentence."),
    slots=(Slot("gliner", "gliner", "GLiNER entity model", "gliner", hint="builds the entity table"),
           Slot("local", "t2", "Local graph model", hint="draws the standard graph per section"),
           Slot("frontier", "t3", "Frontier reviewer", hint="tests and fixes the local graph")),
    lanes=(("IN", "Ingest", "parse"), ("GLINER", "GLiNER", "entity table"), ("LOCAL", "Local", "graph model"),
           ("FRONTIER", "Frontier", "review · fix"), ("OUT", "Out", "commit · audit")),
    nodes=(DOCS,
           Node("U1", "Parse & segment", S.INGEST, "rules", None, PARSE, ("U2", "U3"), "IN"),
           Node("U2", "GLiNER entity table", S.GLINER, "gliner", "gliner", GLINER_DETAIL, ("LOCAL",), "GLINER"),
           Node("U3", "Time anchor", S.TIME, "rules", None, TIME_DETAIL, ("LOCAL",), "GLINER"),
           Node("LOCAL", "Local graph model", S.LOCAL, "model", "local",
                "the original Datanaut extraction per section, given the GLiNER entity table", ("U6",), "LOCAL"),
           Node("U6", "Merge & contract", S.CONTRACT, "rules", None, CONTRACT_DETAIL, ("REVIEW",), "LOCAL"),
           Node("REVIEW", "Frontier review", S.T2, "model", "frontier",
                "checks every local fact against its source: confirm, correct, remove or add (with quotes)",
                ("U8",), "FRONTIER"),
           Node("U8", "Resolve & reconcile", S.RESOLVE, "rules", None, RESOLVE_DETAIL, ("U11",), "FRONTIER"),
           Node("U11", "Commit", S.COMMIT, "store", None, "the final graph: frontier-verified facts", ("U12",), "OUT"),
           AUDIT),
    run=lambda ctx, docs: _runner()._run_minus0(ctx, docs),
    extra={"verify_endpoint": "t3"},
))

register(Engine(
    id="t2_only", name="Datanaut Classic", status="classic",
    tagline="one local model, the original Datanaut prompt",
    description=("The original Datanaut pipeline: one local model reads each section with the original extraction "
                 "prompt; its quotes are grounded to sentences, entities resolved, facts committed. No GLiNER, no "
                 "frontier."),
    slots=(Slot("local", "t2", "Local graph model", hint="the original Datanaut extraction"),),
    lanes=(("IN", "Ingest", "parse"), ("LOCAL", "Local", "graph model"), ("OUT", "Out", "commit · audit")),
    nodes=(DOCS,
           Node("U1", "Parse & segment", S.INGEST, "rules", None, PARSE, ("ARM",), "IN"),
           Node("ARM", "Local graph model", S.ARM_EXTRACT, "model", "local",
                "the original Datanaut extraction per section", ("U8",), "LOCAL"),
           Node("U8", "Resolve & reconcile", S.RESOLVE, "rules", None, RESOLVE_DETAIL, ("U11",), "LOCAL"),
           Node("U11", "Commit", S.COMMIT, "store", None, "commit grounded facts", ("U12",), "OUT"),
           AUDIT),
    run=lambda ctx, docs: _runner()._run_single_tier(ctx, docs, tier="t2"),
))

register(Engine(
    id="full", name="Ruminant", status="legacy",
    tagline="GLiNER + micro LLM → contraction → T2 verifier → frontier on leftovers",
    description=("The original Synth thesis: GLiNER and a small micro model build micro graphs per chunk, code "
                 "contracts them, a mid-size model verifies each section, and the frontier only sees the hard "
                 "leftovers the gate escalates."),
    slots=(Slot("gliner", "gliner", "GLiNER entity model", "gliner", hint="builds the entity table"),
           Slot("micro", "t1_micro", "Micro LLM", hint="micro graphs per chunk"),
           Slot("verifier", "t2", "T2 verifier", hint="verifies and repairs each section"),
           Slot("frontier", "t3", "Frontier (escalations)", required=False,
                hint="only the gate's hard leftovers; without it they are held for a human")),
    lanes=(("IN", "Ingest", "parse"), ("T1", "Tier 1", "micro graphs"), ("T2", "Tier 2", "verify · resolve"),
           ("T3", "Tier 3", "frontier"), ("OUT", "Out", "eval · audit")),
    nodes=(DOCS,
           Node("U1", "Parse & segment", S.INGEST, "rules", None, PARSE, ("U2", "U3", "U5"), "IN"),
           Node("U2", "GLiNER pass", S.GLINER, "gliner", "gliner", GLINER_DETAIL, ("U6",), "T1"),
           Node("U3", "Time anchor", S.TIME, "rules", None, TIME_DETAIL, ("U6",), "T1"),
           Node("U5", "Micro LLM pass", S.MICRO, "model", "micro", "micro graphs per chunk (S4)", ("U6",), "T1"),
           Node("U6", "Merge & contract", S.CONTRACT, "rules", None, CONTRACT_DETAIL, ("U7",), "T1"),
           Node("U7", "T2 verifier", S.T2, "model", "verifier", "verify and repair the micro rows per section",
                ("U8",), "T2"),
           Node("U8", "Resolve & reconcile", S.RESOLVE, "rules", None, RESOLVE_DETAIL, ("U9",), "T2"),
           Node("U9", "Gate & packet", S.GATE, "rules", None, "pick the hard leftovers (≤10% escalate)", ("U10",),
                "T2"),
           Node("U10", "Frontier", S.FRONTIER, "model", "frontier", "answers escalations", ("U11",), "T3"),
           Node("U11", "Commit", S.COMMIT, "store", None, "apply verdicts, commit verified facts", ("U12",), "T3"),
           AUDIT),
    run=lambda ctx, docs: _runner()._run_full(ctx, docs),
))

register(Engine(
    id="t3_only", name="Frontier only", status="baseline",
    tagline="the frontier model reads everything (quality ceiling, cost baseline)",
    description="The frontier model extracts every section directly. The baseline the cheaper engines are measured "
                "against.",
    slots=(Slot("frontier", "t3", "Frontier model", hint="reads every section"),),
    lanes=(("IN", "Ingest", "parse"), ("FRONTIER", "Frontier", "extraction"), ("OUT", "Out", "commit · audit")),
    nodes=(DOCS,
           Node("U1", "Parse & segment", S.INGEST, "rules", None, PARSE, ("ARM",), "IN"),
           Node("ARM", "Frontier extraction", S.ARM_EXTRACT, "model", "frontier", "extraction per section",
                ("U8",), "FRONTIER"),
           Node("U8", "Resolve & reconcile", S.RESOLVE, "rules", None, RESOLVE_DETAIL, ("U11",), "FRONTIER"),
           Node("U11", "Commit", S.COMMIT, "store", None, "commit grounded facts", ("U12",), "OUT"),
           AUDIT),
    run=lambda ctx, docs: _runner()._run_single_tier(ctx, docs, tier="t3"),
))
