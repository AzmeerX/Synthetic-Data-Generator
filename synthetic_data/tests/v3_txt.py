"""Shared fixtures for the Phase C TXT renderer and ingestion tests.

Two fixture sets:

* the **pilot** (seed 7, seven prose documents, one workbook) — real Phase B
  content covering all seven prose types, footnotes, exhibits, ordered lists,
  tables, signatures, a correspondence thread and cross-document facts;
* a **feature document** built directly from the Phase A model, so that the
  renderer paths the pilot happens not to use — an explicit ``page_break`` block,
  a blank table cell, an over-long sentence — are covered by construction rather
  than by hoping the planner emits them.

Entry points:

* :func:`render_pilot` / :func:`render_feature` render with ``DEFAULT_TXT_PROFILE``;
* :func:`subprocess_digests` re-renders the pilot in a fresh interpreter so
  byte-identity across processes is asserted, not assumed.
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
from synthetic_data.generation import CorpusConfig, build_corpus
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
    MessageMeta,
    Section,
    SignatureRecord,
    Table,
    TableCell,
    TableRow,
)
from synthetic_data.renderers import DEFAULT_TXT_PROFILE, TxtRenderer

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_HASHES = Path(__file__).with_name("v3_txt_fixtures.json")
PILOT = dict(seed=7, corpus_name="v3_pilot", documents=7, workbooks=1)

_CACHE: dict[str, object] = {}


@dataclass(frozen=True)
class Rendered:
    doc_id: str
    doc_type: str
    document: CdmDocument
    result: object
    text: str
    path: Path

    @property
    def pages(self) -> int:
        return len(self.text.split("\f"))

    @property
    def lines(self) -> list[str]:
        """Every line in reading order; page breaks are not line breaks."""
        return [line for page in self.text.split("\f") for line in page.split("\n")]

    @property
    def blank_share(self) -> float:
        lines = self.lines
        return sum(1 for line in lines if not line.strip()) / max(len(lines), 1)


@dataclass(frozen=True)
class Ingested:
    rendered: Rendered
    view: DocView
    summary: dict
    alignments: tuple
    evidence: tuple

    @property
    def doc_id(self) -> str:
        return self.rendered.doc_id


def pilot_plan():
    if "pilot" not in _CACHE:
        _CACHE["pilot"] = build_corpus(CorpusConfig(**PILOT))
    return _CACHE["pilot"]


def _with_sentences(document: CdmDocument):
    canonical = canonicalize(document)
    return canonical, apply_statement_sentences(document, canonical)


def render_document(document: CdmDocument, path: Path) -> Rendered:
    canonical, document = _with_sentences(document)
    result = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, path)
    return Rendered(doc_id=document.document_id, doc_type=document.doc_type,
                    document=document, result=result,
                    text=Path(result.path).read_text(encoding="utf-8"),
                    path=Path(result.path))


def render_pilot(out_dir: Path) -> list[Rendered]:
    plan = pilot_plan()
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = [render_document(plan.documents[doc_id],
                                out_dir / f"{index:02d}_{plan.documents[doc_id].doc_type}.txt")
                for index, doc_id in enumerate(plan.prose_ids)]
    _CACHE["pilot_rendered"] = rendered
    return rendered


def render_feature(out_dir: Path) -> Rendered:
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_document(feature_document(), out_dir / "99_feature.txt")
    _CACHE["feature_rendered"] = rendered
    return rendered


# ---------------------------------------------------------------------------
# a hand-built document for the renderer paths the pilot does not exercise
# ---------------------------------------------------------------------------
_LONG = ("The Borrower irrevocably and unconditionally undertakes to the Lender, "
         "on the first Business Day of each calendar month during the Availability "
         "Period, to deliver a compliance certificate substantially in the form set "
         "out in Schedule 4 together with such supporting calculations as the Lender "
         "may reasonably require in order to confirm that the financial covenants "
         "set out in Clause 22 have been observed throughout the preceding period.")


def _run(text: str, fact_ref: str | None = None) -> InlineRun:
    return InlineRun(text=text, fact_ref=fact_ref)


def _cell(cell_id: str, text: str | None, fact_ref: str | None = None) -> TableCell:
    return TableCell(cell_id=cell_id, text=text, display=text or "", fact_ref=fact_ref)


def feature_document() -> CdmDocument:
    """A small document that exercises page breaks, notes, lists and blank cells."""
    table_id = "tbl:90001/02.t01"
    rows = [
        TableRow(row_id="tbl:90001/02.t01/r00",
                 cells=[_cell("tbl:90001/02.t01/r00c00", "Obligation"),
                        _cell("tbl:90001/02.t01/r00c01", "Amount")]),
        TableRow(row_id="tbl:90001/02.t01/r01",
                 cells=[_cell("tbl:90001/02.t01/r01c00", "Compliance certificate"),
                        _cell("tbl:90001/02.t01/r01c01", "GBP 4,000,000",
                              fact_ref="fact:edge:90001:01")]),
        TableRow(row_id="tbl:90001/02.t01/r02",
                 cells=[_cell("tbl:90001/02.t01/r02c00", "Undertaking"),
                        _cell("tbl:90001/02.t01/r02c01", None)]),
        TableRow(row_id="tbl:90001/02.t01/r03",
                 cells=[_cell("tbl:90001/02.t01/r03c00", "Total"),
                        _cell("tbl:90001/02.t01/r03c01", "GBP 4,000,000")]),
    ]
    table = Table(table_id=table_id, caption="Schedule 3 — Undertakings",
                  columns=[ColumnSpec(name="Obligation"), ColumnSpec(name="Amount")],
                  header_rows=[rows[0]], body_rows=[rows[1], rows[2]],
                  total_rows=[rows[3]],
                  cell_refs={"r01c01": "fact:edge:90001:01"})

    section_one = Section(
        section_id="sec:90001/01", depth=0, number="1",
        heading="Scope and Construction",
        blocks=[
            Block(block_id="blk:90001/01.01", kind="paragraph",
                  footnote_refs=["fn:90001/01"],
                  runs=[_run("This agreement is governed by the laws of England and "
                             "Wales. The parties submit to the exclusive jurisdiction "
                             "of the courts of England and Wales. Clause references are "
                             "to clauses of this agreement.")]),
            Block(block_id="blk:90001/01.02", kind="list", ordered=True,
                  items=[ListItem(item_id="blk:90001/01.02/i01",
                                  runs=[_run("the Outstanding Amount on the relevant "
                                             "Utilisation Date;")]),
                         ListItem(item_id="blk:90001/01.02/i02",
                                  runs=[_run("each Compliance Certificate;")]),
                         ListItem(item_id="blk:90001/01.02/i03",
                                  runs=[_run("any other document the Lender reasonably "
                                             "requires.")])]),
            Block(block_id="blk:90001/01.03", kind="paragraph", runs=[_run(_LONG)]),
        ])
    section_two = Section(
        section_id="sec:90001/02", depth=0, number="2", heading="Obligations",
        blocks=[
            Block(block_id="blk:90001/02.01", kind="table", table=table),
            Block(block_id="blk:90001/02.02", kind="page_break"),
            Block(block_id="blk:90001/02.03", kind="paragraph",
                  runs=[_run("The Lender advanced GBP 4,000,000 to the Borrower on "
                             "3 March 2024.",
                             fact_ref="fact:edge:90001:01")]),
        ])
    subsection = Section(
        section_id="sec:90001/02.1", parent_id="sec:90001/02", depth=1, number="2.1",
        heading="Financial Covenants",
        blocks=[Block(block_id="blk:90001/02.1.01", kind="paragraph",
                      runs=[_run("The Borrower shall ensure that group revenue for each "
                                 "Financial Year is not less than the amount stated in "
                                 "Schedule 4.")])])
    section_three = Section(
        section_id="sec:90001/03", depth=0, number="3", heading="Execution",
        blocks=[
            Block(block_id="blk:90001/03.01", kind="exhibit",
                  runs=[_run("Exhibit A: Schedule of property and assets.")]),
            Block(block_id="blk:90001/03.02", kind="signature",
                  runs=[_run("Signed for and on behalf of Amina Kerr, director.")]),
            Block(block_id="blk:90001/03.03", kind="note",
                  runs=[_run("Executed as a deed and delivered on the date first "
                             "written above.")]),
        ])

    statements = [
        FactStatement(statement_id="stmt:90001/02.a", fact_id="fact:edge:90001:01",
                      node_id="blk:90001/02.03",
                      surface="The Lender advanced GBP 4,000,000 to the Borrower on "
                              "3 March 2024.",
                      value_slots={"amount": "GBP 4,000,000"}),
        FactStatement(statement_id="stmt:90001/02.b", fact_id="fact:edge:90001:01",
                      node_id=table_id,
                      surface="GBP 4,000,000",
                      value_slots={"amount": "GBP 4,000,000"}),
    ]
    return CdmDocument(
        document_id="doc:90001", doc_type="financial_agreement",
        structure_id="fixture:feature",
        metadata=DocumentMetadata(
            title="Facility Agreement", subtitle="Between the Lender and the Borrower",
            reference="FA-90001", document_date=IsoDate(value=dt.date(2024, 3, 3)),
            jurisdiction="England and Wales",
            classification="Confidential", version="1.0",
            thread=MessageMeta(thread_id="thr:fixture:0001", message_id="msg:fixture:01",
                               sequence=1)),
        sections=[section_one, section_two, subsection, section_three],
        statements=statements,
        headers=[HeaderFooter(header_footer_id="hf:90001/01", position="header",
                              runs=[_run("Fixture Facility — Confidential")])],
        footers=[HeaderFooter(header_footer_id="hf:90001/02", position="footer",
                              runs=[_run("FA-90001 — execution copy")])],
        exhibits=[],
        signatures=[SignatureRecord(signature_id="sig:90001/01",
                                    signatory=EntityRef(entity_id="ent:person:fixture01"),
                                    capacity="director",
                                    signed_on=IsoDate(value=dt.date(2024, 3, 3)))],
        footnotes=[Footnote(footnote_id="fn:90001/01", marker="1",
                            runs=[_run("Clause references are to clauses of this "
                                       "agreement as amended from time to time.")])],
        plan_fingerprint="fixture:feature",
    )


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def txt_rendered(tmp_path_factory):
    """The pilot plus the feature document, rendered once per test session."""
    out = tmp_path_factory.mktemp("txt_render")
    documents = render_pilot(out)
    documents.append(render_feature(out))
    return documents


@pytest.fixture(scope="session")
def txt_store():
    instance = make_test_store()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture(scope="session")
def txt_config():
    return load_config()


@pytest.fixture(scope="session")
def txt_ingested(txt_rendered, txt_store, txt_config):
    """Render → ingest through ``u01_parser.ingest_path`` → load a DocView."""
    ingested: dict[str, Ingested] = {}
    for rendered in txt_rendered:
        records = ingest_path(txt_store, txt_config, rendered.path)
        assert records, f"no ingest record for {rendered.doc_id}"
        summary = records[0]
        with txt_store.reader() as reader:
            view = DocView.load(reader, summary["doc_id"], txt_config)
        canonical = canonicalize(rendered.document)
        alignments = align_document(canonical, view)
        evidence = align_facts(rendered.document, canonical, alignments,
                               rendered.result.fact_surfaces, view.text)
        ingested[rendered.doc_id] = Ingested(
            rendered=rendered, view=view, summary=summary,
            alignments=tuple(alignments), evidence=tuple(evidence))
    return ingested


# ---------------------------------------------------------------------------
def digest_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_payload() -> dict[str, str]:
    return json.loads(FIXTURE_HASHES.read_text(encoding="utf-8"))["documents"]


_RENDER_SCRIPT = """
import hashlib, json, tempfile
from pathlib import Path
from synthetic_data.canonical import apply_statement_sentences, canonicalize
from synthetic_data.generation import CorpusConfig, build_corpus
from synthetic_data.renderers import DEFAULT_TXT_PROFILE, TxtRenderer

plan = build_corpus(CorpusConfig(seed={seed}, corpus_name={corpus_name!r},
                                 documents={documents}, workbooks={workbooks}))
out = Path(tempfile.mkdtemp())
digests = {{}}
for index, doc_id in enumerate(plan.prose_ids):
    canonical = canonicalize(plan.documents[doc_id])
    document = apply_statement_sentences(plan.documents[doc_id], canonical)
    result = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, out / f"{{index:02d}}.txt")
    digests[doc_id] = hashlib.sha256(Path(result.path).read_bytes()).hexdigest()
print(json.dumps(digests, sort_keys=True))
"""


def _subprocess_digests(hashseed: str = "1") -> dict[str, str]:
    """Render the pilot in a fresh interpreter and return ``{doc_id: sha256}``."""
    code = _RENDER_SCRIPT.format(**PILOT)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = hashseed
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=environment, timeout=300)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def subprocess_digests() -> dict[str, str]:
    return _subprocess_digests("1")


def subprocess_digests_with_seed(seed: int) -> dict[str, str]:
    """Same render with a different ``PYTHONHASHSEED`` (order-independence check)."""
    return _subprocess_digests(str(seed))
