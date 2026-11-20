# Generator Architecture

## High-level flow

```text
seed + target page count
        |
        v
World (entities, facts, events, transactions, contradiction)
        |
        v
DocumentPlan per document (sentences, facts, mentions, times, traps)
        |
        v
rendered .txt files
        |
        v
synth.modules.u01_parser.ingest_path
        |
        v
DocView (canonical text, sentence ids, hashes, page map)
        |
        +--> GoldSet -> write_gold_file -> synth.eval.gold.validate
        |
        +--> DocumentContext -> CorpusIndex -> QA dataset
        |
        v
independent semantic validation -> manifest.json
```

The pipeline writes documents and gold first, then builds the corpus index and QA
dataset, then runs semantic validation, and only then writes the final manifest.
A failure at any stage raises `CorpusGenerationError` rather than producing a
partial "success".

## Generator modules

| Module | Responsibility |
|---|---|
| `world.py` | Seeded entities, name pools, facts, events, transactions, contradiction, append-only cohorts |
| `documents.py` | Page plans, document structures, sentence specifications, trap scheduling |
| `annotations.py` | Converts plans and canonical `DocView` sentences into Synth `GoldSet` objects |
| `queries.py` | Corpus index, answerable QA generation, unanswerable proofs, duplicate metrics |
| `semantics.py` | Independent text-level checks of relationships, status, time, identity and proofs |
| `evaluation.py` | Answer, citation, evidence and answerability metrics for submitted predictions |
| `pipeline.py` | End-to-end generation, ingestion, gold validation, QA, semantics and manifest |
| `cli.py` | `generate` and `evaluate` commands |
| `first_sample.py` | Small manual ingestion example used during development |

`schemas/qa.py` defines the strict Pydantic contracts for the dataset and
prediction files. Unknown fields are rejected with `extra="forbid"`.

## Vendored dependency slice

`synth/` is copied in full from the source checkout. The generator imports:

- `synth.config` — run configuration and repository root;
- `synth.ontology` — ontology loading and relation validation;
- `synth.eval.frame` — the extraction data frame;
- `synth.eval.gold` — gold models, writer and validator;
- `synth.eval.match` and related evaluation helpers used by the gold path;
- `synth.modules.common` — `DocView`;
- `synth.modules.u01_parser` — real ingestion;
- `synth.modules.u01_chunking` and `synth.modules.u04_whoswho` — parser/gold dependencies;
- `synth.store` — in-memory SQLite store, tables and append-only guards;
- `synth.gateway` — model-gateway types and token counting imported by shared modules;
- `synth.provenance` — quote locator used by gold validation;
- `synth.ids`, `synth.contracts`, `synth.prompts`, `synth.states` — shared primitives.

The full package is retained rather than attempting a partial extraction. The
extra API, console, viewer, export and orchestrator modules are present but are
not imported by generation. They add roughly 2 MB and avoid the risk of a
missing transitive import.

Inactive code is not a supported interface in this export; only generation,
validation, tests and the documented CLIs were exercised.

## Minimal `app` slice

The parser imports `app.ingest.adapter`, which in turn needs:

| File | Role |
|---|---|
| `app/__init__.py` | Package marker |
| `app/config.py` | `IngestConfig` and `Settings` used by the adapter |
| `app/ingest/__init__.py` | Parser exports |
| `app/ingest/adapter.py` | File discovery, hashing, page counting and Docling conversion |
| `app/ingest/models.py` | `Passage`, `SourceDocument`, `PageOutcome`, `OrderFlags` |
| `app/ingest/order_check.py` | Reading-order warning signals |

The adapter imports Docling and `pypdfium2` lazily, inside conversion functions.
The generator only creates `.txt` files, so those imports are not reached and
Docling is not a runtime dependency of this workflow. The same applies to
`pypdf`, which the parser imports lazily for PDF fallback paths.

## Configuration resolution

`synth/config.py` defines:

```python
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "configs" / "synth" / "p1.default.yaml"
```

Because the package is imported from the source checkout, `REPO_ROOT` resolves to
the export root. The run config points at:

- `configs/synth/p1.default.yaml` — run defaults;
- `configs/synth/models.yaml` — endpoint placeholders;
- `configs/synth/ontology/corporate_legal.v1.yaml` — ontology and critical relations.

`load_config()` reads the models file if it exists. The generator does not call a
model endpoint. Missing prompt or profile files are not required by the
generation path.

### Packaging consequence

An editable install (`pip install -e .`) keeps the package files in the source
tree, so `REPO_ROOT` remains correct. A normal non-editable wheel would copy
`synth/` into `site-packages`, where `configs/synth/` does not exist unless the
files are packaged as resources and the path logic is refactored. That refactor
was not performed and no wheel support is claimed.

## Store and safety boundaries

- `generate_corpus()` creates `synth.store.test_store()`, an in-memory SQLite
  store, unless a store is supplied.
- It refuses a store whose dialect is not `sqlite`.
- One store is reused for all documents in a run.
- PostgreSQL is never opened.
- No model client, gateway call or network request is made.
- Generated gold is written and validated but never promoted.
- The CLI refuses output paths outside `<repo>/synthetic_data/output/`.
- Existing artifacts are not overwritten unless `--overwrite` is passed.

## Test architecture

The 112 tests cover:

- world determinism and ontology validity;
- page allocation and trap coverage;
- end-to-end generation through real ingestion and gold validation;
- rejection of mismatched quotes, missing quotes, soft sentence mismatches and
  ontology violations;
- QA grounding, evidence and unanswerable proofs;
- evaluation metrics and prediction schemas;
- manifest completeness, reproducibility and seed divergence;
- SQLite safety, overwrite and CLI output-root guards;
- negative semantic tests for reversed family direction, facility-as-loan
  phrasing, ambiguous ownership, unsupported fiscal dates, namesake conflation,
  allegation speaker/reporter confusion, negated-positive statuses, non-genuine
  contradictions, unpaired contradiction flags, payment transactions for
  balances and tampered evidence quotes;
- V2 page plans, all seven document types, 3–6 traps per document, stable prefix,
  append-only cohorts, QA distribution, proof consistency, duplicate caps,
  closed-world wording, performance fields and determinism.

Tests use temporary directories created by pytest and do not write into the four
frozen dataset directories.
