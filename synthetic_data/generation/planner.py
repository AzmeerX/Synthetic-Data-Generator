"""Canonical content planning for the seven prose types.

Each planner produces a :class:`CdmDocument`: a metadata block, a section tree,
paragraphs, clauses, lists, tables, notes, exhibits and signatures, plus the
:class:`FactStatement` records that tie the prose to the semantic world.  No
renderer, parser or coordinate appears here.

Documents are pure functions of ``(matter, doc_id, config)``: the section grammar,
the register, the table layout and every sentence are selected from a
document-scoped RNG, so two runs of one configuration produce identical content
and adding a document cannot perturb an earlier one.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass, field

from synthetic_data.model import (
    Block,
    CdmDocument,
    CellFormula,
    ColumnSpec,
    DocumentMetadata,
    Exhibit,
    FactStatement,
    Footnote,
    HeaderFooter,
    InlineRun,
    IsoDate,
    ListItem,
    Money,
    Percentage,
    Section,
    SignatureRecord,
    Table,
    TableCell,
    TableRow,
)

from .config import CorpusConfig
from .grammar import POOLS, paragraphs
from .ids import IdAllocator
from .rng import pick, sample, scope_rng, shuffled
from .surface import (
    RoleMap,
    Sentence,
    Surface,
    date_number_format,
    fmt_date,
    fmt_money,
    fmt_percent,
    money_number_format,
    percent_number_format,
)
from .world import Matter

__all__ = ["DocBuilder", "plan_document", "CellSpec", "STRUCTURES"]

CORPORATE_SUFFIXES = ("Limited", "plc", "LLC", "GmbH", "S.A.", "LLP")


@dataclass
class CellSpec:
    text: str | None = None
    value: object | None = None
    display: str = ""
    fact_ref: str | None = None
    entity_ref: str | None = None
    number_format: str | None = None
    formula: str | None = None
    cached: object | None = None
    colspan: int = 1
    rowspan: int = 1

    def surface(self) -> str:
        return self.display or self.text or ""


@dataclass
class SectionSpec:
    key: str
    heading: tuple[str, ...]
    optional: bool = True


#: 4-5 structural grammars per type.  The grammar decides section order and which
#: optional sections appear; ``plan_fingerprint`` records the realised shape.
STRUCTURES: dict[str, tuple[tuple[SectionSpec, ...], ...]] = {
    "financial_agreement": (
        (
            SectionSpec("recitals", ("Parties and Recitals", "Parties, Recitals and Purpose"), False),
            SectionSpec("definitions", ("Definitions and Interpretation", "Interpretation"), True),
            SectionSpec("facility", ("The Facility", "Facility and Availability", "Availability and Purpose"), False),
            SectionSpec("interest", ("Interest and Fees", "Interest, Fees and Costs"), False),
            SectionSpec("schedules", ("Drawdown and Repayment Schedules", "Drawdowns and Repayment"), False),
            SectionSpec("outstanding", ("Outstanding Balance", "Balance Outstanding"), True),
            SectionSpec("security", ("Guarantees and Security", "Transaction Security and Guarantees"), False),
            SectionSpec("covenants", ("Covenants and Undertakings", "Financial Covenants"), False),
            SectionSpec("representations", ("Representations", "Representations and Warranties"), True),
            SectionSpec("defaults", ("Events of Default and Amendments", "Default, Amendment and Waiver"), False),
            SectionSpec("execution", ("Execution and Notices", "Execution"), False),
        ),
        (
            SectionSpec("recitals", ("Recitals", "Background"), False),
            SectionSpec("facility", ("The Term Facility", "The Facility"), False),
            SectionSpec("conditions", ("Conditions Precedent", "Conditions to Drawdown"), True),
            SectionSpec("interest", ("Interest, Margin and Fees", "Interest"), False),
            SectionSpec("schedules", ("Drawdowns and Repayment", "Repayment Schedule"), False),
            SectionSpec("security", ("Security and Guarantees", "Security Package"), False),
            SectionSpec("covenants", ("Financial Covenants and Undertakings", "Covenants"), False),
            SectionSpec("defaults", ("Events of Default", "Default"), False),
            SectionSpec("execution", ("Execution", "Signature"), False),
        ),
        (
            SectionSpec("recitals", ("Parties and Background", "Recitals"), False),
            SectionSpec("definitions", ("Definitions", "Defined Terms"), False),
            SectionSpec("facility", ("Availability and Purpose", "The Facility"), False),
            SectionSpec("interest", ("Interest and Costs", "Interest"), True),
            SectionSpec("schedules", ("Drawdown and Repayment", "Repayment"), False),
            SectionSpec("outstanding", ("Balance Outstanding", "Outstanding Amount"), True),
            SectionSpec("covenants", ("Covenants", "Financial Covenants"), False),
            SectionSpec("security", ("Guarantees and Security", "Security"), False),
            SectionSpec("representations", ("Representations and Warranties", "Representations"), False),
            SectionSpec("defaults", ("Default and Amendment", "Events of Default"), False),
            SectionSpec("execution", ("Execution and Notices", "Execution"), False),
        ),
        (
            SectionSpec("recitals", ("Recitals and Purpose", "Parties and Recitals"), False),
            SectionSpec("facility", ("The Facility", "Facility"), False),
            SectionSpec("interest", ("Interest and Fees", "Interest"), False),
            SectionSpec("schedules", ("Drawdowns and Repayment", "Repayment Schedule"), True),
            SectionSpec("outstanding", ("Outstanding Balance", "Balance"), False),
            SectionSpec("security", ("Security", "Transaction Security"), True),
            SectionSpec("covenants", ("Undertakings and Covenants", "Covenants"), False),
            SectionSpec("defaults", ("Events of Default and Amendments", "Default"), False),
            SectionSpec("execution", ("Execution", "Execution and Delivery"), False),
        ),
    ),
    "due_diligence": (
        (
            SectionSpec("scope", ("Scope and Sources", "Scope, Sources and Limitations"), False),
            SectionSpec("ownership", ("Group Structure and Ownership", "Ownership Structure"), False),
            SectionSpec("management", ("Directors and Management", "Management and Governance"), True),
            SectionSpec("financial", ("Financial Summary", "Financial Position"), False),
            SectionSpec("facility", ("Facility and Indebtedness", "Indebtedness"), False),
            SectionSpec("security", ("Security and Guarantees", "Security Package"), True),
            SectionSpec("proceedings", ("Legal Proceedings", "Litigation"), False),
            SectionSpec("allegations", ("Allegations and Findings", "Findings on the Allegations"), False),
            SectionSpec("recommendations", ("Findings and Recommendations", "Recommendations"), False),
            SectionSpec("appendix", ("Appendices", "Appendix"), True),
        ),
        (
            SectionSpec("scope", ("Scope and Basis of Review", "Scope"), False),
            SectionSpec("financial", ("Financial Review", "Financial Summary"), False),
            SectionSpec("ownership", ("Ownership and Group Structure", "Shareholding"), False),
            SectionSpec("management", ("Management", "Board and Management"), False),
            SectionSpec("proceedings", ("Proceedings and Disputes", "Litigation"), True),
            SectionSpec("allegations", ("Allegations", "Allegations and Responses"), False),
            SectionSpec("security", ("Guarantees and Security", "Security"), True),
            SectionSpec("recommendations", ("Recommendations", "Conclusions and Recommendations"), False),
            SectionSpec("appendix", ("Appendices", "Schedule of Documents"), True),
        ),
        (
            SectionSpec("scope", ("Scope, Sources and Limitations", "Introduction and Scope"), False),
            SectionSpec("ownership", ("Shareholding and Subsidiaries", "Group Structure"), False),
            SectionSpec("management", ("Directors", "Board Composition"), True),
            SectionSpec("financial", ("Financial Summary", "Financial Performance"), False),
            SectionSpec("facility", ("Existing Facility", "Facility Arrangements"), True),
            SectionSpec("proceedings", ("Litigation and Proceedings", "Legal Proceedings"), False),
            SectionSpec("allegations", ("Allegations and Credibility", "Allegations"), False),
            SectionSpec("recommendations", ("Recommendations", "Findings and Recommendations"), False),
            SectionSpec("appendix", ("Appendices", "Appendix"), True),
        ),
        (
            SectionSpec("scope", ("Scope and Sources", "Basis of Preparation"), False),
            SectionSpec("financial", ("Financial Position", "Financial Summary"), False),
            SectionSpec("ownership", ("Ownership Structure", "Group Structure and Ownership"), False),
            SectionSpec("facility", ("Indebtedness and Facility", "Facility"), False),
            SectionSpec("security", ("Security and Guarantees", "Guarantees"), True),
            SectionSpec("proceedings", ("Legal Proceedings", "Disputes"), False),
            SectionSpec("allegations", ("Allegations and Findings", "Findings"), False),
            SectionSpec("recommendations", ("Conclusions", "Recommendations"), False),
            SectionSpec("appendix", ("Appendices", "Documents Reviewed"), True),
        ),
    ),
    "board_minutes": (
        (
            SectionSpec("opening", ("Opening and Attendance", "Attendance and Quorum"), False),
            SectionSpec("quorum", ("Quorum and Interests", "Declarations of Interest"), True),
            SectionSpec("minutes", ("Previous Minutes", "Minutes of the Previous Meeting"), True),
            SectionSpec("resolutions", ("Resolutions", "Resolutions and Voting"), False),
            SectionSpec("facility", ("Facility and Funding", "Funding Arrangements"), True),
            SectionSpec("changes", ("Changes to the Board", "Director Changes"), True),
            SectionSpec("schedule", ("Supporting Schedule", "Schedule of Drawdowns"), True),
            SectionSpec("other", ("Other Business", "Any Other Business"), True),
            SectionSpec("closing", ("Close of Meeting", "Conclusion and Signatures"), False),
        ),
        (
            SectionSpec("opening", ("Attendance", "Opening, Attendance and Quorum"), False),
            SectionSpec("minutes", ("Minutes of the Previous Meeting", "Previous Minutes"), False),
            SectionSpec("resolutions", ("Resolutions", "Business of the Meeting"), False),
            SectionSpec("changes", ("Appointments and Resignations", "Board Changes"), True),
            SectionSpec("facility", ("Approval of the Facility", "Facility Approval"), False),
            SectionSpec("other", ("Other Business", "Other Matters"), True),
            SectionSpec("closing", ("Close of Meeting", "Close"), False),
        ),
        (
            SectionSpec("opening", ("Opening and Quorum", "Attendance and Constitution"), False),
            SectionSpec("resolutions", ("Resolutions and Decisions", "Resolutions"), False),
            SectionSpec("facility", ("Facility and Security", "Funding"), True),
            SectionSpec("changes", ("Director Changes", "Appointments"), True),
            SectionSpec("schedule", ("Schedule", "Supporting Schedule"), True),
            SectionSpec("closing", ("Conclusion", "Close of Meeting and Signatures"), False),
        ),
        (
            SectionSpec("opening", ("Attendance and Quorum", "Opening"), False),
            SectionSpec("quorum", ("Interests and Conflicts", "Declarations"), True),
            SectionSpec("resolutions", ("Resolutions", "Resolutions and Voting"), False),
            SectionSpec("facility", ("Funding and Drawdown", "Facility"), False),
            SectionSpec("minutes", ("Previous Minutes", "Prior Meeting"), True),
            SectionSpec("other", ("Other Business", "Any Other Business"), True),
            SectionSpec("closing", ("Close of Meeting", "Conclusion"), False),
        ),
    ),
    "case_notes": (
        (
            SectionSpec("matter", ("Matter and Parties", "Parties and Representation"), False),
            SectionSpec("chronology", ("Chronology", "Chronology of Events"), False),
            SectionSpec("evidence", ("Evidence Register", "Evidence"), True),
            SectionSpec("submissions", ("Submissions", "Submissions of the Parties"), False),
            SectionSpec("allegations", ("Allegations and Findings", "Allegations and the Court's Findings"), False),
            SectionSpec("procedure", ("Procedural History", "Procedure and Orders"), False),
            SectionSpec("next", ("Next Steps", "Orders and Next Steps"), True),
        ),
        (
            SectionSpec("matter", ("Parties", "Matter and Representation"), False),
            SectionSpec("procedure", ("Procedural History", "Procedural Position"), False),
            SectionSpec("chronology", ("Chronology", "Chronology of Events"), False),
            SectionSpec("submissions", ("Submissions", "Arguments"), False),
            SectionSpec("evidence", ("Evidence", "Evidence Register"), True),
            SectionSpec("allegations", ("Allegations and Findings", "Findings"), False),
            SectionSpec("next", ("Next Steps", "Orders"), False),
        ),
        (
            SectionSpec("matter", ("Matter and Parties", "Parties and Representation"), False),
            SectionSpec("chronology", ("Chronology", "Timeline"), False),
            SectionSpec("evidence", ("Evidence", "Documentary Evidence"), False),
            SectionSpec("procedure", ("Procedural Status", "Status and Orders"), False),
            SectionSpec("submissions", ("Submissions", "Submissions"), False),
            SectionSpec("allegations", ("Allegations", "Allegations and Findings"), True),
            SectionSpec("next", ("Next Steps", "Further Steps"), True),
        ),
    ),
    "memorandum": (
        (
            SectionSpec("summary", ("Executive Summary", "Summary of Advice"), False),
            SectionSpec("background", ("Background", "Background and Transaction"), False),
            SectionSpec("analysis", ("Analysis", "Analysis and Advice"), False),
            SectionSpec("relationships", ("Legal Relationships", "Ownership and Relationships"), True),
            SectionSpec("financial", ("Financial Evidence", "Financial Position"), False),
            SectionSpec("exceptions", ("Exceptions and Qualifications", "Qualifications"), True),
            SectionSpec("recommendations", ("Recommendations", "Conclusion and Recommendations"), False),
            SectionSpec("exhibits", ("Exhibits", "Schedule of Exhibits"), True),
        ),
        (
            SectionSpec("summary", ("Summary", "Executive Summary"), False),
            SectionSpec("background", ("Background", "Facts"), False),
            SectionSpec("analysis", ("Legal Analysis", "Analysis"), False),
            SectionSpec("financial", ("Financial Evidence", "Financial Analysis"), True),
            SectionSpec("exceptions", ("Qualifications", "Qualifications and Assumptions"), False),
            SectionSpec("recommendations", ("Recommendations", "Recommendation"), False),
            SectionSpec("exhibits", ("Exhibits", "Documents Referred To"), True),
        ),
        (
            SectionSpec("summary", ("Executive Summary", "Conclusions"), False),
            SectionSpec("analysis", ("Analysis", "Analysis of the Issues"), False),
            SectionSpec("background", ("Background", "Background to the Transaction"), True),
            SectionSpec("relationships", ("Relationships and Obligations", "Legal Relationships"), False),
            SectionSpec("financial", ("Financial Evidence", "Financial Summary"), True),
            SectionSpec("exceptions", ("Exceptions", "Exceptions and Qualifications"), False),
            SectionSpec("recommendations", ("Recommendations", "Next Steps"), False),
        ),
    ),
    "press_release": (
        (
            SectionSpec("announcement", ("Announcement", "Announcement and Transaction"), False),
            SectionSpec("quotes", ("Quotes", "Comment"), True),
            SectionSpec("financial", ("Financial Highlights", "Results"), True),
            SectionSpec("about", ("About the Group", "About the Company"), False),
            SectionSpec("contact", ("Forward-Looking Statements and Contacts", "Contacts"), False),
        ),
        (
            SectionSpec("announcement", ("Announcement",), False),
            SectionSpec("financial", ("Financial Highlights", "Trading Update"), False),
            SectionSpec("quotes", ("Comment", "Quotes"), True),
            SectionSpec("about", ("About the Group", "Company Information"), False),
            SectionSpec("contact", ("Notes and Contacts", "Contacts and Notes"), False),
        ),
        (
            SectionSpec("announcement", ("Announcement", "Acquisition Announcement"), False),
            SectionSpec("quotes", ("Comment", "Executive Comment"), False),
            SectionSpec("about", ("About the Group", "Group Profile"), True),
            SectionSpec("contact", ("Contacts", "Media Contacts"), False),
        ),
    ),
    "correspondence": (
        (
            SectionSpec("subject", ("Subject and Reference", "Subject"), False),
            SectionSpec("position", ("Position", "The Company's Position"), False),
            SectionSpec("request", ("Request", "Request and Next Steps"), False),
            SectionSpec("thread", ("Thread References", "Previous Correspondence"), True),
            SectionSpec("attachments", ("Attachments", "Enclosures"), True),
            SectionSpec("closing", ("Closing", "Signature"), False),
        ),
        (
            SectionSpec("subject", ("Subject", "Reference"), False),
            SectionSpec("thread", ("Previous Correspondence", "Thread"), True),
            SectionSpec("position", ("Response", "Position"), False),
            SectionSpec("request", ("Requests", "Request"), False),
            SectionSpec("correction", ("Correction", "Correction of Earlier Statement"), True),
            SectionSpec("attachments", ("Enclosures", "Attachments"), True),
            SectionSpec("closing", ("Closing", "Signature and Contact"), False),
        ),
        (
            SectionSpec("subject", ("Subject and Reference", "Reference"), False),
            SectionSpec("position", ("Position", "Statements"), False),
            SectionSpec("request", ("Request and Conditions", "Requests"), False),
            SectionSpec("correction", ("Correction", "Correction of Error"), True),
            SectionSpec("closing", ("Closing", "Yours Faithfully"), False),
        ),
    ),
}


class DocBuilder:
    """Accumulates one canonical document and allocates every id it needs."""

    def __init__(self, matter: Matter, doc_id: str, doc_type: str,
                 alloc: IdAllocator, config: CorpusConfig, register: str) -> None:
        self.matter = matter
        self.doc_id = doc_id
        self.doc_type = doc_type
        self.alloc = alloc
        self.config = config
        self.register = register
        self.doc_scope = doc_id.split(":")[1]
        self.ctx = dict(matter.ctx)
        self.ctx["document_date"] = fmt_date(matter.dates[doc_id], 0)
        self.ctx["document_date_short"] = fmt_date(matter.dates[doc_id], 3)
        self.rng = scope_rng(alloc.key, "document", doc_id)
        self.roles = RoleMap(entities=matter.entities, roles=dict(matter.entity_roles))
        self.surface = Surface(self.roles, alloc.key, self.doc_scope, register)
        self.sections: list[Section] = []
        self.statements: list[FactStatement] = []
        self.headers: list[HeaderFooter] = []
        self.footers: list[HeaderFooter] = []
        self.exhibits: list[Exhibit] = []
        self.signatures: list[SignatureRecord] = []
        self.footnotes: list[Footnote] = []
        self._section_by_key: dict[str, Section] = {}
        self._section_path: dict[str, str] = {}
        self._block_index: dict[str, int] = {}
        self._table_index: dict[str, int] = {}
        self._statement_index = 0
        self._footnote_index = 0
        self.fact_ids = list(matter.doc_facts.get(doc_id, []))
        self._used_facts: set[str] = set()
        #: every sentence already written in this document, so the same wording is
        #: never produced twice by accident (roles, boilerplate or restatement)
        self.used_texts: set[str] = set()
        #: the §10.2 length axis: sampled per document, then clamped to the type's
        #: published range by ``_normalise`` rather than allowed to drift
        self.length_factor = self.rng.uniform(0.75, 1.35)

    # -- sections ----------------------------------------------------------
    def section(self, key: str, heading: str) -> Section:
        number = len(self.sections) + 1
        path = f"{number:02d}"
        section_id = self.alloc.alloc("sec", f"{self.doc_scope}/{path}")
        section = Section(section_id=section_id, depth=0, number=str(number),
                          heading=heading, blocks=[])
        self.sections.append(section)
        self._section_by_key[key] = section
        self._section_path[key] = path
        self._block_index[key] = 0
        return section

    def subsection(self, parent: Section, key: str, heading: str) -> Section:
        parent_key = next(k for k, s in self._section_by_key.items() if s is parent)
        siblings = [s for s in self.sections if s.parent_id == parent.section_id]
        index = len(siblings) + 1
        path = f"{self._section_path[parent_key]}.{index:02d}"
        section_id = self.alloc.alloc("sec", f"{self.doc_scope}/{path}")
        section = Section(section_id=section_id, parent_id=parent.section_id, depth=1,
                          number=f"{parent.number}.{index}", heading=heading, blocks=[])
        self.sections.append(section)
        self._section_by_key[key] = section
        self._section_path[key] = path
        self._block_index[key] = 0
        return section

    # -- blocks ------------------------------------------------------------
    def _next_block(self, section: Section) -> tuple[str, str, int]:
        key = next(k for k, s in self._section_by_key.items() if s is section)
        self._block_index[key] += 1
        index = self._block_index[key]
        block_id = self.alloc.alloc(
            "blk", f"{self.doc_scope}/{self._section_path[key]}.{index:02d}")
        return block_id, self._section_path[key], index

    def paragraphs(self, section: Section, texts: list[str], *, per_block: int = 2) -> None:
        """Group filled templates into paragraph blocks of ``per_block`` sentences."""
        for start in range(0, len(texts), per_block):
            chunk = texts[start:start + per_block]
            block_id, _, _ = self._next_block(section)
            runs = [InlineRun(text=_block_text(text)) for text in chunk]
            self.used_texts.update(run.text for run in runs)
            section.blocks.append(Block(block_id=block_id, kind="paragraph", runs=runs))

    def prose(self, section: Section, role: str, count: int, *, per_block: int = 2) -> None:
        texts = paragraphs(role, self.ctx, self.rng, count, used=self.used_texts)
        self.paragraphs(section, texts, per_block=per_block)

    def fact_paragraphs(self, section: Section, fact_ids: list[str], *,
                        per_block: int = 2, extras: dict[str, dict[str, str]] | None = None
                        ) -> None:
        sentences = [self.realise(fact_id, extras.get(fact_id) if extras else None)
                     for fact_id in fact_ids]
        for start in range(0, len(sentences), per_block):
            chunk = sentences[start:start + per_block]
            block_id, path, index = self._next_block(section)
            runs = [self._run(sentence) for sentence in chunk]
            section.blocks.append(Block(block_id=block_id, kind="paragraph", runs=runs))
            for sentence in chunk:
                self._statement(sentence, block_id, path, index)

    def flush_unused(self, section: Section, *, per_block: int = 3) -> list[str]:
        """State any assigned fact the grammar's sections did not reach.

        An optional section that the chosen grammar omits must not silently drop
        facts: the leftover facts are stated in a closing section rather than
        becoming orphans.
        """
        remainder = [fid for fid in self.fact_ids if fid not in self._used_facts]
        if remainder:
            self.fact_paragraphs(section, remainder, per_block=per_block)
        return remainder

    def realise(self, fact_id: str, extra: dict[str, str] | None = None) -> Sentence:
        fact = self.matter.facts[fact_id]
        slots = dict(extra or {})
        for share in self.matter.shareholdings:
            if share.fact_id == fact_id:
                slots.setdefault("pct", share.percent.display())
        sentence = self.surface.fact(fact, f"{self.doc_id}/{fact_id}", slots,
                                     used=self.used_texts)
        self._used_facts.add(fact_id)
        return sentence

    def _run(self, sentence: Sentence) -> InlineRun:
        return InlineRun(
            text=sentence.text,
            entity_ref=_ref(sentence.entity_id),
            value=sentence.value,
            fact_ref=sentence.fact_id,
        )

    def _statement(self, sentence: Sentence, node_id: str, path: str, index: int) -> None:
        if sentence.fact_id is None:
            return
        self._statement_index += 1
        statement_id = self.alloc.alloc(
            "stmt", f"{self.doc_scope}/{path}.{index:02d}.{self._statement_index:02d}")
        self.statements.append(FactStatement(
            statement_id=statement_id,
            fact_id=sentence.fact_id,
            node_id=node_id,
            surface=sentence.text,
            mention_ids=list(sentence.mention_ids),
            value_slots=sentence.slot_dict(),
        ))

    def bullet_list(self, section: Section, items: list[str], *, ordered: bool = False,
                    entity_ids: list[str | None] | None = None) -> None:
        block_id, path, index = self._next_block(section)
        entries: list[ListItem] = []
        for position, text in enumerate(items, start=1):
            item_id = self.alloc.alloc(
                "li", f"{self.doc_scope}/{path}.{index:02d}.{position:02d}")
            entity_id = entity_ids[position - 1] if entity_ids else None
            entries.append(ListItem(item_id=item_id,
                                    runs=[InlineRun(text=_block_text(text),
                                                    entity_ref=_ref(entity_id))]))
        section.blocks.append(Block(block_id=block_id, kind="list", items=entries,
                                    ordered=ordered))

    def note(self, section: Section, text: str) -> str:
        block_id, path, index = self._next_block(section)
        self._footnote_index += 1
        footnote_id = self.alloc.alloc("fnt", f"{self.doc_scope}/n{self._footnote_index:02d}")
        marker = str(self._footnote_index)
        self.footnotes.append(Footnote(footnote_id=footnote_id, marker=marker,
                                       runs=[InlineRun(text=_block_text(text))]))
        section.blocks.append(Block(block_id=block_id, kind="note",
                                    runs=[InlineRun(text=_block_text(text),
                                                    footnote_id=footnote_id)],
                                    footnote_refs=[footnote_id]))
        return footnote_id

    def signature_block(self, section: Section, records: list[tuple[str, str, str, bool]]
                        ) -> None:
        """``records`` = (entity_id, capacity, signed_on_iso, digital)."""
        block_id, out_path, index = self._next_block(section)
        runs: list[InlineRun] = []
        for entity_id, capacity, signed_on, digital in records:
            self.signatures.append(SignatureRecord(
                signature_id=self.alloc.alloc(
                    "sig", f"{self.doc_scope}/{out_path}.{len(self.signatures) + 1:02d}"),
                signatory=_ref(entity_id),
                capacity=capacity,
                signed_on=IsoDate(value=dt.date.fromisoformat(signed_on)),
                digital=digital))
            name = self.matter.entities[entity_id].canonical_name
            runs.append(InlineRun(text=f"Signed for and on behalf of {name}, {capacity}."))
        section.blocks.append(Block(block_id=block_id, kind="signature", runs=runs))

    def exhibit_block(self, section: Section, label: str, title: str,
                      *, exhibit_id: str | None = None,
                      document_ref: str | None = None,
                      note: str | None = None) -> None:
        block_id, path, index = self._next_block(section)
        eid = exhibit_id or self.alloc.alloc(
            "exh", f"{self.doc_scope}/{len(self.exhibits) + 1:02d}")
        self.exhibits.append(Exhibit(
            exhibit_id=eid, label=label, title=title,
            document_ref=_doc_ref(document_ref) if document_ref else None))
        runs = [InlineRun(text=f"{label}: {title}.")]
        if note:
            runs.append(InlineRun(text=_block_text(note)))
        section.blocks.append(Block(block_id=block_id, kind="exhibit", runs=runs))

    def page_break(self, section: Section) -> None:
        block_id, _, _ = self._next_block(section)
        section.blocks.append(Block(block_id=block_id, kind="page_break"))

    def add_table(self, section: Section, *, caption: str | None,
                  columns: list[ColumnSpec], header: list[list[CellSpec]],
                  body: list[list[CellSpec]], totals: list[list[CellSpec]] | None = None
                  ) -> Table:
        key = next(k for k, s in self._section_by_key.items() if s is section)
        self._table_index[key] = self._table_index.get(key, 0) + 1
        tindex = self._table_index[key]
        base = f"{self.doc_scope}/{self._section_path[key]}.t{tindex:02d}"
        table_id = self.alloc.alloc("tbl", base)
        width = len(columns)
        rows_spec = list(header) + list(body) + list(totals or [])
        rows: list[TableRow] = []
        cell_refs: dict[str, str] = {}
        row_facts: list[tuple[str, list[CellSpec]]] = []
        for ri, specs in enumerate(rows_spec):
            assert sum(spec.colspan for spec in specs) == width, (
                f"table {caption!r} row {ri} has {sum(s.colspan for s in specs)} cells, "
                f"expected {width}")
            row_id = self.alloc.alloc("tbl", f"{base}/r{ri:02d}")
            cells: list[TableCell] = []
            column = 0
            for ci, spec in enumerate(specs):
                cell_id = self.alloc.alloc("tbl", f"{base}/r{ri:02d}c{column:02d}")
                formula = None
                if spec.formula is not None:
                    formula = CellFormula(
                        formula=spec.formula,
                        cached_value=spec.cached if spec.cached is not None else spec.value)
                cells.append(TableCell(
                    cell_id=cell_id,
                    text=spec.text if (spec.text is None or spec.text.strip()
                                       or spec.value is not None) else None,
                    value=spec.value,
                    display=spec.display or (spec.text or ""),
                    rowspan=spec.rowspan, colspan=spec.colspan,
                    entity_ref=_ref(spec.entity_ref), fact_ref=spec.fact_ref,
                    number_format=spec.number_format, formula=formula))
                if spec.fact_ref:
                    cell_refs[f"r{ri:02d}c{column:02d}"] = spec.fact_ref
                column += spec.colspan
            rows.append(TableRow(row_id=row_id, cells=cells))
            if ri >= len(header):
                referenced = sorted({cell.fact_ref for cell in cells if cell.fact_ref})
                for fact_id in referenced:
                    row_facts.append((fact_id, [cell for cell in cells
                                                if cell.fact_ref == fact_id]))
        table = Table(table_id=table_id, caption=caption,
                      columns=columns,
                      header_rows=rows[:len(header)],
                      body_rows=rows[len(header):len(header) + len(body)],
                      total_rows=rows[len(header) + len(body):],
                      cell_refs=cell_refs)
        block_id, path, index = self._next_block(section)
        section.blocks.append(Block(block_id=block_id, kind="table", table=table))
        for fact_id, cells in row_facts:
            surface = " | ".join(cell.display or cell.text or "" for cell in cells
                                 if cell.fact_ref == fact_id)
            self._used_facts.add(fact_id)
            self._statement(
                Sentence(text=surface or caption or "table row", fact_id=fact_id,
                         value=cells[0].value,
                         mention_ids=tuple(sorted({cell.entity_ref.entity_id
                                                   for cell in cells if cell.entity_ref}))),
                table.table_id, path, index)
        return table

    # -- metadata ----------------------------------------------------------
    def header_footer(self, *, position: str, text: str, applies_to: str = "all") -> None:
        hf_id = self.alloc.alloc(
            "hdr" if position == "header" else "ftr",
            f"{self.doc_scope}/{len(self.headers) + len(self.footers) + 1:02d}")
        entry = HeaderFooter(header_footer_id=hf_id, position=position,
                             runs=[InlineRun(text=text)], applies_to=applies_to)
        (self.headers if position == "header" else self.footers).append(entry)

    def fingerprint(self, structure_id: str) -> str:
        """Deterministic structural signature: order, headings and block kinds.

        Content is deliberately excluded: two documents with the same shape are
        duplicates for diversity purposes even if every noun differs (§10.4).
        """
        shape = "||".join(
            f"{s.number or ''};{s.heading};" + ",".join(b.kind for b in s.blocks)
            for s in self.sections)
        digest = hashlib.blake2b(
            f"{self.doc_type}|{structure_id}|{shape}".encode("utf-8"),
            digest_size=8).hexdigest()
        return digest

    def build(self, *, metadata: DocumentMetadata, structure_id: str) -> CdmDocument:
        return CdmDocument(
            document_id=self.doc_id, doc_kind="prose", doc_type=self.doc_type,
            structure_id=structure_id, metadata=metadata,
            sections=self.sections, statements=self.statements,
            headers=self.headers, footers=self.footers, exhibits=self.exhibits,
            signatures=self.signatures, footnotes=self.footnotes,
            plan_fingerprint=self.fingerprint(structure_id))


def _first_upper(text: str) -> str:
    """Upper-case the first character only; never lower-case the rest.

    ``str.capitalize()`` lower-cases every later character, which silently rewrites
    entity names ("Meridian Finance Limited" -> "meridian finance limited").
    """
    text = " ".join(text.split())
    return text[:1].upper() + text[1:] if text[:1].islower() else text


def _block_text(text: str) -> str:
    text = _first_upper(text)
    if not text:
        raise ValueError("empty block text")
    return text


def _ref(entity_id: str | None):
    from synthetic_data.model import EntityRef
    return EntityRef(entity_id=entity_id) if entity_id else None


def _doc_ref(document_id: str | None):
    from synthetic_data.model import DocumentRef
    return DocumentRef(document_id=document_id) if document_id else None


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _families(builder: DocBuilder) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for fact_id in builder.fact_ids:
        out.setdefault(builder.matter.fact_family[fact_id], []).append(fact_id)
    return out


def _money_cell(money: Money, *, fact_ref: str | None = None,
                entity_ref: str | None = None) -> CellSpec:
    return CellSpec(value=money, display=fmt_money(money, 1),
                    fact_ref=fact_ref, entity_ref=entity_ref,
                    number_format=money_number_format())


def _date_cell(date: IsoDate, *, fact_ref: str | None = None) -> CellSpec:
    return CellSpec(value=date, display=fmt_date(date, 0), fact_ref=fact_ref,
                    number_format=date_number_format())


def _percent_cell(percent: Percentage, *, entity_ref: str | None = None) -> CellSpec:
    return CellSpec(value=percent, display=fmt_percent(percent),
                    entity_ref=entity_ref, number_format=percent_number_format())


def _pct_slots(builder: DocBuilder) -> dict[str, dict[str, str]]:
    return {share.fact_id: {"pct": share.percent.display()}
            for share in builder.matter.shareholdings}


def _section_specs(doc_type: str, rng) -> tuple[SectionSpec, ...]:
    return pick(rng, STRUCTURES[doc_type])


# ---------------------------------------------------------------------------
# planner entry point
# ---------------------------------------------------------------------------
PLANNERS = {}


def planner(doc_type: str):
    def register(function):
        PLANNERS[doc_type] = function
        return function
    return register


def _section_stats(sections) -> tuple[int, int, int]:
    """Words, sentences and paragraph blocks — identical to the metrics definition.

    Sentences are counted by splitting the joined document text, not each block
    separately, so ``_normalise`` and ``compute_metrics`` can never disagree about
    whether a document is inside its range.
    """
    import re as _re
    word_re = _re.compile(r"[A-Za-z][A-Za-z'\-]*")
    split = _re.compile(r"(?<=[.!?])\s+")
    chunks: list[str] = []
    paragraphs = 0
    for section in sections:
        for block in section.blocks:
            if block.kind == "paragraph":
                paragraphs += 1
            for run in block.runs or []:
                if run.text:
                    chunks.append(run.text)
            for item in block.items or []:
                for run in item.runs:
                    if run.text:
                        chunks.append(run.text)
            if block.table is not None:
                for row in [*block.table.header_rows, *block.table.body_rows,
                            *block.table.total_rows]:
                    for cell in row.cells:
                        chunks.append(cell.text or cell.display or "")
    text = " ".join(chunk for chunk in chunks if chunk)
    sentences = len([piece for piece in split.split(text) if piece.strip()])
    words = len(word_re.findall(text))
    return words, sentences, paragraphs


_TOP_UP = {
    "financial_agreement": "obligation",
    "due_diligence": "review",
    "board_minutes": "board",
    "case_notes": "procedure",
    "memorandum": "analysis",
    "press_release": "media",
    "correspondence": "letter",
}


def _prose_stats(sections) -> tuple[int, int]:
    """Words and sentences in prose blocks only (tables excluded)."""
    import re as _re
    word_re = _re.compile(r"[A-Za-z][A-Za-z'\-]*")
    split = _re.compile(r"(?<=[.!?])\s+")
    words = sentences = 0
    for section in sections:
        for block in section.blocks:
            texts: list[str] = []
            for run in block.runs or []:
                if run.text:
                    texts.append(run.text)
            for item in block.items or []:
                texts.extend(run.text for run in item.runs if run.text)
            text = " ".join(texts)
            words += len(word_re.findall(text))
            sentences += len([piece for piece in split.split(text) if piece.strip()])
    return words, sentences


def _table_words(sections) -> int:
    import re as _re
    word_re = _re.compile(r"[A-Za-z][A-Za-z'\-]*")
    total = 0
    for section in sections:
        for block in section.blocks:
            if block.table is None:
                continue
            for row in [*block.table.header_rows, *block.table.body_rows,
                        *block.table.total_rows]:
                for cell in row.cells:
                    total += len(word_re.findall(cell.text or cell.display or ""))
    return total


def _normalise(builder: DocBuilder, doc_type: str) -> None:
    """Clamp a planned document into its type's §11 depth range.

    The sentence and word ranges only overlap for a particular words-per-sentence
    density, so the target is the intersection of both ranges for *this*
    document's measured density.  A per-document point inside that intersection is
    sampled (the §10.2 length axis), then the plan is topped up or trimmed to it.
    Trimming only drops structural paragraphs — never a fact-bearing block, never a
    table — and excess paragraphs are merged so no sentence is lost.
    """
    import math

    from .config import DEPTH_TARGETS
    from . import grammar as g

    target = DEPTH_TARGETS[doc_type]
    low_s, high_s = target.sentences
    low_w, high_w = target.words
    low_p, high_p = target.paragraphs

    prose_words, prose_sentences = _prose_stats(builder.sections)
    density = prose_words / prose_sentences if prose_sentences else 14.0
    density = min(30.0, max(9.0, density))
    table_words = _table_words(builder.sections)
    budget_low = max(low_w * 1.02 - table_words, 0)
    budget_high = max(high_w * 0.98 - table_words, 0)
    s_lo = max(low_s, math.ceil(budget_low / density))
    s_hi = min(high_s, math.floor(budget_high / density))
    if s_hi < s_lo:
        s_hi = s_lo
    share = getattr(builder, "length_share", 0.5)
    target_sentences = int(round(s_lo + (s_hi - s_lo) * share))

    pools = {
        "obligation": (g.OBLIGATION_SUBJECTS, g.OBLIGATION_PREDICATES,
                       g.QUALIFIERS, g.OBLIGATION_FRAMES),
        "review": (g.REVIEW_SUBJECTS, g.REVIEW_PREDICATES,
                   g.REVIEW_QUALIFIERS, g.REVIEW_FRAMES),
        "board": (g.BOARD_SUBJECTS, g.BOARD_PREDICATES,
                  g.BOARD_QUALIFIERS, g.BOARD_FRAMES),
        "procedure": (g.PROCEDURE_SUBJECTS, g.PROCEDURE_PREDICATES,
                      g.PROCEDURE_QUALIFIERS, g.PROCEDURE_FRAMES),
        "analysis": (g.ANALYSIS_SUBJECTS, g.ANALYSIS_PREDICATES,
                     g.ANALYSIS_QUALIFIERS, g.ANALYSIS_FRAMES),
        "media": (g.MEDIA_SUBJECTS, g.MEDIA_PREDICATES,
                  g.MEDIA_QUALIFIERS, g.MEDIA_FRAMES),
        "letter": (g.LETTER_SUBJECTS, g.LETTER_PREDICATES,
                   g.LETTER_QUALIFIERS, g.LETTER_FRAMES),
    }[_TOP_UP[doc_type]]

    def add(count: int, per_block: int = 4,
            *, prefer_long: bool | None = None) -> None:
        frames = pools[3]
        if prefer_long is None:
            prefer_long = share > 0.6
        if prefer_long:
            long_frames = tuple(frame for frame in frames if "{q}" in frame)
            frames = long_frames or frames
        elif share < 0.4:
            short_frames = tuple(frame for frame in frames if "{q}" not in frame)
            frames = short_frames or frames
        texts = g.compose(builder.rng, builder.ctx, subjects=pools[0],
                          predicates=pools[1], qualifiers=pools[2],
                          frames=frames, count=max(1, count),
                          used=builder.used_texts)
        # append to the last *droppable* section so the trimmer can reach it
        target_section = builder.sections[-2] if len(builder.sections) > 1 \
            else builder.sections[-1]
        builder.paragraphs(target_section, texts, per_block=per_block)

    for _ in range(600):
        anchored = {statement.node_id for statement in builder.statements}
        words, sentences, paragraphs = _section_stats(builder.sections)
        if sentences < target_sentences:
            add(target_sentences - sentences)
            continue
        if sentences > target_sentences + 6:
            if not _drop_structural_block(builder, anchored, target_sentences):
                break
            continue
        if paragraphs > high_p:
            if not _merge_paragraphs(builder):
                break
            continue
        if words > high_w * 0.98:
            if not _drop_structural_block(builder, anchored,
                                          max(low_s, target_sentences - 4)):
                break
            continue
        if words < low_w * 1.02:
            add(2, per_block=2, prefer_long=True)
            continue
        if paragraphs < low_p:
            add(4, per_block=2)
            continue
        break

    # final guarantee of the lower bounds: a document may not end below the floor
    for _ in range(40):
        words, sentences, paragraphs = _section_stats(builder.sections)
        if sentences >= low_s and words >= low_w:
            break
        add(max(low_s - sentences, 3), per_block=3,
            prefer_long=words < low_w)
    # excess paragraphs are merged, never by deleting sentences
    for _ in range(200):
        words, sentences, paragraphs = _section_stats(builder.sections)
        if paragraphs <= high_p:
            break
        if not _merge_paragraphs(builder):
            break
    # and finally the ceilings, never below the sentence floor
    for _ in range(400):
        anchored = {statement.node_id for statement in builder.statements}
        words, sentences, paragraphs = _section_stats(builder.sections)
        if words <= high_w and sentences <= high_s:
            break
        if not _drop_structural_block(builder, anchored, low_s):
            break


def _drop_structural_block(builder: DocBuilder, anchored: set[str], floor: int) -> bool:
    """Remove the last droppable structural block, keeping the final section intact."""
    for section in reversed(builder.sections[:-1]):
        removable = [block for block in section.blocks
                     if block.kind in {"paragraph", "list", "note"}
                     and block.block_id not in anchored]
        if not removable:
            continue
        candidate = removable[-1]
        block_sentences = _block_sentence_count(candidate)
        _, sentences, _ = _section_stats(builder.sections)
        if sentences - block_sentences < floor + 2:
            continue
        section.blocks.remove(candidate)
        return True
    return False


def _block_sentence_count(block) -> int:
    import re as _re
    split = _re.compile(r"(?<=[.!?])\s+")
    texts = [run.text for run in (block.runs or []) if run.text]
    for item in block.items or []:
        texts.extend(run.text for run in item.runs if run.text)
    text = " ".join(texts)
    return len([piece for piece in split.split(text) if piece.strip()])


def _merge_paragraphs(builder: DocBuilder) -> bool:
    """Merge two adjacent paragraph blocks and re-point their statements.

    Sentence count is preserved; only the block count falls.  This is what keeps
    a deeply tabled document inside the paragraph range without deleting prose.
    """
    for section in reversed(builder.sections):
        blocks = section.blocks
        for index in range(len(blocks) - 1, 0, -1):
            survivor = blocks[index - 1]
            doomed = blocks[index]
            if survivor.kind != "paragraph" or doomed.kind != "paragraph":
                continue
            if not survivor.runs or not doomed.runs:
                continue
            merged_runs = [*survivor.runs, *doomed.runs]
            blocks[index - 1] = survivor.model_copy(update={"runs": merged_runs})
            blocks.pop(index)
            for position, statement in enumerate(builder.statements):
                if statement.node_id == doomed.block_id:
                    builder.statements[position] = statement.model_copy(
                        update={"node_id": survivor.block_id})
            return True
    return False


def plan_document(matter: Matter, doc_id: str, doc_type: str,
                  alloc: IdAllocator, config: CorpusConfig, *,
                  type_index: int = 0, type_count: int = 1) -> CdmDocument:
    register = pick(scope_rng(alloc.key, "register", doc_id), config.registers)
    builder = DocBuilder(matter, doc_id, doc_type, alloc, config, register)
    # stratified inside the type so the corpus actually spans the length band;
    # the factor shapes how much structure is planned before clamping
    if type_count > 1:
        # raised cosine: documents sit nearer the ends of the length band than a
        # linear split would put them, so the measured p5..p95 spread is real
        position = type_index / (type_count - 1)
        builder.length_share = 0.5 - 0.5 * math.cos(math.pi * position)
    else:
        builder.length_share = 0.5
    builder.length_factor = 0.72 + 0.68 * builder.length_share
    specs = _section_specs(doc_type, builder.rng)
    headings = {spec.key: pick(builder.rng, spec.heading) for spec in specs}
    metadata, structure_id = PLANNERS[doc_type](builder, specs, headings)
    _normalise(builder, doc_type)
    document = builder.build(metadata=metadata, structure_id=structure_id)
    missing = [fid for fid in builder.fact_ids if fid not in builder._used_facts]
    if missing:
        families = sorted({matter.fact_family[fid] for fid in missing})
        raise AssertionError(
            f"{doc_type} {doc_id} never states {len(missing)} assigned fact(s): {families}")
    return document


def _sections(builder: DocBuilder, specs, headings, keys: list[str]) -> dict[str, Section]:
    """Create the grammar's sections in a deterministic order.

    The first section (parties/attendance/scope) and the last (execution/closing)
    keep their positions; the middle sections are permuted.  That is the
    "section ordering" variation axis in §10.2 and it is what stops eight
    documents of one type collapsing onto four structural signatures.
    """
    ordered = list(specs)
    if len(ordered) > 3:
        middle = shuffled(builder.rng, ordered[1:-1])
        ordered = [ordered[0], *middle, ordered[-1]]
    out: dict[str, Section] = {}
    for spec in ordered:
        if spec.key in keys:
            out[spec.key] = builder.section(spec.key, headings[spec.key])
    return out


#: structural sentence depth per type: a facility agreement carries far more
#: clause prose than a letter, and the §11 sentence ranges say so.
_SENTENCE_SCALE = {
    "financial_agreement": 2.2,
    "due_diligence": 2.6,
    "case_notes": 1.6,
    "memorandum": 1.65,
    "board_minutes": 1.0,
    "press_release": 0.8,
    "correspondence": 1.0,
}


def _compose(builder: DocBuilder, count: int, *, subjects, predicates, qualifiers,
             frames) -> list[str]:
    from .grammar import compose
    scaled = max(1, round(count * _SENTENCE_SCALE.get(builder.doc_type, 1.0)
                          * builder.length_factor))
    return compose(builder.rng, builder.ctx, subjects=subjects, predicates=predicates,
                   qualifiers=qualifiers, frames=frames, count=scaled,
                   used=builder.used_texts)


def _metadata(builder: DocBuilder, **kwargs) -> DocumentMetadata:
    matter = builder.matter
    date = matter.dates[builder.doc_id]
    return DocumentMetadata(
        document_date=date,
        jurisdiction=matter.ctx["jurisdiction"],
        formality=builder.register,
        **kwargs)


def _structure_id(doc_type: str, specs) -> str:
    return f"{doc_type}/v{STRUCTURES[doc_type].index(specs) + 1}"


def m_corrections(builder: DocBuilder) -> set[str]:
    return builder.matter.typed.get("correction_docs", set())


def _all_owned_money(builder: DocBuilder) -> Money:
    return builder.matter.typed["principal"]          # type: ignore[return-value]


# ---------------------------------------------------------------------------
# financial agreement
# ---------------------------------------------------------------------------
@planner("financial_agreement")
def _plan_financial_agreement(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    sec = _sections(b, specs, headings, keys)
    m = b.matter
    typed = m.typed
    principal: Money = typed["principal"]              # type: ignore[assignment]

    # 1. parties, recitals and the group's ownership
    b.prose(sec["recitals"], "fa.recital", 4, per_block=3)
    b.paragraphs(sec["recitals"], _compose(
        b, 8, subjects=g.CLAUSE_SUBJECTS, predicates=g.CLAUSE_PREDICATES,
        qualifiers=g.CLAUSE_QUALIFIERS, frames=g.CLAUSE_FRAMES), per_block=3)
    b.fact_paragraphs(sec["recitals"], fam.get("acquisition", []))
    _shareholding_table(b, sec["recitals"], fam.get("ownership", []), caption="Group structure")

    # 2. definitions
    if "definitions" in sec:
        terms = paragraphs("fa.definition_term", b.ctx, b.rng, 9,
                           used=b.used_texts)
        # a definition list, not prose: each entry is one defined term
        definitions = _definition_items(b, terms)
        b.bullet_list(sec["definitions"], definitions)
        b.prose(sec["definitions"], "fa.interpretation", 3, per_block=3)
        b.paragraphs(sec["definitions"], _compose(
            b, 5, subjects=g.CLAUSE_SUBJECTS, predicates=g.CLAUSE_PREDICATES,
            qualifiers=g.CLAUSE_QUALIFIERS, frames=g.CLAUSE_FRAMES), per_block=3)

    # 3. facility
    b.prose(sec["facility"], "fa.facility", 4, per_block=3)
    b.fact_paragraphs(sec["facility"], fam.get("facility", []) +
                      fam.get("subsidiary", []) + fam.get("intragroup", []))
    b.paragraphs(sec["facility"], _compose(
        b, 10, subjects=g.OBLIGATION_SUBJECTS, predicates=g.OBLIGATION_PREDICATES,
        qualifiers=g.QUALIFIERS, frames=g.OBLIGATION_FRAMES), per_block=3)

    # 4. conditions precedent (optional grammar)
    if "conditions" in sec:
        b.prose(sec["conditions"], "fa.conditions", 3, per_block=3)
        conditions = [m.conditions[cid].predicate for cid in m.typed["condition_ids"]]
        b.bullet_list(sec["conditions"],
                      [f"The Lender must receive evidence that {text}." for text in conditions],
                      ordered=True)

    # 5. interest
    b.prose(sec["interest"], "fa.interest", 4, per_block=3)
    b.paragraphs(sec["interest"], _compose(
        b, 9, subjects=g.CLAUSE_SUBJECTS, predicates=g.CLAUSE_PREDICATES,
        qualifiers=g.CLAUSE_QUALIFIERS, frames=g.CLAUSE_FRAMES), per_block=3)
    b.paragraphs(sec["interest"], [_interest_example(b, principal)], per_block=1)

    # 6. drawdown and repayment schedules
    _drawdown_table(b, sec["schedules"], fam.get("drawdown", []))
    b.prose(sec["schedules"], "fa.repayment", 3, per_block=3)
    _repayment_table(b, sec["schedules"], fam.get("repayment", []))
    b.paragraphs(sec["schedules"], _compose(
        b, 8, subjects=g.OBLIGATION_SUBJECTS, predicates=g.OBLIGATION_PREDICATES,
        qualifiers=g.QUALIFIERS, frames=g.OBLIGATION_FRAMES), per_block=3)

    # 7. outstanding balance
    if "outstanding" in sec:
        b.prose(sec["outstanding"], "fa.repayment", 2, per_block=2)
        b.fact_paragraphs(sec["outstanding"], fam.get("outstanding", []))
        b.paragraphs(sec["outstanding"], _compose(
            b, 6, subjects=g.CLAUSE_SUBJECTS, predicates=g.CLAUSE_PREDICATES,
            qualifiers=g.CLAUSE_QUALIFIERS, frames=g.CLAUSE_FRAMES), per_block=3)

    # 8. guarantees and security
    b.prose(sec["security"], "fa.guarantee", 2, per_block=2)
    b.prose(sec["security"], "fa.security", 2, per_block=2)
    _security_table(b, sec["security"], fam.get("security", []))
    b.fact_paragraphs(sec["security"], fam.get("guarantee", []))
    b.paragraphs(sec["security"], _compose(
        b, 6, subjects=g.OBLIGATION_SUBJECTS, predicates=g.OBLIGATION_PREDICATES,
        qualifiers=g.QUALIFIERS, frames=g.OBLIGATION_FRAMES), per_block=3)

    # 9. covenants and financial information
    b.prose(sec["covenants"], "fa.undertaking", 3, per_block=3)
    _covenant_table(b, sec["covenants"], fam.get("covenant", []))
    b.fact_paragraphs(sec["covenants"], fam.get("financial", []))
    b.paragraphs(sec["covenants"], _compose(
        b, 8, subjects=g.OBLIGATION_SUBJECTS, predicates=g.OBLIGATION_PREDICATES,
        qualifiers=g.QUALIFIERS, frames=g.OBLIGATION_FRAMES), per_block=3)

    # 10. representations
    if "representations" in sec:
        b.prose(sec["representations"], "fa.representation", 4, per_block=3)
        b.fact_paragraphs(sec["representations"], fam.get("representation", []))

    # 11. default and amendment
    b.prose(sec["defaults"], "fa.default", 4, per_block=3)
    b.paragraphs(sec["defaults"], _compose(
        b, 8, subjects=g.CLAUSE_SUBJECTS, predicates=g.CLAUSE_PREDICATES,
        qualifiers=g.CLAUSE_QUALIFIERS, frames=g.CLAUSE_FRAMES), per_block=3)

    # 12. execution
    b.prose(sec["execution"], "fa.notice", 2, per_block=2)
    b.fact_paragraphs(sec["execution"], fam.get("director", []) +
                      fam.get("representative", []) + fam.get("advisor", []), per_block=3)
    directors = [eid for eid in m.typed["directors"]][:2]
    b.signature_block(sec["execution"], [
        (directors[0], "director", m.dates[b.doc_id].iso(), True),
        (directors[1], "company secretary", m.dates[b.doc_id].iso(), True),
    ])
    b.flush_unused(sec["execution"], per_block=3)
    b.header_footer(position="header",
                    text=f"{m.ctx['holdco']} — Facility Agreement {m.ctx['facility_ref']}")
    b.header_footer(position="footer", text=f"Facility Agreement — {m.ctx['facility_ref']} — Confidential")

    metadata = _metadata(
        b, title="Facility Agreement", subtitle=m.ctx["facility_ref"],
        issuer=_entity_ref(typed["lender"]), addressee=_entity_ref(typed["holdco"]),
        reference=m.ctx["facility_ref"], version="1.0", classification="Confidential")
    return metadata, _structure_id("financial_agreement", specs)


# ---------------------------------------------------------------------------
# due diligence
# ---------------------------------------------------------------------------
@planner("due_diligence")
def _plan_due_diligence(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    sec = _sections(b, specs, headings, keys)
    m = b.matter

    b.prose(sec["scope"], "dd.scope", 3, per_block=3)
    b.prose(sec["scope"], "dd.sources", 2, per_block=2)
    b.prose(sec["scope"], "dd.limitation", 2, per_block=2)
    b.paragraphs(sec["scope"], _compose(
        b, 10, subjects=g.REVIEW_SUBJECTS, predicates=g.REVIEW_PREDICATES,
        qualifiers=g.REVIEW_QUALIFIERS, frames=g.REVIEW_FRAMES), per_block=3)

    b.prose(sec["ownership"], "dd.ownership", 3, per_block=3)
    _shareholding_table(b, sec["ownership"], fam.get("ownership", []),
                        caption="Shareholding structure")
    b.fact_paragraphs(sec["ownership"], fam.get("subsidiary", []), per_block=3)

    if "management" in sec:
        b.prose(sec["management"], "dd.ownership", 2, per_block=2)
        _director_table(b, sec["management"], fam.get("director", []))

    b.prose(sec["financial"], "dd.financial", 3, per_block=3)
    _financial_table(b, sec["financial"], fam.get("financial", []))
    b.fact_paragraphs(sec["financial"], fam.get("financial", []), per_block=3)
    b.paragraphs(sec["financial"], _compose(
        b, 6, subjects=g.REVIEW_SUBJECTS, predicates=g.REVIEW_PREDICATES,
        qualifiers=g.REVIEW_QUALIFIERS, frames=g.REVIEW_FRAMES), per_block=3)

    if "facility" in sec:
        _repayment_table(b, sec["facility"], fam.get("repayment", []), caption="Repayment schedule")
        b.fact_paragraphs(sec["facility"], fam.get("facility", []) +
                          fam.get("drawdown", []) + fam.get("outstanding", []) +
                          fam.get("intragroup", []), per_block=3)

    if "security" in sec:
        b.prose(sec["security"], "fa.guarantee", 2, per_block=2)
        _security_table(b, sec["security"], fam.get("security", []))
        b.fact_paragraphs(sec["security"], fam.get("guarantee", []) +
                          fam.get("security", []), per_block=3)

    b.prose(sec["proceedings"], "dd.proceedings", 3, per_block=3)
    b.fact_paragraphs(sec["proceedings"], fam.get("proceeding", []) +
                      fam.get("order", []), per_block=2)

    b.prose(sec["allegations"], "dd.scope", 2, per_block=2)
    b.fact_paragraphs(sec["allegations"], fam.get("allegation", []) +
                      fam.get("finding", []) + fam.get("covenant", []), per_block=3)
    b.paragraphs(sec["allegations"], _compose(
        b, 10, subjects=g.FINDING_SUBJECTS, predicates=g.FINDING_PREDICATES,
        qualifiers=g.FINDING_QUALIFIERS, frames=g.FINDING_FRAMES), per_block=3)

    b.prose(sec["recommendations"], "dd.limitation", 2, per_block=2)
    b.fact_paragraphs(sec["recommendations"], fam.get("recommendation", []) +
                      fam.get("representation", []) + fam.get("advisor", []), per_block=3)
    b.bullet_list(sec["recommendations"], [
        "The lender should obtain an updated compliance certificate before the next drawdown.",
        "The group should document the related-party balance in the next quarterly pack.",
        "The borrower should appoint an independent valuer for the charged assets.",
    ], ordered=True)

    if "appendix" in sec:
        b.prose(sec["appendix"], "dd.appendix", 2, per_block=2)
        b.exhibit_block(sec["appendix"], "Appendix 1", "Documents provided and dates received")
        b.exhibit_block(sec["appendix"], "Appendix 2", "Group structure chart and share register")
        b.note(sec["appendix"],
               "This report is prepared for the sole benefit of the addressee and may not "
               "be relied on by any other person.")
    b.flush_unused(sec["recommendations"], per_block=3)
    b.header_footer(position="header", text=f"{m.ctx['holdco']} — Due Diligence Report")

    metadata = _metadata(
        b, title="Due Diligence Report", subtitle=m.ctx["holdco"],
        issuer=_entity_ref(m.typed["lender"]), addressee=_entity_ref(m.typed["holdco"]),
        reference=f"DD-{b.doc_scope}", version="1.0", classification="Confidential")
    return metadata, _structure_id("due_diligence", specs)


# ---------------------------------------------------------------------------
# board minutes
# ---------------------------------------------------------------------------
@planner("board_minutes")
def _plan_board_minutes(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    sec = _sections(b, specs, headings, keys)
    m = b.matter

    b.prose(sec["opening"], "bm.opening", 3, per_block=3)
    _attendance_table(b, sec["opening"], fam.get("attendance", []))
    b.paragraphs(sec["opening"], _compose(
        b, 8, subjects=g.BOARD_SUBJECTS, predicates=g.BOARD_PREDICATES,
        qualifiers=g.BOARD_QUALIFIERS, frames=g.BOARD_FRAMES), per_block=3)

    if "quorum" in sec:
        b.prose(sec["quorum"], "bm.quorum", 2, per_block=2)
        b.paragraphs(sec["quorum"], _compose(
            b, 6, subjects=g.BOARD_SUBJECTS, predicates=g.BOARD_PREDICATES,
            qualifiers=g.BOARD_QUALIFIERS, frames=g.BOARD_FRAMES), per_block=3)

    if "minutes" in sec:
        b.prose(sec["minutes"], "bm.prior", 2, per_block=2)
        b.fact_paragraphs(sec["minutes"], fam.get("prior_minutes", []))

    b.prose(sec["resolutions"], "bm.resolution", 3, per_block=3)
    _resolution_table(b, sec["resolutions"], fam.get("resolution", []))
    b.fact_paragraphs(sec["resolutions"], fam.get("resolution", []), per_block=3)
    b.paragraphs(sec["resolutions"], _compose(
        b, 10, subjects=g.BOARD_SUBJECTS, predicates=g.BOARD_PREDICATES,
        qualifiers=g.BOARD_QUALIFIERS, frames=g.BOARD_FRAMES), per_block=3)

    if "facility" in sec:
        b.prose(sec["facility"], "bm.decision", 2, per_block=2)
        b.fact_paragraphs(sec["facility"], fam.get("facility", []) +
                          fam.get("drawdown", []) + fam.get("outstanding", []) +
                          fam.get("guarantee", []) + fam.get("security", []) +
                          fam.get("acquisition", []), per_block=3)
        b.paragraphs(sec["facility"], _compose(
            b, 6, subjects=g.BOARD_SUBJECTS, predicates=g.BOARD_PREDICATES,
            qualifiers=g.BOARD_QUALIFIERS, frames=g.BOARD_FRAMES), per_block=3)

    if "changes" in sec:
        b.prose(sec["changes"], "bm.director", 3, per_block=3)
        b.paragraphs(sec["changes"], _compose(
            b, 5, subjects=g.BOARD_SUBJECTS, predicates=g.BOARD_PREDICATES,
            qualifiers=g.BOARD_QUALIFIERS, frames=g.BOARD_FRAMES), per_block=3)

    if "schedule" in sec:
        b.prose(sec["schedule"], "bm.schedule", 2, per_block=2)
        _drawdown_table(b, sec["schedule"], fam.get("drawdown", []),
                        caption="Drawdown timetable")

    if "other" in sec:
        b.paragraphs(sec["other"], _compose(
            b, 7, subjects=g.BOARD_SUBJECTS, predicates=g.BOARD_PREDICATES,
            qualifiers=g.BOARD_QUALIFIERS, frames=g.BOARD_FRAMES), per_block=3)

    b.prose(sec["closing"], "bm.closing", 2, per_block=2)
    b.fact_paragraphs(sec["closing"], fam.get("director", []) + fam.get("chair", []),
                      per_block=4)
    chair = m.typed["directors"][0]
    secretary = _entity_id_by_name(m, m.ctx["secretary"])
    b.signature_block(sec["closing"], [
        (chair, "chair of the meeting", m.dates[b.doc_id].iso(), False),
        (secretary, "company secretary", m.dates[b.doc_id].iso(), False),
    ])
    b.flush_unused(sec["closing"], per_block=4)
    b.header_footer(position="header", text=f"{m.ctx['holdco']} — Board Minutes")
    b.header_footer(position="footer", text=f"Meeting of {m.ctx['meeting_date']} — Confidential")

    metadata = _metadata(
        b, title="Minutes of a Meeting of the Board of Directors",
        subtitle=m.ctx["holdco"], issuer=_entity_ref(m.typed["holdco"]),
        reference=f"BM-{b.doc_scope}", version="1.0", classification="Confidential")
    return metadata, _structure_id("board_minutes", specs)


# ---------------------------------------------------------------------------
# case notes
# ---------------------------------------------------------------------------
@planner("case_notes")
def _plan_case_notes(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    sec = _sections(b, specs, headings, keys)
    m = b.matter

    b.prose(sec["matter"], "cn.matter", 2, per_block=2)
    b.prose(sec["matter"], "cn.parties", 2, per_block=2)
    _party_table(b, sec["matter"], fam.get("representative", []))
    b.fact_paragraphs(sec["matter"], fam.get("facility", []) +
                      fam.get("subsidiary", []) + fam.get("guarantee", []) +
                      fam.get("security", []) + fam.get("financial", []), per_block=3)

    if "procedure" in sec:
        b.prose(sec["procedure"], "cn.procedural", 3, per_block=3)
        b.fact_paragraphs(sec["procedure"], fam.get("order", []), per_block=2)
        b.paragraphs(sec["procedure"], _compose(
            b, 8, subjects=g.PROCEDURE_SUBJECTS, predicates=g.PROCEDURE_PREDICATES,
            qualifiers=g.PROCEDURE_QUALIFIERS, frames=g.PROCEDURE_FRAMES), per_block=3)

    b.prose(sec["chronology"], "cn.chronology", 2, per_block=2)
    _chronology_table(b, sec["chronology"], fam.get("proceeding", []) + fam.get("order", []))
    b.fact_paragraphs(sec["chronology"], fam.get("proceeding", []) + fam.get("order", []),
                      per_block=3)

    if "evidence" in sec:
        b.prose(sec["evidence"], "cn.evidence", 2, per_block=2)
        _evidence_table(b, sec["evidence"], fam.get("proceeding", []))
        b.note(sec["evidence"],
               "Admissibility of the internal report remains subject to the court's ruling.")

    b.prose(sec["submissions"], "cn.submission", 3, per_block=3)
    b.paragraphs(sec["submissions"], _compose(
        b, 10, subjects=g.SUBMISSION_SUBJECTS, predicates=g.SUBMISSION_PREDICATES,
        qualifiers=g.SUBMISSION_QUALIFIERS, frames=g.SUBMISSION_FRAMES), per_block=3)

    b.prose(sec["allegations"], "cn.evidence", 2, per_block=2)
    b.fact_paragraphs(sec["allegations"], fam.get("allegation", []) +
                      fam.get("finding", []) + fam.get("ownership", []) +
                      fam.get("director", []), per_block=3)

    if "next" in sec:
        b.prose(sec["next"], "cn.next", 3, per_block=3)
        b.bullet_list(sec["next"], [
            "The parties are to agree a consent order and submit it for approval.",
            "The defendant is to confirm whether it intends to apply for summary judgment.",
            "The claimant is to serve any supplementary witness statement by the date ordered.",
        ], ordered=True)
    b.flush_unused(sec["next"], per_block=3)
    b.header_footer(position="header", text=f"{m.ctx['holdco']} — Case Notes")
    b.header_footer(position="footer", text=f"Claim {m.ctx['case_number']} — Privileged and Confidential")

    metadata = _metadata(
        b, title="Case Notes", subtitle=f"{m.ctx['holdco']} v {m.ctx['counterparty']}",
        issuer=_entity_ref(m.typed["holdco"]), reference=m.ctx["case_number"],
        version="1.0", classification="Privileged and Confidential")
    return metadata, _structure_id("case_notes", specs)


# ---------------------------------------------------------------------------
# memorandum
# ---------------------------------------------------------------------------
@planner("memorandum")
def _plan_memorandum(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    sec = _sections(b, specs, headings, keys)
    m = b.matter

    b.prose(sec["summary"], "mm.summary", 3, per_block=3)
    b.fact_paragraphs(sec["summary"], fam.get("recommendation", []), per_block=2)
    b.bullet_list(sec["summary"], [
        "The guarantee is enforceable against each Guarantor on the basis of the corporate benefit recorded in the board minutes.",
        "The security is capable of registration and should be registered within 21 days.",
        "The financial covenants are within market norms, subject to the definition of EBITDA.",
        "The conditions precedent are capable of satisfaction before the first drawdown.",
    ])

    b.prose(sec["background"], "mm.background", 3, per_block=3)
    b.fact_paragraphs(sec["background"], fam.get("acquisition", []) + fam.get("facility", []) +
                      fam.get("outstanding", []), per_block=3)

    b.prose(sec["analysis"], "mm.analysis", 4, per_block=3)
    b.paragraphs(sec["analysis"], _compose(
        b, 12, subjects=g.ANALYSIS_SUBJECTS, predicates=g.ANALYSIS_PREDICATES,
        qualifiers=g.ANALYSIS_QUALIFIERS, frames=g.ANALYSIS_FRAMES), per_block=3)
    b.fact_paragraphs(sec["analysis"], fam.get("representation", []) +
                      fam.get("allegation", []) + fam.get("finding", []), per_block=3)

    if "relationships" in sec:
        b.prose(sec["relationships"], "mm.relationships", 2, per_block=2)
        _shareholding_table(b, sec["relationships"], fam.get("ownership", []),
                            caption="Ownership and control")
        b.fact_paragraphs(sec["relationships"], fam.get("guarantee", []) +
                          fam.get("security", []) + fam.get("advisor", []), per_block=3)

    b.prose(sec["financial"], "dd.financial", 2, per_block=2)
    _financial_table(b, sec["financial"], fam.get("financial", []))
    b.fact_paragraphs(sec["financial"], fam.get("financial", []), per_block=3)

    b.prose(sec["exceptions"], "mm.exceptions", 3, per_block=3)
    b.paragraphs(sec["exceptions"], _compose(
        b, 7, subjects=g.ANALYSIS_SUBJECTS, predicates=g.ANALYSIS_PREDICATES,
        qualifiers=g.ANALYSIS_QUALIFIERS, frames=g.ANALYSIS_FRAMES), per_block=3)

    b.prose(sec["recommendations"], "mm.summary", 2, per_block=2)
    b.fact_paragraphs(sec["recommendations"], fam.get("quote", []), per_block=2)

    if "exhibits" in sec:
        b.prose(sec["exhibits"], "mm.exhibit", 2, per_block=2)
        b.exhibit_block(sec["exhibits"], "Exhibit 1", "Facility agreement executed copy")
        b.exhibit_block(sec["exhibits"], "Exhibit 2", "Security documents and registration evidence")
    b.flush_unused(sec["recommendations"], per_block=3)
    b.header_footer(position="header", text=f"{m.ctx['holdco']} — Legal Memorandum")
    b.header_footer(position="footer", text="Privileged and Confidential — Prepared for the addressee only")

    metadata = _metadata(
        b, title="Memorandum of Advice", subtitle=f"Facility {m.ctx['facility_ref']}",
        issuer=_entity_ref(m.typed["holdco"]), addressee=_entity_ref(m.typed["lender"]),
        reference=f"MM-{b.doc_scope}", version="1.0", classification="Privileged")
    return metadata, _structure_id("memorandum", specs)


# ---------------------------------------------------------------------------
# press release
# ---------------------------------------------------------------------------
@planner("press_release")
def _plan_press_release(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    sec = _sections(b, specs, headings, keys)
    m = b.matter

    headline = paragraphs("pr.headline", b.ctx, b.rng, 1,
                              used=b.used_texts)[0]
    b.paragraphs(sec["announcement"], [headline], per_block=1)
    b.prose(sec["announcement"], "pr.announcement", 3, per_block=3)
    b.fact_paragraphs(sec["announcement"], fam.get("acquisition", []) +
                      fam.get("facility", []) + fam.get("outstanding", []), per_block=3)
    b.paragraphs(sec["announcement"], _compose(
        b, 6, subjects=g.MEDIA_SUBJECTS, predicates=g.MEDIA_PREDICATES,
        qualifiers=g.MEDIA_QUALIFIERS, frames=g.MEDIA_FRAMES), per_block=3)

    if "quotes" in sec:
        b.prose(sec["quotes"], "pr.status", 2, per_block=2)
        b.fact_paragraphs(sec["quotes"], fam.get("quote", []), per_block=2)

    if "financial" in sec:
        b.prose(sec["financial"], "pr.historical", 2, per_block=2)
        _financial_table(b, sec["financial"], fam.get("financial", []),
                         caption="Financial highlights")
        b.fact_paragraphs(sec["financial"], fam.get("financial", []), per_block=3)

    b.prose(sec["about"], "pr.boilerplate", 2, per_block=2)
    b.fact_paragraphs(sec["about"], fam.get("ownership", []) + fam.get("proceeding", []),
                      per_block=6)

    b.prose(sec["contact"], "pr.contact", 1, per_block=1)
    b.fact_paragraphs(sec["contact"], fam.get("forward", []), per_block=1)
    b.flush_unused(sec["about"], per_block=4)
    b.header_footer(position="header", text=f"{m.ctx['holdco']} — News Release")

    metadata = _metadata(
        b, title=headline, subtitle="News release",
        issuer=_entity_ref(m.typed["holdco"]),
        reference=f"PR-{b.doc_scope}", classification="Public")
    return metadata, _structure_id("press_release", specs)


# ---------------------------------------------------------------------------
# correspondence
# ---------------------------------------------------------------------------
@planner("correspondence")
def _plan_correspondence(b: DocBuilder, specs, headings):
    from . import grammar as g
    fam = _families(b)
    keys = [spec.key for spec in specs]
    if b.doc_id not in m_corrections(b):
        keys = [key for key in keys if key != "correction"]
    else:
        # a correction pushes the letter to seven sections; the optional thread
        # commentary is dropped so the letter stays within the §11 section range
        keys = [key for key in keys if key != "thread"] if len(keys) > 6 else keys
    sec = _sections(b, specs, headings, keys)
    m = b.matter
    meta = m.letter_meta[b.doc_id]
    letter_ctx = dict(b.ctx)
    if meta.in_reply_to is not None:
        parent = _meta_by_message_id(m, meta.in_reply_to)
        parent_doc = _doc_for_meta(m, parent)
        letter_ctx["parent_date"] = fmt_date(m.dates[parent_doc], 0)
    else:
        letter_ctx["parent_date"] = fmt_date(m.dates[b.doc_id], 0)
    saved = b.ctx
    b.ctx = letter_ctx

    subject = m.threads[0].subject if m.threads else "Facility correspondence"
    b.paragraphs(sec["subject"], [
        f"Subject: {subject}",
        f"Reference: {m.ctx['facility_ref']} / {meta.message_id}",
    ], per_block=2)

    if "thread" in sec:
        b.prose(sec["thread"], "co.thread", 2, per_block=2)

    b.prose(sec["position"], "co.opening", 2, per_block=2)
    b.prose(sec["position"], "co.position", 3, per_block=3)
    b.fact_paragraphs(sec["position"], b.fact_ids, per_block=4)
    b.paragraphs(sec["position"], _compose(
        b, 8, subjects=g.LETTER_SUBJECTS, predicates=g.LETTER_PREDICATES,
        qualifiers=g.LETTER_QUALIFIERS, frames=g.LETTER_FRAMES), per_block=3)

    b.prose(sec["request"], "co.request", 3, per_block=3)
    _outstanding_table(b, sec["request"], [fid for fid in b.fact_ids
                                           if m.fact_family[fid] == "outstanding"])

    if "correction" in sec and b.doc_id in m.typed.get("correction_docs", set()):
        b.prose(sec["correction"], "co.correction", 1, per_block=1)

    if "attachments" in sec:
        b.prose(sec["attachments"], "co.attachment", 1, per_block=1)
        b.exhibit_block(sec["attachments"], "Enclosure 1",
                        f"Compliance certificate and covenant calculation under facility "
                        f"{m.ctx['facility_ref']}",
                        exhibit_id=m.exhibits.get(b.doc_id))

    b.prose(sec["closing"], "co.closing", 2, per_block=2)
    sender = _entity_id_by_name(m, m.ctx["secretary"])
    b.signature_block(sec["closing"], [
        (sender, "company secretary", m.dates[b.doc_id].iso(), True)])
    b.flush_unused(sec["position"], per_block=4)
    b.header_footer(position="header", text=f"{m.ctx['holdco']} — {subject}")
    b.ctx = saved

    metadata = _metadata(
        b, title=f"Letter: {subject}", subtitle=m.ctx["facility_ref"],
        issuer=_entity_ref(m.typed["holdco"]), addressee=_entity_ref(m.typed["lender"]),
        reference=f"CO-{b.doc_scope}", classification="Confidential", thread=meta)
    return metadata, _structure_id("correspondence", specs)


# ---------------------------------------------------------------------------
# table and small helpers
# ---------------------------------------------------------------------------
def _entity_ref(entity_id: str | None):
    from synthetic_data.model import EntityRef
    if entity_id is None:
        raise ValueError("entity id required")
    return EntityRef(entity_id=entity_id)


def _entity_id_by_name(matter: Matter, name: str) -> str:
    for entity in matter.entities.values():
        if entity.canonical_name == name:
            return entity.entity_id
    raise KeyError(f"no entity named {name!r}")


def _meta_by_message_id(matter: Matter, message_id: str):
    for meta in matter.letter_meta.values():
        if meta.message_id == message_id:
            return meta
    raise KeyError(f"unknown message id {message_id}")


def _doc_for_meta(matter: Matter, meta) -> str:
    for doc_id, candidate in matter.letter_meta.items():
        if candidate.message_id == meta.message_id:
            return doc_id
    raise KeyError(f"message {meta.message_id} has no document")


def _definition_items(builder: DocBuilder, terms: list[str]) -> list[str]:
    items: list[str] = []
    for term in terms:
        if " means " in term:
            head, body = term.split(" means ", 1)
            items.append(f"{head.strip()}: {body.strip()}")
        else:
            items.append(term)
    return items


def _interest_example(builder: DocBuilder, principal: Money) -> str:
    rate_bp = int(builder.matter.typed["rate"].basis_points) + int(  # type: ignore[attr-defined]
        builder.matter.typed["margin"].basis_points)               # type: ignore[attr-defined]
    quarterly = principal.amount_minor * rate_bp // 10_000 // 4
    opening = principal.amount_minor // 8
    example = Money(amount_minor=opening, currency=principal.currency)
    interest = Money(amount_minor=quarterly, currency=principal.currency)
    return (f"For illustration, interest on an opening balance of {fmt_money(example, 1)} "
            f"for one quarter at {rate_bp / 100:.2f}% per annum would be "
            f"{fmt_money(interest, 1)}, calculated on a 365-day basis.")



def _with_extra_column(columns, header, body, totals, column, header_cell, factory):
    """Append an optional column — the §10.2 table-layout variation axis."""
    columns = [*columns, column]
    header = [[*header[0], header_cell]]
    body = [[*row, factory(index)] for index, row in enumerate(body)]
    totals = [[*row, factory(len(body) + index)]
              for index, row in enumerate(totals or [])]
    return columns, header, body, totals


def _shareholding_table(builder: DocBuilder, section: Section, fact_ids: list[str],
                        *, caption: str) -> None:
    shares = [share for share in builder.matter.shareholdings if share.fact_id in fact_ids]
    if not shares:
        return
    holdco = builder.matter.typed["holdco"]
    rows: list[list[CellSpec]] = []
    for share in shares:
        child = builder.matter.entities[share.child_id]
        rows.append([
            CellSpec(text=builder.matter.entities[holdco].canonical_name,
                     entity_ref=holdco),
            CellSpec(text=child.canonical_name, entity_ref=child.entity_id),
            CellSpec(value=share.percent, display=share.percent.display(),
                     number_format=percent_number_format(),
                     fact_ref=share.fact_id, entity_ref=child.entity_id),
            CellSpec(text=child.jurisdiction or builder.matter.ctx["jurisdiction"]),
        ])
    total = sum(share.percent.basis_points for share in shares)
    rows.append([
        CellSpec(text="Total"),
        CellSpec(text=""),
        CellSpec(value=Percentage(basis_points=total), display=Percentage(basis_points=total).display(),
                 number_format=percent_number_format()),
        CellSpec(text=""),
    ])
    columns = [ColumnSpec(name="Shareholder", type="text"),
               ColumnSpec(name="Subsidiary", type="text"),
               ColumnSpec(name="Holding", type="percent", align="right"),
               ColumnSpec(name="Jurisdiction", type="text")]
    header = [[CellSpec(text="Shareholder"), CellSpec(text="Subsidiary"),
               CellSpec(text="Holding"), CellSpec(text="Jurisdiction")]]
    if builder.rng.randrange(2):
        issued = 2_500_000
        columns, header, body_rows, total_rows = _with_extra_column(
            columns, header, rows[:-1], [rows[-1]],
            ColumnSpec(name="Shares", type="int", align="right"),
            CellSpec(text="Shares"),
            lambda index: CellSpec(value=issued, display=f"{issued:,}",
                                   number_format="#,##0")
            if index < len(rows[:-1]) else CellSpec(text=""))
    else:
        body_rows, total_rows = rows[:-1], [rows[-1]]
    builder.add_table(section, caption=caption, columns=columns, header=header,
                      body=body_rows, totals=total_rows)


def _transaction_rows(builder: DocBuilder, fact_ids: list[str], kinds: tuple[str, ...]):
    wanted = set(fact_ids)
    return [txn for txn in builder.matter.transactions.values()
            if txn.fact_id in wanted and txn.kind in kinds]


def _drawdown_table(builder: DocBuilder, section: Section, fact_ids: list[str],
                    *, caption: str = "Drawdown schedule") -> None:
    txns = _transaction_rows(builder, fact_ids, ("drawdown", "disbursement"))
    if not txns:
        return
    rows = []
    for index, txn in enumerate(txns, start=1):
        rows.append([
            CellSpec(text=str(index)),
            CellSpec(value=txn.date, display=fmt_date(txn.date, 0),
                     number_format=date_number_format()),
            _money_cell(txn.amount, fact_ref=txn.fact_id,
                        entity_ref=txn.to_entity.entity_id),
            CellSpec(text=f"Account {index:03d}"),
        ])
    total = sum(txn.amount.amount_minor for txn in txns)
    rows.append([
        CellSpec(text="Total"),
        CellSpec(text=""),
        CellSpec(value=Money(amount_minor=total, currency=txns[0].amount.currency),
                 display=fmt_money(Money(amount_minor=total, currency=txns[0].amount.currency), 1),
                 number_format=money_number_format()),
        CellSpec(text=""),
    ])
    columns = [ColumnSpec(name="Tranche", type="text"),
               ColumnSpec(name="Date", type="date"),
               ColumnSpec(name="Amount", type="money", align="right"),
               ColumnSpec(name="Account", type="text")]
    header = [[CellSpec(text="Tranche"), CellSpec(text="Date"),
               CellSpec(text="Amount"), CellSpec(text="Account")]]
    if builder.rng.randrange(2):
        columns, header, body_rows, total_rows = _with_extra_column(
            columns, header, rows[:-1], [rows[-1]],
            ColumnSpec(name="Reference", type="text"),
            CellSpec(text="Reference"),
            lambda index: CellSpec(text=f"DD-{index + 1:02d}")
            if index < len(rows[:-1]) else CellSpec(text=""))
    else:
        body_rows, total_rows = rows[:-1], [rows[-1]]
    builder.add_table(section, caption=caption, columns=columns, header=header,
                      body=body_rows, totals=total_rows)


def _repayment_table(builder: DocBuilder, section: Section, fact_ids: list[str],
                     *, caption: str = "Repayment schedule") -> None:
    order = builder.matter.typed["repayment_facts"]
    rows_data = builder.matter.typed["repayment_rows"]
    index = {fact_id: position for position, fact_id in enumerate(order)}
    selected = [index[fid] for fid in fact_ids if fid in index]
    if not selected:
        return
    rows: list[list[CellSpec]] = []
    for position in selected:
        row = rows_data[position]
        rows.append([
            CellSpec(text=str(row["instalment"])),
            CellSpec(value=IsoDate(value=row["due"]), display=fmt_date(IsoDate(value=row["due"]), 0),
                     number_format=date_number_format()),
            _money_cell(row["principal"], fact_ref=order[position]),
            _money_cell(row["interest"]),
            _money_cell(row["balance"]),
        ])
    total = sum(row["principal"].amount_minor for row in (rows_data[p] for p in selected))
    rows.append([
        CellSpec(text="Total"), CellSpec(text=""),
        CellSpec(value=Money(amount_minor=total, currency=rows_data[selected[0]]["principal"].currency),
                 display=fmt_money(Money(amount_minor=total,
                                         currency=rows_data[selected[0]]["principal"].currency), 1),
                 number_format=money_number_format()),
        CellSpec(text=""),
        CellSpec(text=""),
    ])
    columns = [ColumnSpec(name="Instalment", type="text"),
               ColumnSpec(name="Due date", type="date"),
               ColumnSpec(name="Principal", type="money", align="right"),
               ColumnSpec(name="Interest", type="money", align="right"),
               ColumnSpec(name="Balance", type="money", align="right")]
    header = [[CellSpec(text="Instalment"), CellSpec(text="Due date"),
               CellSpec(text="Principal"), CellSpec(text="Interest"),
               CellSpec(text="Balance")]]
    if builder.rng.randrange(2):
        cumulative = 0
        values = []
        for position in selected:
            cumulative += rows_data[position]["principal"].amount_minor
            values.append(Money(amount_minor=cumulative,
                                currency=rows_data[position]["principal"].currency))
        columns, header, body_rows, total_rows = _with_extra_column(
            columns, header, rows[:-1], [rows[-1]],
            ColumnSpec(name="Cumulative", type="money", align="right"),
            CellSpec(text="Cumulative"),
            lambda index: _money_cell(values[index]) if index < len(values)
            else CellSpec(text=""))
    else:
        body_rows, total_rows = rows[:-1], [rows[-1]]
    builder.add_table(section, caption=caption, columns=columns, header=header,
                      body=body_rows, totals=total_rows)


def _security_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    facts = [builder.matter.facts[fid] for fid in fact_ids
             if builder.matter.facts[fid].relation == "grants_security_over"]
    if not facts:
        return
    rows = []
    for index, fact in enumerate(facts, start=1):
        asset = builder.matter.entities[fact.object.entity_id]
        rows.append([
            CellSpec(text=asset.canonical_name, entity_ref=asset.entity_id,
                     fact_ref=fact.fact_id),
            CellSpec(text=f"Fixed charge {index:02d}"),
            CellSpec(text="First ranking"),
        ])
    columns = [ColumnSpec(name="Asset", type="text"),
               ColumnSpec(name="Charge", type="text"),
               ColumnSpec(name="Ranking", type="text")]
    header = [[CellSpec(text="Asset"), CellSpec(text="Charge"),
               CellSpec(text="Ranking")]]
    body_rows = rows
    if builder.rng.randrange(2):
        base = builder.matter.typed["principal"]
        columns, header, body_rows, _ = _with_extra_column(
            columns, header, rows, [],
            ColumnSpec(name="Value", type="money", align="right"),
            CellSpec(text="Value"),
            lambda index: _money_cell(Money(
                amount_minor=base.amount_minor // (index + 2), currency=base.currency)))
    builder.add_table(section, caption="Transaction Security", columns=columns,
                      header=header, body=body_rows)


def _covenant_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    facts = [builder.matter.facts[fid] for fid in fact_ids]
    if not facts:
        return
    rows = []
    for fact in facts:
        rows.append([
            CellSpec(text=_first_upper(fact.text or ""), fact_ref=fact.fact_id,
                     entity_ref=fact.speaker.entity_id if fact.speaker else None),
            CellSpec(text=builder.matter.ctx["leverage"]),
            CellSpec(text="Compliant" if fact.status == "found" else "Under review"),
        ])
    columns = [ColumnSpec(name="Covenant", type="text"),
               ColumnSpec(name="Threshold", type="text"),
               ColumnSpec(name="Status", type="text")]
    header = [[CellSpec(text="Covenant"), CellSpec(text="Threshold"),
               CellSpec(text="Status")]]
    body_rows = rows
    if builder.rng.randrange(2):
        tested = builder.matter.ctx["as_at"]
        columns, header, body_rows, _ = _with_extra_column(
            columns, header, rows, [],
            ColumnSpec(name="Tested at", type="text"),
            CellSpec(text="Tested at"),
            lambda index: CellSpec(text=tested))
    builder.add_table(section, caption="Financial covenants", columns=columns,
                      header=header, body=body_rows)


def _director_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    facts = [builder.matter.facts[fid] for fid in fact_ids
             if builder.matter.facts[fid].relation == "is_director_of"]
    if not facts:
        return
    rows = []
    for fact in facts:
        person = builder.matter.entities[fact.subject.entity_id]
        company = builder.matter.entities[fact.object.entity_id]
        rows.append([
            CellSpec(text=person.canonical_name, entity_ref=person.entity_id,
                     fact_ref=fact.fact_id),
            CellSpec(text=company.canonical_name, entity_ref=company.entity_id),
            CellSpec(text="Appointed"),
        ])
    builder.add_table(
        section, caption="Directors and appointments",
        columns=[ColumnSpec(name="Director", type="text"),
                 ColumnSpec(name="Company", type="text"),
                 ColumnSpec(name="Status", type="text")],
        header=[[CellSpec(text="Director"), CellSpec(text="Company"),
                 CellSpec(text="Status")]],
        body=rows)


def _attendance_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    m = builder.matter
    facts = [m.facts[fid] for fid in fact_ids]
    rows = []
    for index, fact in enumerate(facts):
        person = m.entities[fact.subject.entity_id]
        status = "Present" if index < len(facts) - 1 else "Apologies"
        rows.append([
            CellSpec(text=person.canonical_name, entity_ref=person.entity_id,
                     fact_ref=fact.fact_id),
            CellSpec(text="Director"),
            CellSpec(text=status),
            CellSpec(value=index + 1, display=str(index + 1),
                     number_format="#,##0"),
        ])
    columns = [ColumnSpec(name="Director", type="text"),
               ColumnSpec(name="Role", type="text"),
               ColumnSpec(name="Status", type="text"),
               ColumnSpec(name="Entitlement", type="int", align="right")]
    header = [[CellSpec(text="Director"), CellSpec(text="Role"),
               CellSpec(text="Status"), CellSpec(text="Entitlement")]]
    body_rows = rows
    if builder.rng.randrange(2):
        columns, header, body_rows, _ = _with_extra_column(
            columns, header, rows, [],
            ColumnSpec(name="Apologies", type="text"),
            CellSpec(text="Apologies"),
            lambda index: CellSpec(text="None" if index else "Received"))
    builder.add_table(section, caption="Attendance", columns=columns,
                      header=header, body=body_rows)


def _resolution_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    m = builder.matter
    facts = [m.facts[fid] for fid in fact_ids]
    if not facts:
        return
    rows = []
    for index, fact in enumerate(facts, start=1):
        rows.append([
            CellSpec(text=f"{index:02d}"),
            CellSpec(text=_first_upper(fact.text or ""), fact_ref=fact.fact_id),
            CellSpec(text=m.ctx["chair"]),
            CellSpec(value=6, display="6", number_format="#,##0"),
            CellSpec(text=fact.status.capitalize()),
        ])
    columns = [ColumnSpec(name="Item", type="text"),
               ColumnSpec(name="Resolution", type="text"),
               ColumnSpec(name="Proposed by", type="text"),
               ColumnSpec(name="Votes for", type="int", align="right"),
               ColumnSpec(name="Outcome", type="text")]
    header = [[CellSpec(text="Item"), CellSpec(text="Resolution"),
               CellSpec(text="Proposed by"), CellSpec(text="Votes for"),
               CellSpec(text="Outcome")]]
    body_rows = rows
    if builder.rng.randrange(2):
        columns, header, body_rows, _ = _with_extra_column(
            columns, header, rows, [],
            ColumnSpec(name="Seconded by", type="text"),
            CellSpec(text="Seconded by"),
            lambda index: CellSpec(text=builder.matter.ctx["director1"]))
    builder.add_table(section, caption="Resolutions and voting", columns=columns,
                      header=header, body=body_rows)


def _financial_table(builder: DocBuilder, section: Section, fact_ids: list[str],
                     *, caption: str = "Financial summary") -> None:
    m = builder.matter
    facts = [m.facts[fid] for fid in fact_ids]
    if not facts:
        return
    rows = []
    for fact in facts:
        year = fact.effective.start.value.year if fact.effective and fact.effective.start else m.ctx["period_end"]
        label = "Revenue" if "revenue" in (fact.text or "") else (
            "EBITDA" if "EBITDA" in (fact.text or "") else "Net debt")
        rows.append([
            CellSpec(text=str(year)),
            CellSpec(text=label),
            _money_cell(fact.amount, fact_ref=fact.fact_id),
            CellSpec(text="—"),
        ])
    columns = [ColumnSpec(name="Year", type="text"),
               ColumnSpec(name="Measure", type="text"),
               ColumnSpec(name="Amount", type="money", align="right"),
               ColumnSpec(name="Change", type="text")]
    header = [[CellSpec(text="Year"), CellSpec(text="Measure"),
               CellSpec(text="Amount"), CellSpec(text="Change")]]
    body_rows = rows
    if builder.rng.randrange(2):
        columns = columns[:-1]
        header = [header[0][:-1]]
        body_rows = [row[:-1] for row in rows]
    builder.add_table(section, caption=caption, columns=columns, header=header,
                      body=body_rows)


def _party_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    m = builder.matter
    facts = [m.facts[fid] for fid in fact_ids]
    if not facts:
        return
    rows = []
    for fact in facts:
        actor = m.entities[fact.subject.entity_id]
        party = m.entities[fact.object.entity_id]
        rows.append([
            CellSpec(text=party.canonical_name, entity_ref=party.entity_id),
            CellSpec(text=actor.canonical_name, entity_ref=actor.entity_id,
                     fact_ref=fact.fact_id),
            CellSpec(text="Representative"),
            CellSpec(text="Filed" if "filed" in actor.canonical_name else "Appointed"),
        ])
    court_id = _entity_id_by_name(m, m.ctx["court"])
    judge_id = _entity_id_by_name(m, m.ctx["judge"])
    rows.append([
        CellSpec(text=m.ctx["court"], entity_ref=court_id),
        CellSpec(text=m.ctx["judge"], entity_ref=judge_id),
        CellSpec(text="Court"),
        CellSpec(text="Presiding"),
    ])
    rows.append([
        CellSpec(text=m.ctx["holdco"], entity_ref=m.typed["holdco"]),
        CellSpec(text=m.ctx["secretary"], entity_ref=_entity_id_by_name(m, m.ctx["secretary"])),
        CellSpec(text="Company secretary"),
        CellSpec(text="Instructing"),
    ])
    columns = [ColumnSpec(name="Party", type="text"),
               ColumnSpec(name="Representative", type="text"),
               ColumnSpec(name="Role", type="text"),
               ColumnSpec(name="Status", type="text")]
    header = [[CellSpec(text="Party"), CellSpec(text="Representative"),
               CellSpec(text="Role"), CellSpec(text="Status")]]
    body_rows = rows
    if builder.rng.randrange(2):
        columns, header, body_rows, _ = _with_extra_column(
            columns, header, rows, [],
            ColumnSpec(name="Capacity", type="text"),
            CellSpec(text="Capacity"),
            lambda index: CellSpec(text="Solicitor" if index % 2 == 0 else "Counsel"))
    builder.add_table(section, caption="Parties and representation", columns=columns,
                      header=header, body=body_rows)


def _chronology_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    m = builder.matter
    facts = [m.facts[fid] for fid in fact_ids if m.facts[fid].effective
             and m.facts[fid].effective.start]
    if not facts:
        return
    rows = []
    for fact in sorted(facts, key=lambda f: f.effective.start.value):
        actor = m.entities[fact.subject.entity_id].canonical_name
        target = m.entities[fact.object.entity_id].canonical_name
        rows.append([
            CellSpec(value=fact.effective.start, display=fmt_date(fact.effective.start, 0),
                     number_format=date_number_format()),
            CellSpec(text=f"{actor} {fact.relation.replace('_', ' ')} {target}",
                     fact_ref=fact.fact_id),
            CellSpec(text="Claim file"),
        ])
    builder.add_table(
        section, caption="Chronology",
        columns=[ColumnSpec(name="Date", type="date"),
                 ColumnSpec(name="Event", type="text"),
                 ColumnSpec(name="Source", type="text")],
        header=[[CellSpec(text="Date"), CellSpec(text="Event"),
                 CellSpec(text="Source")]],
        body=rows)


def _evidence_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    m = builder.matter
    entries = [
        ("Claim form and particulars of claim", "Served", "Admitted"),
        ("Defence and counterclaim", "Filed", "Admitted"),
        ("Internal report dated " + m.ctx["as_at"], "Disclosed", "Objected to"),
        ("Correspondence bundle", "Agreed", "Admitted"),
        ("Expert report on quantum", "Pending", "Not yet exchanged"),
        ("Witness statements", "Pending", "Not yet exchanged"),
    ]
    rows = []
    for description, status, admissibility in entries:
        rows.append([
            CellSpec(text=description),
            CellSpec(text=status),
            CellSpec(text=admissibility),
            CellSpec(text=m.ctx["counsel"]),
        ])
    builder.add_table(
        section, caption="Evidence register",
        columns=[ColumnSpec(name="Document", type="text"),
                 ColumnSpec(name="Status", type="text"),
                 ColumnSpec(name="Admissibility", type="text"),
                 ColumnSpec(name="Source", type="text")],
        header=[[CellSpec(text="Document"), CellSpec(text="Status"),
                 CellSpec(text="Admissibility"), CellSpec(text="Source")]],
        body=rows)


def _outstanding_table(builder: DocBuilder, section: Section, fact_ids: list[str]) -> None:
    m = builder.matter
    facts = [m.facts[fid] for fid in fact_ids]
    if not facts:
        return
    rows = []
    for fact in facts:
        rows.append([
            CellSpec(text="Outstanding balance"),
            CellSpec(value=fact.effective.start, display=fmt_date(fact.effective.start, 0),
                     number_format=date_number_format()),
            _money_cell(fact.amount, fact_ref=fact.fact_id),
        ])
    builder.add_table(
        section, caption="Amounts outstanding",
        columns=[ColumnSpec(name="Item", type="text"),
                 ColumnSpec(name="As at", type="date"),
                 ColumnSpec(name="Amount", type="money", align="right")],
        header=[[CellSpec(text="Item"), CellSpec(text="As at"),
                 CellSpec(text="Amount")]],
        body=rows)
