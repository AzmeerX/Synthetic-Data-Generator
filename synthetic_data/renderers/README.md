# V3 renderers — TXT (Phase C), PDF (Phase D), DOCX (Phase E), XLSX (Phase F)

`TxtRenderer` implements the `Renderer` protocol from
`docs/V3_CANONICAL_DOCUMENT_MODEL.md` §7.2 for `format == "txt"`.  It is a pure
function of `(CdmDocument, RenderProfile)`: no clock, no randomness, no model,
network or store access, and no third-party imports.

```
renderer = TxtRenderer()
result = renderer.render(document, DEFAULT_TXT_PROFILE, Path("out/doc.txt"))
result.path        # file written
result.sha256      # sha256 of the exact bytes
result.spans       # RenderSpan per canonical node and per sentence
result.fact_surfaces
result.warnings    # layout exceptions, reported not repaired
```

## Layout contract

| Element | TXT form |
| --- | --- |
| metadata | `Label: value` lines under the header |
| title / subtitle | leading lines, before the metadata block |
| sections | numbered heading line (`1. Heading`, `2.1 Heading`) |
| paragraphs | whole sentences packed per line, one blank line between blocks |
| lists | `- ` unordered, `(n) ` ordered — never `n. `, which the parser reads as a heading |
| tables | one row per line, `\| cell \| cell \|`, caption line above |
| footnotes | marker `[n]` appended to the last line of the referencing block, notes at the end |
| exhibits / signatures | block runs rendered verbatim |
| headers / footers | once, at the top and the bottom of the document |
| page breaks | `\f`; one logical page per top-level section, plus explicit `page_break` blocks |

`\f` is the only page separator.  Pages are **logical**, never physical: the
renderer never invents a physical page number, and `RenderLoc.pageless` is always
`False` with `bbox is None` for TXT.

## Sentences

`synthetic_data/canonical.py` allocates `snt:<doc>/<section>.<block>.<nn>:<tag>`
from the document id alone, so a canonical document renders to the same sentence
ids in any corpus and any process.  The canonical text of a node is the *same*
string the renderer writes (`block_text`, `row_text`, `list_item_text`,
`heading_text`), which is what lets a sentence id and a rendered line be compared
without heuristics.  Parser sentence ids (`snt_…` ULIDs) live in a separate
namespace and are never written into a canonical field.

A sentence is the unit of addressability, so the renderer **never splits one
across lines**.  Sentences are packed per line up to `page_width_chars`; a single
sentence longer than the width occupies its own line and produces a
`RendererWarning`.  This is a deliberate, documented deviation from a literal
"no line exceeds the width" rule: a line is a parser block, and splitting a
sentence would break the evidence contract (§8.3), which matters more than the
column limit.  `test_v3_txt_renderer.py` asserts that every over-width line holds
exactly one sentence and carries a warning.

## Workbooks

A `doc_kind != "prose"` document is refused with `ValueError`.  A workbook is an
XLSX document and must never be flattened into prose TXT (§7.7).

## Alignment

`synthetic_data/alignment.py` maps canonical sentences to parser sentences and
returns `exact` / `normalised` / `split` / `merged` / `partial` / `ambiguous` /
`missing`.  Quotes are always sliced out of `DocView.text`.  `require_fact_evidence`
raises `AlignmentError` when a fact's sentence is missing, ambiguous, partial,
filtered, or when a rendered surface cannot be found in the fact's quotes.

## Corpus context (optional, Phase D interface)

Entity names live in the corpus world, not in the document, so a renderer that wants
name-level fact surfaces accepts an immutable `RenderContext`:

```
result = TxtRenderer().render(doc, profile, path,
                              context=RenderContext.from_world(world, roles=roles))
```

The context is **additive, read-only and byte-neutral**: `render(doc, profile, path)`
is unchanged, layout and sha256 are identical with or without it, and it only adds
entity names/roles to `fact_surfaces` when the name literally appears in the
rendered sentence.  It carries no clock, no rng, no store and no mutability, and it
never re-derives a canonical id.

## Value surfaces

`Money`, `IsoDate` and `Percentage` stay typed; display strings are presentation.
Each document picks one money style and one date style from the corpus key, so an
instrument is internally consistent while the corpus still varies.  Comparison never
looks at the presentation: `synthetic_data/surfaces.py` normalises

* `"£4,000,000"`, `"GBP 4,000,000.00"`, `"4,000,000.00 pounds sterling"` → `GBP:400000000`
* `"23 January 2023"`, `"2023-01-23"`, `"January 23, 2023"` → `2023-01-23`
* `"40.00%"`, `"40.00 per cent"`, `"4000 basis points"` → `4000bp`

`surfaces_equivalent` and `alignment.surface_in_quote` use those keys, so a
formatting variation is never mistaken for a different amount and a different amount
is never mistaken for a formatting variation.

## Known limits

* `colspan`/`rowspan` are flattened: TXT repeats the cell text once and does not
  merge cells.
* Blank-line share is a consequence of the block layout (one blank between
  blocks); it is asserted ≤ 25 % on the fixture set rather than tuned per document.
* Long tables are one row per line; there is no column alignment padding, because
  padding would move cell offsets without adding information.


# PDF (Phase D)

`PdfRenderer` writes physical pages with ReportLab (`invariant=1`).  `render(document,
profile, out)` is unchanged; `render_with_layout(...)` also returns the layout when a
test needs the page text buffers.

## Geometry and provenance

| item | convention |
| --- | --- |
| page size | `RenderProfile.page_size` — A4 / Letter / Legal, in points |
| margins | `RenderProfile.margins_mm` = (left, top, right, bottom) |
| bbox | `(x0, y0, x1, y1)` in PDF points, origin **bottom-left** (Docling's `BOTTOMLEFT`), always inside the page, `x1 > x0`, `y1 > y0`, `null` when an occurrence has no drawn area |
| char span | `char_start/char_end` index the page text buffer: the drawn strings in draw order joined by `"\n"` — `page_text(page)[start:end] == span.text` |
| page | physical, 1-based; a span never carries `null` page and never `pageless` |

Node spans (sections, blocks, tables, rows, cells) carry a bbox; sentence spans carry
char offsets inside their block, because a sentence is a sub-range of drawn lines, not
a separate rectangle.  Both are real coordinates, neither is estimated.

## Layout rules

* one physical page per top-level section, plus explicit `page_break` blocks;
* headers name the document *kind* (`Facility Agreement — Confidential`) and footers
  carry `Page n of m` and the reference; repeating the *title* in the running head
  would duplicate a body sentence and make it ambiguous evidence, so it does not;
* a title that the first paragraph already states is not drawn twice (warned);
* table columns are sized from each column's widest *word* first (never "Outco me"),
  then the spare width is shared in proportion to demand; the table font shrinks one
  step at a time if the minimum widths do not fit;
* `colspan` merges columns, `rowspan` merges rows; a row covered by a rowspan keeps a
  placeholder cell (the model requires every row to span the declared columns) that
  is located but not drawn twice;
* a table taller than a page is split by rows, repeating the header row visually
  without emitting a second span for it.

## Ingestion reality (D7)

Docling's native PDF pipeline returns **no table items**: every cell is its own text
passage and the row's structure is gone.  The alignment records this rather than
papering over it — a row that arrives as its cells is aligned `split` with
`missing_row_structure=True`, its quote is the exact contiguous parser substring, and
a cell-level statement is quoted from *its own* cell.  A row whose cells arrive as one
sentence is aligned `exact`/`merged`.  Gold for PDF must therefore never assume that a
row exists in the parsed document.


# DOCX (Phase E)

`DocxRenderer` writes a real OOXML package with `python-docx`: `Title`/`Heading N`
styles, ordinary paragraphs, Word list styles, `Table Grid` tables with `gridSpan`
and `vMerge`, a header and footer, a notes section and fixed core properties.

## Pageless provenance (D11)

| item | convention |
| --- | --- |
| page | always `null` — the format has no pagination the parser can establish |
| pageless | always `true` |
| bbox | always `null` |
| section_index | index of the section in the model (0-based) |
| paragraph_index | index in `Document.paragraphs` (0-based), headings and metadata included |
| table_index | ordinal of the table in the document (0-based) |
| row / column / cell_ref | 1-based position and `rNNcNN` for cells |

The parser reports one synthetic page for a `.docx` (D11). That number belongs to the
parser and is never copied into the render map, so a consumer that wants a page must
say so explicitly rather than inherit a fabricated one.

## Determinism

Two saves of one document differ in bytes by default: `python-docx` stamps ZIP members
with the local time and writes `datetime.now()` into `docProps/core.xml`. The renderer
fixes the core properties and then repacks the archive (`normalise_package`): sorted
members, fixed timestamp, fixed attributes, fixed compression. Only container metadata
is rewritten; every XML part is copied byte for byte.

## Ingestion reality (D4, D8-analogue)

* Docling keeps **real table rows** for DOCX: a row arrives as one passage whose cells
  are separated by ` | `. The aligner matches it `normalised` (delimiters ignored) and
  records `missing_row_structure=False` — unlike PDF, the row survived.
* A vertically merged cell is reported once, at its starting row, so the rows it covers
  arrive with fewer cells than the model declares.
* A bold `Label:` splits from its value into two passages (D4). Alignment still locates
  the sentence and the value is still quotable, so field-level gold survives the split,
  but one parser block is **not** one field.
* `section_path` is unreliable: some documents get heading paths, others none. Section
  provenance for DOCX therefore comes from the render map, not the parser.
* Footnote markers are text (`[1]`) plus a notes section, not `footnotes.xml` entries,
  and the renderer declares `footnotes=False`.


# XLSX (Phase F)

`XlsxRenderer` writes a real spreadsheet with `openpyxl`: one worksheet per model sheet,
typed cells, currency/percent/date number formats, frozen header rows, filters, column
widths, defined names and formulas with cached results.  A workbook is never flattened
into prose (DEC-4) and no covering memo is produced.

## Locations

| item | convention |
| --- | --- |
| page | always `null`; the sheet is an addressing unit, not a page |
| pageless | always `true`, `bbox` always `null` |
| sheet_name | the model's sheet name (also the Excel sheet name) |
| section_index | the sheet's ordinal in the workbook |
| table_index | the table's ordinal in the workbook (0-based, unique across sheets) |
| row / column / cell_ref | 1-based sheet coordinates and `A1`-style references |

The parser reports one "page" per sheet (5 for a five-sheet workbook).  That number is
the parser's, and is never copied into the render map.

## Formulas

`openpyxl` writes `<f>SUM(...)</f><v></v>` — the formula with an *empty* cached value, so
a reader that does not recalculate sees nothing.  After saving, the renderer rewrites
each worksheet part to put the model's `CellFormula.cached_value` into `<v>` (with
`t="str"` for text results).  Both `data_only=False` (formula) and `data_only=True`
(value) therefore work, and the file stays a valid workbook.  A formula that exists only
as text is not counted as verified anywhere in the tests.

## Ingestion reality (D6, measured)

* currency symbols and thousands separators are **lost**: `£12,000,000.00` arrives as
  `12000000`;
* percentage and date formats **are** honoured: `40.00%`, `23 Jan 2023`;
* a formula's text is not extracted — its cached value is;
* every row arrives as a `table_row`; no cell coordinates, no sheet name in the row
  text, and no `section_path`;
* blank rows separate the title block from the table, so a sheet's parsed row count is
  the declared table rows **plus** the title row.

Currency is therefore presented by the number format and stated in words on the cover
sheet; gold must not infer `GBP` from a symbol the parser never saw.
