"""Regenerate the Phase C TXT regression fixtures.

Deliberately **not** a pytest test and never invoked by the suite: a test that
rewrites its own expected values proves nothing.  Run it only when a renderer or
layout change is intended and reviewed:

    py -3.13 synthetic_data/tests/v3_txt_fixtures.py

It writes ``v3_txt_fixtures.json`` next to this file with a sha256 of the rendered
bytes for every pilot document plus the hand-built feature document, along with
the line/page/span/sentence counts that make a silent layout drift visible.

``test_v3_txt_fixtures.py`` compares against those hashes and fails loudly.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from synthetic_data.canonical import canonicalize  # noqa: E402
from synthetic_data.renderers import DEFAULT_TXT_PROFILE  # noqa: E402
from synthetic_data.tests.v3_txt import (PILOT, feature_document,  # noqa: E402
                                         pilot_plan, render_document)

TARGET = Path(__file__).with_name("v3_txt_fixtures.json")


def build_entries() -> dict[str, dict]:
    config = DEFAULT_TXT_PROFILE
    descriptions = {
        "seed": PILOT["seed"], "corpus_name": PILOT["corpus_name"],
        "documents": PILOT["documents"], "workbooks": PILOT["workbooks"],
        "page_width_chars": config.page_width_chars,
        "table_style": config.table_style,
        "include_headers": config.include_headers,
        "include_footers": config.include_footers,
    }
    plan = pilot_plan()
    out = Path(tempfile.mkdtemp())
    documents: dict[str, dict] = {}
    for index, doc_id in enumerate(plan.prose_ids):
        document = plan.documents[doc_id]
        rendered = render_document(document, out / f"{index:02d}.txt")
        documents[doc_id] = _describe(rendered, document)
    feature = render_document(feature_document(), out / "feature.txt")
    documents[feature.doc_id] = _describe(feature, feature.document)
    payload = {
        "description": "Phase C TXT renderer regression fixtures: all seven prose "
                       "types plus a hand-built document covering page breaks, "
                       "footnotes, ordered lists, exhibits and blank cells",
        "config": descriptions,
        "profile": descriptions,
        "command": "py -3.13 synthetic_data/tests/v3_txt_fixtures.py",
        "hash": "sha256(rendered bytes)",
        "documents": documents,
    }
    return payload


def _describe(rendered, document) -> dict:
    canonical = canonicalize(rendered.document)
    return {
        "doc_type": rendered.doc_type,
        "sha256": rendered.result.sha256,
        "bytes": len(rendered.text.encode("utf-8")),
        "lines": len(rendered.lines),
        "pages": rendered.pages,
        "spans": len(rendered.result.spans),
        "sentences": len(canonical.sentences),
        "facts": len(rendered.result.fact_surfaces),
        "block_count": len(document.node_ids()),
    }


def main() -> int:
    payload = build_entries()
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {len(payload['documents'])} document hashes to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
