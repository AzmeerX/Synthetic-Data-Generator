"""JSON Schema export (Phase A deliverable: a schema for ``CdmDocument``).

The schemas are generated from the Pydantic models, so they cannot drift from
the implementation.  Generation is deterministic: the same models always produce
byte-identical files.  Nothing here validates at runtime — Pydantic does that;
this module exists for downstream consumers and for review.
"""
from __future__ import annotations

import json
from pathlib import Path

from .content import CdmDocument
from .corpus import Corpus
from .render import RenderMap, RenderResult
from .workbook import CdmWorkbook

__all__ = ["SCHEMAS", "schema_for", "export_schemas", "SCHEMA_DIR"]

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

SCHEMAS = {
    "cdm_document": CdmDocument,
    "cdm_workbook": CdmWorkbook,
    "corpus": Corpus,
    "render_map": RenderMap,
    "render_result": RenderResult,
}


def schema_for(name: str) -> dict:
    try:
        model = SCHEMAS[name]
    except KeyError as exc:
        raise KeyError(f"unknown schema {name!r}; expected one of {sorted(SCHEMAS)}") from exc
    return model.model_json_schema(mode="serialization")


def export_schemas(out_dir: str | Path = SCHEMA_DIR) -> dict[str, Path]:
    """Write every schema as pretty, key-sorted JSON and return the paths."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in sorted(SCHEMAS):
        path = target / f"{name}.json"
        path.write_text(json.dumps(schema_for(name), indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        written[name] = path
    return written
