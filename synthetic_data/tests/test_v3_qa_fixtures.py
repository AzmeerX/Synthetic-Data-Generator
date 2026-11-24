"""Byte determinism of QA generation across separate processes (Phase H).

The recorded fixture is a digest of one generated set.  Two fresh interpreters must
produce it, and it must match the recorded value — so a generator change shows up as
a fixture diff instead of silently moving the benchmark.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("v3_qa_fixtures.json")
SCRIPT = Path(__file__).with_name("v3_qa_fixtures.py")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run() -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                               text=True, env=environment, cwd=str(REPO_ROOT),
                               timeout=600)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def recorded() -> dict:
    if not FIXTURE.exists():
        pytest.skip("no recorded QA fixture")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_two_processes_produce_the_same_bytes(recorded):
    first = _run()
    second = _run()
    assert first["sha256"] == second["sha256"]


def test_the_recorded_digest_is_reproduced(recorded):
    current = _run()
    assert current["sha256"] == recorded["sha256"], (
        "the QA generator changed; re-record with "
        "py -3.13 synthetic_data/tests/v3_qa_fixtures.py --record")
    assert current["questions"] == recorded["questions"]
    assert current["counts"] == recorded["counts"]


def test_the_fixture_is_a_real_question_set(recorded):
    assert recorded["questions"] >= 10
    assert recorded["counts"]
    assert all(question.endswith("?") for question in recorded["first_questions"])
