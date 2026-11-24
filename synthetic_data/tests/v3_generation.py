"""Shared fixtures for the Phase B generation tests.

The pilot (7 prose + 1 workbook) and the checkpoint (70 prose + 10 workbooks) are
built once per module: generation is deterministic, so a second build is only
asserted where determinism itself is under test.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from synthetic_data.generation import CorpusConfig, build_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_HASHES = Path(__file__).with_name("v3_generation_fixtures.json")
PILOT = dict(seed=7, corpus_name="v3_pilot", documents=7, workbooks=1)
CHECKPOINT = dict(seed=11, corpus_name="v3_checkpoint", documents=70, workbooks=10)


_CACHE: dict[str, object] = {}


def pilot_plan():
    """Build the pilot once per process; the models are frozen, so reuse is safe."""
    if "pilot" not in _CACHE:
        _CACHE["pilot"] = build_corpus(CorpusConfig(**PILOT))
    return _CACHE["pilot"]


def checkpoint_plan():
    if "checkpoint" not in _CACHE:
        _CACHE["checkpoint"] = build_corpus(CorpusConfig(**CHECKPOINT))
    return _CACHE["checkpoint"]


def document_digest(document) -> str:
    payload = document.model_dump_json(exclude_none=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def corpus_digests(plan) -> dict[str, str]:
    return {doc_id: document_digest(document)
            for doc_id, document in sorted(plan.documents.items())}


def fixture_digests() -> dict[str, str]:
    return json.loads(FIXTURE_HASHES.read_text(encoding="utf-8"))["documents"]


def subprocess_corpus_digest(config: CorpusConfig) -> str:
    """Build the same corpus in a fresh interpreter and hash its documents."""
    code = (
        "import hashlib, json\n"
        "from synthetic_data.generation import CorpusConfig, build_corpus\n"
        f"plan = build_corpus(CorpusConfig(seed={config.seed}, "
        f"corpus_name={config.corpus_name!r}, documents={config.documents}, "
        f"workbooks={config.workbooks}))\n"
        "digests = {k: hashlib.sha256(v.model_dump_json().encode()).hexdigest() "
        "for k, v in sorted(plan.documents.items())}\n"
        "print(json.dumps(digests, sort_keys=True))\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=environment, timeout=300)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])
