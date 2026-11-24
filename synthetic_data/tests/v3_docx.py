"""Shared fixtures for the DOCX tests (Phase E).

Rendering is done once per session and ingested through the real parser, because
DOCX ingestion is the expensive part and the result is immutable.

``docx_package`` exists because a DOCX assertion must be about *OOXML*, not about
what the renderer believed it wrote: a test that trusts the renderer's own object
model cannot tell a real table from a paragraph of pipes.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from synth.config import load_config
from synth.modules.common import DocView
from synth.modules.u01_parser import ingest_path
from synth.store import test_store as make_test_store

from synthetic_data.alignment import align_document, align_facts
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.model import (
    Block,
    CdmDocument,
    DocumentMetadata,
    FactStatement,
    InlineRun,
    Section,
)
from synthetic_data.renderers import DEFAULT_DOCX_PROFILE, DocxRenderer, RenderContext

from synthetic_data.tests.v3_txt import pilot_plan

PROSE_TYPES = {"case_notes", "due_diligence", "financial_agreement", "memorandum",
               "board_minutes", "press_release", "correspondence"}

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_HASHES = Path(__file__).with_name("v3_docx_fixtures.json")


@dataclass(frozen=True)
class DocxRendered:
    doc_id: str
    doc_type: str
    document: CdmDocument
    result: object
    path: Path

    @property
    def spans(self):
        return self.result.spans

    @property
    def sha256(self) -> str:
        return self.result.sha256

    def package(self) -> dict[str, bytes]:
        return docx_package(self.path)

    def part(self, name: str) -> str:
        return self.package()[name].decode("utf-8")


@dataclass(frozen=True)
class DocxIngested:
    rendered: DocxRendered
    view: DocView
    summary: dict
    alignments: tuple
    evidence: tuple

    @property
    def doc_id(self) -> str:
        return self.rendered.doc_id


def docx_package(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def render_docx(document: CdmDocument, path: Path,
                context: RenderContext | None = None) -> DocxRendered:
    canonical = canonicalize(document)
    document = apply_statement_sentences(document, canonical)
    result = DocxRenderer().render(document, DEFAULT_DOCX_PROFILE, path, context=context)
    return DocxRendered(doc_id=document.document_id, doc_type=document.doc_type,
                        document=document, result=result, path=Path(result.path))


@pytest.fixture(scope="session")
def docx_rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp("docx_render")
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)
    rendered = []
    for index, doc_id in enumerate(plan.prose_ids):
        document = plan.documents[doc_id]
        rendered.append(render_docx(document, out / f"{index:02d}_{document.doc_type}.docx",
                                    context=context))
    return rendered


@pytest.fixture(scope="session")
def docx_by_type(docx_rendered):
    return {item.doc_type: item for item in docx_rendered}


@pytest.fixture(scope="session")
def docx_store():
    instance = make_test_store()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture(scope="session")
def docx_config():
    return load_config()


@pytest.fixture(scope="session")
def docx_ingested(docx_rendered, docx_store, docx_config):
    ingested: dict[str, DocxIngested] = {}
    for rendered in docx_rendered:
        records = ingest_path(docx_store, docx_config, rendered.path)
        assert records, f"no ingest record for {rendered.doc_id}"
        summary = records[0]
        with docx_store.reader() as reader:
            view = DocView.load(reader, summary["doc_id"], docx_config)
        canonical = canonicalize(rendered.document)
        alignments = align_document(canonical, view)
        evidence = align_facts(rendered.document, canonical, alignments,
                               rendered.result.fact_surfaces, view.text)
        ingested[rendered.doc_id] = DocxIngested(rendered=rendered, view=view,
                                                 summary=summary,
                                                 alignments=tuple(alignments),
                                                 evidence=tuple(evidence))
    return ingested


# ---------------------------------------------------------------------------
def label_value_document() -> CdmDocument:
    """A document whose *canonical* sentences are ``Label: value`` (D4).

    The parser splits such a paragraph into two items, so this is the case Phase E
    has to survive: the field must still be located and its value must still be
    quotable, even though no single parser block contains the whole sentence.
    """
    party_block = Block(
        block_id="blk:92001/01.01", kind="paragraph",
        runs=[InlineRun(text="Borrower:", emphasis="bold"),
              InlineRun(text="Meridian Holdings Limited."),
              InlineRun(text="Lender:", emphasis="bold"),
              InlineRun(text="Caldervale Bank Limited.")])
    section = Section(section_id="sec:92001/01", depth=0, number="1", heading="Parties",
                      blocks=[party_block])
    return CdmDocument(
        document_id="doc:92001", doc_type="financial_agreement",
        structure_id="fixture:docx-label-value",
        metadata=DocumentMetadata(title="Facility Agreement", reference="FA-92001",
                                  classification="Confidential"),
        sections=[section],
        statements=[
            FactStatement(statement_id="stmt:92001/01.a", fact_id="fact:edge:92001:01",
                          node_id="blk:92001/01.01",
                          surface="Borrower: Meridian Holdings Limited.",
                          value_slots={"borrower": "Meridian Holdings Limited"}),
            FactStatement(statement_id="stmt:92001/01.b", fact_id="fact:edge:92001:02",
                          node_id="blk:92001/01.01",
                          surface="Lender: Caldervale Bank Limited.",
                          value_slots={"lender": "Caldervale Bank Limited"}),
        ],
        plan_fingerprint="fixture:docx-label-value",
    )


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_payload() -> dict:
    return json.loads(FIXTURE_HASHES.read_text(encoding="utf-8"))


_SUBPROCESS = """
import json, tempfile
from pathlib import Path
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.renderers import DEFAULT_DOCX_PROFILE, DocxRenderer, RenderContext
from synthetic_data.tests.v3_pdf import pdf_feature_document
from synthetic_data.tests.v3_txt import pilot_plan

plan = pilot_plan()
context = RenderContext.from_world(plan.world)
out = Path(tempfile.mkdtemp())
digests = {}
for index, doc_id in enumerate(plan.prose_ids):
    canonical = canonicalize(plan.documents[doc_id])
    document = apply_statement_sentences(plan.documents[doc_id], canonical)
    result = DocxRenderer().render(document, DEFAULT_DOCX_PROFILE,
                                   out / f"{index:02d}.docx", context=context)
    digests[doc_id] = result.sha256
feature = pdf_feature_document()
canonical = canonicalize(feature)
document = apply_statement_sentences(feature, canonical)
digests[feature.document_id] = DocxRenderer().render(
    document, DEFAULT_DOCX_PROFILE, out / "99_feature.docx").sha256
print(json.dumps(digests, sort_keys=True))
"""


def subprocess_digests(hashseed: str = "3") -> dict[str, str]:
    """Render every pilot document in a fresh interpreter (D: not in-process)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = hashseed
    completed = subprocess.run([sys.executable, "-c", _SUBPROCESS], capture_output=True,
                               text=True, env=environment, timeout=600)
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])
