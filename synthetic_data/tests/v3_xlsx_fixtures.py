"""Regenerate the Phase F XLSX regression fixtures.

Deliberately **not** a pytest test and never invoked by the suite:

    py -3.13 synthetic_data/tests/v3_xlsx_fixtures.py

Records, per workbook: sha256 of the rendered bytes, size, sheets, spans, cells, tables,
fact surfaces and renderer warnings, plus the library versions that produced them.  The
bytes depend on openpyxl (which serialises the package) and on lxml (which rewrites the
worksheet parts to add cached formula results), so both are pinned and recorded.
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from synthetic_data.renderers import RenderContext  # noqa: E402
from synthetic_data.tests.v3_txt import PILOT, pilot_plan  # noqa: E402
from synthetic_data.tests.v3_xlsx import render_xlsx, workbook_feature  # noqa: E402

TARGET = Path(__file__).with_name("v3_xlsx_fixtures.json")


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:            # pragma: no cover
        return "not installed"


def build() -> dict:
    out = Path(tempfile.mkdtemp())
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)
    documents: dict[str, dict] = {}
    document = plan.documents[plan.workbook_ids[0]]
    documents[document.document_id] = _describe(
        render_xlsx(document, out / "pilot.xlsx"))
    feature = workbook_feature()
    documents[feature.document_id] = _describe(render_xlsx(feature, out / "feature.xlsx"))
    return {
        "description": "Phase F XLSX renderer regression fixtures: the pilot workbook "
                       "(Cover, Share register, Transaction ledger, Financial summary, "
                       "Security schedule) and a hand-built workbook with an ownership "
                       "schedule, a reconciliation sheet, cross-sheet formulas, an "
                       "interest calculation and a valid defined name",
        "config": PILOT,
        "profile": {"font_family": "Calibri", "table_style": "Excel default"},
        "versions": {"openpyxl": _version("openpyxl"),
                     "et-xmlfile": _version("et-xmlfile"),
                     "lxml": _version("lxml"),
                     "docling-slim": _version("docling-slim")},
        "command": "py -3.13 synthetic_data/tests/v3_xlsx_fixtures.py",
        "hash": "sha256(rendered bytes)",
        "documents": documents,
    }


def _describe(rendered) -> dict:
    spans = rendered.spans
    sheets = rendered.document.workbook.sheets
    declared_rows = sum(len([*t.header_rows, *t.body_rows, *t.total_rows])
                        for sheet in sheets for t in sheet.tables)
    return {
        "doc_type": rendered.document.doc_type,
        "sha256": rendered.result.sha256,
        "bytes": rendered.path.stat().st_size,
        "sheets": len(sheets),
        "sheet_names": [sheet.name for sheet in sheets],
        "spans": len(spans),
        "cells": sum(1 for span in spans if span.kind == "cell"),
        "rows": sum(1 for span in spans if span.kind == "row"),
        "tables": sum(1 for span in spans if span.kind == "table"),
        "declared_rows": declared_rows,
        "facts": len(rendered.result.fact_surfaces),
        "warnings": len(rendered.result.warnings),
        "pageless": all(span.loc.page is None for span in spans),
    }


def main() -> int:
    payload = build()
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {len(payload['documents'])} XLSX fixtures to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
