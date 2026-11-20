# Publication Checklist

**Status: NOT APPROVED FOR PUBLICATION.**

This folder is a verified local export. It has not been committed, pushed or
published. The technical checks below are complete; the legal, ownership and
attribution checks are not. Do not create a public repository from this folder
until every pending item is resolved and recorded.

## Status legend

- `[x]` verified in this export
- `[ ]` pending or not yet performed
- **PENDING** requires a human decision, not an automated check

## Technical readiness

- [x] Source checkout inspected and dependency graph traced.
- [x] Generator, schemas and tests copied without changing generation or
      validation semantics.
- [x] Required `synth` package copied.
- [x] Minimal `app` slice copied (`config.py` and `ingest/`).
- [x] Required config, model placeholder and ontology files copied.
- [x] Four V2 datasets copied unchanged.
- [x] Old V1 `pilot_20` excluded (no test requires it).
- [x] `pyproject.toml` discovers `synthetic_data`, `synth` and `app`.
- [x] Editable install verified from the export root.
- [x] CLI entry point and `py -m synthetic_data` verified.
- [x] 112 tests pass in a fresh external venv.
- [x] 20-page V2 smoke generation passes.
- [x] Gold and QA validation pass in the smoke run.
- [x] External-directory generation through the Python API passes.
- [x] Oracle evaluator check scores 1.0 on all applicable metrics.
- [x] All four datasets byte-identical to source (4,576 files, 77,535,717 bytes).
- [x] No imports from the original Datanaut checkout during verification.
- [x] `pip check` reports no broken requirements.
- [x] `SHA256SUMS.txt` created and verified.
- [x] `.gitignore` covers caches, virtual environments, secrets and scratch
      output while keeping the four datasets.
- [x] Ten project documents written.
- [x] README documents actual commands and measured results.
- [x] Scope statement avoids claiming an extraction-pipeline evaluation.

## Security and privacy

- [x] No `.git` directory in the export.
- [x] No `.env`, `config.local.yaml`, `config.yaml` or secret store copied.
- [x] No virtual environment copied.
- [x] No `__pycache__`, `.pytest_cache`, `.egg-info` or build artifact in the
      final tree.
- [x] No `.pstats`, profiling database or failed-run scratch artifact copied.
- [x] Config files inspected for credentials: none found.
- [x] `models.yaml` contains a placeholder endpoint and an environment-variable
      name (`SYNTH_T3_API_KEY`), not a key.
- [x] `p1.default.yaml` contains a local-only default PostgreSQL URL, but the
      generator uses in-memory SQLite and never connects to PostgreSQL.
- [x] No network access is required for generation or tests.
- [x] All people, companies and events are fictional; the README says so.
- [x] No private user documents or database rows are included.
- [ ] **PENDING:** confirm that no real person or organisation is accidentally
      identifiable from the fixed name pools or generated combinations.
- [ ] **PENDING:** confirm that the datasets may be redistributed publicly.

## Licensing and ownership

### What was found

- The source `pyproject.toml` declares `license = { text = "Apache-2.0" }`.
- No root `LICENSE` or `NOTICE` file was found in the source checkout.
- No author list or contributor attribution was found in a form that can be
  copied safely into this export.
- `synth/console/static/vendor/cytoscape-3.30.2.min.js` is third-party code.
  Its MIT licence text is included at
  `synth/console/static/vendor/cytoscape-LICENSE.txt`.
- Runtime dependencies (pydantic, SQLAlchemy, PyYAML, jsonschema, RapidFuzz,
  pysbd, python-ulid, httpx, pytest) are installed from PyPI; their licences are
  not vendored in this folder.
- This export does not add a `LICENSE` file and does not invent authors,
  contributors or permissions.

### Pending approvals

- [ ] **PENDING:** team permission to republish the copied `synth/` and `app/`
      code.
- [ ] **PENDING:** supervisor / project-owner permission for public release.
- [ ] **PENDING:** ownership decision for the generated datasets.
- [ ] **PENDING:** licence decision. A declared Apache-2.0 value in a project
      file is not, by itself, proof that every copied file may be redistributed.
- [ ] **PENDING:** attribution text for Datanaut/Synth and any third-party code.
- [ ] **PENDING:** decision on whether to include the inactive `synth/console`,
      `synth/api`, `synth/viewer`, `synth/export` and orchestrator code, or to
      publish a tested trimmed slice instead.
- [ ] **PENDING:** decision on whether the four datasets belong in the public
      repository, Git LFS, or GitHub Release assets.

### Third-party notice

If the full `synth/` package is published, keep
`synth/console/static/vendor/cytoscape-LICENSE.txt` with the vendored
`cytoscape-3.30.2.min.js` file. Do not remove or rewrite that notice.

## Repository and data review

- Export size: **about 80.5 MB** (77 MiB) across 4,701 files, including
  `SHA256SUMS.txt`.
- Largest file: `synthetic_data/output/corpus_5000_v2/qa_dataset.json`
  (27,398,845 bytes).
- The 5,000-page corpus directory is 59.5 MB and 3,503 files.
- No file is near the common 100 MB per-file Git limit, but the JSON files are
  large enough that Git LFS or Release assets may be preferable.
- [ ] **PENDING:** decide whether all four datasets are committed or only the
      20-page pilot is committed with larger corpora attached to a release.
- [ ] **PENDING:** check the destination GitHub organisation's size and data
      policies.
- [ ] **PENDING:** confirm that generated data has no licence conflict with the
      code licence.

Before any commit:

```powershell
git status --short
```

Expected: only intended source, docs and dataset files; no `.venv`,
`__pycache__`, `.pytest_cache`, `.egg-info` or scratch output.

## Publication steps still to perform

These are deliberately not done:

1. [ ] Obtain the approvals above and record them in this file.
2. [ ] Add the approved `LICENSE` and any required `NOTICE`/attribution file.
3. [ ] Review the README's publication-status paragraph and remove or update it
       only after approval.
4. [ ] Decide the dataset hosting strategy (repository, LFS or release assets).
5. [ ] Run `git init` in this folder, review `git status`, and make the first
       commit.
6. [ ] Create the remote repository.
7. [ ] Push the commit.
8. [ ] Re-run the tests and `SHA256SUMS.txt` verification from a fresh clone.
9. [ ] Archive the release and record the archive hash.

No commit, tag, remote or push has been created by this export.

## Claims to keep out of the public README until verified

Do **not** claim any of the following:

- that the Datanaut/FYP extraction pipeline has been evaluated;
- that a model has been benchmarked on these datasets;
- that the gold is human-annotated, blind or adjudicated;
- that the project supports a normal wheel/PyPI install;
- that PDF, DOCX or XLSX extraction has been tested;
- that the data describes real organisations;
- that the datasets are licensed under a particular licence;
- that all copied code is cleared for public redistribution.

## Sign-off

| Item | Status | Owner / note |
|---|---|---|
| Technical export verified | Complete | Local isolation test, 112 tests, smoke run |
| Secret scan | Complete | No credentials found |
| Dataset byte-identity | Complete | 4,576 files, 0 mismatches |
| Documentation | Complete | Ten docs + README |
| Team permission | **PENDING** | |
| Supervisor permission | **PENDING** | |
| Ownership decision | **PENDING** | |
| Licence decision | **PENDING** | |
| Attribution decision | **PENDING** | |
| Public repository creation | **PENDING** | Not performed |
| Commit / push | **PENDING** | Not performed |
