"""Recorded QA fixture: the digest of one generated set (Phase H).

    py -3.13 synthetic_data/tests/v3_qa_fixtures.py            # print digests
    py -3.13 synthetic_data/tests/v3_qa_fixtures.py --record   # rewrite the fixture

The fixture pins the generated question set for one document-format so a change in
the generator shows up as a diff rather than as a silent shift in the benchmark.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from synthetic_data.qa.pilot import build_one

FIXTURE = Path(__file__).with_name("v3_qa_fixtures.json")
DOCUMENT = "doc:00000:qr3hgvjuuciha"
FORMAT = "txt"


def digests() -> dict:
    qa = build_one(DOCUMENT, FORMAT)
    payload = json.dumps(qa.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    return {
        "command": "py -3.13 synthetic_data/tests/v3_qa_fixtures.py",
        "document": DOCUMENT,
        "format": FORMAT,
        "questions": len(qa.questions),
        "counts": dict(qa.counts),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "first_questions": [question.question for question in qa.questions[:3]],
    }


if __name__ == "__main__":
    result = digests()
    if "--record" in sys.argv:
        FIXTURE.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
        print(f"recorded {FIXTURE}")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
