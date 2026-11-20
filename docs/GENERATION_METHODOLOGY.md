# Generation Methodology

## Determinism model

Everything starts from an integer seed plus a target page count and a layout
(`v1` or `v2`). The same inputs produce the same world, documents, gold files
and QA dataset. Entity ids (`person_03`, `company_00`), fact ids (`fact_0001`),
cohort fact ids (`cfact_000001`), document ids (`gen_doc_00000`) and query ids
are stable.

The generator uses Python's `random.Random(seed)` for name allocation and fact
selection. It does not use hash randomisation, wall-clock time, network calls or
model output in any artifact that affects the data. Time and memory measurements
are recorded separately in the manifest.

## World construction

The base 20-page world contains 149 entities:

| Kind | Count |
|---|---:|
| Person | 40 |
| Company | 24 |
| Legal document | 30 |
| Asset | 20 |
| Place | 12 |
| Bank | 8 |
| Law firm | 6 |
| Government body | 6 |
| Court | 3 |

Entities carry a stable id, a canonical name, an entity type and subtype, and
optional attributes such as jurisdiction, auditor or registration number. The
ontology in `configs/synth/ontology/corporate_legal.v1.yaml` defines the allowed
entity classes and relations.

The base world contains 344 edge facts. The important relations are ownership,
shareholding, family, guarantee, lending, debt, representation, chairing,
employment, location and document signing. Some facts are deliberately
distributed across documents so QA can require a cross-document join.

The world also contains:

- **32 events** with dates and participants;
- **26 completed loan transactions**;
- **one cross-document contradiction** about a loan amount;
- facility-capacity propositions that are not loans or disbursements;
- proposed, alleged, conditional and negated statements.

### Append-only cohorts

Larger corpora add entities in cohorts:

| Cohort threshold | Adds |
|---:|---|
| 100 pages | 24 companies, 4 banks, 4 law firms, 4 government bodies, 2 courts, 8 places, 16 assets, 32 people, 16 legal documents |
| 500 pages | 160 companies, 20 banks, 20 law firms, 20 government bodies, 10 courts, 40 places, 80 assets, 160 people, 80 legal documents |
| 1,000 pages | 200 companies, 20 banks, 20 law firms, 20 government bodies, 10 courts, 40 places, 80 assets, 160 people, 80 legal documents |
| 5,000 pages | 2,000 companies, 80 banks, 80 law firms, 40 government bodies, 20 courts, 80 places, 400 assets, 1,000 people, 400 legal documents |

A 5,000-page world includes all earlier cohorts. New ids are appended after the
existing ids. Earlier names, facts and assignments are never rewritten. Cohort
facts use a separate `cfact_` namespace so base `fact_` ids are never renumbered.

This produces the manifest counts:

| Dataset | Entities | World edge facts | Events | Transactions |
|---|---:|---:|---:|---:|
| 20 pages | 149 | 344 | 32 | 26 |
| 500 pages | 849 | 3,214 | 216 | 210 |
| 1,000 pages | 1,479 | 6,332 | 416 | 410 |
| 5,000 pages | 5,579 | 37,518 | 2,416 | 2,410 |

## Document plans

A `DocumentPlan` is not a text blob. It is an ordered list of sentence
specifications. Each sentence knows:

- its exact text;
- which entities it mentions and in which surface form;
- which fact it states, if any;
- its time expression and resolution;
- which trap it exercises, if any;
- whether it is a heading, body sentence or closing.

Rendering joins the sentences with newlines and form-feed page breaks. The same
rendered text is hashed, written to disk and passed to ingestion.

### Page plans

The V2 plan is a repeating seven-document block:

```text
(3, 3, 3, 3, 3, 3, 2)
```

Each 20-page block contains one document of every type. The first seven
documents at 20, 500, 1,000 and 5,000 pages are identical. Page counts are exact:
the plan is validated against both the requested total and the parser's
recognised page count.

V1 remains available for compatibility but is not used by the included datasets.
Its base 20-page block is `(5, 4, 3, 4, 4)`, rotated on later blocks, and it
emits five documents per block.

### Document structures

V2 documents contain realistic sections rather than isolated sentences:

- **Board minutes** — attendance, agenda, matters discussed, resolutions,
  actions.
- **Facility agreement** — recitals, parties, facility terms, conditions
  precedent, repayment, execution.
- **Due diligence** — scope, findings, risks, discrepancies, recommendations.
- **Memorandum** — to/from/date, subject, background, analysis,
  recommendations.
- **Case notes** — parties, procedural history, submissions, findings and
  allegations, orders and next steps.
- **Correspondence** — sender, recipient, date, subject, body, closing.
- **Press release** — headline, publication date, corporate statement,
  announcements, contact, closing.

Every V2 document contains 5–6 traps. The pipeline rejects a V2 plan with fewer
than 3 or more than 6.

## Trap catalogue

| Trap kind | What it tests |
|---|---|
| `same_name_different_people` | Two people share a full name but have different ids |
| `alias_same_person` | A former name or alias resolves to one person |
| `historical_change` | A chair hand-over over time, not a contradiction |
| `cross_document_contradiction` | Two documents state different amounts for one event |
| `negated_relationship` | An explicitly denied relationship must not become positive |
| `proposed_vs_completed` | A proposal is not a completed transaction |
| `conditional_relationship` | An obligation that only applies under a condition |
| `relative_date` | A date anchored on a stated document date |
| `fiscal_date` | An explicitly stated fiscal-year period |
| `changing_financial_amounts` | A facility increase is a capacity, not a new loan |
| `alleged_fact` | An allegation attributed to a speaker, not asserted as proven |
| `irrelevant_distractor` | Text with no gold fact |
| `cross_document_relationship` | A relation that requires joining two documents |

The manifest records trap counts globally and, for V2, per document. The 20-page
pilot has 36 trap statements across all 13 kinds. The 5,000-page corpus has 8,253.

## Ingestion and canonical text

The generated `.txt` files are ingested with:

```python
from synth.modules.u01_parser import ingest_path
records = ingest_path(store, config, path)
```

The parser produces the canonical text, sentence ids (`s0`, `s1`, …), page map
and text hash. The generator then loads a `DocView` from the store.

Before writing gold, `annotations.py` compares the complete list of planned
sentences with the canonical sentence list. Any difference raises
`GoldGenerationError`. This prevents evidence from silently shifting when a
sentence splitter changes.

## Gold generation

Gold is derived from the plan's structured facts, not from a model reading the
document. The generator uses the canonical sentence labels and hashes only to
anchor the annotations. It writes a Synth `GoldSet` per document passage with:

- entities and mention quotes;
- edges and evidence;
- propositions, speakers and statuses;
- logic links;
- identity traps and contradiction flags;
- time references.

`synth.eval.gold.validate` locates every quote in the cited sentence. The
pipeline treats any `sentence_mismatch`, `NOT_FOUND` item or error as a fatal
generation error.

## QA generation

The corpus index records, for every fact and entity, which document sentences
state it. QA generation uses that index to produce:

- lookup questions (entity, relationship, financial, temporal, provenance);
- reasoning questions (multi-hop, identity, negation,
  proposed-versus-completed, alleged status, contradiction, historical change);
- unanswerable questions with structural proofs.

Every answerable answer is computed from the world or a plan. Every evidence
quote is taken from the canonical sentence text. Every unanswerable question is
checked against the `FactIndex`; keyword absence alone is not sufficient in V2.

## Duplicate control

V2 uses deterministic wording variants keyed by stable fact and document ids. A
normalized question/answer pair may occur at most twice. The manifest reports:

- total queries;
- unique normalized forms;
- duplicate instances and percentage;
- maximum occurrences of one normalized pair.

All four datasets report zero duplicate normalized instances and a maximum of
one occurrence.

## Failure behaviour

Generation fails rather than emitting a misleading artifact when:

- the requested page count is not a positive multiple of 20;
- a document plan has an invalid page count or trap count;
- two documents have the same filename or identical bytes;
- the parser returns a non-`SEGMENTED` status;
- the parser's page or sentence counts differ from the plan;
- a planned sentence does not match canonical text;
- a gold quote cannot be located;
- a gold file has a sentence mismatch or error;
- the QA dataset violates its schema;
- a semantic check fails;
- a non-SQLite store is supplied.

## Output format

Each dataset directory contains:

```text
documents/                 plain .txt, form-feed page breaks
gold/                      one JSON gold file per document
qa_dataset.json            questions, answers, evidence and proofs
qa_dataset.schema.json     JSON Schema for the QA dataset
manifest.json              metadata, counts, hashes and validation
```

There are no binary documents, model outputs or database files in the datasets.
