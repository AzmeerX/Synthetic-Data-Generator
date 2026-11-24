"""Shared fixtures for the Phase G gold tests.

Rendering and ingestion happen once per session and are cached, because a gold
build is only meaningful on real parser output: every test here uses ``ingest_path``
and a ``DocView``, never a hand-written parser sentence.

``golded`` helpers::

    golded = gold_document(document, "txt", out, world=plan.world)
    pair   = golded.pair                # v1 + v2 + alignment
    files  = write_gold_pair(...)       # three files, atomically

``mutated`` helpers re-ingest a *changed* rendering of the same canonical
document, which is how the defect tests prove that a wrong parser sees a wrong
corpus rather than a tolerated one.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from synth.config import load_config
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.gold import GoldInputs, GoldPair, build_gold
from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.renderers import (
    DEFAULT_DOCX_PROFILE,
    DEFAULT_PDF_PROFILE,
    DEFAULT_TXT_PROFILE,
    DEFAULT_XLSX_PROFILE,
    DocxRenderer,
    PdfRenderer,
    TxtRenderer,
    XlsxRenderer,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT = dict(seed=7, corpus_name="v3_pilot", documents=7, workbooks=1)

RENDERERS = {
    "txt": (TxtRenderer, DEFAULT_TXT_PROFILE, ".txt"),
    "pdf": (PdfRenderer, DEFAULT_PDF_PROFILE, ".pdf"),
    "docx": (DocxRenderer, DEFAULT_DOCX_PROFILE, ".docx"),
    "xlsx": (XlsxRenderer, DEFAULT_XLSX_PROFILE, ".xlsx"),
}

_CACHE: dict[str, object] = {}
_STORE = None


def session_store():
    """One store for the whole session: gold validation resolves the parsed doc."""
    global _STORE
    if _STORE is None:
        _STORE = make_test_store()
    return _STORE


@dataclass(frozen=True)
class Golded:
    document: object
    canonical: object
    render: object
    view: DocView
    pair: GoldPair
    parse_doc_id: str
    format: str
    path: Path

    @property
    def document_id(self) -> str:
        return self.document.document_id

    @property
    def row_quotes(self) -> dict[str, str]:
        alignment = self.pair.workbook_alignment
        if alignment is None:
            return {}
        quotes = {row.row_id: row.quote for row in alignment.rows}
        quotes.update({cell.cell_id: cell.row_quote for cell in alignment.cells})
        return quotes


def pilot_plan():
    if "pilot" not in _CACHE:
        _CACHE["pilot"] = build_corpus(CorpusConfig(**PILOT))
    return _CACHE["pilot"]


def gold_document(document, fmt: str, out: Path, *, world=None,
                  filename: str | None = None) -> Golded:
    """Render one document, ingest it, and build its gold pair."""
    key = f"{document.document_id}:{fmt}"
    if key in _CACHE:
        return _CACHE[key]
    renderer_class, profile, suffix = RENDERERS[fmt]
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{document.document_id.replace(':', '_')}{suffix}"

    canonical = None
    if document.doc_kind == "prose":
        canonical = canonicalize(document)
        document = apply_statement_sentences(document, canonical)

    render = renderer_class().render(document, profile, path)
    store = session_store()
    records = ingest_path(store, load_config(), render.path)
    assert records, "no ingest record"
    parse_doc_id = records[0]["doc_id"]
    with store.reader() as reader:
        view = DocView.load(reader, parse_doc_id, load_config())
    pair = build_gold(GoldInputs(
        document=document, canonical=canonical, render=render, view=view,
        parse_doc_id=parse_doc_id, filename=filename or path.name,
        world=world if world is not None else pilot_plan().world,
        seed=PILOT["seed"]))
    golded = Golded(document=document, canonical=canonical, render=render, view=view,
                    pair=pair, parse_doc_id=parse_doc_id, format=fmt, path=Path(render.path))
    _CACHE[key] = golded
    return golded


def ingest_text(path: Path) -> tuple[DocView, str]:
    """Ingest a text file that a test has deliberately changed."""
    store = make_test_store()
    try:
        records = ingest_path(store, load_config(), path)
        assert records, "no ingest record"
        with store.reader() as reader:
            view = DocView.load(reader, records[0]["doc_id"], load_config())
        return view, records[0]["doc_id"]
    finally:
        store.dispose()


def mutate_text(golded: Golded, transform, out: Path, name: str = "mutated.txt"
                ) -> tuple[Path, DocView, str]:
    """Write ``transform(text)`` and ingest it — a parser that saw a different file."""
    text = Path(golded.path).read_text(encoding="utf-8")
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(transform(text), encoding="utf-8")
    view, doc_id = ingest_text(path)
    return path, view, doc_id


# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def gold_store():
    yield session_store()


@pytest.fixture(scope="session")
def gold_config():
    return load_config()


@pytest.fixture(scope="session")
def gold_txt(tmp_path_factory):
    """One TXT case-notes document with real tables, notes and facts."""
    plan = pilot_plan()
    document = plan.documents[plan.prose_ids[2]]
    return gold_document(document, "txt", tmp_path_factory.mktemp("gold_txt"))


@pytest.fixture(scope="session")
def gold_pdf(tmp_path_factory):
    plan = pilot_plan()
    document = plan.documents[plan.prose_ids[2]]
    return gold_document(document, "pdf", tmp_path_factory.mktemp("gold_pdf"))


@pytest.fixture(scope="session")
def gold_docx(tmp_path_factory):
    plan = pilot_plan()
    document = plan.documents[plan.prose_ids[2]]
    return gold_document(document, "docx", tmp_path_factory.mktemp("gold_docx"))


@pytest.fixture(scope="session")
def gold_workbook(tmp_path_factory):
    plan = pilot_plan()
    document = plan.documents[plan.workbook_ids[0]]
    return gold_document(document, "xlsx", tmp_path_factory.mktemp("gold_wb"))


@pytest.fixture(scope="session")
def gold_pilot(tmp_path_factory):
    """All seven prose types in all three prose formats, plus the workbook."""
    plan = pilot_plan()
    out = tmp_path_factory.mktemp("gold_pilot")
    built: list[Golded] = []
    for doc_id in plan.prose_ids:
        for fmt in ("txt", "pdf", "docx"):
            built.append(gold_document(plan.documents[doc_id], fmt, out / fmt))
    built.append(gold_document(plan.documents[plan.workbook_ids[0]], "xlsx",
                               out / "xlsx"))
    return built
