# Synthetic Data Generator

A deterministic generator for fictional corporate/legal document corpora and
answer-level QA benchmarks. It was developed to test document-extraction and
question-answering systems against realistic documents with controlled reasoning
traps, without hand-authoring every corpus.

> **All people, companies, banks, law firms, courts, places and events in this
> project are fictional.** Names are drawn from fixed pools and the datasets are
> generated from a seed. Nothing here describes real organisations or real
> transactions.

> **Publication status:** this is a local export prepared for review. It has not
> been committed, pushed or published. Licensing, ownership and attribution
> approvals are still pending. See
> [`docs/PUBLICATION_CHECKLIST.md`](docs/PUBLICATION_CHECKLIST.md).

## What this project does

The generator produces four linked artifacts from one seeded scenario model:

1. **Documents** — seven types of synthetic corporate/legal text, written as
   plain `.txt` files with form-feed page breaks.
2. **Gold annotations** — entity, relationship, proposition, time, contradiction
   and identity annotations that are grounded in the real ingestion output of
   the vendored Synth parser.
3. **QA benchmark data** — answerable and unanswerable questions with expected
   answers, exact evidence quotes and, for unanswerable questions, structured
   proofs of absence.
4. **A manifest** — counts, validation results, semantic checks, hashes and
   performance measurements.

The four completed V2 datasets are included in
[`synthetic_data/output/`](synthetic_data/output/):

| Dataset | Pages | Documents | Gold items | QA questions |
|---|---:|---:|---:|---:|
| `pilot_20_v2` | 20 | 7 | 331 | 95 |
| `corpus_500_v2` | 500 | 175 | 7,843 | 2,365 |
| `corpus_1000_v2` | 1,000 | 350 | 15,668 | 4,707 |
| `corpus_5000_v2` | 5,000 | 1,750 | 78,268 | 23,408 |

Every gold item in these datasets is reported as `EXACT` by the Synth gold
validator, with zero sentence mismatches, zero not-found items and zero
semantic-validation failures. The four datasets were copied into this export
unchanged.

## Why it was developed

Testing an extraction pipeline on a handful of manually written documents does
not exercise scale, entity ambiguity, cross-document joins, temporal language or
questions that should be refused. This generator provides:

- a **seeded world model** so entity ids, facts and documents are reproducible;
- **real ingestion** through Synth's parser, so gold evidence is anchored to the
  same canonical sentences the pipeline sees;
- **planted traps** such as namesakes, aliases, negated relationships, proposals
  versus completions, relative dates and a cross-document contradiction;
- **answer-level QA metrics** separate from graph-extraction metrics; and
- **cumulative corpora** where each smaller checkpoint is a prefix of the larger
  one at the same seed.

## Scope and honest limits

This project covers **synthetic data generation and its validation**. It does
**not** report an evaluation of the Datanaut/FYP extraction pipeline. No model
baseline has been run or scored against these datasets. The included evaluation
command scores submitted predictions; it does not itself run an extractor.

Additional limits:

- Generated gold is **generator-produced**, **not human-verified** and **not
  blind**. It is written with `mode: "generated"` and is never promoted into a
  gold-set table.
- The default and tested document format is `.txt` with form-feed page breaks.
  The real Synth parser is exercised, but PDF layout extraction is not.
- Questions are deterministic template variants. They are not natural
  paraphrases and a 0% normalized-duplicate rate does not imply stylistic
  diversity.
- The semantic validator checks the defect classes exercised by this corpus. It
  is not a general natural-language inference engine.
- This export is designed for a **source checkout with an editable install**.
  A normal non-editable wheel is not supported because `synth.config.REPO_ROOT`
  and the ontology/configuration lookup are relative to the source tree.

## Document types

The V2 layout uses one instance of every type in each 20-page block:

| Type | Structure |
|---|---|
| Board minutes | Attendance, agenda, matters discussed, resolutions, actions |
| Facility agreement | Recitals, parties, facility terms, conditions, repayment, execution |
| Due-diligence report | Scope, findings, risks, discrepancies, recommendations |
| Memorandum | To/from/date, subject, background, analysis, recommendations |
| Legal case notes | Parties, procedural history, submissions, findings, orders |
| Correspondence | Sender, recipient, date, subject, body, closing |
| Press release | Headline, date, corporate statement, announcements, contact |

Each V2 document is 2–3 pages. The V2 page plan is a repeating seven-document block
`(3, 3, 3, 3, 3, 3, 2)`, so every 20-page checkpoint contains all seven types.

## How the world is built

The generator starts from fixed name pools and a seeded `random.Random`. The
20-page world contains 149 entities: companies, people, banks, law firms, courts,
government bodies, places, assets and legal documents. Relationships include
ownership, shareholding, family, guarantee, lending, debt, representation,
chairing and signing. The world also contains events, completed loan
transactions and one deliberate cross-document contradiction.

For larger checkpoints, the world is expanded with **append-only cohorts** at
100, 500, 1,000 and 5,000 pages. Cohorts add new entity ids after the existing
ones and never rewrite the base world or an earlier cohort. This is why the
first seven V2 documents are stable across all four datasets. Fact ids use a
separate `cfact_` namespace so frozen `fact_` ids are never renumbered.

The ontology is
[`configs/synth/ontology/corporate_legal.v1.yaml`](configs/synth/ontology/corporate_legal.v1.yaml).
The critical relations exercised by the corpora are `owns`, `shareholder_of`,
`family_of`, `lent_to`, `owes`, `guarantor_for` and `represents`.

## Reasoning traps

The V2 corpora plant 5–6 traps per document (the pipeline rejects anything
outside 3–6). The trap kinds are:

- two different people with the same full name;
- one person with an alias or former name;
- a chair hand-over (a historical change, not a contradiction);
- a cross-document contradiction about a loan amount;
- explicitly negated relationships;
- proposed or alleged statements versus completed or proven ones;
- conditional obligations;
- relative dates anchored on a stated document date;
- explicitly stated fiscal-year periods;
- a facility-capacity increase, never represented as a new loan or disbursement;
- irrelevant distractors with no gold fact;
- cross-document relationship joins.

At 20 pages the manifest records 36 trap statements across 13 trap kinds. The
5,000-page corpus records 8,253 trap statements.

## Gold annotations

Gold files live beside each dataset under `gold/`. Each file is a Synth gold
document for one passage of one generated document. It contains:

- entities with verbatim mention quotes, occurrence indices and stable keys;
- edges with subject, relation, object, verbatim evidence and time roles;
- propositions with speaker, status, negation and logic links;
- identity traps and contradiction flags;
- time references with resolved values and granularity;
- expected flags connecting a contradiction to its counterpart fact and file.

Gold is generated **from structured scenario facts**, never by asking a model
what a document says. The generated text is first ingested through
`synth.modules.u01_parser.ingest_path`; the generator then maps every planned
sentence to the parser's canonical sentence labels. If the splitter disagrees
with the plan, generation fails rather than shifting evidence silently. Synth's
own `synth.eval.gold.write_gold_file` and `synth.eval.gold.validate` functions
are used unchanged.

The validator checks every quote by locating it in the cited sentence. The
pipeline additionally rejects any `sentence_mismatch` even though Synth reports
those as a soft warning.

## QA generation

Questions and expected answers are computed from the same world model and
document plans. No language model is involved. Each answerable question carries
exact evidence: document id, filename, text hash, sentence index, sentence id
and verbatim quote. Each unanswerable question carries no evidence and no gold
facts.

The 13 query types are: entity lookup, relationship lookup, financial, temporal,
historical change, multi-hop, identity resolution, negation,
proposed-versus-completed, alleged status, contradiction, provenance and
unanswerable.

### The five unanswerable categories

V2 emits five kinds of unanswerable question, with a structural proof derived
from the corpus `FactIndex`:

1. **Absent attribute** — for example, an auditor or VAT number the company does
   not have.
2. **Absent relationship** — no `advises` edge, with a plausible but different
   relation recorded as a distractor.
3. **Unsupported historical date** — the earliest chair fact starts after the
   asked date.
4. **Absent financial information** — no dividend fact, while lending and
   balance facts exist.
5. **False presupposition / near miss** — a proposed acquisition or unrelated
   ownership edge, with no completed acquisition.

Each proof names the subject, relation or attribute, checked fact ids, checked
document ids and a human-readable explanation. Unknown proof facts or documents
are rejected by the semantic validator.

At 20 pages the QA set contains 70 answerable and 25 unanswerable questions (five
of each kind). Across all four datasets the unanswerable share is 26.3%.

## Installation

Requirements: Python 3.11 or newer. The verified environment used Python 3.13.5
on Windows.

From the project root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Using the venv's `python.exe` directly avoids PowerShell execution-policy issues.
`requirements.txt` contains the runtime dependencies for environments that do
not use the editable install.

**Packaging limitation:** install this as a source checkout with `-e`. Do not
build or publish a normal wheel. In a non-editable install, `synth` would be
placed under `site-packages` and `synth.config.REPO_ROOT` would no longer point
at the repository root where `configs/synth/` lives. The project is not on PyPI
and no wheel-install support is claimed.

## Generate a dataset

The CLI writes only below `synthetic_data/output/` and refuses to write
elsewhere. Generate into a new directory so the frozen datasets are not
overwritten:

```powershell
py -m synthetic_data generate `
  --pages 20 --seed 7 --layout v2 `
  --out synthetic_data/output/my_pilot_20_v2
```

The page count must be a positive multiple of 20. To regenerate an existing
directory, add `--overwrite`; without it, generation refuses to replace existing
artifacts.

Other checkpoints use the same command:

```powershell
py -m synthetic_data generate --pages 500  --seed 7 --layout v2 --out synthetic_data/output/my_corpus_500_v2
py -m synthetic_data generate --pages 1000 --seed 7 --layout v2 --out synthetic_data/output/my_corpus_1000_v2
py -m synthetic_data generate --pages 5000 --seed 7 --layout v2 --out synthetic_data/output/my_corpus_5000_v2
```

To write outside the repository, use the Python API:

```python
from pathlib import Path
from synthetic_data.generator.pipeline import generate_corpus

result = generate_corpus(
    seed=7,
    pages=20,
    out_dir=Path(r"C:\Temp\synthetic_smoke"),
    layout="v2",
)
print(result.manifest["validation"])
```

Each run writes:

```text
documents/                 .txt documents, form-feed page breaks
gold/                      one validated Synth gold file per document
qa_dataset.json            questions, answers, evidence and proofs
qa_dataset.schema.json     JSON Schema for qa_dataset.json
manifest.json              counts, validation, hashes, performance
```

Generation uses an in-memory SQLite store (`synth.store.test_store`). It never
opens PostgreSQL, starts a model server or makes a network request.

## Validate and test

Run the full generator test suite from the project root:

```powershell
py -m pytest synthetic_data/tests -q
```

The verified result is **112 passed**. Tests use temporary directories and do not
modify the four frozen datasets.

The pipeline validates gold and QA during generation:

- gold is written and validated through
  `synth.eval.gold.write_gold_file` / `synth.eval.gold.validate`;
- every planned sentence must match the ingested canonical sentence;
- semantic validation re-reads the document text and checks relationship
  direction, unique answers, temporal support, proposition status, entity
  identity, transactions, contradictions and unanswerable proofs.

## Evaluate predictions

```powershell
py -m synthetic_data evaluate `
  --dataset synthetic_data/output/pilot_20_v2/qa_dataset.json `
  --predictions path/to/predictions.json `
  --out path/to/eval_report.json
```

Prediction format:

```json
{
  "predictions": [
    {
      "query_id": "q_00003",
      "answer": "Juniper Data Centre",
      "answerable": true,
      "citations": [
        {"document_id": "gen_doc_00000", "sentence_id": "s19"}
      ]
    }
  ]
}
```

A bare JSON list is also accepted, and citations may use `filename` plus
`sentence_idx` instead of `document_id` plus `sentence_id`. The evaluator reports
exact match, normalized exact match, token F1, citation accuracy, evidence
precision/recall/F1, document retrieval, answerability accuracy, answerable
detection and unanswerable abstention, overall and by query type.

An oracle prediction set built from the gold answers and evidence scores 1.0 on
every applicable metric; this tests the evaluator and is not an extraction
baseline.

## Repository layout

```text
Synthetic-Data-Generator/
├── README.md
├── pyproject.toml
├── requirements.txt
├── .gitignore
├── SHA256SUMS.txt
├── synthetic_data/
│   ├── generator/           world, documents, annotations, QA, semantics, CLI
│   ├── schemas/             strict Pydantic models for datasets and predictions
│   ├── tests/               112 unit, integration and negative tests
│   ├── __init__.py
│   ├── __main__.py
│   └── output/
│       ├── pilot_20_v2/
│       ├── corpus_500_v2/
│       ├── corpus_1000_v2/
│       └── corpus_5000_v2/
├── synth/                   vendored Synth implementation used by the generator
├── app/
│   ├── __init__.py
│   ├── config.py
│   └── ingest/              Docling adapter, models and reading-order checks
├── configs/synth/
│   ├── p1.default.yaml
│   ├── models.yaml
│   └── ontology/corporate_legal.v1.yaml
└── docs/                    ten project documents
```

`synth/` is included in full because the generator imports its configuration,
ontology, evaluation, parser, store and gateway modules. Only the minimal `app/`
subset needed by the parser is included. There is no `synthetic_data/templates/`
directory in the source project; the generator builds sentences from code and
does not use template files.

## Benchmark results

All counts below come from the four `manifest.json` files.

| Metric | Pilot 20 | 500-page | 1,000-page | 5,000-page |
|---|---:|---:|---:|---:|
| Pages | 20 | 500 | 1,000 | 5,000 |
| Documents | 7 | 175 | 350 | 1,750 |
| World entities | 149 | 849 | 1,479 | 5,579 |
| World edge facts | 344 | 3,214 | 6,332 | 37,518 |
| Unique documented relations | 75 | 1,779 | 3,554 | 17,754 |
| Events | 32 | 216 | 416 | 2,416 |
| Transactions | 26 | 210 | 410 | 2,410 |
| Trap statements | 36 | 828 | 1,653 | 8,253 |
| Gold items (all `EXACT`) | 331 | 7,843 | 15,668 | 78,268 |
| QA questions | 95 | 2,365 | 4,707 | 23,408 |
| Answerable | 70 | 1,743 | 3,468 | 17,248 |
| Unanswerable | 25 | 622 | 1,239 | 6,160 |
| Semantic failures | 0 | 0 | 0 | 0 |

The 5,000-page corpus contains 17,754 unique documented relations, 2,416 events,
2,410 transactions, 78,268 exact gold items, 17,248 answerable questions and
6,160 unanswerable questions. Its dataset directory contains 3,503 files and
59,471,980 bytes.

Performance, validation and reproducibility evidence are recorded in
[`docs/VALIDATION_RESULTS.md`](docs/VALIDATION_RESULTS.md),
[`docs/PERFORMANCE_AND_OPTIMIZATION.md`](docs/PERFORMANCE_AND_OPTIMIZATION.md)
and [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## Documentation

- [Project overview](docs/PROJECT_OVERVIEW.md)
- [Generator architecture](docs/GENERATOR_ARCHITECTURE.md)
- [Generation methodology](docs/GENERATION_METHODOLOGY.md)
- [Gold annotations](docs/GOLD_ANNOTATIONS.md)
- [QA benchmark](docs/QA_BENCHMARK.md)
- [Dataset catalog](docs/DATASET_CATALOG.md)
- [Validation results](docs/VALIDATION_RESULTS.md)
- [Performance and optimization](docs/PERFORMANCE_AND_OPTIMIZATION.md)
- [Reproducibility](docs/REPRODUCIBILITY.md)
- [Publication checklist](docs/PUBLICATION_CHECKLIST.md)
