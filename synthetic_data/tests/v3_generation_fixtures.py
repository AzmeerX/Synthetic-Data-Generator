"""Regenerate the canonical V3 Phase B regression fixtures.

This script is deliberately **not** a pytest test and is never invoked by the test
suite: a test that rewrites its own expected values proves nothing.  Run it only
when a planner change is intended and reviewed:

    py -3.13 synthetic_data/tests/v3_generation_fixtures.py

It writes ``v3_generation_fixtures.json`` next to this file with a sha256 per
document, the configuration that produced them, and the generation command.  The
test module ``test_v3_generation_determinism.py`` compares against those hashes
and fails loudly on drift.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from synthetic_data.generation import CorpusConfig, build_corpus  # noqa: E402

PILOT = dict(seed=7, corpus_name="v3_pilot", documents=7, workbooks=1)
TARGET = Path(__file__).with_name("v3_generation_fixtures.json")


def build_digests() -> dict[str, str]:
    plan = build_corpus(CorpusConfig(**PILOT))
    return {doc_id: hashlib.sha256(document.model_dump_json().encode("utf-8")).hexdigest()
            for doc_id, document in sorted(plan.documents.items())}


def main() -> int:
    digests = build_digests()
    payload = {
        "description": "Phase B canonical regression fixtures (one document per "
                       "prose type plus one workbook)",
        "config": PILOT,
        "command": "py -3.13 synthetic_data/tests/v3_generation_fixtures.py",
        "hash": "sha256(model_dump_json())",
        "documents": digests,
    }
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {len(digests)} document hashes to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
