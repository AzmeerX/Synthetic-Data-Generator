# Project Overview

## Purpose

The Synthetic Data Generator creates deterministic, fictional corporate/legal
document corpora plus gold annotations and an answer-level QA benchmark. It was
developed inside the Datanaut final-year project to exercise document extraction
and question answering against controlled language phenomena that small manual
test sets do not cover: namesakes, aliases, negation, proposals versus completed
actions, relative and fiscal dates, cross-document joins and a deliberate
contradiction.

The generator is not a document-extraction model. It builds the scenario, writes
the documents, sends them through the real Synth parser, derives gold from the
structured facts, validates every quote against the ingested canonical text, and
generates questions whose answers are known by construction.

## What is included

| Component | Location | Role |
|---|---|---|
| Generator package | `synthetic_data/generator/` | World, document plans, gold, QA, semantic validation, CLI |
| Schemas | `synthetic_data/schemas/` | Strict Pydantic models for QA datasets and predictions |
| Tests | `synthetic_data/tests/` | 112 unit, integration and negative tests |
| Vendored Synth | `synth/` | Configuration, ontology, gold evaluation, parser, store, gateway |
| Minimal app slice | `app/` | `app.config`, `app.ingest.adapter`, models and order checks |
| Required configuration | `configs/synth/` | Run defaults, model placeholders and corporate/legal ontology |
| Frozen datasets | `synthetic_data/output/` | Four completed V2 checkpoints |
| Documentation | `docs/` | Ten project documents, including this one |

## The four datasets

| Dataset | Pages | Documents | Gold items | QA questions |
|---|---:|---:|---:|---:|
| `pilot_20_v2` | 20 | 7 | 331 | 95 |
| `corpus_500_v2` | 500 | 175 | 7,843 | 2,365 |
| `corpus_1000_v2` | 1,000 | 350 | 15,668 | 4,707 |
| `corpus_5000_v2` | 5,000 | 1,750 | 78,268 | 23,408 |

All four were generated with seed 7 and `layout="v2"`. They are cumulative: the
20-page dataset is a byte-stable prefix of each larger dataset for document,
gold and QA artifacts, apart from the manifest's fresh Synth document ULIDs and
performance timings. See `REPRODUCIBILITY.md`.

## Scope

In scope:

- seeded construction of a fictional entity/relationship world;
- seven structured document types;
- planted reasoning traps;
- real parser ingestion through Synth's `u01_parser`;
- gold generation and validation through Synth's gold module;
- deterministic QA generation with exact evidence;
- structured proofs for five unanswerable-question categories;
- independent semantic validation;
- answer-level evaluation of submitted predictions;
- reproducible cumulative corpora at 20, 500, 1,000 and 5,000 pages.

Out of scope:

- evaluating or benchmarking the Datanaut/FYP extraction pipeline;
- training or fine-tuning a model;
- natural-language paraphrase generation;
- PDF/DOCX layout extraction (the tested format is `.txt`);
- human adjudication of gold;
- publishing or licensing decisions.

## What the numbers mean

`manifest.json` is the source of truth for each dataset. It records the batch
parameters, entity and relationship counts, trap counts, QA distributions,
gold-validation results, semantic checks, per-document hashes and performance
measurements.

All four manifests report:

- every gold document valid;
- every gold item grounded as `EXACT`;
- zero sentence mismatches;
- zero not-found quotes;
- zero gold errors;
- zero semantic-validation failures.

These results validate internal consistency and grounding. They do **not** prove
that a model can extract the same facts or answer the questions.

## Provenance

This folder was created as a copy-only export from the local Datanaut
experimental checkout at commit `e32c27c7762edc5d412c6ada6f31d250766833d1`
(branch `azmeer/synthetic-data-generator`) on 2026-11-19. The original checkout
was not modified. The four dataset directories, the generator source and the
required Synth/app/config slice were copied; the old V1 `pilot_20` dataset and
unrelated Datanaut code were excluded.

The source repository's `pyproject.toml` declares Apache-2.0, but no root
`LICENSE` or `NOTICE` file was found. This export does not add a license or claim
ownership. See `PUBLICATION_CHECKLIST.md`.

## Status

- Local project: built and verified.
- Independent generation: verified in a fresh external virtual environment.
- Test suite: 112 passed.
- Public release: **not approved and not performed**.
- Git commit/push: **not performed**.

## Intended use

- Testing extraction systems on controlled, fictional corporate/legal text.
- Measuring answer quality, citation accuracy and abstention behaviour.
- Reproducing a benchmark from a seed.
- Teaching or demonstrating grounded synthetic-data construction.

Do not present the data as real corporate records or as evidence about real
people or organisations.

## Related documents

- `GENERATOR_ARCHITECTURE.md` — module structure and dependency slice.
- `GENERATION_METHODOLOGY.md` — how worlds, documents, traps and corpora are built.
- `GOLD_ANNOTATIONS.md` — gold schema, generation and validation.
- `QA_BENCHMARK.md` — query types, evidence and unanswerable proofs.
- `DATASET_CATALOG.md` — per-dataset inventory and hashes.
- `VALIDATION_RESULTS.md` — measured validation and test results.
- `PERFORMANCE_AND_OPTIMIZATION.md` — timings, memory and design choices.
- `REPRODUCIBILITY.md` — environment, commands and determinism checks.
- `PUBLICATION_CHECKLIST.md` — outstanding approvals before publication.
