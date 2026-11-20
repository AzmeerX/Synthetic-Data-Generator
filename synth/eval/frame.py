"""The data frame for test-data authors: what Synth extracts, and the gold format.

Purpose: give someone writing synthetic documents and gold annotations one
machine-readable description of the extraction target (entity types, relations
with their allowed ends, time model, claims and logic links, flags) plus the JSON
Schema of a gold file — both generated from the code, so they never drift.
Contract: :func:`frame_dict` ``(ontology_path) -> dict``; :func:`export_frame`
``(out_dir, ontology_path=None) -> dict[str, Path]`` writes ``frame.json`` and
``gold.schema.json``. Stores written: none (files only). Token caps: none.
Spec: P1 §8 (ontology pack), §12 (gold format).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from synth.config import REPO_ROOT
from synth.eval.gold import GoldSet

DEFAULT_ONTOLOGY = REPO_ROOT / "configs" / "synth" / "ontology" / "corporate_legal.v1.yaml"

TIME = {
    "kinds": ["INSTANT", "INTERVAL", "PERIOD", "DURATION", "RECURRING", "RELATIVE"],
    "granularities": ["year", "quarter", "month", "week", "day", "hour", "minute"],
    "anchor_types": ["ABSOLUTE", "DOC_DATE", "ROLLING_BASE", "EVENT", "UNRESOLVED"],
    "value_format": "ISO 8601 at the stated precision: 2024, 2024-03, 2024-03-14; intervals/periods give both ends "
                    "as dates (FY2025 with a July start = 2024-07-01..2025-06-30); durations put the ISO duration "
                    "(P3Y) in value_start; unknown stays null",
}
STANCES = ["stated", "inferred"]          # synth.contracts.Stance
FLAG_KINDS = ["identity_ambiguity", "inconsistency", "conflict", "merge_blocked"]


def frame_dict(ontology_path: str | Path | None = None) -> dict[str, Any]:
    raw = yaml.safe_load(Path(ontology_path or DEFAULT_ONTOLOGY).read_text(encoding="utf-8"))
    functional = set(raw.get("functional_relations") or [])
    critical = set(raw.get("critical_relations") or [])
    relations = []
    for group, rels in (raw.get("relations") or {}).items():
        for name, (source, target) in rels.items():
            relations.append({"name": name, "group": group, "source": source, "target": target,
                              "functional": name in functional, "critical": name in critical})
    return {
        "pack": raw.get("pack"), "version": raw.get("version"),
        "entity_types": [{"class": cls, "subtypes": subs} for cls, subs in (raw.get("entity_types") or {}).items()],
        "relations": relations,
        "relation_aliases": raw.get("relation_aliases") or {},
        "edge_fields": {"stance": STANCES, "negated": "true when the text says it is NOT so",
                        "nested": "true when stated inside reasoning (because, unless, provided that, …)",
                        "time_roles": (raw.get("time") or {}).get("time_roles") or []},
        "propositions": {"status": raw.get("proposition_status") or [],
                         "note": "claims, decisions, findings, allegations, proposals: things the text holds "
                                 "rather than a relation between two entities"},
        "link_types": raw.get("link_types") or [],
        "time": {**TIME, "fiscal_year_start_month": (raw.get("time") or {}).get("fiscal_year_start_month")},
        "gliner_labels": raw.get("gliner_labels") or [],
        "expected_flag_kinds": FLAG_KINDS,
        "names": {"honorifics_strip": raw.get("honorifics_strip") or [],
                  "honorifics_keep": raw.get("honorifics_keep") or []},
    }


def export_frame(out_dir: str | Path, ontology_path: str | Path | None = None) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"frame": out / "frame.json", "gold_schema": out / "gold.schema.json"}
    paths["frame"].write_text(json.dumps(frame_dict(ontology_path), indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    paths["gold_schema"].write_text(json.dumps(GoldSet.model_json_schema(), indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
    return paths
