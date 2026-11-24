"""Regenerate the Phase E DOCX regression fixtures.

Deliberately **not** a pytest test and never invoked by the suite.  Run it only when a
renderer change is intended and reviewed:

    py -3.13 synthetic_data/tests/v3_docx_fixtures.py

It records, for every pilot document plus the feature document:

* sha256 of the rendered bytes (the determinism contract);
* bytes, spans, sentences, facts and structural counts (sections, paragraphs, tables,
  rows, cells) so a regression in *structure* is caught, not just a byte change;
* the library versions that produced them, because the bytes depend on python-docx's
  template and on the ZIP/XML serialisation it performs.
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

from synthetic_data.canonical import canonicalize  # noqa: E402
from synthetic_data.renderers import RenderContext  # noqa: E402
from synthetic_data.tests.v3_docx import render_docx  # noqa: E402
from synthetic_data.tests.v3_pdf import pdf_feature_document  # noqa: E402
from synthetic_data.tests.v3_txt import PILOT, pilot_plan  # noqa: E402

TARGET = Path(__file__).with_name("v3_docx_fixtures.json")


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
        rendered = render_docx(document, out / f"{index:02d}.docx", context=context)
        documents[doc_id] = _describe(rendered)
    feature = render_docx(pdf_feature_document(), out / "99_feature.docx")
    documents[feature.doc_id] = _describe(feature)
    return {
        "description": "Phase E DOCX renderer regression fixtures: seven prose types "
                       "plus a hand-built document with colspan/rowspan, a page break, "
                       "a footnote and a signature",
        "config": PILOT,
        "profile": {"page_size": "A4", "margins_mm": [25.4, 25.4, 25.4, 25.4],
                    "font_family": "Calibri", "table_style": "Table Grid"},
        "versions": {"python-docx": _version("python-docx"),
                     "docling-slim": _version("docling-slim"),
                     "docling-core": _version("docling-core"),
                     "lxml": _version("lxml")},
        "command": "py -3.13 synthetic_data/tests/v3_docx_fixtures.py",
        "hash": "sha256(rendered bytes)",
        "documents": documents,
    }


def _describe(rendered) -> dict:
    canonical = canonicalize(rendered.document)
    spans = rendered.spans
    return {
        "doc_type": rendered.doc_type,
        "sha256": rendered.result.sha256,
        "bytes": rendered.path.stat().st_size,
        "spans": len(spans),
        "sentences": len(canonical.sentences),
        "facts": len(rendered.result.fact_surfaces),
        "sections": sum(1 for span in spans if span.kind == "section"),
        "paragraphs": sum(1 for span in spans if span.kind == "block"),
        "tables": sum(1 for span in spans if span.kind == "table"),
        "rows": sum(1 for span in spans if span.kind == "row"),
        "cells": sum(1 for span in spans if span.kind == "cell"),
        "pageless": all(span.loc.page is None for span in spans),
    }


def main() -> int:
    payload = build()
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {len(payload['documents'])} DOCX fixtures to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
