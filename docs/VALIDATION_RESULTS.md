# Validation Results

## Summary

The four frozen datasets pass every validation that the generator performs:

- every gold document validates;
- every gold item is grounded as `EXACT`;
- no sentence mismatches or not-found quotes;
- no gold errors;
- no semantic-validation failures;
- no duplicate normalized QA forms.

The export also passes an independent smoke test and the full 112-test suite in
a fresh virtual environment that does not import from the original Datanaut
checkout.

## Gold validation by dataset

Source: each dataset's `manifest.json` → `validation`.

| Dataset | Documents | Valid | Items | Grounding | Sentence mismatches | Not found | Errors |
|---|---:|---:|---:|---|---:|---:|---:|
| `pilot_20_v2` | 7 | 7 | 331 | 331 `EXACT` | 0 | 0 | 0 |
| `corpus_500_v2` | 175 | 175 | 7,843 | 7,843 `EXACT` | 0 | 0 | 0 |
| `corpus_1000_v2` | 350 | 350 | 15,668 | 15,668 `EXACT` | 0 | 0 | 0 |
| `corpus_5000_v2` | 1,750 | 1,750 | 78,268 | 78,268 `EXACT` | 0 | 0 | 0 |

`EXACT` means the validator located the quoted text exactly in the cited
canonical sentence. The pipeline treats a soft sentence mismatch as a fatal
error, so the zero counts are enforced, not merely observed.

## Semantic validation

The independent semantic validator re-reads the generated documents rather than
trusting the gold objects. It checks relationship direction, unique answers,
temporal support, proposition status, entity identity, transaction semantics,
contradictions and unanswerable proofs.

All four datasets report `"ok": true`, `"failure_count": 0` and no failures.

### 5,000-page corpus

| Check | Checked items |
|---|---:|
| `evidence_quotes` | 96,032 |
| `relationship_direction` | 26,647 |
| `unique_answers` | 14,788 |
| `temporal_support` | 1,502 |
| `proposition_statuses` | 7,504 |
| `entity_identity` | 81,559 |
| `transactions` | 7,070 |
| `contradictions` | 501 |
| `unanswerable_proofs` | 6,160 |
| **Failures** | **0** |

### 20-page pilot

| Check | Checked items |
|---|---:|
| `evidence_quotes` | 411 |
| `relationship_direction` | 120 |
| `unique_answers` | 81 |
| `temporal_support` | 8 |
| `proposition_statuses` | 30 |
| `entity_identity` | 345 |
| `transactions` | 61 |
| `contradictions` | 3 |
| `unanswerable_proofs` | 25 |
| **Failures** | **0** |

The 500- and 1,000-page manifests report the same check names and zero failures;
their item counts scale with corpus size.

## Independent 5,000-page validation

A separate validation pass re-read the frozen 5,000-page corpus and checked it
against the manifest, the canonical parser output and the QA contracts:

- 1,750 document files and 1,750 gold files present;
- 5,000 planned pages, 5,000 parser-recognised pages;
- page pattern matches the V2 plan;
- 46,504 recognised sentences;
- 7 document types, 250 documents each;
- 78,268 gold items, all `EXACT`;
- 23,408 QA queries, all five unanswerable kinds present;
- 17,248 answerable and 6,160 unanswerable;
- 17,764 evidence items across 1,750 evidence documents;
- 0 unknown gold fact ids, 0 provenance errors, 0 proof-scope errors;
- 0 duplicate query ids and 0 duplicate normalized forms;
- semantic checks: 0 failures;
- determinism: 1,750 regenerated documents byte-identical, 1,750 gold files
  byte-identical, QA dataset byte-identical, manifest document hashes and prefix
  hash matched.

The independently regenerated QA file had SHA-256
`6e3487aaa72546b1ebf6df9afd96969c50dd7266bb72076e0f76fef6c8fc9f12`, identical
to the frozen file.

## Export smoke test

The export was tested from `C:\Users\user\Desktop\Synthetic-Data-Generator` in a
fresh external virtual environment.

### CLI generation

Command:

```powershell
py -m synthetic_data generate `
  --pages 20 --seed 7 --layout v2 `
  --out synthetic_data/output/_smoke_20_v2
```

Result:

- 20 pages in 7 documents;
- 7/7 gold documents valid;
- 331 grounded gold items, all `EXACT`;
- 0 sentence mismatches, 0 not found, 0 errors;
- 95 QA queries: 70 answerable, 25 unanswerable;
- five unanswerable questions of each kind;
- 0 duplicate normalized questions;
- semantic validation: `ok: true`, 0 failures;
- 17 files, 259,256 bytes.

The smoke directory was removed after the test. It did not overwrite or modify
the four frozen datasets.

### Byte comparison with the frozen pilot

The regenerated artifacts were compared with `pilot_20_v2`:

- all 7 documents byte-identical;
- all 7 gold files byte-identical;
- `qa_dataset.json` byte-identical;
- `qa_dataset.schema.json` byte-identical;
- manifest `batch`, `entities`, `relationships`, `events`, `transactions`,
  `contradictions`, `traps`, `queries`, `validation`, `semantics`,
  `reproducibility` and `diversity` sections identical;
- manifest `documents` rows differ only because they contain fresh Synth ULIDs;
- manifest performance timings differ.

This is the documented determinism contract: data artifacts are reproducible;
the manifest contains run-specific identifiers and timings.

### External-directory generation

The Python API was also run with `out_dir` pointing outside the repository:

```python
generate_corpus(seed=7, pages=20, out_dir=Path(temp_dir), layout="v2")
```

Result: 7 documents, 20 pages, 331 `EXACT` gold items, 95 QA queries (70
answerable, 25 unanswerable), semantic validation `ok: true`. This confirms the
generation path does not depend on the repository's output directory; only the
CLI's safety guard imposes that restriction.

### Evaluator check

An oracle prediction set derived from the gold answers and evidence was scored
against the 20-page smoke dataset:

- 95 queries, 70 answerable, 25 unanswerable, 0 missing predictions;
- exact match, normalized exact match, token F1, citation accuracy, evidence
  retrieval, evidence precision, evidence F1, document retrieval, answerability
  accuracy, answerable detection and unanswerable abstention all **1.0**.

This validates the evaluator and evidence contract. It is not a model result.

## Test suite

Command, from the export root:

```powershell
py -m pytest synthetic_data/tests -o addopts= -q
```

Result:

```text
112 passed in 12.48s
```

The JUnit XML from the same environment reports 112 tests, 0 failures, 0 errors,
0 skipped.

The tests include end-to-end generation, gold validation, QA generation,
semantic negative tests, CLI guards, manifest checks, determinism and V2 cohort
behaviour. They use pytest temporary directories and do not write into the
frozen output.

## Isolation verification

The test environment was a fresh venv at
`C:\Users\user\AppData\Local\Temp\opencode\sdg_venv`, created outside both the
source checkout and the export. The export was installed with:

```powershell
python -m pip install -e "C:\Users\user\Desktop\Synthetic-Data-Generator[dev]"
```

With `PYTHONPATH` empty and the working directory set to the export root:

```text
synthetic_data = C:\Users\user\Desktop\Synthetic-Data-Generator\synthetic_data\__init__.py
synth          = C:\Users\user\Desktop\Synthetic-Data-Generator\synth\__init__.py
app            = C:\Users\user\Desktop\Synthetic-Data-Generator\app\__init__.py
REPO_ROOT      = C:\Users\user\Desktop\Synthetic-Data-Generator
DEFAULT_CONFIG = C:\Users\user\Desktop\Synthetic-Data-Generator\configs\synth\p1.default.yaml
```

No path containing `Datanaut-Experimental` appeared in `sys.path`. `pip check`
reported no broken requirements. The console entry point
`synthetic-data --help` also ran from the venv.

## Dataset byte-identity

All four destination dataset directories were compared with the source:

| Dataset | Files | Bytes | Mismatches |
|---|---:|---:|---:|
| `pilot_20_v2` | 17 | 259,254 | 0 |
| `corpus_500_v2` | 353 | 5,946,351 | 0 |
| `corpus_1000_v2` | 703 | 11,858,132 | 0 |
| `corpus_5000_v2` | 3,503 | 59,471,980 | 0 |
| **Total** | **4,576** | **77,535,717** | **0** |

Every file's size and SHA-256 matched. The recheck was performed after tests and
smoke generation.

## What is not validated

- Gold has not been reviewed or adjudicated by a human.
- No extraction model has been run or scored on these datasets.
- PDF/DOCX layout extraction has not been exercised; only `.txt` documents are
  generated.
- The semantic validator covers the generator's controlled phenomena, not
  arbitrary natural language.
- A non-editable wheel install is not supported or tested.
- Questions are templates; human evaluation of question quality is outstanding.
