# V3 Canonical Document Model — Architecture and Design

**Status:** design only. No generator, renderer, schema or dataset is implemented in this phase.
**Branch:** `v3-multiformat` (created from clean `main` at `9758f30`; nothing staged, committed or pushed).
**Date:** 2026-11-22
**Revision 2 (2026-11-22):** architecture approved; decisions DEC-1–DEC-7 resolved by review (§16.2) and propagated through
the model, provenance, alignment, XLSX, gold-versioning and phase sections. Remaining blockers are listed in §16.3.
**Revision 3 (2026-11-22):** Phase A closure amendments DEC-8–DEC-11 (§4.2, §4.4, §5.1, §7.5, §16.2): corrected the id
formula, added optional canonical sentence references, made nested tables an explicit error, and made cell types
authoritative over optional column expectations.
**Evidence base:** the V2 source tree (`synthetic_data/generator/*`, `synth/*`), the measured V2 corpora, and the two completed audits:

- `sdg-audit/results/V2_DOCUMENT_DEPTH_REPORT.md` — 2,282 documents, 6,520 pages, all four corpora.
- `sdg-audit/results/AUDIT_REPORT.md` — Docling/multi-format ingestion feasibility, findings D1–D15.

Every current-behaviour claim below is taken from those sources; no generator behaviour is inferred or invented.

---

## 1. Executive summary

V2 is internally consistent and fully validated, but it is a **sentence list rendered to plain text**. The
`DocumentPlan` that drives generation holds `page_lines: list[list[str]]` and `sentences: list[str]` — there is no
section tree, no paragraph object, no table object, no cell, and no place to record that two statements describe the
same fact. Consequences are measured, not speculative: a median document is 234 words across 2.86 nominal pages,
86.7 words per page, with zero tables in 2,282 documents and 98.81% cross-corpus sentence-template duplication at
5,000 documents.

Gold annotation today depends on an exact one-to-one sentence match: `_canonical_sentence_index()` compares the
planned sentence list to the ingested `DocView` sentence list element by element and raises `GoldGenerationError`
on any difference. That is a strength for TXT — it is why 78,268 gold items validate `EXACT` — and it is precisely
the assumption that breaks when the same content is rendered to PDF, DOCX and XLSX. The ingestion audit measured
identical content yielding 19 sentences as PDF, 19 as DOCX, 11 as Markdown/HTML, 16 as PPTX and 10 as XLSX (D14).

V3 therefore separates three things V2 conflates:

1. **What the document says** — a canonical semantic layer (entities, facts, events, transactions, propositions,
   conditions, contradictions) that exists once per corpus and is never a function of a file format.
2. **How it is laid out** — a format-independent canonical content tree (sections, paragraphs, lists, tables,
   cells, notes, signatures).
3. **How it is written to bytes** — renderers that realize the canonical tree for TXT, PDF, DOCX and XLSX (a
   document kind declares which apply, §7.7) and emit a machine-readable map from canonical node ids to output
   coordinates.

Gold is then built from **parser-validated evidence**, not from the generator's intention: align canonical nodes to
parser sentences with an explicit many-to-many alignment record, take the quotation from the parser's canonical
text, and reject any fact whose alignment is missing, ambiguous or altered.

The central architectural decision is that **stable IDs are allocated in the canonical layer and never by a
renderer or parser**. A transaction keeps one id whether it appears in a repayment table, a paragraph and an XLSX
row; the renderer records where each occurrence went; the aligner records which parser sentence each occurrence
became. Format is a projection, not an identity.

---

## 2. Current V2 limitations, with the measurements that establish them

### 2.1 Measured depth and density

All figures from `V2_DOCUMENT_DEPTH_REPORT.md` (n = 2,282 documents, 6,520 logical pages, 7 types × 4 corpora).

| measure | mean | median | min | p5 | p25 | p75 | p95 | max |
|---|---|---|---|---|---|---|---|---|
| words / document | 244.8 | **234** | 203 | 208 | 221 | 264 | 317 | 326 |
| characters / document | 1,632.9 | 1,571.5 | 1,300 | 1,369 | 1,456 | 1,751 | 2,101 | 2,211 |
| sentences / document | 26.6 | **25** | 24 | 24 | 25 | 28 | 31 | 32 |
| paragraphs / document | 26.6 | 25 | 24 | 24 | 25 | 28 | 31 | 32 |
| logical pages / document | 2.86 | 3 | 2 | 2 | 3 | 3 | 3 | 3 |
| words / page | 86.7 | 81.7 | 67.7 | 69.7 | 76.0 | 100.3 | 110.0 | 113.0 |
| characters / page | 579.0 | 553.3 | 433.3 | 456.7 | 508.1 | 671.3 | 742.0 | 787.0 |
| sentences / page | 9.45 | 9.33 | 8.0 | 8.0 | 8.33 | 10.33 | 12.5 | 12.5 |
| words / sentence | 9.19 | 9.21 | 7.96 | 8.18 | 8.68 | 9.65 | 10.23 | 10.52 |

A typeset business page holds roughly 500 words and 25–30 sentences. V2 delivers **17% of that word density and
33% of that sentence density**, while claiming three pages.

### 2.2 Structural limitations

| limitation | measurement | architectural cause |
|---|---|---|
| No tables anywhere | 0 `table_row` blocks in 2,282 documents; 0 TAB characters, 0 pipe rows, 0 CSV rows; 0 structured records | `DocumentPlan` has no table type; the renderer writes lines only |
| Paragraphs are sentences | paragraphs / document = sentences / document = 25 (exact 1:1, 9.3 per page) | blocks are emitted one sentence each; no paragraph object exists |
| Page breaks are synthetic | 4,238 page breaks, **76.92% mid-section**; 8–13 blocks per page regardless of content | pagination is a fixed page-line budget (`page_counts_v2` → 3/3/3/3/3/3/2 pages) |
| ~half of all lines are blank | blank share **51.03%** of physical lines; 1.82 physical lines per sentence | one blank separator between every sentence |
| Lines are not wrapped | max content line 184 chars, prose mean 73.7 chars | no wrap width is applied; the "60-line" impression is separators, not wrapping |
| Single format | 100% `.txt` in all four frozen corpora | renderer is `plan.render_text()` joining `page_lines` with `\f` |
| Length is a constant | within-type p5→p95 span 16–23 words; corpus range 203–326 (1.6×) | length is decided by page budget, not by content need |
| Depth does not scale with corpus | physical-line mean 49.57 → 48.43 across 7 → 1,750 documents | corpus size changes count only |

Per-type medians (pooled, n = 326 each):

| type | words | sentences | paragraphs | pages | words/page | entities | facts | facts/100w |
|---|---|---|---|---|---|---|---|---|
| board_minutes | 304 | 31 | 31 | 3 | 101.3 | 18 | 14 | 4.61 |
| memorandum | 267 | 28 | 28 | 3 | 89.0 | 16 | 13 | 4.87 |
| correspondence | 236 | 25 | 25 | 3 | 78.7 | 17 | 13 | 5.51 |
| due_diligence | 231 | 28 | 28 | 3 | 77.0 | 13 | 12 | 5.19 |
| case_notes | 224 | 25 | 25 | 3 | 74.7 | 11 | 11 | 4.93 |
| press_release | 212 | 25 | 25 | 2 | 106.0 | 14 | 11 | 5.19 |
| financial_agreement | 210 | 24 | 24 | 3 | 70.0 | 10 | 8 | 3.81 |

### 2.3 Repetition

| corpus | docs | sentences | unique templates | exact dup % | template dup % | boilerplate % | max docs sharing one sentence |
|---|---|---|---|---|---|---|---|
| pilot_20_v2 | 7 | 190 | 104 | 3.68 | 45.26 | 59.47 | 7 |
| corpus_500_v2 | 175 | 4,654 | 180 | 35.45 | 96.13 | 98.82 | 175 |
| corpus_1000_v2 | 350 | 9,304 | 259 | 36.36 | 97.22 | 98.85 | 350 |
| corpus_5000_v2 | 1,750 | 46,504 | 552 | 38.92 | **98.81** | 99.53 | 1,750 |

Within a document, duplication is ~0 (0.00% exact, 0.27% fuzzy). The repetition is **cross-document**: 552
templates cover 46,504 sentences, and the most common template (`<bank> lent <company> <amount> on <num> <month>
<num>`) appears 2,250 times across all 1,750 documents. Facts are not redundant — 17,460 distinct triples, 1.64%
recurring in two or more documents — there are simply very few per document (12) drawn from ~11 distinct triples.

### 2.4 Gold and QA coupling

- Gold evidence is `(sentence_id, quote)` where `sentence_id` comes from an **element-wise equality check** between
  planned and ingested sentences (`annotations.py::_canonical_sentence_index`). Any splitter or block change fails
  the build loudly — correct behaviour, but it makes the gold path TXT-only in practice.
- 78,268 gold items across 5,000 documents validate `EXACT` with 0 sentence mismatches, 0 not-found, 0 errors.
- 23,408 QA items at 5,000 documents (13.38 per document), of which 6,160 are unanswerable.
- `synth.eval.gold.validate()` treats `SENTENCE_MISMATCH` as a soft warning, so the generator wraps it in
  `assert_gold_valid()` to make any mismatch fatal.
- Gold is generated, not blind, and never promoted. That distinction must survive V3 unchanged.

### 2.5 What the ingestion audit already tells us about V3

From `AUDIT_REPORT.md` (14 fixtures end-to-end through `u01_parser.ingest_path`), the constraints V3 must design
around rather than wish away:

| id | finding | design consequence |
|---|---|---|
| D1 | `.json` advertised but `FAILED` | do not advertise JSON output |
| D2 | `.xml` `SKIPPED` → `EMPTY` with the reason discarded | do not advertise XML; V3 reports unsupported inputs explicitly |
| D3 | HTML duplicates the table header row | do not use HTML as an evaluation cohort without a dedup rule |
| D4 | DOCX splits `Label: value` into two items | field-level gold cannot assume one parser block per field |
| D5 | PPTX body text labelled `list_item` | PPTX out of scope for V3 |
| D6 | XLSX loses currency, separators and number formats | numeric fidelity must be checked on raw values, not display strings |
| D7 | PDF tables are not tables (0 table items; pipe-free tables flatten to one cell per line) | PDF table gold must not assume row structure exists |
| D8 | PDF headings depend on a text heuristic | section recovery from PDF is best-effort, never guaranteed |
| D9 | `standard` pipeline and OCR need torch + ~2.5 GB | no OCR in the V3 baseline |
| D10 | allowlist and backend disagree in both directions | supported-format matrix must be explicit per cohort |
| D11 | `.docx`, `.md`, `.html`, `.csv` get one synthetic page | never fabricate page numbers for pageless formats |
| D12 | document date can come from file metadata | dates for gold come from text evidence, not metadata |
| D13 | corrupt PDF page tree cannot be recovered | fixture cohort must include a damaged file |
| D14 | canonical text is not format-invariant (19/19/11/16/10 sentences) | alignment must be many-to-many, not positional |
| D15 | unquoted CSV commas split cells | CSV is not a V3 target format |

---

## 3. Canonical document model

### 3.1 Three layers, one identity space

```text
L1  SEMANTIC LAYER            CorpusWorld
    entities, facts, events, transactions, propositions,
    conditions, contradictions, cross-references
    (format-independent, document-independent, stable ids)

L2  CONTENT LAYER             CdmDocument
    metadata, section tree, blocks (paragraph/list/table/note/
    signature/exhibit), inline runs
    (format-independent, document-scoped, stable ids)

L3  RENDER LAYER              RenderResult + RenderMap
    bytes for txt/pdf/docx/xlsx, plus canonical-node -> output-location map
    (format-specific, disposable, reproducible from L1+L2)
```

Rules that make the model work:

1. **Facts live in L1 only.** Text is never the source of truth for a fact. A document *states* facts; it does not
   own them.
2. **Every L2 node carries the ids of the L1 objects it realises** (`fact_refs`, `entity_refs`, `event_refs`,
   `transaction_refs`). This is what lets a table row and a paragraph refer to the same transaction.
3. **Every L1 fact that is stated anywhere must have at least one `FactStatement`** naming the L2 node that states
   it. A fact with no statement is a planning error; a statement with no fact is a validation error.
4. **Values are typed objects, not strings.** `Money(amount_minor, currency)`, `IsoDate`, `Percentage(basis_points)`,
   `EntityRef(id)`. Renderers format them; they never parse them.
5. **The renderer cannot change semantics.** It receives an immutable `CdmDocument` and returns bytes plus a map.
6. **Ids are allocated in L1/L2 before any rendering**, so the same content rendered twice — or to four formats —
   keeps one identity.
7. **One semantic source of truth.** There is exactly one `CorpusWorld` per corpus and exactly one `CdmDocument`
   per document; renderers receive them as immutable inputs and have no write path. A fact referenced from a
   paragraph, a table row, an XLSX cell and a second document is the **same `fact_id`** — location changes, identity
   does not.
8. **Facts, statements and occurrences are different objects** with different lifetimes: a fact is corpus-scoped, a
   statement is document-scoped, an occurrence is artifact-scoped. Collapsing any two of them is the V2 defect
   (§3.2).

### 3.2 Fact vs statement vs occurrence

This is the distinction V2 lacks and V3 needs.

```text
SemanticFact  fact:edge:00042:07        "Alpha Holdings acquired Beta Trading for GBP 4,000,000 on 3 March 2024"
   |
   +-- FactStatement  stmt:00042:07.a   realised in a paragraph (block blk:00042:03.02)
   +-- FactStatement  stmt:00042:07.b   realised in table row  tbl:00042:02/r03
   |
   +-- RenderingOccurrence  (per format)
         txt  : page 2, line 14, chars 0..96
         pdf  : physical page 2, bbox [72, 410, 523, 438]
         docx : para 41, run 0..3
         xlsx : sheet "Transactions", row 14, col B..D
```

One fact, two statements, four renderings, one id. A change to the amount changes `SemanticFact.amount` once; every
statement and every rendering is regenerated from it; a stale copy cannot exist because there is nothing to copy.

### 3.3 What the canonical model must be able to say

Content (all format-independent):

- metadata: document id, type, title, subtitle, author/issuer, addressee, jurisdiction, document date, reference,
  version, classification, page-count *intent* (not a page number), language, register/formality.
- section tree with depth, numbering scheme (`1.`, `1.1`, `(a)`, none), heading text, optional summary.
- blocks: `paragraph` (with inline runs), `list` (ordered/unordered, items with nesting), `table` (header, rows,
  cells with spans), `note` (footnote/endnote anchor), `signature` block, `exhibit`/`appendix` wrapper, `page_break`
  (an explicit instruction, not a coordinate), `header`, `footer`.
- inline runs: text, emphasis, entity reference, cross-reference, footnote marker, monetary/date/percent value.
- tables: caption, header rows, column types (text/money/date/percent/int), column alignment, totals rows,
  cell `rowspan`/`colspan`, per-cell source fact/entity refs.
- signatures: signatory entity, capacity/role, date, wet/digital marker, witness.
- appendices/exhibits: label, title, referenced-from anchors.

Semantics (all content-independent):

- entities with stable ids, type/subtype, aliases, attributes, jurisdiction.
- edges (subject, relation, object) with negation, nesting, modality, time role.
- propositions (text, speaker, reporter, status, polarity).
- events (participants, date, place, kind).
- transactions (from, to, amount, date, status, instrument, linked fact).
- conditions and exceptions (`provided_that`, `unless`, `subject_to`, `notwithstanding`) as first-class objects with
  a scope: which obligation/fact they constrain.
- temporal information: instant/interval/period/duration/relative, anchor, granularity, fiscal-calendar caveats.
- contradictions: two fact ids, same event, genuinely different values, plus the documents that state each side.
- cross-document references: typed links (`amends`, `supersedes`, `responds_to`, `exhibits`, `repeats`,
  `contradicts`, `supports`) between documents and between specific facts.
- evidence/provenance: per statement, the intended textual span and the L2 node; per occurrence, the render
  location and later the parser location.

### 3.4 Deliberate non-goals

- No layout engine. The canonical model describes structure, not coordinates. Coordinates belong to renderers.
- No free-text authoring. Every sentence is generated from a statement or a structural element.
- No OCR path, no image content, no scanned output in V3.
- No guarantee that a PDF preserves table structure — the model can *express* a table; the environment's parser
  may still flatten it (D7). Alignment and evaluation must cope with that, not assume it away.
- No prose-in-spreadsheet rendering. A workbook is a workbook; prose documents are not projected into XLSX (§7.7).

---

## 4. Proposed data structures and relationships

Illustrative schemas only. They are not implemented, not placed in `synthetic_data/schemas/`, and carry no imports.

### 4.1 Semantic layer

```python
# --- values (typed, comparable, format-free) -------------------------------
class Money:            amount_minor: int; currency: str  # "GBP"
class Percentage:       basis_points: int                  # 350 = 3.50%
class IsoDate:          value: date; precision: str        # day|month|year
class EntityRef:        entity_id: str
class DocumentRef:      document_id: str; version: int | None

# --- entities -------------------------------------------------------------
class Entity:
    entity_id: str            # ent:company:0017
    entity_type: str          # ontology class
    subtype: str | None
    canonical_name: str
    aliases: list[str]
    attributes: dict[str, str]
    jurisdiction: str | None

# --- facts ----------------------------------------------------------------
class SemanticFact:
    fact_id: str              # fact:edge:00042:07
    kind: Literal["edge", "proposition"]
    subject: EntityRef | None
    relation: str | None
    object: EntityRef | None
    status: str               # completed|proposed|alleged|held|denied|...
    negated: bool
    nested: bool
    modality: str | None      # may|shall|must|intends_to
    effective: TimeScope | None
    amount: Money | None
    text: str | None          # propositions
    speaker: EntityRef | None
    reporter: EntityRef | None
    conditions: list[str]     # condition ids
    source_event: str | None

class Condition:
    condition_id: str
    kind: Literal["provided_that", "unless", "subject_to", "notwithstanding"]
    constrains: str           # fact id or obligation id
    predicate: str
    satisfied: Literal["yes", "no", "unknown"]

class Event:
    event_id: str; name: str; kind: str; event_date: IsoDate
    participants: list[EntityRef]; place: EntityRef | None

class Transaction:
    transaction_id: str; kind: str
    from_entity: EntityRef; to_entity: EntityRef
    amount: Money; date: IsoDate
    status: str; instrument: str | None; fact_id: str

class Contradiction:
    contradiction_id: str
    fact_a: str; fact_b: str
    dimension: Literal["amount", "date", "status", "relationship"]
    documents: list[str]

# --- correspondence threads (decision DEC-7) ---------------------------------
class Thread:
    thread_id: str            # thr:<corpus>:<nnnn>:<hash>
    subject: str
    participants: list[EntityRef]
    root_document: str        # document id
    messages: list[str]       # document ids in canonical sequence
    latest_document: str
    closed_by: str | None     # document id that closes the thread

```

A thread is a **corpus-level object**; every message is still an independently addressable `CdmDocument` with its
own document id, filename, gold file and QA set. The thread adds ordering and reply structure; it never merges
documents or shares their identities. The per-message link (`MessageMeta`) is document metadata and is defined in
§4.2.

### 4.2 Content layer

```python
class DocumentMetadata:
    title: str | None
    subtitle: str | None
    author: EntityRef | None
    issuer: EntityRef | None
    addressee: EntityRef | None
    jurisdiction: str | None
    document_date: IsoDate | None
    reference: str | None
    version: str | None
    classification: str | None
    register: Literal["formal", "neutral", "terse"]
    thread: MessageMeta | None      # correspondence only (DEC-7)

class MessageMeta:                  # embedded in DocumentMetadata for correspondence
    thread_id: str
    message_id: str                 # msg:<thread>:<nn>
    sequence: int                   # 1-based position in the thread
    in_reply_to: str | None         # message id being answered
    references: list[str]           # earlier message ids cited in the body
    attachments: list[str]          # exhibit ids

class CdmDocument:
    document_id: str          # doc:00042
    doc_kind: Literal["prose", "workbook"]   # decides which renderers apply (§7.7)
    doc_type: str             # one of the seven, or a workbook family
    structure_id: str         # which structural grammar was used
    metadata: DocumentMetadata
    sections: list[Section]
    statements: list[FactStatement]
    headers: list[HeaderFooter]
    footers: list[HeaderFooter]
    exhibits: list[Exhibit]
    signatures: list[SignatureRecord]
    plan_fingerprint: str     # deterministic structure signature (diversity metric)

class Section:
    section_id: str           # sec:00042:03
    parent_id: str | None
    depth: int
    number: str | None        # "3", "3.2", "(a)"
    heading: str
    blocks: list[Block]

class Block:                  # discriminated union
    block_id: str             # blk:00042:03.02
    kind: Literal["paragraph", "list", "table", "note", "page_break",
                  "signature", "exhibit", "header", "footer"]
    # paragraph
    runs: list[InlineRun] | None
    # list
    items: list[ListItem] | None
    ordered: bool | None
    # table
    table: Table | None
    footnote_refs: list[str] | None

class InlineRun:
    text: str | None
    entity_ref: EntityRef | None
    document_ref: DocumentRef | None
    value: Money | Percentage | IsoDate | None
    emphasis: Literal["none", "bold", "italic"] = "none"
    footnote_id: str | None = None

class Table:
    table_id: str             # tbl:00042:02
    caption: str | None
    header_rows: list[TableRow]
    body_rows: list[TableRow]
    total_rows: list[TableRow]
    columns: list[ColumnSpec] # name, type (None|text|money|date|percent|int|mixed), width_hint, align
    cell_refs: dict[str, str] # "r03c02" -> fact id

class TableRow:
    row_id: str               # tbl:00042:02/r03
    cells: list[TableCell]

class TableCell:
    cell_id: str              # tbl:00042:02/r03c02
    text: str | None
    value: Money | Percentage | IsoDate | int | None   # authoritative type
    display: str              # renderer-independent display string
    number_format: str | None = None                   # e.g. "#,##0.00" (DEC-11)
    rowspan: int = 1; colspan: int = 1
    entity_ref: EntityRef | None
    fact_ref: str | None
    formula: CellFormula | None = None                 # workbook only

class FactStatement:
    statement_id: str         # stmt:00042:07.a
    fact_id: str
    node_id: str              # block/row/cell id that states it
    surface: str              # intended text (checked against alignment, never used as evidence)
    mention_ids: list[str]    # entity mentions expected in this span
    value_slots: dict[str, str]  # "amount" -> rendered surface, for consistency checks
    canonical_sentence_ids: list[str] = []   # DEC-9; optional until Phase C
```

**DEC-9 (sentence references).** `node_id` remains the mandatory structural
anchor — the block, row or cell that states the fact. `canonical_sentence_ids`
is an optional, ordered set of *canonical* sentence ids added by the Phase C
canonicalizer; it may be empty until then, and a table-row or cell statement may
never carry one. Parser sentence ids (`s19`, `sen_…`) are a different namespace
and are rejected here; they appear only in `ParseLoc` and `EvidenceRef`.

**DEC-10 (no nested tables).** A cell carries a scalar value, never a table.
Nested schedules are separate `Table` objects in the section hierarchy, or
separate worksheets in a workbook, joined by an explicit `CrossReference` or a
fact-level link. A cell payload containing `table`, `tables` or `rows` is
rejected rather than flattened.

**DEC-11 (column types are expectations).** `ColumnSpec.type` is optional: a
homogeneous column may declare `text|money|date|percent|int`, a heterogeneous
column declares `mixed`, and `None` means no expectation. The cell's typed
`value` is authoritative and is never coerced or discarded; a typed cell that
disagrees with a concrete declaration is a validation error. `display`,
`number_format` and `formula.cached_value` remain separately recorded, so a
workbook cell keeps its rendered form, its number format, its formula and its
underlying value as distinct facts.

### 4.3 Relationships

```text
CorpusWorld 1 ── n  SemanticFact          fact.statement_ids ── n  FactStatement
CorpusWorld 1 ── n  Transaction           transaction.fact_id ── 1  SemanticFact
CorpusWorld 1 ── n  Event                 event.fact_ids ── n      SemanticFact
CorpusWorld 1 ── n  Contradiction         a/b ── 1 SemanticFact each
CorpusWorld 1 ── n  Thread 1 ── n  CdmDocument   (message order; documents stay independent)
CdmDocument 1 ── n  Section 1 ── n  Block ── (table) 1 Table 1 ── n Row 1 ── n Cell
CdmDocument 1 ── n  FactStatement ── 1 Fact ; ── 1 Block/Row/Cell
CdmDocument 1 ── n  CrossReference       src document+fact ── dst document+fact
RenderResult 1 ── n RenderSpan           span.canonical_id ── 1 Section/Block/Row/Cell/Sentence
ParseAlignment 1 ── n AlignedUnit        unit.canonical_ids ── 1..n parser sentence ids
GoldSet 1 ── n GoldEvidence              evidence.sentence_id ── 1 parser sentence ; quote = parser substring
```

### 4.4 Invariants (machine-checkable)

1. Every `FactStatement.fact_id` resolves; every stated fact has ≥1 statement.
2. Every `TableCell.fact_ref` resolves; a table claiming a transaction must use the same `transaction_id` as the
   prose statement of that transaction.
3. Every `InlineRun.value` is the same typed value as the corresponding fact field (no re-parsed formatting).
4. Every block id is unique within a document; every document id unique within a corpus.
5. Every `CrossReference.dst` resolves to an existing document and (optionally) an existing fact.
6. A `Contradiction` names two different facts with the same `dimension` and genuinely different values; a wording
   difference alone does not qualify.
7. No `FactStatement.surface` is used as gold evidence — evidence always comes from the parsed text.
8. Every rendered byte of a stated fact is accounted for by exactly one `RenderSpan`.
9. **One fact id across every location.** If a fact is stated in a paragraph, a table row and a cross-referenced
   document, all three statements carry the same `fact_id`; a second id for the same fact is a build error.
10. Every `MessageMeta.thread_id` resolves to a `Thread`; a thread's `messages` list is ordered and contains only
    documents whose metadata names that thread; `in_reply_to` precedes the message that cites it.
11. A `Contradiction` is corpus-scoped: its two facts are stated in different documents, and both statements are
    present in the generated corpus (no dangling contradiction).

**Additional model constraints (added by DEC-9–DEC-11; not numbered among the eleven invariants above):**

- **Sentence references are optional and canonical.** `FactStatement.node_id` is always required;
  `canonical_sentence_ids` may be empty until Phase C and must never contain parser ids (`s19`, `sen_…`).
- **No nested tables.** A `TableCell` carries a scalar value, never a table. Nested schedules are separate tables
  in the hierarchy or separate worksheets, linked explicitly.
- **Cell types are authoritative.** `ColumnSpec.type` is an optional expectation (`…|mixed|None`); a typed cell that
  contradicts a concrete column declaration is rejected, never coerced. `display`, `number_format` and
  `formula.cached_value` stay distinct from `value`.

---

## 5. Stable ID strategy

### 5.1 Derivation

IDs are deterministic functions of the seed and a **scope path**, never of wall-clock time, iteration order of a
`dict`, or `hash()` (randomised per process).

```text
id(prefix, scope_path) = prefix + ":" + scope_path + ":" + tag
tag      = b32_lower(blake2b(seed_key || "|" || prefix || "|" || scope_path || "|" || attempt,
                             digest_size=8))
seed_key = blake2b(str(seed) || ":" || corpus_name, digest_size=16)
scope_path = human-readable, ordered, stable
attempt  = 0 normally; incremented only to resolve a tag collision
```

**DEC-8 (authoritative).** The readable scope is part of the identifier, not a
comment: `<prefix>:<readable-scope>:<short-hash>`. The tag is the base32 of an
8-byte digest, 13 characters, no padding. Because the scope is embedded, two
different objects can never produce the same full id; the only possible collision
is on the tag, and it is resolved deterministically by sorting the colliding
scopes and incrementing `attempt`. Earlier drafts of this section omitted the
scope from the formula while §5.2's patterns and examples included it; §5.1 is
the text that was wrong.

Identifier examples elsewhere in this document are abbreviated for readability
(`blk:00042:03.02`, `ent:company:0017`); the full form always carries the trailing
tag (`blk:00042:03.02:q7m2x4k9p1rs`, `ent:company:0017:4f2a9c0e7b31`).

Two design options were considered and one chosen:

| option | example | verdict |
|---|---|---|
| sequential allocation in generation order | `company_0017` | **rejected as the only scheme** — cheap, but renaming or reordering a pool silently re-identifies entities |
| content-addressed hash of the semantic payload | `ent:a3f19c...` | **rejected as the only scheme** — an attribute change re-identifies the entity and breaks cross-document references |
| **hybrid (chosen)** | readable scope path + short hash suffix | stable under content edits, readable in reports, collision-safe |

Chosen form: `ent:company:0017:4f2a`, `doc:00042:9c1e`, `fact:edge:00042:07:ab31`.

### 5.2 ID table

| object | pattern | allocated by | stable across formats | survives content edit |
|---|---|---|---|---|
| corpus | `corp:<name>:<seed-hash>` | pipeline | yes | no (new seed ⇒ new corpus) |
| document | `doc:<ordinal>:<hash>` | planner | yes | yes (hash of type+structure+world revision) |
| section | `sec:<doc>:<number-path>` | planner | yes | yes |
| block | `blk:<doc>:<section-path>.<nn>` | planner | yes | yes |
| sentence (canonical) | `snt:<doc>:<block-id>.<nn>` | canonicalizer | yes | yes |
| table | `tbl:<doc>:<section-path>.<nn>` | planner | yes | yes |
| row | `tbl:<doc>:<nn>/r<nn>` | planner | yes | yes |
| cell | `tbl:<doc>:<nn>/r<nn>c<nn>` | planner | yes | yes |
| entity | `ent:<kind>:<nnnn>:<hash>` | world | yes | yes |
| fact | `fact:<edge\|prop>:<doc>:<nn>:<hash>` | world | yes | yes |
| event | `evt:<nnnn>:<hash>` | world | yes | yes |
| transaction | `txn:<nnnn>:<hash>` | world | yes | yes |
| proposition | `fact:prop:...` (facts and propositions share a namespace) | world | yes | yes |
| condition | `cnd:<fact>:<nn>` | world | yes | yes |
| contradiction | `ctr:<nnnn>:<hash>` | world | yes | yes |
| thread | `thr:<corpus>:<nnnn>:<hash>` | world | yes | yes |
| message | `msg:<thread-id>:<nn>` | planner | yes | yes |
| render occurrence | `rnd:<fmt>:<doc>:<nn` | renderer | **no** (format-scoped) | yes |
| parser sentence | `s<idx>` / `sen_<ulid>` | Synth parser | **no** (per ingest) | no |

The last two rows are the important ones: **render and parse identifiers are explicitly not stable**, and nothing
outside the render/parse layers may key on them. They exist to point *into* an artifact, and they are always stored
alongside the canonical id they came from.

### 5.3 Determinism rules

- All sampling uses `random.Random(blake2b(seed_key || scope).digest())`, one generator per scope. No shared global
  RNG, no `random.random()` module state.
- Iteration over mappings is `sorted()` before sampling anywhere an order could leak into output.
- The world is built before documents; document `n` is a pure function of `(world, n, config)`.
- **No global byte-identical prefix guarantee** (decision DEC-6). Content-driven length means a 70-document corpus is
  not required to begin with the byte-identical 7-document pilot; V2's prefix property is retired.
- Determinism is guaranteed per `(seed, configuration)`: two runs of the same corpus definition produce identical
  bytes, ids, gold and alignment. Configuration includes the document count, type mix, format set and render
  profiles.
- **Fixed canonical regression fixtures** replace the prefix property: a small, versioned fixture set (one document
  per type, plus one workbook and one thread) is checked byte-for-byte on every change, so renderer or planner
  drift is caught even though large corpora are not prefixes of each other.
- ID allocation is a pure function of `(seed, scope_path)`; no counters that depend on how many documents were
  generated before.

---

## 6. Provenance architecture

### 6.1 Four location types — do not conflate them

| layer | name | who creates it | what it means | stable? |
|---|---|---|---|---|
| L2 | **CanonicalSourceLoc** | planner | `doc / section / block / table / row / cell` + char range in the canonical text projection | yes |
| L3 | **RenderLoc** | renderer | where the node landed in this artifact (format-specific) | per artifact |
| L4 | **ParseLoc** | Synth parser | `doc_id, sentence_id, idx, char_start, char_end, page, section_path, kind` | per ingest |
| L5 | **EvidenceRef** | gold builder | parser sentence id + exact parser substring + canonical ids | per gold file |

V2 has only L4→L5 (and only for TXT), with an exact positional match between plan order and parse order. V3 keeps
all four and records the mappings between them.

### 6.2 Format-specific render locations

| format | render location fields | page semantics |
|---|---|---|
| TXT | `logical_page`, `line_no`, `char_start`, `char_end` | `\f`-delimited logical page; **not** a physical page |
| PDF | `physical_page` (**mandatory**), `bbox[x0,y0,x1,y1]` (**optional**), extracted-text `char_start/char_end` | true physical page when the renderer paginates; bbox recorded only when the renderer can compute it reliably |
| DOCX | `section_index`, `paragraph_index`, `table_index`, `row`, `cell`, `run_start/run_end` | `page = null`, `pageless = true` — DOCX has no physical pagination |
| XLSX | `sheet_name`, `table_name`, `row`, `column`, `cell_ref` | `page = null`; `sheet` is not a page |

**Hard rules (decision DEC-2):**

1. **Page-level provenance is mandatory wherever physical pagination exists.** For PDF, every rendered block, row
   and cell records its `physical_page`, and the renderer's page count must equal the parser's independent page
   count. A PDF span without a page is a build failure, not a warning.
2. **Bounding boxes are optional and never fabricated.** `bbox` is recorded when the writer can compute the text
   origin and the line extent reliably; otherwise the field is `null`. A guessed bbox is worse than no bbox: it
   would make a provenance metric pass that has measured nothing. The V3 report counts `bbox_coverage` and grades it,
   it does not gate on 100%.
3. **Pageless formats never carry a page number.** If a renderer or parser cannot establish physical pagination,
   the location records `page: null` and `pageless: true`. No renderer may invent a page number to make a report
   look complete. This is exactly the failure mode D11 documents, and V3 treats it as a first-class field rather
   than a note.
4. **The same rule applies to the parser side.** `ParseLoc.page` is `null` when the format is pageless; gold never
   asserts a page for DOCX or XLSX, and a page-boundary metric is only computed for formats that declare
   `page_kind ∈ {physical, logical}`.

### 6.3 Mapping pipeline

```text
CdmDocument ──render──▶ RenderMap ──align──▶ AlignmentGraph ──emit──▶ GoldSet
   (L2 ids)              (L2→L3)              (L2↔L4)                (L4 + L2)
```

`RenderMap` is a flat list — one row per canonical node, per artifact:

```json
{
  "canonical_id": "blk:00042:03.02",
  "kind": "paragraph",
  "format": "txt",
  "loc": {"logical_page": 2, "line_no": 14, "char_start": 0, "char_end": 96},
  "text": "Alpha Holdings Limited acquired Beta Trading Limited for GBP 4,000,000 on 3 March 2024.",
  "text_sha256": "..."
}
```

`AlignmentGraph` is many-to-many by design:

```json
{
  "canonical_id": "blk:00042:03.02",
  "canonical_sentence_ids": ["snt:00042:03.02.01"],
  "parse_sentence_ids": ["s19"],
  "kind": "exact",
  "normalised_equal": true,
  "char_start": 1204, "char_end": 1300
}
```

`kind ∈ {exact, normalised, split, merged, partial, ambiguous, missing}`.

### 6.4 Location mapping rules per format

| canonical → render | render → parse | notes |
|---|---|---|
| paragraph → TXT page/line range | parser sentence(s) covering the paragraph's char range | 1:1 today; must tolerate 1:n |
| paragraph → PDF page (bbox when present) | parser sentence ids whose char range intersects the block's page, or its bbox text when available | page is mandatory; bbox is optional and never inferred from the parser |
| paragraph → DOCX paragraph index | parser sentence(s) from that paragraph | D4 can split one canonical paragraph into two parser items |
| table row → TXT pipe line | `table_row` sentence(s) | pipe rendering is what the TXT parser recognises |
| table row → PDF flat cells | **no row structure** — individual cell sentences | D7: alignment records `missing_row_structure` and the row's cells align one-by-one |
| table → XLSX row range | one `table_row` sentence per row, no section path | D6: numeric display lost, raw value preserved |
| footnote → TXT inline marker | filtered line or note sentence | must be flagged when the parser filters it |

### 6.5 Evidence references

A gold evidence item keeps the V2 shape (so `synth.eval.gold.validate()` continues to work) and gains additive
fields behind a `gold_version: 2` bump:

```json
{
  "sentence_id": "s19",
  "quote": "Alpha Holdings Limited acquired Beta Trading Limited for GBP 4,000,000 on 3 March 2024.",
  "canonical_node_id": "blk:00042:03.02",
  "canonical_sentence_ids": ["snt:00042:03.02.01"],
  "alignment_kind": "exact",
  "render_format": "docx",
  "render_loc": {"section_index": 3, "paragraph_index": 11}
}
```

The `quote` is always a **substring of the parser's canonical text**, never of the generator's intended surface.
That single rule is what keeps evidence exact when segmentation changes.

---

## 7. Canonical content and renderer architecture

### 7.1 Pipeline

```text
Seed + scenario configuration
            |
            v
World and semantic fact generation            (L1)
            |
            v
Canonical document model                     (L2)
            |
   +--------+---------+----------+----------+
   |        |         |          |          |
  TXT      PDF       DOCX       XLSX     (RenderProfile)
   |        |         |          |
   +--------+---------+----------+----------+
            |
            v
    RenderMap per artifact                    (L3)
            |
            v
Actual Synth ingestion (u01_parser.ingest_path)
            |
            v
Parsed canonical content (DocView, sentences, blocks, page map)   (L4)
            |
            v
Evidence alignment layer (AlignmentGraph)
            |
            v
Gold annotation generation (gold/v1 + gold/v2)
            |
            v
QA generation (per-format and cross-format)
            |
            v
Independent validation (gold validator + semantic checks + alignment checks)
```

Format applicability is per document kind (§7.7): a prose document renders to TXT/PDF/DOCX, a workbook to XLSX. The
equivalence fixture set is the only place where one source is deliberately rendered to all four.

The generator remains model-free and offline: no LLM, no network, no gateway calls. All variation comes from the
seed and structural grammars.

### 7.2 Renderer interface

```python
class RenderProfile:
    page_width_chars: int | None      # TXT wrap width
    page_height_lines: int | None     # TXT concept only
    page_size: str | None             # "A4" | "Letter" | None
    margins_mm: tuple[int, int, int, int] | None
    font_family: str | None
    include_headers: bool
    include_footers: bool
    table_style: str | None
    number_formats: dict[str, str]    # money/percent/date display rules

class RendererCapabilities:
    format: str                       # txt|pdf|docx|xlsx
    paginates: bool                   # physical pages exist
    page_kind: Literal["physical", "logical", "sheet", "none"]
    tables: bool                      # can express row/column structure
    bbox: bool                        # emits coordinates
    footnotes: bool
    headers_footers: bool

class RenderResult:
    format: str
    path: Path
    sha256: str
    spans: list[RenderSpan]           # L2 -> L3 map
    fact_surfaces: dict[str, list[str]]   # fact id -> every rendered surface of its values
    warnings: list[str]
    capabilities: RendererCapabilities

class Renderer(Protocol):
    capabilities: RendererCapabilities
    def render(self, doc: CdmDocument, profile: RenderProfile, out: Path) -> RenderResult: ...
```

### 7.3 Renderer responsibilities (explicit)

Every renderer **must**:

1. Emit every block exactly once, in canonical order, with its canonical id recorded in the `RenderMap`.
2. Format typed values from `Money`/`IsoDate`/`Percentage` — never re-parse a string to recover a number.
3. Record `fact_surfaces[fact_id]` for every value it renders (amount, date, percentage, name), so the
   cross-format consistency check can compare surfaces without guessing.
4. Declare honest capabilities: `paginates`, `page_kind`, `tables`, `bbox`. A paginating format must record a
   page for every span; a pageless format sets `page: null`. `bbox` is declared only when the renderer can compute
   it reliably, and a `null` bbox is always acceptable (§6.2).
5. Be a pure function of `(CdmDocument, RenderProfile)`: same inputs ⇒ identical bytes (and identical sha256).
6. Refuse to render a document that violates a model invariant (unresolved fact ref, duplicate id, cell without a
   value type).
7. Report, not repair: a renderer that cannot express a construct (e.g. `colspan` in TXT) writes a
   `RendererWarning` and a documented fallback, and the fallback is visible in the map.

Every renderer **must not**:

- mutate the `CdmDocument` or any semantic object;
- choose or rename entities, amounts, dates, statuses or conditions;
- drop a stated fact silently;
- invent page numbers, section numbers or headers not present in the model;
- call a model, the network, or the Synth store.

### 7.4 Per-format responsibilities

| renderer | pages | tables | structure | known limitations to encode |
|---|---|---|---|---|
| **TXT** | logical (`\f`), `paginates: logical` | pipe-delimited rows, one row per line | numbered headings, blank line between paragraphs (target ≤25% blank) | no bbox; no true pagination; wrapping is explicit and recorded |
| **PDF** | physical, `paginates: physical` | drawn grids with row/column intent | styled headings, footers | ReportLab (DEC-1); parser sees 0 table items (D7); page provenance mandatory, bbox optional; heading recovery is heuristic (D8) |
| **DOCX** | none, `pageless: true` | real Word tables | real headings, paragraph styles | parser may split `Label: value` (D4); one synthetic page (D11) |
| **XLSX** | sheets, `page_kind: sheet` | native sheet tables | sheet per table family, header rows, typed cells | parser loses currency/format (D6); no section paths |

### 7.5 XLSX: workbook representation

**Decision: a specialised workbook model that shares the semantic spine, not paragraphs-in-cells.**

Rationale:

- A spreadsheet's primary object is a **typed cell**, not a paragraph. Modelling sheets as sections and cells as
  runs would lose exactly the information the format exists to carry (types, formulas, number formats, ranges).
- Conversely, giving workbooks a completely separate model would duplicate entity/fact/evidence handling and break
  the single-identity rule.

Proposed shape:

```python
class CdmWorkbook(CdmDocument):
    workbook: Workbook          # present when doc_kind == "workbook" (§7.7)
    # sections[] is empty for pure workbooks; a workbook may still carry
    # a title block and a notes sheet modelled as sections.

class Workbook:
    workbook_id: str
    sheets: list[Sheet]
    named_ranges: dict[str, str]

class Sheet:
    sheet_id: str               # sht:00042:01
    name: str
    blocks: list[Block]         # title rows, note rows, table blocks
    tables: list[Table]         # reuse the same Table/Row/Cell types
    frozen_header: bool

class CellFormula:
    formula: str                # "=SUM(D4:D15)"
    cached_value: Money | int   # what a reader sees without recalculation
    recalc_on_load: bool
```

Rules for workbooks:

1. Cells carry **both** `value` (typed) and `display` (renderer-independent string). The parser sees a
   representation of `display`; gold asserts against `value`.
2. Formulas are optional and always paired with a cached value. Gold never requires a reader to recalculate.
3. A transaction table in XLSX must reference the same `transaction_id` as the prose document that describes the
   same transaction — a corpus-level transaction can appear in a workbook and a memo.
4. Numeric fidelity is evaluated on raw values (D6), and display formatting is evaluated separately as a
   *rendering* property, not a semantic one.
5. **No covering memo is generated by default** (decision DEC-4). A workbook is a standalone semantic document. When a
   scenario needs prose around it, the memorandum is a separately planned, separately identified document that
   references the workbook by `document_id`; it is never emitted as an automatic companion artifact. This keeps the
   semantic-document count honest and avoids doubling artifacts for every spreadsheet.
6. A workbook may still carry its own title block and a notes sheet modelled as ordinary sections. Those are part of
   the workbook, not a memo.
7. **No table inside a cell** (DEC-10). A second schedule becomes a second sheet or a second table, referenced from
   the first; it is never nested in a cell payload.
8. **Column typing is advisory, cell typing is authoritative** (DEC-11). A sheet may declare a column `money`,
   `mixed` or nothing at all; typed cells keep their own type, display string, number format and cached formula
   result. Disagreement with a concrete column declaration fails validation instead of converting the value.

### 7.6 Cross-format consistency guarantee

Rendering the same `CdmDocument` to several formats must not change names, amounts, dates, ownership percentages,
relationships, transaction status, contradictions or conditions. This is enforced, not assumed:

- **By construction:** all four renderers read the same immutable model and the same typed values; there is no
  per-format fact store to drift.
- **By verification:** after rendering, compare `RenderResult.fact_surfaces` across formats. For each fact id and
  each value slot, the normalised surface must be equal after format-specific normalisation (currency symbol vs
  code, `3 March 2024` vs `2024-03-03`, `3.50%` vs `350` bp).
- **On mismatch:** the render fails. A differing date or amount between two renderings of one document is a
  correctness bug, not a warning.

### 7.7 Format applicability by document kind

Not every document should be rendered to every format. A 4,000-word facility agreement rendered into spreadsheet
cells is not a spreadsheet; a repayment schedule flattened into prose is not an agreement. The corpus therefore has
two document kinds, and the format matrix is explicit.

| doc kind | doc types | primary formats | optional | never |
|---|---|---|---|---|
| `prose` | the seven types in §9 | TXT, PDF, DOCX | — | XLSX |
| `workbook` | schedule, register, model, reconciliation | XLSX | PDF/DOCX of the same table set (equivalence only) | TXT as a primary artifact |

Consequences:

1. A workbook is its **own semantic document** with its own `document_id`, not a projection of a prose document.
   When a scenario needs prose around it, the memorandum is separately planned and references the workbook by id
   (decision DEC-4) — it is never an automatic companion.
2. A prose document and a workbook may state the **same** `transaction_id` or `fact_id`; that cross-reference is a
   semantic link, not a rendering relationship.
3. The cross-format equivalence fixture (§13.2) is the one place where a single table-heavy document is deliberately
   rendered to all four formats, because that is the comparison the fixture exists to make.
4. `RenderMap` is written only for the formats actually produced; a missing format is not a warning, it is the
   declared configuration.

---

## 8. Gold alignment strategy

### 8.1 Why the V2 approach cannot simply be extended

`_canonical_sentence_index()` asserts `ingested_sentences == planned_sentences` element-wise. That is a
**positional** contract. The audit shows positional contracts do not survive format changes:

- PDF: 19 sentences for content that is 11 in Markdown (D14);
- DOCX: `Borrower: Meridian Holdings Limited` split into two items (D4);
- HTML: table header duplicated, 24 vs 23 sentences (D3);
- XLSX: one sentence per row, no headings (D6);
- PDF tables: cells become separate sentences, row association lost (D7).

V3 replaces positional equality with **validated alignment**: equality is one of several acceptable outcomes, and
every outcome is recorded.

### 8.2 Alignment algorithm

```text
align(cdm_document, render_result, parse_result) -> AlignmentGraph

1. Project the canonical document to a canonical character stream:
   canonical_text = render_canonical_projection(doc)     # deterministic, no format styling
2. Normalise both sides:
   normalise(text) = NFKC; collapse whitespace; unify quotes/dashes; strip soft hyphens
   (reuse the semantics of synth.provenance.locator.normalize)

3. Anchor pass — exact and normalised:
   for each canonical sentence (in order):
       find parser sentences whose normalised text equals it
       if exactly one -> kind=exact (or normalised)
       if several -> kind=ambiguous  (reject unless all are in the same block and text is identical)
       if none -> continue to pass 2

4. Neighbourhood pass — split and merge:
   for each unmatched canonical sentence:
       candidates = parser sentences overlapping its canonical char range ± one neighbour
       if canonical text contains all candidate texts concatenated -> kind=split
       if one candidate contains canonical text of several consecutive sentences -> kind=merged
       if candidate overlap >= coverage threshold and normalised similarity >= 0.90 -> kind=partial

5. Structural pass — tables, notes, headers/footers:
   align table cells individually when row structure is unavailable (PDF D7)
   align footnote/endnote text to filtered or note sentences
   mark header/footer text separately; it must never be offered as fact evidence

6. Verdict per canonical sentence: exact | normalised | split | merged | partial | ambiguous | missing
   Document-level: aligned if every fact-bearing statement has a verdict in
    {exact, normalised, split, merged, partial-with-coverage>=threshold}
    and no fact-bearing statement is ambiguous or missing.
```

### 8.3 Rules that keep evidence exact

1. **Quote = parser substring.** The evidence quote is sliced from `DocView.text` using the parser's
   `char_start/char_end`. The generator's intended surface is stored separately (`statement.surface`) and is used
   only to verify that the aligned text still expresses the fact.
2. **Sentence id = parser sentence id.** Never a canonical id in the `sentence_id` field. Canonical ids go into
   the additive `canonical_*` fields, so existing validators keep working.
3. **Split/merge does not weaken grounding.** If a parser splits one canonical sentence into two, each resulting
   sentence must still contain the fact's required entity mentions and value slots; the gold evidence names the
   sentence(s) that together carry them, and the quote is the concatenation of adjacent parser substrings — still
   an exact substring of the parser text, because they are adjacent.
4. **Merged sentences are allowed but checked.** If a parser merges two canonical sentences, the evidence quote is
   the merged parser sentence, and the fact's value slots must both be present; a merged quote that also carries a
   *contradicting* fact is rejected.
5. **Ambiguity is fatal.** If a quote occurs twice (HTML header duplication, repeated boilerplate) and the two
   occurrences carry different facts, alignment fails. If they carry the same fact, the first occurrence is used
   and the duplicate is recorded as `duplicate_occurrence`.
6. **Filtered text is never evidence.** Lines the parser filters (headers/footers) cannot ground a fact.

### 8.4 Gold generation steps

```text
for each document:
  1. render to the target format                  -> RenderResult (bytes + RenderMap)
  2. ingest with the real parser                  -> DocView (sentences, blocks, pages, section paths)
  3. align                                        -> AlignmentGraph
  4. verify each FactStatement:
        - alignment verdict acceptable
        - every required entity mention present in the aligned parser span
        - every value slot present (amount/date/percent/status)
        - no contradicting fact in the same span
  5. build the versioned gold pair (8.7):
        v1 projection: the V2-compatible GoldSet (entities, edges, propositions,
                       links, flags, traps, time_refs) with parser sentence ids
                       and exact parser quotes
        v2 artifact:    the same items plus canonical_node_id, canonical_sentence_ids,
                       alignment_kind, render_format and render_loc per evidence item
        entities:       parser sentence ids + exact parser quotes
        edges/propositions: evidence from parser span, canonical ids in the v2 file only
        time_refs:      from parser text where possible, canonical value as cross-check
        flags/traps:    unchanged semantics, evidence re-grounded
  6. write BOTH files (v1 and v2); run synth.eval.gold.validate() on the v1 projection
     and treat SENTENCE_MISMATCH as fatal (assert_gold_valid already does this)
  7. run the V3 gold validator on the v2 artifact, then the alignment assertions (8.5)
     and the semantic checks
  8. any failure -> CorpusGenerationError; never a partial success
```

### 8.5 Defect detection the alignment layer must provide

| defect | detection |
|---|---|
| missing fact | a stated fact has no acceptable alignment → fail |
| duplicated fact | one parser sentence carries the same fact twice, or two statements of one fact land on the same sentence with different values → fail |
| altered fact | entity mention missing, value slot absent, or aligned text expresses a different relation → fail |
| extra fact | parser sentence in a fact-bearing block encodes a fact not in the model → fail (prevents silent additions) |
| ambiguous evidence | quote matches 2+ sentences with different facts → fail |
| unsupported alignment | only `partial` below coverage threshold remains → fail |
| format drift | `fact_surfaces` differ across formats for the same doc → fail |

### 8.6 Gold remains generated, not blind

V3 keeps `annotator = "synthetic_data.generator"`, `mode = "generated"`, and the explicit note that the gold is not
blind and not human-verified. The alignment layer makes gold **source-grounded in the parser**, which is a
correctness property, not an annotation-quality claim. Human/blind adjudication stays a separate future track with
its own `mode`, and no V3 result may be reported as human-adjudicated.

### 8.7 Gold artifact versioning (decision DEC-3)

**Decision: side-by-side versioned gold artifacts. The vendored `synth.eval.gold` model is not modified.**

Layout per corpus:

```text
gold/
├── v1/  gen_doc_00000_board_minutes__0-41.json     V2-compatible GoldSet (frozen schema)
├── v2/  gen_doc_00000_board_minutes__0-41.json     V3 artifact (superset)
└── alignment/
    └── gen_doc_00000_board_minutes.json            AlignmentGraph (not an annotation)
```

Rules:

1. **v1 is the compatibility contract.** It is byte-shape-compatible with the current `GoldSet`, so
   `synth.eval.gold.validate()`, `assert_gold_valid()` and every existing consumer work unchanged. It carries parser
   sentence ids and exact parser quotes — the V3 improvement that matters to them.
2. **v2 is the V3 contract.** It adds `canonical_node_id`, `canonical_sentence_ids`, `alignment_kind`,
   `render_format` and `render_loc` per evidence item, plus `canonical_fact_id` on edges/propositions and thread
   metadata on correspondence.
3. **v2 is derived from v1, never the reverse.** The builder constructs the v1 items first (already validated by the
   vendored validator) and then enriches them; an enrichment that cannot be justified by an aligned parser span is
   dropped with a build failure.
4. **Both files are written atomically, or neither is.** A run that fails after writing v1 must remove it, so a
   corpus cannot contain a v1 gold file without its v2 counterpart.
5. **Validation runs twice.** The v1 projection is validated by the vendored validator; the v2 artifact is validated
   by a V3-side validator that re-checks the v1 invariants plus canonical-id resolution, render-location presence
   (page mandatory for PDF, null for pageless) and alignment-kind legality.
6. **No forking the dependency.** If the alignment prototype proves a required evidence variant cannot be expressed
   even as a v2 superset, the response is an ADR proposing an upstream change, not a local edit to `synth/`. Phase G
   carries that check as an explicit exit condition.
7. **Consumers state which version they read.** Evaluation and QA default to v2; compatibility tests and any external
   comparison read v1. Nothing infers the version from the filename alone.
8. **Storage cost is accepted.** Two gold trees cost roughly 1.4× a single tree at the sizes in §15; the alternative
   — mutating a shared model in a vendored dependency — costs reviewer trust and reproducibility.

---

## 9. Rich document structures by type

Seven **prose** types, each with 3–5 structural grammars (see §10), plus workbook families (`schedule`, `register`,
`model`, `reconciliation`) covered by §7.5. The tables below define the **content obligations** of each prose
grammar; a document must satisfy its grammar, not merely use its headings. Workbooks are a separate `doc_kind`, not
an eighth prose type, and are never produced by rendering one of these seven into spreadsheet cells (§7.7).

### 9.1 Board minutes

| element | content | semantic objects |
|---|---|---|
| header block | company, meeting date/time, place, reference, quorum statement | metadata, place |
| attendance table | name, role, status (present/apologies/absent), voting entitlement | persons, roles |
| resolutions table | item, resolution, proposed by, seconded by, votes for/against/abstain, outcome | propositions, statuses |
| decisions prose | narrative per resolution, conditional approvals | conditions, edges |
| director changes | appointment/resignation, effective date, chair hand-over | events, historical change |
| prior-meeting references | date + minute reference + what was noted | cross-document references |
| supporting schedule | exhibit reference, summary rows | exhibits, tables |
| signature block | chair signature, date, secretary | signature records |

Tables 2–3 (attendance, resolutions/votes, schedule). Must contain at least one **rejected or deferred**
proposal and at least one **conditional** approval — otherwise the status vocabulary is untested.

### 9.2 Financial agreement

| element | content | semantic objects |
|---|---|---|
| parties & recitals | lender, borrower, guarantor, purpose | entities, edges |
| facility terms | principal, currency, tenor, rate basis, availability period | Money, percentages, time scope |
| interest calculation | margin, reference rate, day-count, worked example | computed values, table |
| disbursement schedule | drawdown date, amount, account | transactions |
| repayment schedule | instalment, due date, principal, interest, balance | transactions, table |
| outstanding balance | as-at date, amount, status | Money, as_of time |
| guarantees & collateral | guarantor, secured assets, coverage ratio | edges, assets |
| covenants | covenant, threshold, test date | table, conditions |
| conditions precedent | condition, satisfied/waived, date | conditions |
| amendments/defaults | clause amended, effective date, default event | events, contradictions |
| execution | signatories, capacity, date | signatures |

Tables 3–6. Must include at least one **computed** figure whose arithmetic is checkable (instalment sum =
principal; interest = principal × rate × days/basis).

### 9.3 Due diligence

| element | content | semantic objects |
|---|---|---|
| scope & sources | documents reviewed, limitations, date range | provenance, document refs |
| ownership structure | shareholder, shares, percentage, jurisdiction | ownership table, edges |
| subsidiaries | entity, holding %, incorporation, status | entities, edges |
| financial summary | revenue, EBITDA, net debt, period | Money, periods, table |
| legal proceedings | matter, court, parties, status, exposure | events, propositions |
| allegations | allegation, source, response, corroboration | propositions, speaker, evidence |
| discrepancies | conflicting values across sources | contradictions |
| findings & recommendations | finding, severity, recommendation | propositions |
| appendices | document list, extracts | exhibits, cross-references |

Tables 3–5. Must contain **conflicting findings across sources** (one contradiction minimum) and at least one
allegation that is not established fact.

### 9.4 Case notes

| element | content | semantic objects |
|---|---|---|
| matter header | parties, court, case number, judge, stage | metadata, entities |
| parties & representatives | party, counsel, role | table, edges (`represents`) |
| chronology | date, event, source document | events table, time refs |
| evidence register | exhibit, description, date, admissibility | exhibits, table |
| submissions | party, argument, authority | propositions |
| allegations vs findings | allegation, evidence, finding | propositions, statuses |
| procedural status changes | stage from → to, date, order | events, status transitions |
| orders & next steps | order, deadline, responsible party | conditions, deadlines |

Tables 2–3. Must show a **status transition** and at least one allegation that is expressly *not* a finding.

### 9.5 Correspondence

| element | content | semantic objects |
|---|---|---|
| letterhead | sender org, address, date, reference | metadata |
| addressee block | recipient, role, organisation | entities |
| subject line | matter + reference | cross-reference |
| body | position, request, condition, deadline | propositions, conditions |
| thread references | earlier letter date/ref, response-to, message sequence | `Thread`, `MessageMeta`, cross-document references |
| attachments | list, referenced documents | exhibits |
| corrections | correction/retraction of an earlier statement | contradiction, supersedes |
| signature | name, title | signature record |

Tables 0–1 (e.g. a schedule of outstanding items). Must include at least one **conditional** statement or a
**correction** in a proportion of documents — not every letter, or the pattern becomes a template.

**Thread model (decision DEC-7).** Every letter belongs to exactly one `Thread` and carries a `MessageMeta` with a
stable `message_id`, a 1-based `sequence`, an optional `in_reply_to` and a list of cited earlier message ids. The
thread is a corpus-level object: it fixes the order and the reply structure, while each message remains an
independently addressable document with its own filename, gold pair and QA set. A thread must satisfy:

- `sequence` is contiguous within the thread and matches `Thread.messages` order;
- `in_reply_to` names an earlier message of the same thread (never a later one, never another thread);
- a "re:" subject references the parent message's reference, not a free-text guess;
- a thread of ≥ 3 messages contains at least one correction or supersession, so reply semantics are exercised;
- a single-message thread is allowed but carries no thread-level claims.

### 9.6 Memorandum

| element | content | semantic objects |
|---|---|---|
| header | to/from/cc/date/reference/privilege marker | metadata |
| executive summary | 3–5 bullet conclusions | propositions |
| background | facts and history | edges, events |
| analysis | issue-by-issue reasoning with authorities | propositions, links |
| legal relationships | who owes what to whom, on what instrument | edges, transactions |
| financial evidence | amounts, periods, comparisons | Money, table |
| exceptions & qualifications | carve-outs, assumptions | conditions |
| recommendations | decision requested, options | propositions, statuses |
| exhibits | referenced documents and extracts | exhibits, cross-references |

Tables 1–2. Must contain a **qualified** conclusion (condition/exception), not only assertions.

### 9.7 Press release

| element | content | semantic objects |
|---|---|---|
| headline & dateline | headline, city, date | metadata |
| announcement | transaction/appointment/result | facts, statuses |
| quotes | named spokesperson, verbatim quote | propositions with speaker |
| transaction status | proposed/completed/subject to conditions | status, conditions |
| historical comparison | prior period figures | Money, periods |
| boilerplate | about-the-company paragraph | entity attributes |
| contact | media contact, forward-looking statement | metadata |

Tables 0–1. Short by nature (350–650 words) but must carry **verifiable facts** (amounts, dates, percentages) and
at least one **forward-looking** statement that is clearly not a completed fact.

---

## 10. Diversity and deterministic generation strategy

### 10.1 Where V2's sameness comes from

The corpus has 552 sentence templates for 46,504 sentences because each document type follows one script:
`_add_body_sections_v2` calls the same group functions in a fixed order, and the sentence text per fact is a single
template with slots. Variation exists in names, amounts and dates — not in **structure** or **information shape**.
Measured within-document duplication is ~0%, so string-level de-duplication would not have caught this: the
problem is structural, which is why V3 measures information structure, not sentence strings.

### 10.2 Five independent variation axes

| axis | mechanism | deterministic by |
|---|---|---|
| **structure** | 3–5 structural grammars per type, each a different section order and block mix | `structure_id = weighted_choice(seed_scope("structure", doc_n))` |
| **realisation** | 2–6 surface templates per (relation, status, register), chosen per statement | `seed_scope("surface", statement_id)` |
| **register** | formal/neutral/terse profiles controlling sentence length, clause depth, passive voice | document-level choice |
| **length** | sampled per type from a bounded distribution (not a constant page budget) | `seed_scope("length", doc_n)` |
| **fact mix** | which optional fact families appear (guarantees, amendments, chronology, allegations) | weighted per structure, capped per document |

Additional variation that does not weaken validation:

- section ordering permutations **within** structural constraints (e.g. "Execution" always last);
- table layout variants (2-column vs 4-column attendance; 3-column vs 5-column repayment);
- controlled omissions: a grammar may omit one optional family, and the manifest records which;
- cross-document reference density;
- contradiction placement: which document pair carries the two sides, distributed deterministically across document
  types and formats (decision DEC-5, §11.3);
- correspondence thread shape: length 1–5, reply fan-out, and where a correction lands.

### 10.3 Anti-template rules

1. **No document may reuse a structural signature already used in the same corpus** until all signatures for its
   type are exhausted (`plan_fingerprint` uniqueness check, relaxable only with an explicit warning).
2. **Template inventory must grow superlinearly with corpus size**: unique surface templates ≥ 1.2 × document
   count at every checkpoint (V2 sits at 0.32 per document at 5,000).
3. **Information-shape diversity is measured directly**: sequence of (relation, subject-type, object-type, status)
   tuples per document must have ≥ 60% distinct signatures across the corpus.
4. **No two documents may share a sentence skeleton with identical slot *types*** — masked templating (as the audit
   does) is applied per corpus and the duplication ceiling is a build gate.
5. **Length must vary within type**: p5–p95 span ≥ 35% of the median (V2: 8%).

### 10.4 Measurable structural diversity metrics (V3 validation)

Diversity is judged on **structure and information shape**, not on surface wording. Two documents that use
different nouns but the same section order, the same table shapes and the same predicate sequence are duplicates for
this purpose — V2's 0.27% within-document fuzzy duplication coexisted with 98.81% cross-corpus template reuse, which
is exactly the failure a text-similarity metric misses.

| metric | definition | V2 | V3 gate |
|---|---|---|---|
| template duplication % | masked unique templates / total sentences | 98.81 | ≤ 80 |
| boilerplate sentence % | sentences appearing in ≥2 documents | 99.53 | ≤ 70 |
| unique templates per 1,000 docs | templates / documents × 1,000 | 315 | ≥ 1,200 |
| max documents sharing one sentence | largest sharing set | 1,750 (100%) | ≤ 15% of corpus |
| structural signature duplication | duplicate `plan_fingerprint` share | n/a (1 script/type) | ≤ 5% |
| information-shape diversity | distinct predicate/argument-type sequences / documents | not measured | ≥ 0.60 |
| section-order entropy | Shannon entropy of section-order permutations | ~0 | ≥ 1.5 bits/type |
| length spread | (p95 − p5) / median words, per type | 0.08 | ≥ 0.35 |
| entity co-occurrence spread | distinct entity pairs / possible pairs | low | report + threshold per type |
| fact-type coverage | distinct relation types used / ontology relations | partial | 100% of V3 relation set |
| thread-shape diversity | distinct (length, reply depth, correction position) shapes / threads | n/a | ≥ 0.5 |
| table-shape diversity | distinct (columns, column types, row count band) per type | 0 (no tables) | ≥ 0.5 |
| contradiction distribution | (document type × format) cells containing ≥1 contradiction / expected cells | 1 corpus-level | all planned cells, deterministic |

Every metric is computed from the saved per-document rows, so it can be re-run without regenerating.

---

## 11. Document-depth target ranges

Targets are per type, not global. Word counts assume 350–450 words per rendered page for PDF/DOCX and are the
*content* target — table rows count as content, not padding. Sentence counts assume 11–14 words/sentence.

The single global range used in the V2 depth report (1,200–1,600 words for every type) is **superseded** by this
table: a press release and a facility agreement are not the same document and must not share a gate. The V3 gate
file must express ranges per type; the `corpus-depth-audit` harness already supports a per-type targets file, and
its global default is to be replaced when V3 gate definitions are written.

| type | words | pages (intended) | sections | paragraphs | sentences | tables | rows × cols | entities | distinct facts | transactions | cross-doc refs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| press_release | 350–650 | 1–2 | 3–5 | 8–14 | 28–50 | 0–1 | 3–6 × 2–4 | 8–18 | 8–16 | 0–2 | 0–2 |
| correspondence | 500–900 | 1–2 | 3–6 | 10–20 | 38–70 | 0–1 | 3–8 × 2–4 | 8–20 | 10–20 | 0–2 | 1–3 |
| case_notes | 1,200–1,900 | 3–5 | 5–8 | 22–38 | 90–140 | 2–3 | 5–12 × 3–5 | 15–30 | 25–45 | 0–3 | 1–3 |
| memorandum | 1,300–2,000 | 3–5 | 5–8 | 20–36 | 100–150 | 1–2 | 4–10 × 3–5 | 14–28 | 25–45 | 0–3 | 1–3 |
| board_minutes | 1,400–2,200 | 4–6 | 6–9 | 24–40 | 105–160 | 2–3 | 5–12 × 4–6 | 18–35 | 30–50 | 0–4 | 1–4 |
| due_diligence | 2,000–3,200 | 5–8 | 7–11 | 35–60 | 150–240 | 3–5 | 6–14 × 3–6 | 25–45 | 40–70 | 2–6 | 2–6 |
| financial_agreement | 2,500–4,500 | 6–10 | 8–12 | 45–80 | 190–330 | 3–6 | 5–15 × 3–6 | 20–40 | 50–90 | 4–12 | 2–5 |

### 11.1 Structural minima that carry the depth

Depth must come from records, not from adjectives:

- **board_minutes**: ≥ 1 rejected/deferred resolution, ≥ 1 conditional approval, ≥ 1 prior-meeting reference.
- **financial_agreement**: ≥ 3 transactions in a schedule, ≥ 1 computed total that reconciles, ≥ 1 condition
  precedent with a satisfied/waived state.
- **due_diligence**: ≥ 1 ownership table with percentages summing to 100%, ≥ 1 contradiction, ≥ 1 allegation
  explicitly not established.
- **case_notes**: ≥ 1 chronology table with ≥ 6 dated events, ≥ 1 status transition.
- **correspondence**: ≥ 1 cross-document reference, and in ~25% of documents a correction/retraction.
- **memorandum**: ≥ 1 qualified conclusion and ≥ 1 authority reference.
- **press_release**: ≥ 2 verifiable figures and ≥ 1 attributed quote.

### 11.2 Trade-offs

| choice | benefit | cost |
|---|---|---|
| deeper documents (2.5–4.5k words for agreements) | realism; tables carry real records; fewer docs needed for the same fact count | parse time (~33–38 pages/s PDF in the audit); memory; longer QA prompts |
| more documents at V2 depth | cheap statistical power; fast ingestion | does not test extraction of structure; duplication dominates |
| more tables per document | exercises table extraction, the biggest V2 gap | PDF table fidelity is the weakest link (D7); gold must tolerate flattening |
| wider length distribution | realistic variety; tests pagination | page-count-based planning must be replaced by content-based planning |
| prose in 3 formats + separate workbooks | cross-format evaluation without pretending prose is a spreadsheet | 3× parse cost per prose document; workbooks parsed once |

Recommended corpus shape for the V3 pilot: **seven prose documents (one per type) × 3 prose formats = 21
artifacts**, plus **one workbook** (XLSX), plus a **four-format equivalence fixture set counted separately** (§13).
Scale to ~70 prose documents × 3 formats + ~10 workbooks before considering 500. Rendering prose to XLSX is
explicitly out of scope (§7.7).

Physical line count is explicitly **not** a depth measure in V3. The audit showed a 60-line document can carry 234
words; V3 gates on words, sentences, sections, tables, records and facts.

### 11.3 Contradiction budget and distribution (decision DEC-5)

- **Density:** at least **one deliberately grounded contradiction per 50 documents**, rounded up. A 7-document pilot
  therefore contains at least one; a 70-document checkpoint at least two; a 500-document corpus at least ten.
- **Grounding:** both sides are stated facts with parser-validated evidence in different documents, with a shared
  event or subject and a genuinely different amount, date, status or relationship dimension (a wording difference is
  not a contradiction).
- **Deterministic distribution:** the pair is chosen by `seed_scope("contradiction", n)` from a table of eligible
  document-type pairs, so the assignment is reproducible and is *not* always the same two types.
- **Distribution requirement:** across a corpus, contradictions must appear in ≥ 3 distinct document types and in
  every format cohort that carries prose. A contradiction is never placed inside a single document, and never only
  in the equivalence fixture set.
- **Variety:** no more than 40% of contradictions use the same dimension (amount/date/status/relationship) in one
  corpus.
- **Pilot:** the 7-document pilot must include one contradiction scenario even though 7 < 50; the rounding rule makes
  this mandatory rather than optional.

---

## 12. V3 validation strategy

### 12.1 Existing checks that must survive unchanged

- `synth.eval.gold.validate()` — every quote located in canonical text; references valid.
- `assert_gold_valid()` — treats `SENTENCE_MISMATCH` as fatal.
- Semantic checks (`semantics.py`): `check_relationship_direction`, `check_proposition_statuses`,
  `check_temporal_support`, `check_entity_identity`, `check_transactions`, `check_contradictions`,
  `check_unanswerable_proofs`, `check_evidence_quotes`, `check_unique_answers`.
- Determinism (same seed ⇒ identical bytes), idempotent ingest, overwrite guards, SQLite-only store.

### 12.2 New checks

| check | what it proves | failure mode |
|---|---|---|
| model invariants (§4.4) | the canonical document is internally coherent before rendering | build error |
| renderer totality | every canonical block appears exactly once in the `RenderMap` | build error |
| render determinism | two renders of one document are byte-identical | build error |
| cross-format surface equality | names/amounts/dates/percentages/statuses identical across formats | build error |
| alignment completeness | every fact-bearing statement has an acceptable verdict | build error |
| alignment uniqueness | no ambiguous quote with different facts | build error |
| evidence exactness | every quote is a substring of the parsed text | build error |
| table fidelity | declared rows/cols vs parser `table_row` count, per format | graded metric |
| provenance completeness | every gold item carries canonical + render + parse locations | build error |
| diversity gates (§10.4) | the corpus is not a template farm | build error at checkpoint |
| depth gates (§11) | each type meets its range | build error at checkpoint |
| contradiction grounding | every contradiction has two parser-grounded sides in different documents | build error |
| contradiction distribution | density and type/format spread satisfy §11.3 | build error at checkpoint |
| gold pair parity | v1 projection and v2 artifact describe the same items; no item exists in only one | build error |
| regression fixtures | the fixed fixture set matches its recorded hashes byte-for-byte | build error |

### 12.3 Severity model

- **Build-fatal:** invariant violation, missing/ambiguous/altered fact, cross-format drift, evidence not a
  substring, depth or diversity gate breach.
- **Warning (recorded in manifest):** parser flattens a table (D7), heading recovered heuristically (D8), numeric
  display lost (D6), pageless format, duplicate occurrence in HTML (D3).
- **Reported metric only:** extraction recall/precision against gold, per format (§13), and `bbox_coverage` — an
  absent bounding box is acceptable and is never treated as a warning or a failure (§6.2).

A warning never downgrades a build-fatal condition. This mirrors V2's discipline, where the gold validator's soft
`SENTENCE_MISMATCH` was wrapped to make it fatal.

---

## 13. Multi-format evaluation methodology

### 13.1 Unit of counting: the semantic document, not the file

The same canonical document rendered four ways is **one** semantic unit. Counting each file as an independent
document would inflate fact counts 4× and let one fact appear four times in precision/recall.

```text
prose semantic document (doc:00042)      workbook document (doc:00077)
├── txt  artifact   (sha256 a1)          └── xlsx artifact (sha256 b1)
├── pdf  artifact   (sha256 a2)                    │
└── docx artifact   (sha256 a3)                    │
        │                                          │
        └────────── fact:txn:0042:03 ──────────────┘   same fact id, two documents
```

Rules:

1. Fact counts are keyed by `fact_id`; a fact is counted once per semantic document regardless of occurrences.
2. Per-format scores are computed **within** a cohort, then averaged with equal weight per format — not pooled by
   file count.
3. Corpora that mix "TXT-only documents" and "multi-format documents" must report them as separate cohorts.
4. **Cross-format equivalence fixtures are not benchmark items.** The same semantic document rendered to TXT, PDF,
   DOCX and XLSX is a *verification* artifact, not four documents. Equivalence fixtures live in their own tree
   (`fixtures/equivalence/`), are excluded from corpus document counts, fact counts, QA counts and every benchmark
   metric, and are reported in a separate equivalence section. Their purpose is to prove renderers and parsers agree,
   not to add volume.
5. **One format per benchmark semantic document,** unless a format cohort is explicitly declared as duplicating it
   (e.g. `prose_pdf` and `prose_docx` are different cohorts, each counted once, never averaged into one "document"
   count).
6. **Dedupe keys are explicit:** benchmark counts dedupe on `(corpus, fact_id)`; equivalence counts dedupe on
   `(fixture_id, fact_id)` and are never summed with the benchmark. A single total that mixes both is a reporting
   bug.

### 13.2 Format cohorts

| cohort | contents | purpose |
|---|---|---|
| `prose_txt` | TXT only | V2-comparable baseline; parser = plaintext |
| `prose_pdf` | PDF only | physical pagination, bbox, heuristic headings |
| `prose_docx` | DOCX only | real structure, split fields, pageless |
| `table_pdf` | PDF with positioned tables | the known-weak path (D7) |
| `workbook_xlsx` | XLSX workbooks | typed cells, sheets, no section paths |
| `equivalence_fixture` | the same fixture documents in all 4 formats | cross-format consistency only — **excluded from benchmark counts** |

The equivalence fixture set is a fixed, versioned subset (one document per prose type plus one workbook and one
thread), stored under `fixtures/equivalence/` and hashed. It is rendered on every change and checked in §13.5, but it
never contributes to corpus size, fact totals, QA totals or extraction metrics. Keeping it separate is what prevents
identical semantic content from inflating evaluation counts fourfold.

### 13.3 Metrics, per format

| metric | definition | notes |
|---|---|---|
| sentence alignment rate | canonical sentences with verdict ∈ {exact, normalised, split, merged} / total | split/merge reported separately |
| extraction recall | gold facts found / gold facts | per fact type |
| extraction precision | found facts that are gold / found facts | requires a matcher; reuse `synth.eval.match` |
| evidence exactness | gold quotes that are exact parser substrings / gold quotes | must be 100% |
| provenance accuracy | gold items whose parse location matches the aligned sentence | must be 100% |
| table cell F1 | cells matched with row/col association | expected low for PDF (D7) |
| table row recovery | rows reconstructed with correct arity / declared rows | PDF vs DOCX vs XLSX comparison |
| header/footer contamination | filtered lines that leaked into canonical text | must be 0 |
| page-boundary accuracy | parsed page for a span == render page (physical formats only) | null for pageless |
| bbox coverage | PDF spans with a real bbox / all PDF spans | graded, never gated at 100%; a fabricated box is a failure |
| numeric fidelity | values whose parsed scalar equals the canonical value | XLSX display loss (D6) separated from value loss |
| date fidelity | parsed date == canonical date | ISO vs long-form normalisation recorded |
| reading-order flags | documents flagged by `order_check` | expected 0 for single-column |

### 13.4 Known limitations to encode, not solve

| limitation | evaluation consequence |
|---|---|
| PDF tables lose structure (D7) | `table_pdf` cell F1 is expected to be low; row recovery is the reported metric, not a pass/fail |
| DOCX/PDF block boundaries differ (D4, D14) | alignment rate is the metric; positional equality is never asserted |
| XLSX loses formatting (D6) | numeric fidelity graded on values; display formatting graded separately |
| HTML duplicates table headers (D3) | HTML excluded from `equivalence_fixture`; if used, duplicates are deduped before scoring |
| OCR unavailable (D9) | no scanned cohort; scanned inputs are `EMPTY` and excluded |
| JSON/XML dispatch inconsistent (D1, D2) | not V3 output formats; unparseable inputs must surface a reason, not `EMPTY` |

**No renderer is promised to fix a parser limitation.** A V3 PDF renderer can draw a real table; the current
`native` pipeline will still flatten it. The evaluation reports that gap rather than hiding it.

### 13.5 Format-equivalence checks

For every cohort document, after ingestion:

1. Canonical fact set per format == canonical fact set of the semantic document (no additions, no losses).
2. `fact_surfaces` normalised across formats → identical (§7.6).
3. Section count within tolerance for structured formats (PDF headings are heuristic — tolerance ±1).
4. Table count/rows: exact for DOCX/XLSX, graded for PDF.
5. No fact is grounded on header/footer text in any format.

---

## 14. Dependency implications

### 14.1 Runtime dependencies V3 adds

| capability | package | size / note |
|---|---|---|
| DOCX read/write | `python-docx` | small; pure-Python |
| XLSX read/write | `openpyxl` | small; pure-Python |
| PDF write | `reportlab` (**selected**, DEC-1) | deterministic output, no system libraries; `weasyprint` rejected — CSS layout is less predictable and needs native libs |
| PDF/DOCX/XLSX parsing | `docling-slim[convert-core,format-pdf,format-office,format-web]` | **492 MB** venv measured; offline; no torch |
| PDF page facts | `pypdfium2` | already used by the audit adapter |
| OCR / table / layout models | torch + docling-ibm-models | **~2.5 GB + ~0.5 GB weights**; out of scope |

Notes:

- TXT rendering stays stdlib. A TXT-only run must continue to work with **zero** new dependencies.
- The parser's imports are lazy today (`docling`, `pypdfium2`, `pypdf` are imported inside conversion functions),
  so a TXT-only pipeline does not pay the cost.
- V3 should declare these as an optional extra (e.g. `pip install -e .[multiformat]`) rather than hard
  dependencies; the base package must remain installable with the current requirements.
- No package is installed in this phase (explicit restriction).

### 14.2 Environment constraints

- `app/ingest/adapter.py` lazily imports Docling; without it every non-TXT file becomes `FAILED`. V3 renderers add
  **output** dependencies, not parse dependencies — the two must be declared separately.
- `synth/config.py` resolves `REPO_ROOT` from the source tree; the export only works as an editable install. V3
  must not make that worse (no new absolute paths in package data).
- Windows path handling: renderers must write with explicit encodings (`utf-8`) and no `\r\n` surprises in TXT.

---

## 15. Storage and performance considerations

### 15.1 Per-artifact sizes (estimates to be measured in Phase A)

| artifact | estimated size | basis |
|---|---|---|
| `CdmDocument` JSON | 40–120 KB | 1.5–3k words + tables + statements |
| `RenderMap` JSON | 10–40 KB | one row per block/row/cell |
| `AlignmentGraph` JSON | 15–60 KB | one row per canonical sentence + cells |
| TXT artifact | 10–30 KB | 3k words |
| DOCX artifact | 30–80 KB | zipped XML |
| XLSX artifact | 25–70 KB | zipped XML |
| PDF artifact | 40–150 KB | embedded fonts dominate |
| Gold file (v2) | 15–120 KB | grows with evidence metadata |

At a 500-document corpus (§15.4: 1,570 artifacts): roughly **0.4–1.2 GB** of artifacts plus **0.2–0.4 GB** of
canonical/alignment JSON,
plus **two gold trees** (`gold/v1` + `gold/v2`, ≈1.4× a single tree, §8.7) and a small equivalence fixture set that is
versioned with the code rather than counted as corpus data.
The 5,000-document corpus at V3 depth would be ~4–10 GB and is **not** recommended before the pilot is measured.

### 15.2 Processing cost

Measured in the audit: PDF parse 1.30 s for 50 pages, full ingest 1.54 s (~33–38 pages/s); 14 mixed fixtures in
4.15 s including converter construction. V2's 5,000-document corpus ingested quickly because each document is 3
pages of plain text.

At V3 depth (avg ~6 pages), a 500-document corpus implies ~3,000 prose pages across three formats plus 70 workbook
sheets, i.e. roughly **80–120 s** of PDF parsing plus DOCX/XLSX (faster) — acceptable. The dominant cost becomes **canonical generation
and alignment**, which are pure Python, and **gold/QA validation**, which re-reads every document.

### 15.3 Store and memory

- One in-memory SQLite store per run is the current contract (`synth.store.test_store()`), reused for all
  documents. V3 keeps it, but alignment results must be written to disk as artifacts rather than held in RAM for the
  whole corpus.
- Renderers must stream large tables row-by-row for XLSX; `openpyxl` write-only mode avoids holding a workbook.
- Alignment should be per document and released after the gold file is written (V2 already writes gold per
  document).

### 15.4 Scaling guidance

| corpus | semantic documents to artifacts | recommendation |
|---|---|---|
| pilot | 7 prose x 3 + 1 workbook = 22 (+ equivalence fixtures, uncounted) | Phase I gate; full validation, all checks fatal |
| checkpoint | 70 prose x 3 + 10 workbooks = 220 (+ fixtures) | Phase J; diversity/depth/contradiction gates measured |
| scale | 500 prose x 3 + 70 workbooks = 1,570 (+ fixtures) | only after timing and storage are measured at 220 |

Artifact counts drive cost, not semantic-document counts: a prose document costs three parses, a workbook one. The
earlier four-formats-per-document figure over-counted prose and under-modelled workbooks.

---

## 16. Risks, resolved decisions and remaining blockers

### 16.1 Risks

| # | risk | likelihood | impact | mitigation |
|---|---|---|---|---|
| R1 | Alignment ambiguity on repeated boilerplate (HTML header duplication, D3) | medium | gold rejection | unique-match rule; dedupe; exclude HTML from equivalence cohort |
| R2 | PDF tables never recover structure (D7) | high | table gold for PDF weak | grade PDF tables, gate DOCX/XLSX; do not claim PDF table fidelity |
| R3 | Renderer/parser drift: a renderer changes text subtly (hyphenation, ligatures, smart quotes) | medium | alignment failures | renderers emit plain Unicode; normalisation pass; byte-determinism test |
| R4 | Diversity illusion: more templates, same information structure | high | V3 looks varied but is not | information-shape metric; structural-signature uniqueness |
| R5 | Depth inflation by padding | medium | violates the brief | fact/table/record minima per type; no adjective-only sections |
| R6 | Gold v2 incompatibility with existing validators | medium | broken tooling | side-by-side v1/v2 artifacts (DEC-3); v1 stays validator-compatible; no edit to `synth/` |
| R7 | Generation time blows up with tables + multiple formats | medium | slow iteration | pilot-first; per-document parallelism; render only the formats a document kind declares (§7.7) |
| R8 | New dependencies break the zero-dependency TXT path | low | install friction | optional extra; lazy imports; TXT path stdlib-only |
| R9 | Cross-format facts drift because a renderer formats a value independently | medium | silent inconsistency | `fact_surfaces` comparison as build-fatal |
| R10 | QA explosion: more facts → more questions → review burden | high | unusable benchmark | cap QA per fact type; sample deterministically; keep unanswerable ratio |
| R11 | ReportLab pagination drifts from the parser's page count | medium | page-boundary metric invalid | prototype in Phase D gates the writer; assert renderer page count == `pypdfium2` count before proceeding |
| R12 | Contradiction budget impossible at small sizes without distorting type mix | low | pilot fails a gate | rounding rule (§11.3) makes 7 documents require one; distribution checked per corpus, not per document |
| R13 | Thread model adds cross-document coupling that breaks determinism | medium | non-reproducible letters | thread chosen from `seed_scope("thread", n)` before messages; message order fixed by id, not generation order |

### 16.2 Resolved decisions (approved at review)

| id | decision | status | where applied |
|---|---|---|---|
| DEC-1 | **PDF renderer = ReportLab**, subject to a parser-ingestion prototype in Phase D. Deterministic generation and controlled layout take priority over CSS-based layout. | approved | §7.4, Phase D |
| DEC-2 | **Page-level provenance is mandatory where physical pagination exists; bounding boxes are stored only when reliably available and are never fabricated.** | approved | §6.2, §6.4, §7.3, §13.3 |
| DEC-3 | **Gold versioning uses side-by-side versioned artifacts** (`gold/v1`, `gold/v2`). The vendored `synth.eval.gold` model is not modified unless the alignment prototype proves it necessary (ADR required). | approved | §8.4, §8.7, Phase G |
| DEC-4 | **XLSX uses the specialised workbook representation and renderer.** No covering memo is generated by default; a separately authored memorandum may reference the workbook. | approved | §7.5, Phase F |
| DEC-5 | **At least one deliberately grounded contradiction per 50 documents**, deterministically distributed across document types and formats; the pilot must contain a contradiction scenario. | approved | §11.3, §10.4, Phase B, Phase I |
| DEC-6 | **No global byte-identical prefix requirement** for content-driven corpora. Fixed canonical regression fixtures plus determinism per `(seed, configuration)` replace it. | approved | §5.3, §12.2, Phase B, Phase J |
| DEC-7 | **Explicit correspondence thread model**; individual messages remain independently addressable documents with stable thread and message identifiers. | approved | §4.1, §4.2, §5.2, §9.5, Phase B |
| DEC-8 | **Authoritative id format is `<prefix>:<readable-scope>:<short-hash>`.** The implementation in `ids.py` stands; §5.1's earlier formula was corrected to match it. | approved | §5.1, §5.2 |
| DEC-9 | **`FactStatement` carries optional canonical sentence ids** alongside its mandatory structural node reference; parser sentence ids stay separate and are never invented in Phase A. | approved | §4.2, §4.4, Phase A |
| DEC-10 | **Nested tables inside cells are unsupported and rejected.** Nested schedules become separate tables or worksheets with explicit references. | approved | §4.2, §4.4, §7.5, Phase A |
| DEC-11 | **Cell types are authoritative; column types are optional expectations (`None`/`mixed`).** Display, number format and cached formula value stay distinguishable; no coercion. | approved | §4.2, §4.4, §7.5, Phase A |

These are no longer open questions. A later change to any of them requires a superseding ADR that names the phase
affected and the tests that must be re-run.

### 16.2.1 Review constraints and where they are enforced

| constraint from review | enforced in | mechanism |
|---|---|---|
| one semantic source of truth across renderers | §3.1 rule 7, §7.3, §7.6 | immutable `CdmDocument`; renderers have no write path; `fact_surfaces` compared across formats |
| a fact in several locations keeps one `fact_id` | §3.2, §4.4 rule 9, §7.5 rule 3 | statements reference a fact; a second id for one fact is a build error |
| facts, statements, rendered occurrences stay distinct | §3.2, §4.1, Appendix C | three object types with different lifetimes and owners |
| gold evidence comes from parser-extracted content | §8.3 rule 1, §8.7 rule 2 | quote sliced from `DocView.text`; intended surface never used as evidence |
| ambiguous or unsupported alignment is rejected | §8.3 rule 5, §8.5 | build-fatal; no partial-credit path |
| equivalence fixtures stay out of the benchmark | §13.1 rules 4–6, §13.2 | separate `fixtures/equivalence/` tree and dedupe keys |
| structural diversity, not textual variation | §10.1, §10.4 | information-shape, signature and table-shape metrics |

### 16.3 Remaining blockers (must be closed before the phase that needs them)

| # | blocker | blocks | closure criterion |
|---|---|---|---|
| B1 | ReportLab must demonstrate that its page count matches the parser's independent page count on the seven-type fixture set | Phase D exit | renderer page count == `pypdfium2` count on 7/7 fixtures, twice, across processes |
| B2 | The alignment prototype must prove that split/merge handling preserves exact parser substrings for every fact type (including table cells) | Phase G start | 100% exact-substring evidence on the fixture set, with at least one split and one merge case per prose type |
| B3 | The v2 gold artifact must be expressible without modifying `synth/eval/gold.py`; if not, an upstream ADR is required | Phase G exit | v1 projection validates with the vendored validator; v2 validates with the V3-side validator; no edit under `synth/` |
| B4 | PDF table behaviour must be measured on the V3 writer, not assumed from the audit (which used a different fixture generator) | Phase D / Phase I | table row-recovery metric reported for V3 PDFs; no pass/fail claimed |
| B5 | Storage envelope must be measured on the pilot before the 70-document checkpoint is authorised | Phase I exit | measured artifact sizes within ±50% of §15.1 estimates, or §15 revised |
| B6 | Contradiction distribution must be shown to work at 7, 70 and 500 documents without repeating the same type pair | Phase B / I / J | distribution report per corpus; ≥ 3 types per cohort; ≤ 40% one dimension |

### 16.4 Deferred quality gates (explicitly not blockers)

| gate | when | note |
|---|---|---|
| `mypy`/`pyright` clean on `synthetic_data/model` | Phase B CI | The modules are fully annotated; the tools are not installed in the Phase A environment and installing packages was out of scope. This is a CI gate, not an unresolved design question. |
| property-based tests for id allocation and table dimensions | Phase C | The Phase A suite covers example-based determinism and collision cases; property tests scale that to random structures. |

---

## 17. Phased implementation plan

Each phase is independently testable and ends with an explicit exit criterion. No phase may begin before the
previous phase's exit criteria are met. All phases keep V2 datasets and the Synth/app ingestion code untouched.

### Phase A — Canonical model and schema

**Deliverables.** `synthetic_data/model/` with `values.py`, `semantic.py`, `content.py`, `workbook.py`, `ids.py`,
`render.py` (dataclasses/protocols, no generation logic); `Thread`/`MessageMeta`; JSON Schema for `CdmDocument`;
unit tests for ID determinism, invariant checks, serialisation round-trip; ADRs recording the ID scheme, the XLSX
workbook decision (DEC-4), the thread model (DEC-7) and the gold artifact layout (DEC-3).

**Depends on.** Nothing.

**Acceptance tests.** Same `(seed, scope)` yields identical ids across processes; ids do not change when a document's
optional section is omitted; thread and message ids are stable and message ids sort in thread order; invariant
checker rejects each of the 11 violations in §4.4; round-trip serialise→deserialise→serialise is byte-identical;
fully type-annotated with mypy/pyright enforced as a CI gate from Phase B (§16.4, not a Phase A blocker);
no file under `synth/` or `app/` is modified.

**Phase A amendments (DEC-8–DEC-11, applied after the first acceptance).** `FactStatement.canonical_sentence_ids`
(optional, canonical-only); explicit rejection of nested table payloads in cells; `ColumnSpec.type` optional with a
`mixed` declaration; `TableCell.number_format`; type-authoritative cell validation with no coercion. Schemas and
regression tests updated with the models.

**Exit criteria.** A hand-written `CdmDocument` (one per type) validates, round-trips, and fails on each injected
invariant violation. No generator code modified.

### Phase B — Rich content and world generation

**Deliverables.** Extend `world.py` with conditions, cross-references, ownership percentages, transaction
schedules, threads and the contradiction schedule; structural grammars (3–5 per type) with deterministic selection;
content planners producing `CdmDocument` for all seven types; the fixed canonical regression fixture set; depth,
diversity and contradiction-distribution metrics computed from plans (no rendering).

**Depends on.** Phase A.

**Acceptance tests.** Plans for all 7 types meet the §11 ranges at the pilot size; structural signature uniqueness
≥ 95%; template inventory ≥ 1.2 per document in the pilot; no document exceeds its type's fact cap; facts resolve
to entities and conditions; every contradiction is cross-document and satisfies §11.3 density and spread; threads
satisfy the §9.5 rules; the regression fixtures reproduce their recorded hashes; deterministic across processes at
fixed `(seed, configuration)` — **no prefix relation is asserted**.

**Exit criteria.** 70 planned documents across 7 types satisfy every depth, diversity, thread and contradiction gate
**without rendering**, and the regression fixture set is committed as hashes in the manifest test.

### Phase C — TXT renderer and baseline tests

**Deliverables.** `renderers/txt.py` implementing the `Renderer` protocol; `RenderMap` emission; TXT profile with
explicit wrapping; tests for totality, determinism, blank-line share ≤ 25%, and byte-stability.

**Depends on.** Phases A–B.

**Acceptance tests.** Every canonical block appears exactly once; blank-line share ≤ 25%; no line exceeds the wrap
width unless a single token does; two renders byte-identical; `RenderMap` covers 100% of blocks/rows/cells; TXT
renders with zero third-party imports.

**Exit criteria.** 70 TXT documents render and re-ingest; the regression fixtures match byte-for-byte; existing V2
gold path still passes its own tests.

### Phase D — PDF renderer and parser validation

**Deliverables.** First a **parser-ingestion prototype** (decision DEC-1 / blocker B1): a 2-document ReportLab spike proving
page count, text extraction and provenance survive `u01_parser.ingest_path`. Only then `renderers/pdf.py` (ReportLab);
physical pagination; headers/footers; tables drawn as grids; `RenderMap` with a mandatory page and an optional bbox;
a fixture set of 7 documents.

**Depends on.** Phase C.

**Acceptance tests.** Prototype: `pypdfium2` page count equals the renderer's on 7/7 fixtures, twice, across
processes; extracted text contains every fact sentence. Renderer: page recorded for 100% of spans; `bbox` is either
a real box or `null` — never estimated; gold alignment rate ≥ 95% for prose blocks; table cells align individually
when the parser flattens rows (D7); no invented page numbers; determinism across processes (same sha256).

**Exit criteria.** B1 closed (page-count parity), PDF cohort ingests with 0 build-fatal alignment failures on prose
facts, `bbox_coverage` reported as a metric, and the known table gap recorded as graded rather than surprising.

### Phase E — DOCX renderer and parser validation

**Deliverables.** `renderers/docx.py` (python-docx); real headings, paragraphs, tables, footnotes; `pageless: true`
provenance.

**Depends on.** Phase C.

**Acceptance tests.** Section count preserved; table row/col count exact; `Label: value` split (D4) handled by
alignment without gold loss; alignment rate ≥ 95%; no page numbers emitted.

**Exit criteria.** DOCX cohort passes all build-fatal checks with documented `pageless` semantics.

### Phase F — XLSX workbook renderer and parser validation

**Deliverables.** `model/workbook.py`, `renderers/xlsx.py` (openpyxl write-only); sheet-per-table-family; typed
cells; display vs value; optional formulas with cached values; workbook-to-document linking. **No covering memo is
generated** (decision DEC-4): a memo is a separately planned document when a scenario calls for one.

**Depends on.** Phases A, E (shares table model).

**Acceptance tests.** Cell values round-trip exactly for money/percent/date/int; parser `table_row` count equals
declared row count; numeric fidelity 100% on values; display fidelity graded (D6); no section paths expected and
none claimed.

**Exit criteria.** XLSX cohort passes value-fidelity checks with display loss recorded as a known limitation, and
no automatic companion artifact is produced for any workbook.

### Phase G — Evidence alignment and gold generation

**Deliverables.** `alignment/` with the §8 algorithm; `AlignmentGraph` artifact; **side-by-side `gold/v1` and
`gold/v2` emission** (decision DEC-3); a V3-side v2 validator; defect detectors (missing/duplicated/altered/extra/
ambiguous); alignment report per document; B2 split/merge proof.

**Depends on.** Phases C–F.

**Acceptance tests.** 100% of gold quotes are exact parser substrings; 0 ambiguous alignments; at least one split
and one merge case per prose type preserved with exact quotes; every injected defect (missing fact, altered amount,
duplicated fact, extra fact) is detected and fails the build; v1 projection passes the vendored
`synth.eval.gold.validate()`; v2 passes the V3 validator; no file under `synth/` is modified; alignment is
deterministic.

**Exit criteria.** B2 and B3 closed, all four format cohorts produce a v1/v2 gold pair with zero build-fatal defects,
and the "no upstream change needed" conclusion is recorded (or an ADR is raised).

### Phase H — QA and independent semantic validation

**Deliverables.** QA generation over canonical facts reading the **v2 gold artifact** with per-format evidence; new
question types for status, conditions, contradictions, thread reply structure and cross-document references;
unanswerable proofs updated for richer structures; semantic checks extended to tables, conditions and threads.

**Depends on.** Phase G.

**Acceptance tests.** Every answerable question's evidence is a parser-exact quote; unanswerable proofs still hold
(no fact exists in the canonical layer); QA counts within the per-type cap; no duplicate questions across formats.

**Exit criteria.** QA validates on all four cohorts; existing 112 tests still pass.

### Phase I — Small V3 pilot

**Deliverables.** 7 prose documents × 3 formats = 21 artifacts plus 1 workbook = 22, **including at least one
contradiction scenario** (decision DEC-5); a separate `fixtures/equivalence/` set; full v1/v2 gold, QA, validation and
evaluation report;
manifest with depth, diversity, contradiction, alignment and per-format metrics.

**Depends on.** Phases A–H.

**Acceptance tests.** All depth/diversity gates pass at the pilot size; the pilot contains ≥ 1 grounded cross-document
contradiction; equivalence fixtures are reported separately and absent from benchmark totals; alignment rate ≥ 95%
prose; table metrics reported; storage and timing measured (B5); no build-fatal condition downgraded.

**Exit criteria.** B5 closed, and a reviewed pilot report that states which V2 limitations are measurably fixed and
which remain (PDF tables, OCR, XLSX display).

### Phase J — Scale and benchmark

**Deliverables.** 70 prose documents × 3 formats + 10 workbooks = 220 artifacts; ≥ 2 contradictions distributed per
§11.3 (B6); timing and storage benchmark; evaluation scorecard per format and per type, **with equivalence fixtures excluded**;
reproducibility check from the manifest and the regression fixture hashes.

**Depends on.** Phase I.

**Acceptance tests.** Determinism across two full runs at fixed `(seed, configuration)`; no prefix relation asserted;
depth/diversity/contradiction gates hold at scale; parse throughput and artifact sizes within the §15 envelope;
benchmark totals exclude `fixtures/equivalence/`; scorecard reproducible from artifacts alone.

**Exit criteria.** B6 closed, and a go/no-go decision for a 500-document V3 corpus with measured cost per
document-format.

---

## Appendix A — Mapping from V2 concepts to V3

| V2 concept | V3 equivalent | change |
|---|---|---|
| `World.entities` | `CorpusWorld.entities` | + jurisdiction, aliases, attributes preserved |
| `World.facts` | `SemanticFact` | + modality, conditions, statement links |
| `World.transactions` | `Transaction` | + instrument, linked fact, schedule membership |
| `World.contradictions` | `Contradiction` | + dimension, document pair |
| `DocumentPlan.page_lines` | `CdmDocument.sections[].blocks[]` | lines → structure |
| `DocumentPlan.sentences` | `FactStatement` + structural sentences | sentences become statements + prose |
| `DocumentPlan.stated_facts` | `FactStatement` | one fact may have several statements |
| `DocumentPlan.mentions` | `InlineRun.entity_ref` | mentions anchored to runs |
| `DocumentPlan.render_text()` | `Renderer.render()` | format-specific |
| `_canonical_sentence_index` | `AlignmentGraph` | positional → many-to-many |
| `GoldSet` (v1) | `gold/v1` (validator-compatible) + `gold/v2` (superset) | side-by-side versioned artifacts (DEC-3); `synth/` untouched |
| — (no thread model) | `Thread` + `MessageMeta` | letters become ordered, independently addressable messages (DEC-7) |
| `manifest.json` | V3 manifest | + alignment, diversity, depth, per-format metrics |

## Appendix B — Reusable validation tooling

The `corpus-depth-audit` skill and its `scripts/audit.py` already measure everything §10.4 and §11 gate on:
words, characters, sentences, paragraphs, pages, tables, entities, facts, duplication (within-document and masked
cross-corpus), percentiles and histograms. V3 validation should call that harness rather than reimplement the
metrics:

```bash
# prose cohort (TXT/PDF/DOCX); workbooks are a separate corpus tree
python <skill>/scripts/audit.py --repo <repo> --corpora <v3-prose-corpus> \
       --formats txt,pdf,docx --out <outside-repo>/v3-audit-prose

# workbook cohort
python <skill>/scripts/audit.py --repo <repo> --corpora <v3-workbook-corpus> \
       --formats xlsx --out <outside-repo>/v3-audit-xlsx
```

The two runs are reported separately because they measure different document kinds (§7.7); summing them would
repeat the over-counting that §13.1 exists to prevent.

Its report already separates formatting inflation from content density and refuses to report physical lines as
depth — the same discipline §11 requires.

## Appendix C — Definitions

| term | meaning in V3 |
|---|---|
| canonical document | the L2 `CdmDocument`; format-independent structure + statement links |
| semantic document | the L1 corpus objects a document states; counted once across formats |
| statement | one realisation of a fact at one canonical node |
| occurrence | one rendering of a statement in one artifact |
| alignment | the many-to-many map between canonical sentences and parser sentences |
| equivalence cohort | the same semantic document rendered to several formats |
| build-fatal | a condition that fails generation; never downgraded to a warning |
| pageless | a format with no physical pagination; page fields are null by contract |
| thread | a corpus-level ordered set of correspondence messages; documents stay independent |
| message | one correspondence document, identified by `message_id` and positioned by `sequence` |
| contradiction density | grounded cross-document contradictions per 50 documents (§11.3) |
| equivalence fixture | a fixed cross-format verification artifact; excluded from benchmark counts |
| regression fixture | a fixed canonical document rendered and hashed on every change (replaces prefix checks) |
| canonical sentence id | `snt:…` identifier assigned by the Phase C canonicalizer; never a parser sentence id |
| column expectation | optional `ColumnSpec.type`; describes a column, never coerces a cell value |
| nested table | a table inside a cell — unsupported by design (DEC-10) |
| bbox coverage | share of PDF spans with a real bounding box; graded, never fabricated |
