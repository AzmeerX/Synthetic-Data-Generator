"""U12 metrics: score one run (and its derived arms) against the promoted gold.

Purpose: every number of the scorecard, per arm, with 95% bootstrap intervals over
documents, written to L8 ``eval_scores`` (P1 §12). Matching is in
:mod:`synth.eval.match`; the arm's graph comes from :mod:`synth.eval.derived`.

Arms: the run's own arm (``full`` | ``t2_only`` | ``t3_only``: COMMITTED macro edges,
propositions and links) and, for ``full`` runs, the derived ``t1_only`` (micro edges
CONTRACTED after CONTRACT) and ``t1_t2`` (macro edges before the gate).

Metrics (``metric`` / ``slice``; P/R/F1 metrics end in ``.p`` ``.r`` ``.f1``):

* gold-based, per passage, bootstrap CI: ``entity.mention``, ``entity.cluster``,
  ``resolution.pairwise.p|r``, ``resolution.false_merge_rate`` (merged ÷ distinct
  same-class gold entity pairs), ``resolution.trap_pass_rate``,
  ``resolution.trap_false_merge_rate``, ``edge.strict``, ``edge.lenient`` (slices
  ``all``, ``class=<relation family>``, ``simple``/``nested``, ``blind``/``prefill``),
  ``proposition``, ``link``, ``fact`` (strict edges + propositions — the H1 fact F1),
  ``time.ref_accuracy``, ``time.edge_accuracy`` (gold time roles on leniently matched
  edges), ``flag.expected_recall``, ``coverage.gold``, ``coverage.micro_label_fnr``,
  ``anchoring.t1_false_kept_rate`` (arm ``t1_t2``);
* run-level, per document, bootstrap CI: ``cost.ftvf`` (t3 ``model_calls``
  tokens_in + tokens_out ÷ committed facts), ``cost.local_tokens_per_fact``,
  ``cost.t1_micro_tokens_per_page``, ``grounding.committed_rate``,
  ``coverage.read|micro_read|yield|loss_rate``;
* run-level scalars: ``cost.frontier_tokens_per_correct_fact`` (FTVF ÷ fact
  precision), ``cost.seconds_per_page``, ``cost.gpu_mem_peak_mb``, ``cost.t3_tokens``,
  ``cost.local_tokens``, ``cost.committed_facts``, ``escalation.rate``,
  ``escalation.verdict_share`` (slice = verdict), ``contract.E``, ``contract.C``,
  ``grounding.pregate_failure_rate``, ``coverage.silent_drops``,
  ``coverage.terminal`` (slice = outcome), ``flow.source|micro|contracted|t2|t3``,
  ``miss.<CATEGORY>`` (slice ``edge`` | ``proposition`` | ``all``).

Miss attribution (real arms; gold facts not matched even leniently), first that
applies: ``NOT_READ`` → ``GROUNDING_FAILED`` → ``NO_MICRO_EDGE_AND_NOT_ADDED`` →
``REMOVED_BY_T2`` → ``ER_ERROR`` → ``REJECTED_BY_GATE`` → ``REJECTED_BY_T3`` →
``HELD`` → ``OTHER`` (found with a field wrong, e.g. negation or status). Candidate
system facts for a cause are same-relation facts citing one of the gold fact's
sentences; ``GROUNDING_FAILED`` = an L4 ``DROPPED`` item of that relation citing the
sentence (or, uncited, quoting it closely).

Contract: :func:`evaluate_run` ``(store, run_id, *, config, ontology) -> dict``;
:func:`score_arm` -> :class:`ArmScore` (also used by the scorecard for paired
bootstraps). ``eval_scores`` rows are keyed by (run, arm, metric, slice); a rerun
updates them in place (no DELETE).
Stores written: L8 ``eval_scores``. Token caps: none (no model). Spec: P1 §7, §12.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from rapidfuzz import fuzz

from synth import states as S
from synth.audit.coverage import compute as compute_coverage, run_documents
from synth.eval import stats
from synth.eval.derived import STAGE_INDEX, ArmView, arm_view, stage_index
from synth.eval.gold import gold_locator, load_gold_sets, locate_evidence, passage_sentences
from synth.eval.match import (
    GEdge, GEntity, GLink, GMention, GoldView, GProp, GTime, SEdge, SLink, SMention, SProp,
    STime, SystemView, edge_entities_ok, edge_time_hits, evidence_ok, flag_recalled,
    match_edges, match_entities, match_links, match_mentions, match_props, match_times,
    merged_gold_pairs, pairwise_resolution, similarity,
)
from synth.modules.common import DocView
from synth.provenance import parse_label
from synth.provenance.locator import GROUNDED, NOT_FOUND, normalize

def real_arms() -> tuple[str, ...]:
    """Arms that are real runs: every registered engine (derived arms are scored from a real run)."""
    from synth import engines
    return tuple(engines.ENGINES)
MISS_ORDER = ("NOT_READ", "GROUNDING_FAILED", "NO_MICRO_EDGE_AND_NOT_ADDED", "REMOVED_BY_T2",
              "ER_ERROR", "REJECTED_BY_GATE", "REJECTED_BY_T3", "HELD", "OTHER")
FACT_TYPES = ("micro_edge", "macro_edge", "proposition", "prop_link")
ARM_ENDPOINTS = {"t1_only": ("t1_micro",), "t1_t2": ("t1_micro", "t2")}
DROP_SIMILARITY = 80.0          # dropped quote vs gold sentence, no usable citation
DROP_CITED_SIMILARITY = 50.0    # … when it cites one of the gold fact's sentences


# ------------------------------------------------------------------ counters
def _prf(c: Counter, family: str, slice_: str, tp: int, n_sys: int, n_gold: int) -> None:
    c[("prf", family, slice_, "tp")] += tp
    c[("prf", family, slice_, "sys")] += n_sys
    c[("prf", family, slice_, "gold")] += n_gold


def _ratio(c: Counter, metric: str, slice_: str, num: float, den: float) -> None:
    c[("ratio", metric, slice_, "num")] += num
    c[("ratio", metric, slice_, "den")] += den


def _div(a: float, b: float) -> float | None:
    return a / b if b else None


def metric_fns(keys) -> dict[tuple[str, str], tuple[Callable[[Counter], float | None],
                                                     Callable[[Counter], int]]]:
    """(metric, slice) -> (value fn, n fn) for every counter family present."""
    fns: dict[tuple[str, str], tuple[Callable, Callable]] = {}
    for kind, name, slice_ in sorted({k[:3] for k in keys}):
        if kind == "prf":
            def key(f, s=slice_, n=name):
                return ("prf", n, s, f)
            tp, sy, go = key("tp"), key("sys"), key("gold")
            fns[(f"{name}.p", slice_)] = (lambda c, tp=tp, sy=sy: _div(c[tp], c[sy]), lambda c, sy=sy: c[sy])
            fns[(f"{name}.r", slice_)] = (lambda c, tp=tp, go=go: _div(c[tp], c[go]), lambda c, go=go: c[go])
            fns[(f"{name}.f1", slice_)] = (lambda c, tp=tp, sy=sy, go=go: _div(2 * c[tp], c[sy] + c[go]),
                                           lambda c, sy=sy, go=go: c[sy] + c[go])
        elif kind == "ratio":
            num, den = ("ratio", name, slice_, "num"), ("ratio", name, slice_, "den")
            fns[(name, slice_)] = (lambda c, num=num, den=den: _div(c[num], c[den]),
                                   lambda c, den=den: int(round(c[den])))
    return fns


# ------------------------------------------------------------------ run data
class RunData:
    """Everything about one run the scorer needs, read once."""

    def __init__(self, store, run_id: str, config=None) -> None:
        self.run_id = run_id
        with store.reader() as r:
            self.run = r.get("runs", run_id=run_id) or {}
            manifest = self.run.get("manifest") or {}
            docs = set(run_documents(r, run_id)) | set(manifest.get("doc_ids") or [])
            docs |= {e["doc_id"] for e in r.select("entities", run_id=run_id) if e["doc_id"]}
            self.doc_ids = sorted(d for d in docs if r.get("documents", doc_id=d))
            self.documents = {d: r.get("documents", doc_id=d) for d in self.doc_ids}
            self.docs = {d: DocView.load(r, d, config) for d in self.doc_ids}
            self.spans: dict[str, dict[str, Any]] = {}
            for d in self.doc_ids:
                for s in r.select("spans", doc_id=d):
                    self.spans[s["span_id"]] = s
            self.edge_spans: dict[str, list[str]] = defaultdict(list)
            for row in r.select("edge_spans"):
                self.edge_spans[row["edge_id"]].append(row["span_id"])
            self.edge_times: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for row in r.select("edge_times"):
                self.edge_times[row["edge_id"]].append((row["time_id"], row["role"]))
            self.lineage: dict[str, list[str]] = defaultdict(list)
            for row in r.select("edge_lineage"):
                self.lineage[row["micro_edge_id"]].append(row["macro_edge_id"])
            self.time_refs = {t["time_id"]: t for t in r.select("time_refs", run_id=run_id)}
            sid_pos = {s["sentence_id"]: (d, s["idx"]) for d, v in self.docs.items() for s in v.sentences}
            self.events: dict[tuple[str, int], set[str]] = defaultdict(set)
            for ev in r.select("coverage_events", run_id=run_id):
                pos = sid_pos.get(ev["sentence_id"])
                if pos:
                    self.events[pos].add(ev["outcome"])
            self.calls = r.select("model_calls", run_id=run_id)
            self.stage_metrics = r.select("stage_metrics", run_id=run_id)
            self.escalations = r.select("escalations", run_id=run_id)
            changes = r.select("change_log", run_id=run_id)
            self.dropped = [c for c in changes if c["op"] == "DROPPED"]
            self.creates = Counter((c["object_type"], c["stage"]) for c in changes if c["op"] == "CREATE")
            self.chunk_doc = {c["chunk_id"]: c["doc_id"] for d in self.doc_ids
                              for c in r.select("micro_chunks", doc_id=d)}
            self.section_doc = {s["section_id"]: s["doc_id"] for d in self.doc_ids
                                for s in r.select("sections", doc_id=d)}
            self.entity_doc = {e["entity_id"]: e["doc_id"] for e in r.select("entities", run_id=run_id)}
            self.coverage = compute_coverage(r, run_id)
            self.gold = load_gold_sets(r, self.doc_ids)
        self.has_micro = any(S.MICRO_READ in ev for ev in self.events.values())
        self._loc: dict[str, tuple[str, int, int, frozenset[int]] | None] = {}

    # -- spans ------------------------------------------------------------------
    def span_loc(self, span_id: str | None):
        """(doc_id, start, end, sentence idxs) of a span, or None."""
        if not span_id:
            return None
        if span_id not in self._loc:
            s = self.spans.get(span_id)
            doc = self.docs.get(s["doc_id"]) if s else None
            if not s or doc is None or s["char_start"] is None:
                self._loc[span_id] = None
            else:
                idx = frozenset(x.idx for x in doc.locator.sentences_overlapping(s["char_start"], s["char_end"]))
                self._loc[span_id] = (s["doc_id"], s["char_start"], s["char_end"], idx)
        return self._loc[span_id]

    def located(self, span_id: str | None) -> bool:
        s = self.spans.get(span_id or "")
        return bool(s) and s["grounding"] in GROUNDED and self.span_loc(span_id) is not None

    def object_doc(self, span_ids) -> str | None:
        for sid in span_ids:
            loc = self.span_loc(sid)
            if loc:
                return loc[0]
        return None

    def read(self, doc_id: str, idxs, events=(S.T2_READ, S.T3_READ)) -> bool:
        return any(self.events.get((doc_id, i), set()) & set(events) for i in idxs)

    # -- model calls --------------------------------------------------------------
    def call_doc(self, unit_id: str | None) -> str | None:
        if not unit_id:
            return None
        for table in (self.docs, self.chunk_doc, self.section_doc):
            if unit_id in table:
                return unit_id if table is self.docs else table[unit_id]
        head = unit_id.split(":", 1)[0]
        if head in self.docs:
            return head
        for esc in self.escalations:
            if esc["esc_id"] == unit_id:
                for item in esc["item_ids"] or []:
                    doc = self.object_doc(self.edge_spans.get(item, [])) or self.entity_doc.get(item)
                    if doc:
                        return doc
        return None


# ------------------------------------------------------------------ gold view
def build_gold_view(item: dict[str, Any], doc: DocView, ontology) -> GoldView:
    gold = item["gold"]
    gv = GoldView(gold_id=item["gold_id"], doc_id=doc.doc_id, first_idx=gold.first_idx,
                  last_idx=gold.last_idx, mode=item.get("mode") or gold.mode)
    passage = passage_sentences(doc, gold.first_idx, gold.last_idx)
    gv.sentence_text = {s["idx"]: doc.sentence_text(s) for s in passage}
    locator = gold_locator(doc)

    def where(ev, label):
        loc = locate_evidence(doc, locator, ev, passage)
        if loc.grounding == NOT_FOUND:
            gv.unlocated.append(label)
            return None
        idx = frozenset(s.idx for s in locator.sentences_overlapping(loc.char_start, loc.char_end))
        return loc.char_start, loc.char_end, idx

    def sents(evidence, label) -> frozenset[int]:
        out: set[int] = set()
        for i, ev in enumerate(evidence):
            hit = where(ev, f"{label}#{i}")
            if hit:
                out |= hit[2]
        return frozenset(out)

    def cls(t):
        return (ontology.class_of(t) if ontology else None) or t

    def rel(name):
        return (ontology.canonical_relation(name) if ontology else None) or name

    def family(name):
        r = ontology.relation(name) if ontology else None
        return r.family if r else "unknown"

    for e in gold.entities:
        ge = GEntity(id=e.id, cls=cls(e.type), key=e.key or f"{item['gold_id']}:{e.id}")
        for i, m in enumerate(e.mentions):
            hit = where(m, f"entity {e.id}#{i}")
            if hit:
                ge.mentions.append(len(gv.mentions))
                gv.mentions.append(GMention(e.id, hit[0], hit[1]))
        gv.entities[e.id] = ge
    for e in gold.edges:
        gv.edges.append(GEdge(id=e.id, subject=e.subject, rel=rel(e.rel), family=family(e.rel),
                              object=e.object, negated=e.negated, nested=e.nested,
                              sents=sents(e.evidence, f"edge {e.id}"),
                              times=[(t.role, t.value_start, t.value_end, t.granularity) for t in e.time]))
    for p in gold.propositions:
        gv.props.append(GProp(id=p.id, text=p.text, speaker=p.speaker, status=p.status,
                              negated=p.negated, sents=sents(p.evidence, f"proposition {p.id}")))
    for i, link in enumerate(gold.links):
        gv.links.append(GLink(id=link.id or f"L{i + 1}", src=link.src, dst=link.dst, type=link.type))
    for t in gold.time_refs:
        hit = where(t.evidence, f"time {t.id}")
        if hit:
            gv.times.append(GTime(t.id, t.kind, t.value_start, t.value_end, t.granularity, hit[0], hit[1]))
    gv.flags = [(f.kind, frozenset(f.object_ids)) for f in gold.expected_flags]
    gv.traps = [(t.a, t.b) for t in gold.identity_traps]
    return gv


# ------------------------------------------------------------------ system view
def _sys_edges(rows, graph, data: RunData, doc_id: str, first: int, last: int, ontology) -> list[SEdge]:
    out = []
    for e in rows:
        idx: set[int] = set()
        for span_id in data.edge_spans.get(e["edge_id"], []):
            loc = data.span_loc(span_id)
            if loc and loc[0] == doc_id:
                idx |= loc[3]
        if not any(first <= i <= last for i in idx):
            continue
        rel = (ontology.canonical_relation(e.get("rel")) if ontology else None) or e.get("rel") or ""
        rt = ontology.relation(rel) if ontology else None
        times = []
        for time_id, role in data.edge_times.get(e["edge_id"], []):
            t = data.time_refs.get(time_id) or graph.time_refs.get(time_id) or {}
            times.append((role, t.get("value_start"), t.get("value_end"), t.get("granularity")))
        out.append(SEdge(id=e["edge_id"], src=graph.root(e.get("src_entity_id")),
                         dst=graph.root(e.get("dst_entity_id")), rel=rel,
                         family=rt.family if rt else "unknown", negated=bool(e.get("negated")),
                         status=e.get("status"), sents=frozenset(idx), times=times,
                         tier_last=e.get("tier_last")))
    return out


def build_system_view(view: ArmView, data: RunData, doc_id: str, first: int, last: int,
                      ontology) -> SystemView:
    g = view.graph
    sv = SystemView()

    def in_passage(span_id):
        loc = data.span_loc(span_id)
        return loc if loc and loc[0] == doc_id and any(first <= i <= last for i in loc[3]) else None

    for m in sorted(g.mentions.values(), key=lambda m: m["mention_id"]):
        if not m.get("entity_id") or m.get("status") == S.DISCARDED:
            continue
        root = g.root(m["entity_id"])
        loc = in_passage(m.get("span_id"))
        if root not in g.entities or not loc:
            continue
        sv.mentions.append(SMention(m["mention_id"], root, loc[1], loc[2]))
        ent = g.entities[root]
        sv.clusters[root] = (ontology.class_of(ent.get("type")) if ontology else None) or ent.get("type")
        sv.cluster_sizes[root] = sv.cluster_sizes.get(root, 0) + 1
    sv.edges = _sys_edges(view.edges, g, data, doc_id, first, last, ontology)
    for p in view.propositions:
        loc = in_passage(p.get("span_id"))
        if loc:
            sv.props.append(SProp(id=p["prop_id"], text=p.get("text") or "",
                                  speaker=g.root(p.get("speaker_entity_id")) if p.get("speaker_entity_id") else None,
                                  status=p.get("status") or "", negated=bool(p.get("negated")),
                                  sents=loc[3], state=p.get("state")))
    prop_ids = {p.id for p in sv.props}
    for link in view.links:
        if link.get("src_prop_id") in prop_ids or link.get("dst_prop_id") in prop_ids:
            sv.links.append(SLink(link["link_id"], link["src_prop_id"], link["dst_prop_id"], link.get("type") or ""))
    for t in sorted(g.time_refs.values(), key=lambda t: t["time_id"]):
        loc = in_passage(t.get("span_id"))
        if loc:
            sv.times.append(STime(t["time_id"], t.get("kind"), t.get("value_start"), t.get("value_end"),
                                  t.get("granularity"), loc[1], loc[2]))
    for f in sorted(g.flags.values(), key=lambda f: f["flag_id"]):
        ids = [g.root(i) if i in g.entities else i for i in (f.get("object_ids") or [])]
        sv.flags.append((f.get("kind") or "", ids))
    return sv


# ------------------------------------------------------------------ score
@dataclass
class ArmScore:
    run_id: str
    arm: str
    gold_units: dict[str, Counter] = field(default_factory=dict)
    run_units: dict[str, Counter] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    attribution: list[dict[str, Any]] = field(default_factory=list)
    matched_gold: set[tuple[str, str]] = field(default_factory=set)
    passages: int = 0
    unlocated_gold: list[str] = field(default_factory=list)

    def value(self, metric: str, slice_: str = "all") -> float | None:
        for row in self.rows:
            if row["metric"] == metric and row["slice"] == slice_:
                return row["value"]
        return None

    def row(self, metric: str, slice_: str = "all") -> dict[str, Any] | None:
        return next((r for r in self.rows if r["metric"] == metric and r["slice"] == slice_), None)

    def summary(self) -> dict[str, Any]:
        keys = ("fact.f1", "edge.strict.f1", "edge.lenient.f1", "entity.cluster.f1", "proposition.f1",
                "link.f1", "cost.ftvf", "resolution.trap_pass_rate", "coverage.gold")
        return {"passages": self.passages, "rows": len(self.rows),
                "headline": {k: self.value(k) for k in keys},
                "miss": {r["metric"][5:]: r["value"] for r in self.rows
                         if r["metric"].startswith("miss.") and r["slice"] == "all"}}


def score_passage(gv: GoldView, sv: SystemView, *, arm: str, data: RunData) -> tuple[Counter, dict[str, Any]]:
    """Counters for one passage, plus the matches (for attribution and anchoring)."""
    c: Counter = Counter()
    mention_map = match_mentions(gv, sv)
    _prf(c, "entity.mention", "all", len(mention_map), len(sv.mentions), len(gv.mentions))
    ent_map = match_entities(gv, sv, mention_map)
    _prf(c, "entity.cluster", "all", len(ent_map), len(sv.clusters), len(gv.entities))
    correct, sys_same, gold_same = pairwise_resolution(gv, sv, mention_map)
    _ratio(c, "resolution.pairwise.p", "all", correct, sys_same)
    _ratio(c, "resolution.pairwise.r", "all", correct, gold_same)
    merged = merged_gold_pairs(gv, sv, mention_map)
    ids = sorted(gv.entities)
    distinct = [frozenset((a, b)) for i, a in enumerate(ids) for b in ids[i + 1:]
                if gv.entities[a].cls == gv.entities[b].cls and gv.entities[a].key != gv.entities[b].key]
    _ratio(c, "resolution.false_merge_rate", "all", sum(1 for p in distinct if p in merged), len(distinct))
    trap_merged = sum(1 for a, b in gv.traps if frozenset((a, b)) in merged)
    _ratio(c, "resolution.trap_pass_rate", "all", len(gv.traps) - trap_merged, len(gv.traps))
    _ratio(c, "resolution.trap_false_merge_rate", "all", trap_merged, len(gv.traps))

    prop_sents: set[int] = set()
    for p in gv.props:
        prop_sents |= p.sents
    nested_sents = gv.nested_sents
    by_sys = {s.id: s for s in sv.edges}
    gold_by_id = {g.id: g for g in gv.edges}
    maps = {}
    for mode, lenient in (("strict", False), ("lenient", True)):
        emap = match_edges(gv.edges, sv.edges, ent_map, lenient)
        maps[mode] = emap
        fam = f"edge.{mode}"
        _prf(c, fam, "all", len(emap), len(sv.edges), len(gv.edges))
        sys_to_gold = {sid: gid for gid, sid in emap.items()}
        for g in gv.edges:
            nested = g.nested or bool(g.sents & prop_sents)
            hit = int(g.id in emap)
            for sl in (f"class={g.family}", "nested" if nested else "simple"):
                c[("prf", fam, sl, "tp")] += hit
                c[("prf", fam, sl, "gold")] += 1
        for s in sv.edges:
            g = gold_by_id.get(sys_to_gold.get(s.id, ""))
            if g is not None:
                cls_, nested = g.family, g.nested or bool(g.sents & prop_sents)
            else:
                cls_, nested = s.family, bool(s.sents & nested_sents)
            for sl in (f"class={cls_}", "nested" if nested else "simple"):
                c[("prf", fam, sl, "sys")] += 1
    prop_map = match_props(gv.props, sv.props, ent_map)
    _prf(c, "proposition", "all", len(prop_map), len(sv.props), len(gv.props))
    link_map = match_links(gv.links, sv.links, prop_map)
    _prf(c, "link", "all", len(link_map), len(sv.links), len(gv.links))
    _prf(c, "fact", "all", len(maps["strict"]) + len(prop_map), len(sv.edges) + len(sv.props),
         len(gv.edges) + len(gv.props))

    _tmap, correct_times = match_times(gv.times, sv.times)
    _ratio(c, "time.ref_accuracy", "all", len(correct_times), len(gv.times))
    hits = den = 0
    for gid, sid in maps["lenient"].items():
        g = gold_by_id[gid]
        if g.times:
            hits += edge_time_hits(g, by_sys[sid])
            den += len(g.times)
    _ratio(c, "time.edge_accuracy", "all", hits, den)
    ent_inv = {root: gid for gid, root in ent_map.items()}
    edge_inv = {sid: gid for gid, sid in maps["lenient"].items()}
    recalled = sum(1 for kind, objs in gv.flags if flag_recalled(kind, objs, sv, ent_inv, edge_inv))
    _ratio(c, "flag.expected_recall", "all", recalled, len(gv.flags))

    if arm in real_arms():
        facts = gv.fact_sents()
        _ratio(c, "coverage.gold", "all", sum(1 for f in facts if data.read(gv.doc_id, f)), len(facts))
        if data.has_micro:
            fact_sents = sorted({i for f in facts for i in f})
            unmarked = [i for i in fact_sents
                        if not data.events.get((gv.doc_id, i), set()) & {S.FACT_SENTENCE, S.NESTED_SENTENCE}]
            _ratio(c, "coverage.micro_label_fnr", "all", len(unmarked), len(fact_sents))

    if gv.mode:
        for key in [k for k in c if k[0] in ("prf", "ratio") and k[2] == "all"]:
            c[(key[0], key[1], gv.mode, key[3])] += c[key]
    info = {"ent_map": ent_map, "edge_maps": maps, "prop_map": prop_map}
    return c, info


# ------------------------------------------------------------------ miss attribution
def _drop_matches(drop: dict[str, Any], kind: str, rel: str | None, sents: frozenset[int],
                  texts: list[str], ontology) -> bool:
    """Did this ungrounded (dropped) model item propose the gold fact? Same relation,
    and either it cites one of the fact's sentences (and its quote resembles it) or,
    with no usable citation, its quote is close to the fact's sentence."""
    item = drop.get("after") or {}
    if not isinstance(item, dict):
        return False
    body = item.get("edge") if isinstance(item.get("edge"), dict) else item
    if kind == "edge":
        if drop["object_type"] not in ("micro_edge", "macro_edge"):
            return False
        item_rel = body.get("rel")
        if item_rel and ontology is not None:
            item_rel = ontology.canonical_relation(item_rel) or item_rel
        if item_rel and item_rel != rel:
            return False
    elif drop["object_type"] != "proposition":
        return False
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    quote = normalize(evidence.get("quote") or item.get("quote") or item.get("text") or "")
    if not quote:
        return False
    best = max((fuzz.partial_ratio(quote, normalize(t)) for t in texts if t), default=0.0)
    cited = {p[1] for p in (parse_label(str(x)) for x in evidence.get("sentence_ids") or []) if p}
    if cited:
        return bool(cited & sents) and best >= DROP_CITED_SIMILARITY
    return best >= DROP_SIMILARITY


def attribute_misses(gv: GoldView, ent_map, edge_map, prop_map, data: RunData,
                     all_micro: list[SEdge], all_macro: list[SEdge], all_props: list[SProp],
                     ontology=None) -> list[dict[str, Any]]:
    """Why each gold fact (edge or proposition) that no system fact matched was missed."""
    escalated: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for esc in data.escalations:
        for item in esc["item_ids"] or []:
            escalated[item].append(esc)

    def t3_rejected(e: SEdge) -> bool:
        if e.tier_last == "T3":
            return True
        return any(((esc.get("verdict") or {}).get("verdict") == "REJECT") for esc in escalated.get(e.id, []))

    def after_gate(cands: list[SEdge]) -> str:
        if any(e.status == S.REJECTED and not t3_rejected(e) for e in cands):
            return "REJECTED_BY_GATE"
        if any(e.status == S.REJECTED for e in cands):
            return "REJECTED_BY_T3"
        if any(e.status in (S.HELD_FOR_HUMAN, S.ESCALATED) for e in cands):
            return "HELD"
        return "OTHER"

    out = []
    for g in sorted(gv.edges, key=lambda e: e.id):
        if g.id in edge_map:
            continue
        texts = [gv.sentence_text.get(i, "") for i in sorted(g.sents)]
        if not data.read(gv.doc_id, g.sents):
            cat = "NOT_READ"
        elif any(_drop_matches(d, "edge", g.rel, g.sents, texts, ontology) for d in data.dropped):
            cat = "GROUNDING_FAILED"
        else:
            micro = [e for e in all_micro if e.rel == g.rel and evidence_ok(g.sents, e.sents, False)]
            macro = [e for e in all_macro if e.rel == g.rel and evidence_ok(g.sents, e.sents, False)]
            right = [e for e in macro if edge_entities_ok(g, e, ent_map)]
            if not micro and not macro:
                cat = "NO_MICRO_EDGE_AND_NOT_ADDED"
            elif not macro:
                cat = "REMOVED_BY_T2"
            elif not right:
                cat = "ER_ERROR"
            else:
                cat = after_gate([e for e in right if bool(e.negated) == bool(g.negated)] or right)
        out.append({"gold_id": gv.gold_id, "kind": "edge", "id": g.id, "category": cat})
    for p in sorted(gv.props, key=lambda p: p.id):
        if p.id in prop_map:
            continue
        texts = [gv.sentence_text.get(i, "") for i in sorted(p.sents)]
        if not data.read(gv.doc_id, p.sents):
            cat = "NOT_READ"
        elif any(_drop_matches(d, "proposition", None, p.sents, texts, ontology) for d in data.dropped):
            cat = "GROUNDING_FAILED"
        else:
            near = [s for s in all_props if p.sents & s.sents and similarity(p.text, s.text) >= 70]
            if not near:
                cat = "NO_MICRO_EDGE_AND_NOT_ADDED"
            elif not any((p.speaker is None and s.speaker is None) or
                         (p.speaker is not None and ent_map.get(p.speaker) == s.speaker) for s in near):
                cat = "ER_ERROR"
            elif any(s.state == S.REJECTED for s in near):
                cat = "REJECTED_BY_GATE"
            elif any(s.state in (S.HELD_FOR_HUMAN, S.ESCALATED) for s in near):
                cat = "HELD"
            else:
                cat = "OTHER"
        out.append({"gold_id": gv.gold_id, "kind": "proposition", "id": p.id, "category": cat})
    return out


# ------------------------------------------------------------------ run-level counters
def _arm_facts(view: ArmView, data: RunData) -> dict[str, list[tuple[str, bool]]]:
    """doc -> [(fact id, has a located span)] for the arm's output (whole run)."""
    out: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for e in view.edges:
        spans = data.edge_spans.get(e["edge_id"], [])
        doc = data.object_doc(spans) or "_none"
        out[doc].append((e["edge_id"], any(data.located(s) for s in spans)))
    for p in view.propositions:
        doc = data.object_doc([p.get("span_id")]) or "_none"
        out[doc].append((p["prop_id"], data.located(p.get("span_id"))))
    return out


def _in_scope(call: dict[str, Any], arm: str) -> bool:
    allowed = ARM_ENDPOINTS.get(arm)
    return allowed is None or call["endpoint"] in allowed


def _tokens(call: dict[str, Any]) -> int:
    return (call["tokens_in"] or 0) + (call["tokens_out"] or 0)


def run_counters(view: ArmView, data: RunData, arm: str) -> tuple[dict[str, Counter], dict[str, float]]:
    facts = _arm_facts(view, data)
    docs = list(data.doc_ids)
    units: dict[str, Counter] = {d: Counter() for d in docs}
    n_facts = {d: len(facts.get(d, [])) for d in docs}
    total_facts = sum(n_facts.values()) + len(facts.get("_none", []))
    totals = Counter()
    per_doc: dict[str, Counter] = {d: Counter() for d in docs}
    unassigned = Counter()
    for call in data.calls:
        if not _in_scope(call, arm):
            continue
        kind = "t3" if call["endpoint"] == "t3" else "local"
        tok = _tokens(call)
        totals[kind] += tok
        if call["endpoint"] == "t1_micro":
            totals["t1_micro"] += tok
        if call["endpoint"] == "t2":
            totals["t2"] += tok
        doc = data.call_doc(call["unit_id"])
        target = per_doc[doc] if doc in per_doc else unassigned
        target[kind] += tok
        if call["endpoint"] == "t1_micro":
            target["t1_micro"] += tok
    for d in docs:
        share = (n_facts[d] / total_facts) if total_facts else (1 / len(docs) if docs else 0)
        u = units[d]
        pages = (data.documents[d] or {}).get("pages") or 1
        f = facts.get(d, [])
        _ratio(u, "cost.ftvf", "all", per_doc[d]["t3"] + unassigned["t3"] * share, n_facts[d])
        _ratio(u, "cost.local_tokens_per_fact", "all", per_doc[d]["local"] + unassigned["local"] * share, n_facts[d])
        _ratio(u, "cost.t1_micro_tokens_per_page", "all",
               per_doc[d]["t1_micro"] + unassigned["t1_micro"] * share, pages)
        _ratio(u, "grounding.committed_rate", "all", sum(1 for _i, ok in f if ok), len(f))
    if arm in real_arms():
        for d, cov in data.coverage.per_doc().items():
            if d not in units:
                continue
            u = units[d]
            _ratio(u, "coverage.read", "all", cov["read"], cov["nonfiltered"])
            if data.has_micro:
                _ratio(u, "coverage.micro_read", "all", cov["micro_read"], cov["nonfiltered"])
            _ratio(u, "coverage.yield", "all", cov["committed"], cov["nonfiltered"])
            _ratio(u, "coverage.loss_rate", "all", cov["lost"], cov["nonfiltered"])
    totals["facts"] = total_facts
    totals["pages"] = sum((data.documents[d] or {}).get("pages") or 1 for d in docs)
    return units, dict(totals)


def _seconds(data: RunData, arm: str) -> float:
    run = data.run
    if arm in real_arms() and run.get("started_at") and run.get("finished_at"):
        try:
            return max((run["finished_at"] - run["started_at"]).total_seconds(), 0.0)
        except TypeError:
            pass
    if arm in real_arms():
        wall = sum(m["wall_ms"] or 0 for m in data.stage_metrics)
        if wall:
            return wall / 1000
    return sum(c["latency_ms"] or 0 for c in data.calls if _in_scope(c, arm)) / 1000


def scalar_rows(view: ArmView, data: RunData, arm: str, totals: dict[str, float],
                fact_precision: float | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(metric, value, slice_="all", n=None):
        rows.append(dict(metric=metric, slice=slice_, value=value, ci_low=None, ci_high=None, n=n))
    facts = totals.get("facts", 0)
    ftvf = _div(totals.get("t3", 0), facts)
    add("cost.t3_tokens", float(totals.get("t3", 0)))
    add("cost.local_tokens", float(totals.get("local", 0)))
    add("cost.committed_facts", float(facts))
    add("cost.frontier_tokens_per_correct_fact",
        (ftvf / fact_precision) if ftvf is not None and fact_precision else None)
    pages = totals.get("pages") or 0
    add("cost.seconds_per_page", _div(_seconds(data, arm), pages), n=int(pages))
    peaks = [c["gpu_mem_mb_peak"] for c in data.calls if _in_scope(c, arm) and c["gpu_mem_mb_peak"]]
    add("cost.gpu_mem_peak_mb", float(max(peaks)) if peaks else None)
    src = sum(m["source_tokens"] or 0 for m in data.stage_metrics if m["stage"] == S.CONTRACT)
    cand = sum(m["candidate_tokens"] or 0 for m in data.stage_metrics if m["stage"] == S.CONTRACT)
    contracted = sum(m["contracted_tokens"] or 0 for m in data.stage_metrics if m["stage"] == S.CONTRACT)
    if cand or src:
        add("contract.E", _div(cand, src), n=src)
        add("contract.C", _div(contracted, cand), n=cand)
    if arm not in real_arms():
        return rows
    if not src:
        src = sum((c.get("source_tokens") or 0) for c in data.stage_metrics if c["stage"] == S.MICRO)
    for name, value in (("source", src), ("micro", cand), ("contracted", contracted),
                        ("t2", totals.get("t2", 0)), ("t3", totals.get("t3", 0))):
        add(f"flow.{name}", float(value))
    gate_items = sum(m["items_in"] or 0 for m in data.stage_metrics if m["stage"] == S.GATE)
    if not gate_items:
        gate_items = sum(1 for e in view.graph.macro_edges.values() if not e.get("supersedes"))
    add("escalation.rate", _div(len(data.escalations), gate_items), n=gate_items)
    verdicts = Counter(((e.get("verdict") or {}).get("verdict") or e.get("outcome") or "NONE")
                       for e in data.escalations)
    for verdict, n in sorted(verdicts.items()):
        add("escalation.verdict_share", n / len(data.escalations), slice_=verdict, n=len(data.escalations))
    gate = STAGE_INDEX[S.GATE]
    dropped = sum(1 for d in data.dropped if d["object_type"] in FACT_TYPES and stage_index(d["stage"]) < gate)
    created = sum(n for (t, stage), n in data.creates.items() if t in FACT_TYPES and stage_index(stage) < gate)
    add("grounding.pregate_failure_rate", _div(dropped, dropped + created), n=dropped + created)
    counts = data.coverage.counts()
    add("coverage.silent_drops", float(counts.get(S.SILENT_DROP, 0)))
    for outcome, n in counts.items():
        add("coverage.terminal", float(n), slice_=outcome)
    return rows


# ------------------------------------------------------------------ the arm
def score_arm(store, run_id: str, arm: str, *, config=None, ontology=None,
              data: RunData | None = None) -> ArmScore:
    config, ontology = _defaults(config, ontology)
    resamples, seed = stats.settings(config)
    data = data or RunData(store, run_id, config)
    view = arm_view(store, run_id, arm)
    current = view.graph if arm in real_arms() else None
    t1 = arm_view(store, run_id, "t1_only") if arm == "t1_t2" else None
    t1_t2_ids = {e["edge_id"] for e in view.edges} if arm == "t1_t2" else set()
    score = ArmScore(run_id=run_id, arm=arm)
    for item in data.gold:
        doc = data.docs.get(item["doc_id"])
        if doc is None:
            continue
        gv = build_gold_view(item, doc, ontology)
        score.unlocated_gold.extend(f"{item['gold_id']}: {u}" for u in gv.unlocated)
        sv = build_system_view(view, data, gv.doc_id, gv.first_idx, gv.last_idx, ontology)
        all_view = None
        if current is not None:
            all_view = (_sys_edges(current.micro_edges.values(), current, data, gv.doc_id,
                                   gv.first_idx, gv.last_idx, ontology),
                        _sys_edges(current.macro_edges.values(), current, data, gv.doc_id,
                                   gv.first_idx, gv.last_idx, ontology),
                        build_system_view(arm_view_all_props(view), data, gv.doc_id,
                                          gv.first_idx, gv.last_idx, ontology).props)
        counts, info = score_passage(gv, sv, arm=arm, data=data)
        if all_view is not None:
            att = attribute_misses(gv, info["ent_map"], info["edge_maps"]["lenient"], info["prop_map"],
                                   data, *all_view, ontology=ontology)
            for a in att:
                counts[("count", f"miss.{a['category']}", a["kind"])] += 1
                counts[("count", f"miss.{a['category']}", "all")] += 1
            score.attribution.extend(att)
        if t1 is not None:
            tv = build_system_view(t1, data, gv.doc_id, gv.first_idx, gv.last_idx, ontology)
            tmap = match_edges(gv.edges, tv.edges, match_entities(gv, tv, match_mentions(gv, tv)), True)
            matched = set(tmap.values())
            false = [e for e in tv.edges if e.id not in matched]
            kept = [e for e in false if any(m in t1_t2_ids for m in data.lineage.get(e.id, []))]
            _ratio(counts, "anchoring.t1_false_kept_rate", "all", len(kept), len(false))
        score.matched_gold |= {(item["gold_id"], gid) for gid in info["edge_maps"]["lenient"]}
        unit = score.gold_units.setdefault(gv.doc_id, Counter())
        unit.update(counts)
        score.passages += 1

    gold_fns = metric_fns(k for u in score.gold_units.values() for k in u if k[0] in ("prf", "ratio"))
    run_units, totals = run_counters(view, data, arm)
    score.run_units = run_units
    run_fns = metric_fns(k for u in run_units.values() for k in u)
    for fns, units in ((gold_fns, score.gold_units), (run_fns, run_units)):
        summed = stats.total(units.values())
        cis = stats.bootstrap_many(list(units[d] for d in sorted(units)),
                                   {k: v[0] for k, v in fns.items()}, resamples=resamples, seed=seed)
        for (metric, slice_), (fn, n_fn) in sorted(fns.items()):
            lo, hi = cis[(metric, slice_)]
            score.rows.append(dict(metric=metric, slice=slice_, value=fn(summed), ci_low=lo,
                                   ci_high=hi, n=n_fn(summed)))
    gold_total = stats.total(score.gold_units.values())
    for key in sorted(k for k in gold_total if k[0] == "count"):
        kind = key[2]
        n = sum(v for k, v in gold_total.items() if k[0] == "count" and k[2] == kind)
        score.rows.append(dict(metric=key[1], slice=kind, value=float(gold_total[key]),
                               ci_low=None, ci_high=None, n=n))
    score.rows.extend(scalar_rows(view, data, arm, totals, score.value("fact.p")))
    return score


def arm_view_all_props(view: ArmView) -> ArmView:
    """The same graph with every proposition, whatever its state (for attribution)."""
    return ArmView(view.arm, view.graph, view.level, [],
                   sorted(view.graph.propositions.values(), key=lambda p: p["prop_id"]), [])


# ------------------------------------------------------------------ entry points
def _defaults(config, ontology):
    if config is None:
        from synth.config import load_config
        config = load_config()
    if ontology is None:
        from synth.ontology import load_ontology
        ontology = load_ontology(config.resolve(config.ontology))
    return config, ontology


def write_scores(store, run_id: str, arm: str, rows: list[dict[str, Any]]) -> int:
    """Upsert ``eval_scores`` rows keyed by (run, arm, metric, slice)."""
    with store.begin(run_id=run_id, stage=S.FINALIZE, module="U12") as tx:
        existing = {(r["metric"], r["slice"]) for r in tx.select("eval_scores", run_id=run_id, arm=arm)}
        for row in rows:
            values = {k: row[k] for k in ("value", "ci_low", "ci_high", "n")}
            if (row["metric"], row["slice"]) in existing:
                tx.update("eval_scores", {"run_id": run_id, "arm": arm, "metric": row["metric"],
                                          "slice": row["slice"]}, values)
            else:
                tx.insert("eval_scores", dict(run_id=run_id, arm=arm, metric=row["metric"],
                                              slice=row["slice"], **values))
    return len(rows)


def evaluate_run(store, run_id: str, *, config=None, ontology=None, arms=None,
                 write: bool = True) -> dict[str, Any]:
    """Score the run's arm (and, for ``full``, the derived ``t1_only`` / ``t1_t2``)."""
    config, ontology = _defaults(config, ontology)
    with store.reader() as r:
        run = r.get("runs", run_id=run_id)
    if run is None:
        raise KeyError(f"no run {run_id}")
    base = run["arm"]
    arms = list(arms or ([base] + (["t1_only", "t1_t2"] if base == "full" else [])))
    data = RunData(store, run_id, config)
    out: dict[str, Any] = {}
    for arm in arms:
        score = score_arm(store, run_id, arm, config=config, ontology=ontology, data=data)
        if write:
            write_scores(store, run_id, arm, score.rows)
        out[arm] = score.summary()
    return {"run_id": run_id, "gold_passages": len(data.gold), "arms": out}
