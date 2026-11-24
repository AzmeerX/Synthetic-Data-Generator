"""Shared fixtures for the Phase D PDF renderer, ingestion and cross-format tests.

* ``pdf_rendered`` — the seven pilot prose documents plus a hand-built feature
  document, rendered once per session with ``DEFAULT_PDF_PROFILE``;
* ``pdf_ingested`` — the same files ingested through the real Docling native PDF
  pipeline into a fresh in-memory store, with alignments and fact evidence;
* ``pdf_pages`` — the independent ``pypdfium2`` page count for each file, which is
  the B1 page-parity gate.

The feature document exists because the pilot does not use every layout path:
colspan/rowspan, an explicit page break, an over-long paragraph, an ordered list and
a footnote in one small document.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
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
    ColumnSpec,
    DocumentMetadata,
    EntityRef,
    FactStatement,
    Footnote,
    HeaderFooter,
    InlineRun,
    IsoDate,
    ListItem,
    Section,
    SignatureRecord,
    Table,
    TableCell,
    TableRow,
)
from synthetic_data.renderers import (DEFAULT_PDF_PROFILE, RenderContext, PdfRenderer)

from synthetic_data.tests.v3_txt import PILOT, pilot_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_HASHES = Path(__file__).with_name("v3_pdf_fixtures.json")

PROSE_TYPES = {"case_notes", "due_diligence", "financial_agreement", "memorandum",
               "board_minutes", "press_release", "correspondence"}


@dataclass(frozen=True)
class PdfRendered:
    doc_id: str
    doc_type: str
    document: CdmDocument
    result: object
    path: Path
    page_count: int          # pypdfium2's count, not the renderer's
    page_texts: tuple[str, ...]
    page_buffers: tuple[str, ...]   # the renderer's own page text buffers

    @property
    def spans(self):
        return self.result.spans

    def text(self) -> str:
        return "\n\f\n".join(self.page_texts)


@dataclass(frozen=True)
class PdfIngested:
    rendered: PdfRendered
    view: DocView
    summary: dict
    alignments: tuple
    evidence: tuple

    @property
    def doc_id(self) -> str:
        return self.rendered.doc_id


def pdf_pages(path: Path) -> int:
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(path)) as document:
        return len(document)


def page_texts(path: Path) -> tuple[str, ...]:
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(path)) as document:
        texts = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range())
            textpage.close()
            page.close()
        return tuple(texts)


def render_pdf(document: CdmDocument, path: Path,
               context: RenderContext | None = None) -> PdfRendered:
    canonical = canonicalize(document)
    document = apply_statement_sentences(document, canonical)
    result, layout = PdfRenderer().render_with_layout(document, DEFAULT_PDF_PROFILE,
                                                     path, context=context)
    target = Path(result.path)
    return PdfRendered(doc_id=document.document_id, doc_type=document.doc_type,
                       document=document, result=result, path=target,
                       page_count=pdf_pages(target), page_texts=page_texts(target),
                       page_buffers=tuple(page.text for page in layout.pages))


@pytest.fixture(scope="session")
def pdf_rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp("pdf_render")
    plan = pilot_plan()
    context = RenderContext.from_world(plan.world)
    documents = []
    for index, doc_id in enumerate(plan.prose_ids):
        document = plan.documents[doc_id]
        documents.append(render_pdf(document, out / f"{index:02d}_{document.doc_type}.pdf",
                                    context=context))
    documents.append(render_pdf(pdf_feature_document(), out / "99_feature.pdf"))
    return documents


@pytest.fixture(scope="session")
def pdf_store():
    instance = make_test_store()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture(scope="session")
def pdf_config():
    return load_config()


@pytest.fixture(scope="session")
def pdf_ingested(pdf_rendered, pdf_store, pdf_config):
    ingested: dict[str, PdfIngested] = {}
    for rendered in pdf_rendered:
        records = ingest_path(pdf_store, pdf_config, rendered.path)
        assert records, f"no ingest record for {rendered.doc_id}"
        summary = records[0]
        with pdf_store.reader() as reader:
            view = DocView.load(reader, summary["doc_id"], pdf_config)
        canonical = canonicalize(rendered.document)
        alignments = align_document(canonical, view)
        evidence = align_facts(rendered.document, canonical, alignments,
                               rendered.result.fact_surfaces, view.text)
        ingested[rendered.doc_id] = PdfIngested(rendered=rendered, view=view,
                                                summary=summary,
                                                alignments=tuple(alignments),
                                                evidence=tuple(evidence))
    return ingested


# ---------------------------------------------------------------------------
def _cell(cell_id: str, text: str | None, *, colspan: int = 1, rowspan: int = 1,
          fact_ref: str | None = None) -> TableCell:
    return TableCell(cell_id=cell_id, text=text, display=text or "",
                     colspan=colspan, rowspan=rowspan, fact_ref=fact_ref)


def pdf_feature_document() -> CdmDocument:
    """A small document that exercises spans, breaks, notes and signatures."""
    table = Table(
        table_id="tbl:91001/01.t01", caption="Facility summary",
        columns=[ColumnSpec(name="Tranche"), ColumnSpec(name="Lender"),
                 ColumnSpec(name="Amount")],
        header_rows=[TableRow(row_id="tbl:91001/01.t01/r00",
                              cells=[_cell("tbl:91001/01.t01/r00c00", "Tranche details",
                                           colspan=3)])],
        body_rows=[
            TableRow(row_id="tbl:91001/01.t01/r01",
                     cells=[_cell("tbl:91001/01.t01/r01c00", "Tranche A"),
                            _cell("tbl:91001/01.t01/r01c01", "Caldervale Bank Limited"),
                            _cell("tbl:91001/01.t01/r01c02", "GBP 4,000,000.00",
                                  rowspan=2, fact_ref="fact:edge:91001:01")]),
            TableRow(row_id="tbl:91001/01.t01/r02",
                     cells=[_cell("tbl:91001/01.t01/r02c00", "Tranche B"),
                            _cell("tbl:91001/01.t01/r02c01", "Harrow & Finch LLP"),
                            _cell("tbl:91001/01.t01/r02c02", None)]),
        ],
        total_rows=[TableRow(row_id="tbl:91001/01.t01/r03",
                             cells=[_cell("tbl:91001/01.t01/r03c00", "Total", colspan=2),
                                    _cell("tbl:91001/01.t01/r03c02", "GBP 5,000,000.00")])],
        cell_refs={"r01c02": "fact:edge:91001:01"},
    )
    section_one = Section(
        section_id="sec:91001/01", depth=0, number="1", heading="The Facility",
        blocks=[
            Block(block_id="blk:91001/01.01", kind="paragraph", footnote_refs=["fn:91001/01"],
                  runs=[InlineRun(text="The Lender makes available a term facility to the "
                                        "Borrower on the terms set out in this agreement. "
                                        "The facility is available in two tranches. "
                                        "Amounts repaid may not be re-borrowed.")]),
            Block(block_id="blk:91001/01.02", kind="list", ordered=True,
                  items=[ListItem(item_id="blk:91001/01.02/i01",
                                  runs=[InlineRun(text="a term loan tranche;")]),
                         ListItem(item_id="blk:91001/01.02/i02",
                                  runs=[InlineRun(text="a revolving credit tranche.")])]),
            Block(block_id="blk:91001/01.03", kind="table", table=table),
        ])
    section_two = Section(
        section_id="sec:91001/02", depth=0, number="2", heading="Execution",
        blocks=[
            Block(block_id="blk:91001/02.01", kind="page_break"),
            Block(block_id="blk:91001/02.02", kind="paragraph",
                  runs=[InlineRun(text="The Lender advanced GBP 4,000,000 to the Borrower "
                                       "on 3 March 2024.", fact_ref="fact:edge:91001:01")]),
            Block(block_id="blk:91001/02.03", kind="signature",
                  runs=[InlineRun(text="Signed for and on behalf of Amina Kerr, director.")]),
        ])
    return CdmDocument(
        document_id="doc:91001", doc_type="financial_agreement",
        structure_id="fixture:pdf-feature",
        metadata=DocumentMetadata(title="Facility Agreement",
                                  subtitle="Tranche A and revolving facility",
                                  reference="FA-91001",
                                  document_date=IsoDate(value=dt.date(2024, 3, 3)),
                                  jurisdiction="England and Wales",
                                  classification="Confidential", version="1.0"),
        sections=[section_one, section_two],
        statements=[
            FactStatement(statement_id="stmt:91001/01.a", fact_id="fact:edge:91001:01",
                          node_id="blk:91001/02.02",
                          surface="The Lender advanced GBP 4,000,000 to the Borrower on "
                                  "3 March 2024.",
                          value_slots={"amount": "GBP 4,000,000"}),
            FactStatement(statement_id="stmt:91001/01.b", fact_id="fact:edge:91001:01",
                          node_id="tbl:91001/01.t01",
                          surface="GBP 4,000,000.00",
                          value_slots={"amount": "GBP 4,000,000.00"}),
        ],
        headers=[HeaderFooter(header_footer_id="hf:91001/01", position="header",
                              runs=[InlineRun(text="Facility Agreement — Confidential")])],
        signatures=[SignatureRecord(signature_id="sig:91001/01",
                                    signatory=EntityRef(entity_id="ent:person:fixture-pdf"),
                                    capacity="director",
                                    signed_on=IsoDate(value=dt.date(2024, 3, 3)))],
        footnotes=[Footnote(footnote_id="fn:91001/01", marker="1",
                            runs=[InlineRun(text="Clause references are to clauses of this "
                                                 "agreement.")])],
        plan_fingerprint="fixture:pdf-feature",
    )


# ---------------------------------------------------------------------------
def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_payload() -> dict:
    return json.loads(FIXTURE_HASHES.read_text(encoding="utf-8"))


_SUBPROCESS = """
import hashlib, json, tempfile
from pathlib import Path
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.renderers import DEFAULT_PDF_PROFILE, PdfRenderer
from synthetic_data.tests.v3_txt import pilot_plan

plan = pilot_plan()
out = Path(tempfile.mkdtemp())
digests = {}
for index, doc_id in enumerate(plan.prose_ids):
    canonical = canonicalize(plan.documents[doc_id])
    document = apply_statement_sentences(plan.documents[doc_id], canonical)
    result = PdfRenderer().render(document, DEFAULT_PDF_PROFILE,
                                  out / f"{index:02d}.pdf")
    digests[doc_id] = result.sha256
print(json.dumps(digests, sort_keys=True))
"""


def subprocess_digests(hashseed: str = "3") -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = hashseed
    completed = subprocess.run([sys.executable, "-c", _SUBPROCESS], capture_output=True,
                               text=True, env=environment, timeout=600)
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def subprocess_pages() -> dict[str, int]:
    code = """
import json, tempfile
from pathlib import Path
import pypdfium2 as pdfium
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.renderers import DEFAULT_PDF_PROFILE, PdfRenderer
from synthetic_data.tests.v3_txt import pilot_plan

plan = pilot_plan()
out = Path(tempfile.mkdtemp())
pages = {}
for index, doc_id in enumerate(plan.prose_ids):
    canonical = canonicalize(plan.documents[doc_id])
    document = apply_statement_sentences(plan.documents[doc_id], canonical)
    result = PdfRenderer().render(document, DEFAULT_PDF_PROFILE,
                                  out / f"{index:02d}.pdf")
    pages[doc_id] = len(pdfium.PdfDocument(str(result.path)))
print(json.dumps(pages, sort_keys=True))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=environment, timeout=600)
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])
