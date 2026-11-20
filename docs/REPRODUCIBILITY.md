# Reproducibility

## Provenance

This export was copied from the local Datanaut experimental checkout:

- source path: `C:\Users\user\Desktop\Datanaut-Experimental`
- source commit: `e32c27c7762edc5d412c6ada6f31d250766833d1`
- source branch: `azmeer/synthetic-data-generator`
- export date: 2026-11-19
- destination: `C:\Users\user\Desktop\Synthetic-Data-Generator`

No source file was moved, modified or deleted. The four dataset directories were
copied unchanged and verified by SHA-256.

## Verified environment

| Item | Value |
|---|---|
| OS | Windows 11, version 10.0.26200 |
| Python | 3.13.5 (64-bit) |
| Virtual environment | `C:\Users\user\AppData\Local\Temp\opencode\sdg_venv` (outside the export) |
| Install mode | `pip install -e "C:\Users\user\Desktop\Synthetic-Data-Generator[dev]"` |
| `PYTHONPATH` | Empty |

Installed packages:

```text
annotated-types==0.8.0
anyio==4.16.0
attrs==26.1.0
certifi==2026.10.23
colorama==0.4.6
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.20
iniconfig==2.3.1
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
packaging==26.3
pluggy==1.7.0
pydantic==2.14.4
pydantic_core==2.50.5
Pygments==2.21.0
pysbd==0.3.4
pytest==8.4.2
python-ulid==3.2.1
PyYAML==6.0.3
RapidFuzz==3.14.6
referencing==0.37.0
rpds-py==2026.9.1
SQLAlchemy==2.1.9
synthetic-data-generator==1.0.0 (editable)
typing-inspection==0.4.5
typing_extensions==4.16.0
```

`pip check` reported no broken requirements.

## Install from a clean checkout

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Using the venv's `python.exe` directly avoids activation-policy issues.

Do not replace this with a non-editable wheel install. The export depends on
`synth.config.REPO_ROOT` pointing at the source tree so that
`configs/synth/p1.default.yaml`, `configs/synth/models.yaml` and the ontology are
found. A wheel install would relocate `synth` under `site-packages` and break
those paths.

## Determinism contract

For the same `seed`, `pages` and `layout`:

- every document is byte-identical;
- every gold file is byte-identical;
- `qa_dataset.json` is byte-identical;
- `qa_dataset.schema.json` is byte-identical;
- the manifest's `batch`, `entities`, `relationships`, `events`, `transactions`,
  `contradictions`, `traps`, `queries`, `validation`, `semantics`,
  `reproducibility` and `diversity` sections are identical.

`manifest.json` as a whole is **not** byte-identical across runs because:

- each document row contains the fresh Synth ULID (`synth_doc_id`), which is
  intentionally non-deterministic; and
- the `performance` section contains wall-clock and memory measurements.

Generated gold deliberately sets `doc_id` to `null` and identifies the document
by filename plus `text_sha256`, so the non-deterministic ULID does not leak into
gold or QA artifacts.

## Exact generation commands

The four frozen datasets were generated with:

```powershell
py -m synthetic_data generate --pages 20   --seed 7 --layout v2 --out synthetic_data/output/pilot_20_v2
py -m synthetic_data generate --pages 500  --seed 7 --layout v2 --out synthetic_data/output/corpus_500_v2
py -m synthetic_data generate --pages 1000 --seed 7 --layout v2 --out synthetic_data/output/corpus_1000_v2
py -m synthetic_data generate --pages 5000 --seed 7 --layout v2 --out synthetic_data/output/corpus_5000_v2
```

To regenerate into scratch space without touching the frozen datasets, use a new
directory:

```powershell
py -m synthetic_data generate `
  --pages 20 --seed 7 --layout v2 `
  --out synthetic_data/output/repro_20_v2
```

The CLI refuses paths outside `synthetic_data/output/`. The Python API can write
to any path:

```python
from pathlib import Path
from synthetic_data.generator.pipeline import generate_corpus

generate_corpus(
    seed=7,
    pages=20,
    out_dir=Path(r"C:\Temp\repro_20_v2"),
    layout="v2",
)
```

The page count must be a positive multiple of 20. The generator refuses to
overwrite existing artifacts unless `overwrite=True` (CLI: `--overwrite`).

## Stable prefix

The V2 plan is a repeating seven-document block `(3, 3, 3, 3, 3, 3, 2)`. The
first seven documents, their gold files and the base facts are identical at 20,
500, 1,000 and 5,000 pages. Cohort entities and facts are appended after the
base world; base ids are never renumbered. Cohort facts use the `cfact_` prefix.

The manifest's `reproducibility.prefix_sha256` is the SHA-256 of the
newline-join of **all** document text hashes in that dataset. Because each
checkpoint contains a different number of documents, the values differ:

| Dataset | Prefix SHA-256 |
|---|---|
| `pilot_20_v2` | `25e023d38cd56c76abce878efe061f6aa21a3e0f0414df4c72cbb3379041b7c8` |
| `corpus_500_v2` | `07e8cf5c88dbdb0b4f0653108013bf576fab628af80d749008d43c1414faf93c` |
| `corpus_1000_v2` | `a5f2247ad738c1353a62bf3f02d026317b5f647e53e70f67cd6b46df32c55c20` |
| `corpus_5000_v2` | `98d5dadedbf38ad651489eec4d3c7814c6adcaa22dbe4dbd3c62bdf2af0cd62d` |

The stable prefix is visible in the first seven document hashes, which are the
same in every dataset:

```text
4af2ca6b8b37fc89d2d1a716215f24c79843c80053ed74e5b28f86f43f107c91
569cb567de82ec1fa881899091ca31f5d4a03a100ffb3e965980709338400ebf
c3aa97a20a547709edc3487c038768b7e6d5b0e5e6cdc6713d70c9eb75d0be8b
90f2706519ff766c36395e287d55900560e460df92d84adce6a120aa58d7aa7a
d1c25aba108a5307eb42784c9b1decba7f9fda57f039306c80a473ec01cffebc
93286ef6e506fe350ed0e0ee85125ef18d88a92a6eba19106e4db918097cdc7b
0a1965238088d4349f46e019ca5292ddf9812c5b342a9cd3ed58feb353d9836e
```

## World signature

The 5,000-page manifest records world signature SHA-256
`50bb15c13c30efa91a60826bf43c514a4718656dc895acb6b8befe938a77afd3`.
The independent validation recomputed it and matched. The other datasets record:

| Dataset | World signature SHA-256 |
|---|---|
| `pilot_20_v2` | `dc8a8e8cbabf4a7cb7270ec8af3072eaf698b9fb5165191e2cfda3d2dd7c9a72` |
| `corpus_500_v2` | `8662fb04cb0fd81c41bb0cb55dc64bd75e63ec3ee1fe142be3a42cd0aa4ce67b` |
| `corpus_1000_v2` | `35aaf2056533edfe948903219712b47f28df1ff523372f91f2f17f64e3861b5f` |
| `corpus_5000_v2` | `50bb15c13c30efa91a60826bf43c514a4718656dc895acb6b8befe938a77afd3` |

## Artifact hashes

| Dataset | `qa_dataset.json` SHA-256 | `manifest.json` SHA-256 |
|---|---|---|
| `pilot_20_v2` | `710ad493469543443df04eba2a5eeee8b0e937ec5106d4442078ed6546cbc21f` | `9c3fece1882249d7aa48bc0529013f82ae23786d0e0fdb7c684137d367abbfb9` |
| `corpus_500_v2` | `de4f169b2bf48c74593367bc240cb62fa9c54ee73eb7a479522d5ad36f532026` | `08db1901c44e97f9f4a5e428a0ee517d9a044a1b3c1a9b80c230915a1e0f91fa` |
| `corpus_1000_v2` | `ec63e08d515a1e3e5717423d4ebfb417d36fef0d899c3c5722b934e019dd7cee` | `3bc2fd3fa221a38414afce30078f8a8efcc0a29fffb0a5e0ec23ceb47173c8a7` |
| `corpus_5000_v2` | `6e3487aaa72546b1ebf6df9afd96969c50dd7266bb72076e0f76fef6c8fc9f12` | `ef6e1877fe3f6accc2dd4509ce1af4075bebed2cbc41e8d44997d1474d7907fb` |

The 5,000-page QA dataset was independently regenerated and its SHA-256 matched
the frozen file. Its schema file matched all four datasets:
`fff5a3b593b9d2016bb161987817967b909668de9091434531c98d0d930ce6b5`.

## Verification commands

### Full test suite

```powershell
py -m pytest synthetic_data/tests -o addopts= -q
```

Expected: `112 passed`.

### 20-page smoke generation

```powershell
py -m synthetic_data generate `
  --pages 20 --seed 7 --layout v2 `
  --out synthetic_data/output/repro_20_v2
```

Expected output:

```text
Generated 20 pages in 7 documents.
Gold validation: 7/7 ok, 331 grounded items, 0 sentence mismatches, 0 not found.
QA queries: 95 (70 answerable, 25 unanswerable).
Duplicate normalized questions: 0.0%.
```

The exact timings and peak memory vary by run.

### Compare regenerated artifacts with the frozen pilot

```powershell
py -c "import hashlib,pathlib; a=pathlib.Path('synthetic_data/output/repro_20_v2'); b=pathlib.Path('synthetic_data/output/pilot_20_v2'); bad=[]
for p in sorted(a.rglob('*')):
    if not p.is_file(): continue
    rel=p.relative_to(a)
    if rel.as_posix()=='manifest.json': continue
    q=b/rel
    if not q.exists() or hashlib.sha256(p.read_bytes()).digest()!=hashlib.sha256(q.read_bytes()).digest(): bad.append(rel.as_posix())
print('mismatches', bad)"
```

Expected: `mismatches []`.

### Verify the full export against `SHA256SUMS.txt`

```powershell
py -c "import hashlib,pathlib,sys; bad=[]
for line in pathlib.Path('SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
    digest, rel = line.split('  ', 1)
    p = pathlib.Path(rel)
    if hashlib.sha256(p.read_bytes()).hexdigest() != digest: bad.append(rel)
print('mismatches', bad)
sys.exit(1 if bad else 0)"
```

### Verify dataset byte-identity with the source

From the source checkout, compare each of the four directories. The export audit
did this for every file:

| Dataset | Files | Bytes | Mismatches |
|---|---:|---:|---:|
| `pilot_20_v2` | 17 | 259,254 | 0 |
| `corpus_500_v2` | 353 | 5,946,351 | 0 |
| `corpus_1000_v2` | 703 | 11,858,132 | 0 |
| `corpus_5000_v2` | 3,503 | 59,471,980 | 0 |

## Isolation checks

The export was tested with its own editable install in an external venv. The
following were confirmed:

- `synthetic_data`, `synth` and `app` resolve under
  `C:\Users\user\Desktop\Synthetic-Data-Generator`;
- `synth.config.REPO_ROOT` equals the export root;
- the default config and ontology exist in the export;
- no `Datanaut-Experimental` path appears in `sys.path`;
- the console script and `py -m synthetic_data` both work;
- `pip check` passes;
- the test suite and generation work from the export root.

The original Datanaut checkout was not used as a working directory, an editable
install target or a `PYTHONPATH` entry during these checks.

## Known non-determinism

- Synth document ULIDs in manifest rows.
- Performance timings and memory measurements.
- Python hash randomisation can change iteration order in code that uses sets,
  but all data-affecting iterations are explicitly sorted or list-based. The
  byte-identical regeneration checks are the evidence for this.
- Logging output and traceback paths are not part of the artifacts.

## Limitations

- The verified environment is Windows + Python 3.13.5. Python 3.11+ is declared,
  but only 3.13.5 was exercised in this export.
- Dependency versions are lower/upper bounded in `pyproject.toml`; the exact
  versions above are the tested set, not a lock file.
- No CI configuration is included.
- A non-editable wheel install is not supported.
