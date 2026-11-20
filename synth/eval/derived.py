"""Derived arms: the graph "as of" a stage, rebuilt from the change log (L4).

Purpose: ``t1_only`` and ``t1_t2`` cost no extra runs (P1 §1 Arms). Every create
and update of a logged object writes its after-image to ``change_log``; replaying
the images up to and including a stage gives the store as it stood after it.

* ``t1_only`` = micro edges ``CONTRACTED`` after ``CONTRACT`` (the T1 floor);
* ``t1_t2``   = macro edges not ``REJECTED`` after ``RESOLVE`` (before the gate).

Contract: :func:`load_graph` (current tables) and :func:`graph_as_of`
``(store, run_id, stage) -> Graph`` share one shape; :func:`arm_view` picks the
arm's output edges / propositions / links out of a graph. Ordering inside a stage:
the ``version`` an update writes (entities, macro edges), else the before/after
chain, else change id. Change-log rows of stages outside the pipeline (test
factories, ``RUN``) count as set-up, i.e. before every stage.
Stores written: none. Token caps: none. Spec: P1 §1 (Arms), §4 (L4), §11.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from synth import states as S
from synth.store.tables import LOGGED_OBJECTS

#: Pipeline stage order (both arm shapes). Unknown stages sort first (set-up rows).
STAGE_INDEX = {S.INGEST: 0, S.GLINER: 1, S.TIME: 2, S.MICRO: 3, S.CONTRACT: 4, S.T2: 5,
               S.ARM_EXTRACT: 5, S.RESOLVE: 6, S.GATE: 7, S.FRONTIER: 8, S.COMMIT: 9,
               S.FINALIZE: 10}
DERIVED_STAGE = {"t1_only": S.CONTRACT, "t1_t2": S.RESOLVE}

_TYPES = {"entity": "entities", "mention": "mentions", "time_ref": "time_refs",
          "micro_edge": "micro_edges", "macro_edge": "macro_edges", "proposition": "propositions",
          "prop_link": "links", "flag": "flags"}
_TABLE_PK = {table: pk for table, (_t, pk) in LOGGED_OBJECTS.items()}


def stage_index(stage: str | None) -> int:
    return STAGE_INDEX.get(stage or "", -1)


@dataclass
class Graph:
    run_id: str
    stage: str | None                                  # None = current state
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    mentions: dict[str, dict[str, Any]] = field(default_factory=dict)
    time_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    micro_edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    macro_edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    propositions: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: dict[str, dict[str, Any]] = field(default_factory=dict)
    flags: dict[str, dict[str, Any]] = field(default_factory=dict)

    def root(self, entity_id: str | None) -> str | None:
        """Follow ``merged_into`` (MERGED entities) to the surviving entity."""
        seen = set()
        current = entity_id
        while current and current not in seen:
            seen.add(current)
            row = self.entities.get(current)
            if not row or row.get("status") != S.MERGED or not row.get("merged_into"):
                return current
            current = row["merged_into"]
        return current


def load_graph(store, run_id: str) -> Graph:
    """The run's objects as they are now."""
    g = Graph(run_id=run_id, stage=None)
    with store.reader() as r:
        for table, attr in (("entities", "entities"), ("mentions", "mentions"),
                            ("time_refs", "time_refs"), ("micro_edges", "micro_edges"),
                            ("macro_edges", "macro_edges"), ("propositions", "propositions"),
                            ("prop_links", "links"), ("flags", "flags")):
            pk = _TABLE_PK[table]
            setattr(g, attr, {row[pk]: row for row in r.select(table, run_id=run_id)})
    return g


def _same(before: dict[str, Any] | None, state: dict[str, Any]) -> bool:
    if before is None:
        return False
    keys = (set(before) & set(state)) - {"ts", "version"}
    return all(before[k] == state[k] for k in keys)


def _replay(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Final after-image of one object from its change-log rows (already filtered)."""
    rows = sorted(rows, key=lambda c: (stage_index(c["stage"]), c["op"] != "CREATE",
                                       (c["after"] or {}).get("version") or 0, c["change_id"]))
    state: dict[str, Any] | None = None
    pending = list(rows)
    while pending:
        nxt = pending[0]
        if state is not None:
            first_stage = stage_index(nxt["stage"])
            same_stage = [c for c in pending if stage_index(c["stage"]) == first_stage]
            if not any("version" in (c["after"] or {}) for c in same_stage):
                chained = [c for c in same_stage if _same(c["before"], state)]
                if chained:
                    nxt = chained[0]
        pending.remove(nxt)
        if nxt["after"] is not None:
            state = {**(state or {}), **nxt["after"]}
    return state


def graph_as_of(store, run_id: str, stage: str) -> Graph:
    """The run's graph as it stood after ``stage``, from change-log after-images."""
    limit = stage_index(stage)
    if limit < 0:
        raise ValueError(f"unknown stage {stage!r}")
    by_object: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with store.reader() as r:
        for row in r.select("change_log", run_id=run_id):
            if row["object_type"] not in _TYPES or row["op"] == "DROPPED" or row["object_id"] in (None, "-"):
                continue
            if stage_index(row["stage"]) > limit:
                continue
            by_object[(row["object_type"], row["object_id"])].append(row)
    g = Graph(run_id=run_id, stage=stage)
    for (object_type, object_id), rows in by_object.items():
        if not any(c["op"] == "CREATE" for c in rows):
            continue                                   # created after the stage
        state = _replay(rows)
        if state is not None:
            getattr(g, _TYPES[object_type])[object_id] = state
    return g


# ------------------------------------------------------------------ arm selection
@dataclass
class ArmView:
    arm: str
    graph: Graph
    level: str                                         # micro | macro
    edges: list[dict[str, Any]]
    propositions: list[dict[str, Any]]
    links: list[dict[str, Any]]


def arm_view(store, run_id: str, arm: str, graph: Graph | None = None) -> ArmView:
    """The output an arm is scored on."""
    if arm == "t1_only":
        g = graph or graph_as_of(store, run_id, S.CONTRACT)
        return ArmView(arm, g, "micro",
                       sorted((e for e in g.micro_edges.values() if e.get("status") == S.CONTRACTED),
                              key=lambda e: e["edge_id"]), [], [])
    if arm == "t1_t2":
        g = graph or graph_as_of(store, run_id, S.RESOLVE)

        def keep(row, key="status"):
            return row.get(key) != S.REJECTED
        return ArmView(arm, g, "macro",
                       sorted((e for e in g.macro_edges.values() if keep(e)), key=lambda e: e["edge_id"]),
                       sorted((p for p in g.propositions.values() if keep(p, "state")),
                              key=lambda p: p["prop_id"]),
                       sorted((link for link in g.links.values() if keep(link, "state")),
                              key=lambda link: link["link_id"]))
    g = graph or load_graph(store, run_id)
    return ArmView(arm, g, "macro",
                   sorted((e for e in g.macro_edges.values() if e.get("status") == S.COMMITTED),
                          key=lambda e: e["edge_id"]),
                   sorted((p for p in g.propositions.values() if p.get("state") == S.COMMITTED),
                          key=lambda p: p["prop_id"]),
                   sorted((link for link in g.links.values() if link.get("state") == S.COMMITTED),
                          key=lambda link: link["link_id"]))
