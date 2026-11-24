# Phase G — evidence alignment and gold

Three artifacts per document-format, written together or not at all:

    gold/v1/<doc>__<type>__<fmt>.json         vendored GoldSet (synth.eval.gold)
    gold/v2/<doc>__<type>__<fmt>.json         V3 superset with provenance
    gold/alignment/<doc>__<fmt>.json          canonical -> renderer -> parser map

## What each file is for

`v1` is the compatibility contract. Its sentence ids are parser ids and its quotes
are exact parser substrings, so `synth.eval.gold.validate()` works unchanged. Fact
and entity ids are the **canonical** ids, so the same fact keeps one identity in
every document and every format.

`v2` is the V3 contract: the same items plus, per evidence item, the canonical node,
the canonical sentence ids, the alignment verdict, the render location, the typed
value and — for money — whether the currency is actually in the parser text.

`alignment` is the map itself, including verdicts that would block a build. It is a
record, not an annotation.

## Build order (one-way)

1. align (prose: sentences; workbook: rows and cells through the render map);
2. ground every stated fact and every derived value — missing, ambiguous, partial,
   altered, duplicated and unexplained-money evidence all fail the build;
3. **account for every non-filtered parser sentence** (`synthetic_data/accounting.py`);
4. emit v1, v2 and the alignment record;
5. write all three atomically.

## The accounting gate

Grounding is one-directional: it proves every fact has evidence, not that every
parser sentence was explained. `synthetic_data.accounting` closes that direction.
Each non-filtered sentence must be `aligned`, a declared row/block/heading/caption,
declared metadata/furniture, or blank. Anything else raises `AlignmentError`
(promoted to `GoldBuildError` by the builder) with the document id, parser sentence
id, kind, page and text.

Two allowances exist because the formats need them, and both are bounded:

* drawn lines (headings, captions, metadata, headers, footers, footnotes) match as
  a **contiguous run of their own words**, consumed once — a PDF drops section
  numbers and re-cases running headers, Docling splits `Reference: CO-00006` into
  two sentences, and a paginated PDF may merge `Notes` with the first note;
* a running header is declared **once per page**, because the renderer repeats the
  current section name on every page and this parser does not filter it.

Structural units are consumed one per successful alignment, which is what makes a
duplicate of an already-matched sentence an error rather than a second copy of a
legitimate line.

## Workbook specifics

* the parser returns no sheet name and no coordinates, so sheet/row/column and the
  `A1` reference come from the render map, never from a sentence;
* a row maps to cells only when the token counts agree, otherwise no cell is
  grounded;
* a cell's quote is its own parser token (an exact substring), not the whole row;
* derived values are propositions with `derived_from` inputs, validated against
  their formula and their typed value by `synthetic_data.workbook`;
* `currency_in_quote` is False for a money cell (the cell text is the amount);
  `currency_in_row` is True when the row states the code. The parser never sees a
  currency that only exists in a number format, and gold never claims it did.

## Validation

* v1 — the vendored validator against `configs/synth/ontology/corporate_legal.v3.yaml`
  (a V3 pack, additive; nothing under `synth/` is modified);
* v2 — `synthetic_data.gold.validate.validate_v2`: quote is an exact substring of
  the parser text and inside the cited sentence, canonical ids resolve, render
  locations obey the page rule (physical page mandatory for PDF, `null` and
  `pageless` for DOCX/XLSX/TXT-logical), and every currency claim is recomputed;
* alignment — counts match units, every non-missing unit cites a real sentence.
