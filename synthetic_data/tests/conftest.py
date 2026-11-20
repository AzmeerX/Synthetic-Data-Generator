"""Test fixtures for the synthetic-data generator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from synth.config import load_config  # noqa: E402
from synth.modules.common import DocView  # noqa: E402
from synth.modules.u01_parser import ingest_path  # noqa: E402
from synth.store import test_store as make_test_store  # noqa: E402

from synthetic_data.generator.pipeline import generate_corpus  # noqa: E402
from synthetic_data.generator.queries import DocumentContext  # noqa: E402
from synthetic_data.generator.world import build_world  # noqa: E402


@pytest.fixture(scope="session")
def world():
    return build_world(7)


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture
def store():
    instance = make_test_store()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture(scope="session")
def pilot(tmp_path_factory):
    out = tmp_path_factory.mktemp("pilot_20")
    return generate_corpus(seed=7, pages=20, out_dir=out)


@pytest.fixture(scope="session")
def pilot_v2(tmp_path_factory):
    out = tmp_path_factory.mktemp("pilot_20_v2")
    return generate_corpus(seed=7, pages=20, out_dir=out, layout="v2")


@pytest.fixture
def ingest_plan(store, config):
    def _ingest(plan, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / plan.filename
        path.write_text(plan.render_text(), encoding="utf-8")
        records = ingest_path(store, config, path)
        assert records, "ingest_path returned no records"
        record = records[0]
        with store.reader() as reader:
            view = DocView.load(reader, record["doc_id"], config)
        context = DocumentContext.from_view(plan, record["doc_id"], view)
        return path, record, view, context

    return _ingest
