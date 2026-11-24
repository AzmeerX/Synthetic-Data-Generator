"""Regenerate the Phase D PDF regression fixtures.

Deliberately **not** a pytest test and never invoked by the suite.  Run it only when
a renderer or layout change is intended and reviewed:

    py -3.13 synthetic_data/tests/v3_pdf_fixtures.py

It records, for every pilot document plus the feature document:

* sha256 of the rendered bytes (the determinism contract);
* the physical page count from ``pypdfium2`` (the B1 gate);
* spans, sentences, facts and the renderer's own page count.

The PDF bytes depend on ReportLab, so the file also records the versions of
ReportLab, docling and pypdfium2 that produced them.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from synthetic_data.canonical import canonicalize  # noqa: E402
from synthetic_data.renderers import RenderContext  # noqa: E402
from synthetic_data.tests.v3_pdf import (pdf_feature_document, pdf_pages,  # noqa: E402
                                          render_pdf)
from synthetic_data.tests.v3_txt import PILOT, pilot_plan  # noqa: E402

TARGET = Path(__file__).with_name("v3_pdf_fixtures.json")


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:            # pragma: no cover
        return "not installed"


def build() -> dict:
    out = Path(tempfile.mkdtemp())
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)   # the same context the tests use
    documents: dict[str, dict] = {}
    for index, doc_id in enumerate(plan.prose_ids):
        document = plan.documents[doc_id]
        rendered = render_pdf(document, out / f"{index:02d}.pdf", context=context)
        documents[doc_id] = _describe(rendered)
    feature = render_pdf(pdf_feature_document(), out / "99_feature.pdf")
    documents[feature.doc_id] = _describe(feature)
    return {
        "description": "Phase D PDF renderer regression fixtures: seven prose types "
                       "plus a hand-built document with colspan/rowspan, an explicit "
                       "page break, a footnote and a signature",
        "config": PILOT,
        "profile": {"page_size": "A4", "margins_mm": [22.0, 22.0, 20.0, 20.0],
                    "font_family": "Helvetica", "table_style": "grid"},
        "versions": {"reportlab": _version("reportlab"),
                     "docling-slim": _version("docling-slim"),
                     "docling-parse": _version("docling-parse"),
                     "pypdfium2": _version("pypdfium2")},
        "command": "py -3.13 synthetic_data/tests/v3_pdf_fixtures.py",
        "hash": "sha256(rendered bytes)",
        "documents": documents,
    }


def _describe(rendered) -> dict:
    canonical = canonicalize(rendered.document)
    pages = {span.loc.page for span in rendered.spans}
    return {
        "doc_type": rendered.doc_type,
        "sha256": rendered.result.sha256,
        "bytes": rendered.path.stat().st_size,
        "pages": rendered.page_count,
        "renderer_pages": max(pages) if pages else 0,
        "spans": len(rendered.spans),
        "sentences": len(canonical.sentences),
        "facts": len(rendered.result.fact_surfaces),
        "bbox_spans": sum(1 for span in rendered.spans if span.loc.bbox is not None),
        "cells": sum(1 for span in rendered.spans if span.kind == "cell"),
        "tables": sum(1 for span in rendered.spans if span.kind == "table"),
    }


def main() -> int:
    payload = build()
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {len(payload['documents'])} PDF fixtures to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
