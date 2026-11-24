# V3 generation — Phase B

Deterministic semantic world and canonical content planning for the seven prose
document types, plus the specialised workbook document kind.

**Scope.** This package produces `synthetic_data.model.Corpus` values. It does
not render, parse, annotate, score or write a corpus. No TXT/PDF/DOCX/XLSX
renderer exists here (Phase C onward), and nothing under `synth/` or `app/` is
imported or modified.

## Modules

| module | responsibility |
|---|---|
| `config.py` | `CorpusConfig`, the §11 depth table, matter sizing |
| `rng.py` | scope-seeded `random.Random`; no module-level state |
| `ids.py` | deterministic id allocation with attempt-escalation collisions |
| `vocab.py` | de-duplicating name and attribute pools |
| `surface.py` | typed values → text; facts → sentences (25 relations, 3 registers) |
| `world.py` | one `Matter` per document group: entities, ownership, directors, facility, drawdowns, repayments, guarantees, security, conditions, events, allegations/findings, contradictions, threads, cross-references |
| `grammar.py` | structural prose pools and the compositional sentence engine |
| `planner.py` | 4–5 structural grammars per type, section permutation, seven planners, tables/lists/notes/exhibits/signatures, depth normalisation |
| `workbook.py` | `CdmWorkbook` with typed cells, number formats and formulas |
| `plan.py` | corpus assembly and statement bookkeeping |
| `metrics.py` | depth, diversity and contradiction metrics computed from plans |

## Entry points

```python
from synthetic_data.generation import CorpusConfig, build_corpus, compute_metrics

plan = build_corpus(CorpusConfig(seed=7, corpus_name="v3_pilot",
                                 documents=7, workbooks=1))
corpus = plan.corpus()            # runs every Phase A cross-layer invariant
metrics = compute_metrics(plan)   # depth, diversity, contradictions
```

`plan_contradictions(config)` returns the contradiction schedule alone, so the
distribution rule can be checked at 500 documents without planning them.

## How a corpus is shaped

* `documents` prose documents are split into **matters** of 7–11 documents
  (`matter_sizes`, cycle 7/10/8/9). Every matter carries the six
  non-correspondence types plus 1–5 letters.
* All documents of a matter are planned from the **same** `CorpusWorld`, so a
  repayment stated by the facility agreement, the due-diligence report, the
  board minutes and the workbook is one `fact_id` in every location (§3.2).
* Each fact carries an explicit set of document types that must state it
  (`fact_roles`), and `doc_facts` materialises the exact list per document. The
  planner raises if any assigned fact goes unstated, so `Corpus` can never hold
  an orphan fact.
* Per-type family caps keep a due-diligence report or a letter inside its §11
  transaction range; the facility agreement states every transaction, so no fact
  loses its last home.

## Determinism

* Every choice is `random.Random(blake2b(seed_key || scope))`. There is no
  global RNG and no `hash()`.
* Ids are a pure function of `(seed, corpus, prefix, scope)`. Entity ordinals
  are **corpus-global** (`entity_counters` shared across matters); restarting
  them per matter was a real defect and is pinned by a test.
* Adding documents does not perturb earlier ids: document `n` keeps its id and
  its section ids when the corpus grows.
* Determinism is per `(seed, configuration)`; the §12.2 fixture set replaces the
  retired V2 prefix property.

## Metrics: numerator, denominator, formula

All are computed from saved plans by `metrics.py`, over **prose** documents. A
workbook row is not a sentence and a table cell is not prose.

| metric | numerator | denominator | formula | gate |
|---|---|---|---|---|
| template duplication % | sentences whose masked template is not unique | all sentences | `100 × (1 − unique_masked / total)` | ≤ 80 |
| boilerplate % | sentences occurring verbatim in ≥ 2 documents | all sentences | `100 × shared / total` | ≤ 70 |
| unique templates per 1,000 docs | unique masked templates | documents | `1000 × unique / docs` | ≥ 1,200 |
| max documents sharing one sentence | largest document set containing one identical sentence | — | `max(count)` | ≤ 15% of docs |
| structural signature duplication % | documents whose `plan_fingerprint` repeats | prose documents | `100 × duplicates / docs` | ≤ 5 |
| information-shape diversity | distinct sequences of `(relation or status, subject type, object type)` | prose documents | `distinct / docs` | ≥ 0.60 |
| section-order entropy | heading-permutation counter per type | — | mean Shannon entropy, bits | ≥ 1.5 |
| length spread | p95 − p5 words per type | median words per type | `(p95 − p5) / median`, worst type | ≥ 0.35 |
| table-shape diversity | distinct `(columns, column types, row band)` unions per document | documents per type | mean ratio, capped at 1 | ≥ 0.5 |
| thread-shape diversity | distinct `(length, reply targets, citation counts)` | multi-message threads | `distinct / threads` | ≥ 0.5 |

Masking for the template metric replaces, on the canonical prose sentence: ISO
and long-form dates → `<DATE>`, currency amounts, percentages and numeric runs →
`<NUM>`, and corpus entity names → `<ENT>`. Boilerplate and the sharing metric use
**exact** sentences, because two different amounts that mask to one template are
not boilerplate.

### Reconciling V2's 98.81% with V3's result

The published V2 baseline (`98.81%`) was measured with the audit's own name list.
To compare fairly, one masking function was applied to both corpora — numbers,
dates and proper-noun spans, nothing corpus-specific:

| corpus | documents | sentences | unique templates | duplication | templates / doc |
|---|---|---|---|---|---|
| `corpus_5000_v2` | 1,750 | 54,004 | 1,841 | **96.59%** | 1.05 |
| `v3_checkpoint` | 70 | 7,916 | 4,357 | **44.96%** | 62.24 |

With the shipped V3 metric (corpus entity names rather than a generic proper-noun
mask) the checkpoint reports **46.9%** duplication and 61.6 templates per
document. Both figures describe the same change: V2 had about one distinct
sentence shape per document, V3 has about sixty. The sentence count differs from
the depth report's 46,504 because this count splits every non-empty line of the
V2 text files; the duplication conclusion does not depend on it.

## Contradiction distribution (DEC-5, B6)

Density is `ceil(total_documents / 50)` — one per fifty, rounded up.

| corpus | documents | required | planned | types | max dimension share | repeated pair |
|---|---|---|---|---|---|---|
| pilot | 7 prose + 1 workbook | 1 | 1 | 2 | not applicable | no |
| development | 70 prose + 10 workbooks | 2 | 2 | 4 | 0.50 | no |
| schedule | 500 prose + 70 workbooks | 12 | 12 | 7 | 0.25 | no |

The 40% dimension ceiling is only evaluable once a corpus holds at least three
contradictions; with two, a 50/50 split is the minimum possible, and the
500-document schedule is where the ceiling is demonstrated (25%).

A contradiction always names two different documents of different prose types;
both facts are stated, in disjoint document sets; the discrepancy is checked by
dimension (amount / date / status / relationship), and a `contradicts`
cross-reference records the link.

## Regression fixtures

`synthetic_data/tests/v3_generation_fixtures.json` holds a sha256 per pilot
document. Regenerate it **explicitly** and only after reviewing a planner change:

```bash
py -3.13 synthetic_data/tests/v3_generation_fixtures.py
```

Normal test execution never rewrites the file: the determinism test compares
against it and fails on drift, and the generator is not a test module.

## Boundaries deferred to later phases

* No renderer, `RenderMap` or page/bbox provenance (Phases C–F).
* No alignment or gold generation (Phase G); `FactStatement.surface` is intended
  wording and is never evidence.
* No QA generation (Phase H) and no corpus written to disk.
* Ownership percentages are content-layer typed values on rows that state the
  `owns` edge; `SemanticFact` has no percentage field and Phase A was not changed.
