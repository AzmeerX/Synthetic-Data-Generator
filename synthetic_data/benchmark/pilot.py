"""Phase I pilot: run the evaluator against evaluator fixtures (not a model).

    py -3.13 -m synthetic_data.benchmark.pilot

Three runs over the Phase H pilot corpus:

* **perfect** — a prediction bundle built from gold; must score 1.0 everywhere;
* **empty** — no predictions at all; must score recall 0 with every item counted;
* **corrupted** — one deliberate error per fixture, each moving one metric.

These test the evaluator.  They are *not* an extraction benchmark: no FYP model has
been run, and the report says so on every page.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from synthetic_data.benchmark.evaluate import aggregate_by_question, evaluate_corpus
from synthetic_data.benchmark.fixtures import CORRUPTIONS, corrupt, empty_bundle, \
    perfect_bundle
from synthetic_data.benchmark.gold import load_gold_corpus
from synthetic_data.benchmark.report import markdown_report, summary_line

__all__ = ["GOLD_DIR", "QA_DIR", "EVAL_DIR", "run_pilot", "LABEL"]

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_pilot_portable"
QA_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_qa"
EVAL_DIR = REPO_ROOT / "synthetic_data" / "output" / "v3_eval"

LABEL = ("Evaluator fixture — no extraction model was run. This scores a synthetic "
         "prediction bundle, not the FYP system.")


def _write(root: Path, name: str, result: dict[str, Any],
           question_metrics: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    (root / f"{name}.json").write_text(payload, encoding="utf-8")
    (root / f"{name}.md").write_text(
        markdown_report(result, question_metrics=question_metrics, notes=[LABEL]),
        encoding="utf-8")
    return {"json": f"{name}.json", "markdown": f"{name}.md",
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "summary": summary_line(result)}


def run_pilot(out_dir: Path | None = None) -> dict[str, Any]:
    root = Path(out_dir) if out_dir is not None else EVAL_DIR
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    gold = load_gold_corpus(GOLD_DIR, QA_DIR)
    if not gold:
        raise RuntimeError(f"no gold documents under {GOLD_DIR}")

    runs: dict[str, Any] = {}
    fixtures = {"perfect": perfect_bundle(gold, corpus="v3_pilot"),
                "empty": empty_bundle("v3_pilot")}
    for corruption in CORRUPTIONS:
        fixtures[f"corrupt__{corruption.name}"] = corrupt(gold, corruption)

    for name, bundle in fixtures.items():
        result = evaluate_corpus(gold, bundle, label=name)
        question_metrics = aggregate_by_question(gold, bundle)
        runs[name] = _write(root, name, result, question_metrics)

    # determinism: the same bundle scored twice must produce the same bytes
    repeat = evaluate_corpus(gold, perfect_bundle(gold, corpus="v3_pilot"),
                             label="perfect")
    first = (root / "perfect.json").read_text(encoding="utf-8")
    second = json.dumps(repeat, indent=2, sort_keys=True) + "\n"

    manifest = {
        "phase": "I",
        "label": LABEL,
        "gold": str(GOLD_DIR.relative_to(REPO_ROOT)),
        "questions": str(QA_DIR.relative_to(REPO_ROOT)),
        "documents": len(gold),
        "runs": runs,
        "deterministic": first == second,
        "fixtures": {
            "perfect": "prediction bundle built from gold: must score 1.0",
            "empty": "no predictions: recall 0, every item counted as missing",
            "corrupted": {corruption.name: corruption.description
                          for corruption in CORRUPTIONS},
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    manifest = run_pilot()
    perfect = manifest["runs"]["perfect"]["summary"]
    empty = manifest["runs"]["empty"]["summary"]
    print(json.dumps({"documents": manifest["documents"],
                      "runs": len(manifest["runs"]),
                      "deterministic": manifest["deterministic"],
                      "perfect": perfect, "empty": empty,
                      "directory": str(EVAL_DIR)}, indent=2))
