# Dataset Catalog

## Included datasets

All four datasets were generated with seed 7 and `layout="v2"` and are copied
into this export unchanged from the source checkout.

| Directory | Pages | Documents | Gold files | QA questions | Files | Bytes |
|---|---:|---:|---:|---:|---:|---:|
| `synthetic_data/output/pilot_20_v2/` | 20 | 7 | 7 | 95 | 17 | 259,254 |
| `synthetic_data/output/corpus_500_v2/` | 500 | 175 | 175 | 2,365 | 353 | 5,946,351 |
| `synthetic_data/output/corpus_1000_v2/` | 1,000 | 350 | 350 | 4,707 | 703 | 11,858,132 |
| `synthetic_data/output/corpus_5000_v2/` | 5,000 | 1,750 | 1,750 | 23,408 | 3,503 | 59,471,980 |
| **Total** | **6,520** | **2,282** | **2,282** | **30,575** | **4,576** | **77,535,717** |

The page total adds the four checkpoints; the corpora are cumulative, not four
disjoint samples. The 20-page documents are a prefix of the larger corpora.

## Directory contents

Each dataset has the same layout:

```text
<dataset>/
├── documents/
│   ├── gen_doc_00000_board_minutes.txt
│   ├── gen_doc_00001_financial_agreement.txt
│   └── ...
├── gold/
│   ├── gen_doc_00000_board_minutes__0-31.json
│   ├── gen_doc_00001_financial_agreement__0-29.json
│   └── ...
├── qa_dataset.json
├── qa_dataset.schema.json
└── manifest.json
```

`documents/` contains UTF-8 plain text with form-feed (`\f`) page breaks.
`gold/` contains one validated Synth gold file per document. The three JSON
files are UTF-8 and end with a newline.

## Per-dataset statistics

### `pilot_20_v2`

- 20 pages, 7 documents, one of each document type.
- 149 world entities, 344 world edge facts, 75 unique documented relations.
- 32 events, 26 transactions, 1 contradiction.
- 36 trap statements across all 13 trap kinds.
- 331 gold items, all `EXACT`; 0 sentence mismatches, 0 not found, 0 errors.
- 95 QA questions: 70 answerable, 25 unanswerable (5 of each kind).
- 0 duplicate normalized questions.

This is the smallest end-to-end example and the recommended starting point.

### `corpus_500_v2`

- 500 pages, 175 documents, 25 of each type.
- 849 world entities, 3,214 world edge facts, 1,779 unique documented relations.
- 216 events, 210 transactions, 1 contradiction.
- 828 trap statements.
- 7,843 gold items, all `EXACT`; 0 sentence mismatches, 0 not found, 0 errors.
- 2,365 QA questions: 1,743 answerable, 622 unanswerable.
- 0 duplicate normalized questions.

### `corpus_1000_v2`

- 1,000 pages, 350 documents, 50 of each type.
- 1,479 world entities, 6,332 world edge facts, 3,554 unique documented relations.
- 416 events, 410 transactions, 1 contradiction.
- 1,653 trap statements.
- 15,668 gold items, all `EXACT`; 0 sentence mismatches, 0 not found, 0 errors.
- 4,707 QA questions: 3,468 answerable, 1,239 unanswerable.
- 0 duplicate normalized questions.

### `corpus_5000_v2`

- 5,000 pages, 1,750 documents, 250 of each type.
- 5,579 world entities, 37,518 world edge facts, 17,754 unique documented
  relations.
- 2,416 events, 2,410 transactions, 1 contradiction.
- 8,253 trap statements.
- 78,268 gold items, all `EXACT`; 0 sentence mismatches, 0 not found, 0 errors.
- 23,408 QA questions: 17,248 answerable, 6,160 unanswerable.
- 0 duplicate normalized questions.
- 3,503 files, 59,471,980 bytes.
- Largest file: `qa_dataset.json`, 27,398,845 bytes.

The 5,000-page corpus is the largest completed checkpoint. It has not been
human-audited.

## Artifact hashes

These hashes identify the frozen artifacts. The full file-level manifest is in
`SHA256SUMS.txt` at the repository root.

| Dataset | `qa_dataset.json` SHA-256 | `manifest.json` SHA-256 |
|---|---|---|
| `pilot_20_v2` | `710ad493469543443df04eba2a5eeee8b0e937ec5106d4442078ed6546cbc21f` | `9c3fece1882249d7aa48bc0529013f82ae23786d0e0fdb7c684137d367abbfb9` |
| `corpus_500_v2` | `de4f169b2bf48c74593367bc240cb62fa9c54ee73eb7a479522d5ad36f532026` | `08db1901c44e97f9f4a5e428a0ee517d9a044a1b3c1a9b80c230915a1e0f91fa` |
| `corpus_1000_v2` | `ec63e08d515a1e3e5717423d4ebfb417d36fef0d899c3c5722b934e019dd7cee` | `3bc2fd3fa221a38414afce30078f8a8efcc0a29fffb0a5e0ec23ceb47173c8a7` |
| `corpus_5000_v2` | `6e3487aaa72546b1ebf6df9afd96969c50dd7266bb72076e0f76fef6c8fc9f12` | `ef6e1877fe3f6accc2dd4509ce1af4075bebed2cbc41e8d44997d1474d7907fb` |

The `qa_dataset.schema.json` file is identical in all four datasets, with
SHA-256 `fff5a3b593b9d2016bb161987817967b909668de9091434531c98d0d930ce6b5`.

The manifest's `reproducibility` section records the document hashes and a
prefix hash. The 5,000-page QA dataset was independently regenerated and
matched byte-for-byte; see `VALIDATION_RESULTS.md`.

## What was excluded

- `synthetic_data/output/pilot_20/` — the old V1 pilot. It is not required by any
  test in this export and is not part of the V2 benchmark.
- Empty `synthetic_data/documents/` and `synthetic_data/gold/` directories from
  the source tree.
- `synthetic_data/templates/` — no such directory exists in the source.
- `__pycache__`, `.pytest_cache`, virtual environments, profiling files and
  failed-run scratch artifacts.
- The rest of the Datanaut repository (UI, infrastructure, unrelated pipelines).

## Verification

From the project root, compare every listed file with `SHA256SUMS.txt`:

```powershell
py -c "import hashlib,pathlib,sys; root=pathlib.Path('.'); bad=[]
for line in pathlib.Path('SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
    digest, rel = line.split('  ', 1)
    p = root / rel
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    if actual != digest: bad.append(rel)
print('checked', sum(1 for _ in pathlib.Path('SHA256SUMS.txt').read_text(encoding='utf-8').splitlines()), 'files')
print('mismatches', bad)
sys.exit(1 if bad else 0)"
```

The four source dataset directories and their destination copies were compared
file by file during the export: 4,576 files and 77,535,717 bytes, with no size or
SHA-256 mismatch.
