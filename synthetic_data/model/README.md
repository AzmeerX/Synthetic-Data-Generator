# V3 canonical model — Phase A

Implementation of Layers 1–3 and the corpus aggregate described in
[`docs/V3_CANONICAL_DOCUMENT_MODEL.md`](../../../docs/V3_CANONICAL_DOCUMENT_MODEL.md).
**Model and validation only.** No generation, no renderers, no parser calls, no
gold or QA integration.

## Layer map

| file | layer | contents |
|---|---|---|
| `values.py` | shared | `Money`, `Percentage`, `IsoDate`, `EntityRef`, `DocumentRef`, id patterns |
| `semantic.py` | L1 | `CorpusWorld`, `Entity`, `SemanticFact`, `Condition`, `Event`, `Transaction`, `Contradiction`, `Thread`, `CrossReference`, `TimeScope` |
| `content.py` | L2 | `CdmDocument`, `DocumentMetadata`, `MessageMeta`, `Section`, `Block`, `InlineRun`, `Table`/`TableRow`/`TableCell`, `FactStatement`, `Exhibit`, `SignatureRecord`, `Footnote` |
| `workbook.py` | L2 | `CdmWorkbook`, `Workbook`, `Sheet`, `CellFormula` (re-exported) |
| `render.py` | L3 | `CanonicalSourceLoc`, `RenderLoc`, `ParseLoc`, `EvidenceRef`, `RenderOccurrence`, `RenderSpan`, `RenderMap`, `RenderResult`, `RendererCapabilities`, `RenderProfile` |
| `corpus.py` | aggregate | `Corpus` and the cross-layer validator (`collect_violations`) |
| `ids.py` | identity | `IdRegistry`, `derive_id`, scope-path builders, collision policy |
| `schema.py` | tooling | JSON Schema export and generated schemas in `schemas/` |

## Deterministic identifiers

```
id(prefix, scope) = "<prefix>:<scope>:<tag>"
tag               = base32_lower(blake2b(seed_key | "|" | prefix | "|" | scope | "|" | attempt))[:13]
seed_key          = blake2b("{seed}:{corpus_name}", digest_size=16)
```

* Pure function of `(seed, corpus, prefix, scope)`. No renderer output, no
  parser output, no `hash()`, no iteration order.
* `IdRegistry.build()` is order-independent: specs are sorted before allocation,
  so two runs that discover objects in different orders get identical ids.
* **Tag collisions** are detected across the whole registry and resolved
  deterministically: specs sharing a tag are sorted, and the later ones are
  re-derived with `attempt=1,2,…` until every tag in the corpus is unique. This
  is the only collision that can occur, because the readable scope is part of the
  id. Resolution is recorded in `IdRegistry.collisions`.
* Renderer (`rnd:<fmt>:…`) and parser (`s19`, `sen_…`) identifiers are **not**
  produced here and never substitute for a canonical id.

## Where the 11 invariants are enforced

| # | invariant | enforced by |
|---|---|---|
| 1 | statements resolve; facts list their statements | `Corpus._cross_layer` |
| 2 | cell `fact_ref` resolves and cell value equals the fact amount | `Corpus._cross_layer` |
| 3 | inline run value equals the fact value | `Corpus._cross_layer` |
| 4 | node and document ids unique | `CdmDocument._structure`, `Corpus._cross_layer` |
| 5 | cross-references resolve | `CorpusWorld`, `Corpus` |
| 6 | contradiction compares genuinely different values | `Corpus._contradiction_discrepancy` |
| 7 | evidence never built from an unusable alignment | `EvidenceRef` |
| 8 | every rendered node appears exactly once | `assert_render_coverage` |
| 9 | one fact id per fact | `Corpus._cross_layer` (duplicate payload detection) |
| 10 | thread/message ordering and reply targets | `Thread`, `MessageMeta`, `Corpus` |
| 11 | contradiction grounded in two documents | `Corpus._cross_layer` |

## Pagination contract

`RenderLoc.page` is mandatory for PDF, a logical page for TXT, and `None` for
DOCX/XLSX (`pageless=True`). A DOCX or XLSX span carrying a page is rejected.
`bbox` is optional, PDF-only, and validated when present; nothing synthesises a
page or a box.

## Phase A amendments (DEC-8–DEC-11)

* **DEC-8** — the authoritative id format is `<prefix>:<readable-scope>:<short-hash>`, exactly as implemented. The
  architecture's §5.1 formula was corrected to match; `ids.py` was not changed for this.
* **DEC-9** — `FactStatement.canonical_sentence_ids` is optional. `node_id` stays mandatory, table/cell statements
  need no sentence ids, and parser ids (`s19`, `sen_…`) are rejected by the canonical-id pattern.
* **DEC-10** — a cell payload containing `table`, `tables`, `nested_table` or `rows` is rejected with an explicit
  message; nested schedules are separate tables or worksheets.
* **DEC-11** — `ColumnSpec.type` is optional (`None` = no expectation, `mixed` = explicit); cell `value` is
  authoritative and is never coerced. `TableCell.number_format` records the display format alongside `display`,
  `value` and `formula.cached_value`.

## Implementation clarifications (not design changes)

1. `InlineRun.fact_ref` was added. Invariant 3 compares a run's typed value with
   "the corresponding fact field", which requires the run to name the fact.
2. `CellFormula` lives in `content.py` (a cell property) and is re-exported from
   `workbook.py`; this avoids a circular import between the two modules.
3. `DocumentMetadata.formality` carries the architecture's `register` as its wire
   alias, because `BaseModel.register` already exists in Pydantic v2.
4. `SemanticFact.statement_ids` and `Event.fact_ids` are present because §4.3
   requires the relationship even though the §4.1 pseudo-code omitted the fields.

None of these alters a decision in the architecture document.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 py -3.13 -m pytest synthetic_data/tests \
  -o addopts='' -p no:cacheprovider --basetemp='<scratch>/pyt'
```

Phase A adds 104 model tests across six `test_v3_*.py` files plus `test_v3_amendments.py` and does not modify the
112 existing tests. Full suite: 216 passing.
