# Performance and Optimization

## Measurement caveat

The generator records stage timings and a `tracemalloc` peak in each manifest.
`tracemalloc` is a Python allocation profiler and adds substantial overhead,
especially to code that creates many small objects. The 5,000-page corpus was
also measured separately without `tracemalloc`, with process RSS sampled
externally. Do not compare the two modes as if they were the same benchmark.

The numbers below describe this export on one local machine. They are not a
service-level guarantee or a claim of optimality.

## Environment

| Item | Value |
|---|---|
| OS | Windows 11, version 10.0.26200 |
| Python | 3.13.5 (64-bit) |
| CPU | Intel Core i5-1235U, 10 cores / 12 logical processors |
| RAM | 15.68 GB |
| Storage | Local desktop filesystem |
| Store | In-memory SQLite (`synth.store.test_store`) |
| Models | None |
| Network | None |

## 5,000-page measurement without profiling

An independent run of seed 7 / 5,000 pages / layout V2 recorded:

| Measure | Value |
|---|---:|
| Wall time | 167.132 s |
| Pipeline total | 166.984 s |
| Peak process RSS | 471.465 MB |
| Documents | 1,750 |
| Pages | 5,000 |
| Queries | 23,408 |

Pipeline stages:

| Stage | Time (ms) |
|---|---:|
| `world_generation` | 455.203 |
| `document_generation` | 932.767 |
| `ingestion` | 85,188.181 |
| `gold_validation` | 17,171.358 |
| `qa_generation` | 6,618.783 |
| `semantic_validation` | 54,796.288 |
| **Total** | **166,983.908** |

Ingestion dominates, followed by semantic validation, gold validation and QA
generation. This is expected: every document goes through the real parser and
every gold quote is located in canonical text.

## Manifest timings (with `tracemalloc`)

These are the values recorded in the frozen datasets' manifests. They were
measured with `tracemalloc` active and are useful for relative stage comparisons,
not for wall-clock planning.

| Stage (ms) | Pilot 20 | 500 | 1,000 | 5,000 |
|---|---:|---:|---:|---:|
| `world_generation` | 77.677 | 309.594 | 618.867 | 11,803.690 |
| `document_generation` | 7.026 | 160.163 | 330.017 | 5,500.017 |
| `ingestion` | 592.589 | 18,864.599 | 37,149.750 | 295,438.172 |
| `gold_validation` | 74.879 | 3,129.325 | 6,480.508 | 49,689.089 |
| `qa_generation` | 112.881 | 35,917.924 | 2,976.183 | 27,451.943 |
| `semantic_validation` | 71.106 | 21,111.161 | 27,237.281 | 775,935.287 |
| **Total** | **981.625** | **79,914.886** | **76,566.950** | **1,171,433.093** |
| Peak `tracemalloc` (MB) | 3.377 | 33.303 | 64.698 | 317.266 |

Notes:

- The 5,000-page instrumented total is 1,171.4 s (about 19.5 minutes), versus
  167.1 s unprofiled. The difference is profiler overhead, dominated by
  semantic validation and ingestion object churn.
- The 500- and 1,000-page QA-generation figures are not monotonic. They were
  collected under different machine load and with `tracemalloc`; do not read a
  scaling curve from them.
- `tracemalloc` peak measures Python allocations, not total process memory.
  The unprofiled 5,000-page run peaked at 471.5 MB RSS.

## Scaling observations

- The 20-page pilot completes in about 1 second instrumented and is suitable for
  interactive checks.
- Ingestion grows roughly linearly with document count, but per-document cost is
  small because documents are short.
- Semantic validation is intentionally expensive: it re-reads sentences and
  cross-checks every relationship, time, status and unanswerable proof.
- The 5,000-page world builds cohort entities and facts up front. World
  generation took 11.8 s instrumented and 0.46 s unprofiled.
- The 5,000-page corpus is 59.5 MB on disk; the QA JSON is 27.4 MB of that.

## Design choices that keep runs practical

1. **In-memory SQLite.** No PostgreSQL service, network connection or database
   cleanup is needed. The store is disposed at the end of the run.
2. **One store per run.** Documents share a store, avoiding repeated schema
   creation and connection overhead.
3. **No models.** Documents and answers are computed from structured facts. No
   inference server, token budget or API latency is involved.
4. **Render once.** The pipeline renders each document's text once, hashes that
   exact text, writes it and reuses it for ingestion.
5. **Append-only cohorts.** Larger worlds extend smaller ones instead of
   rebuilding a different world, which preserves the stable prefix and avoids
   renumbering.
6. **Deterministic templates.** QA generation indexes existing facts and
   sentences; it does not search or sample from a model vocabulary.
7. **One store reader per document.** `DocView.load` is scoped to a document,
   limiting the amount of state held at once.

No algorithmic optimization was performed specifically for this export. The
implementation is the tested source implementation, copied without changing
generation or validation semantics.

## Reproducing a measurement

For a small instrumented run:

```powershell
py -m synthetic_data generate `
  --pages 20 --seed 7 --layout v2 `
  --out synthetic_data/output/my_perf_20_v2
```

The CLI prints stage timings and peak `tracemalloc`. The same values are stored
in the new manifest's `performance` section.

For an unprofiled 5,000-page measurement, the pipeline's optional `tracemalloc`
instrumentation must be disabled and process RSS sampled externally. The export
does not include that measurement harness. The reported 167.1 s / 471.5 MB
figure came from an external run of the same pipeline with the profiler
disabled. Do not present the instrumented manifest totals as unprofiled wall
time.

## Limitations

- Single-machine, single-run measurements; no confidence intervals.
- Windows-specific filesystem and allocator behaviour.
- Different Python, SQLAlchemy or `pysbd` versions may change timings.
- The 5,000-page corpus is the largest measured checkpoint. Larger runs are
  possible in principle but were not measured.
- Memory scales with the number of documents, facts, sentences and QA queries;
  the 471.5 MB RSS figure is not a fixed cap.
