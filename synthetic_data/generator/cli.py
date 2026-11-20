"""Command line entry points for the synthetic-data generator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from synth.config import REPO_ROOT

from synthetic_data.generator.evaluation import evaluate_files, write_report
from synthetic_data.generator.pipeline import generate_corpus

DEFAULT_OUTPUT_ROOT = REPO_ROOT / "synthetic_data" / "output"


def _output_root() -> Path:
    return DEFAULT_OUTPUT_ROOT.resolve()


def _ensure_under_output(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(_output_root())
    except ValueError as exc:
        raise SystemExit(
            f"refusing to write outside {DEFAULT_OUTPUT_ROOT}; got {resolved}") from exc
    return resolved


def _generate(args: argparse.Namespace) -> int:
    out_dir = _ensure_under_output(Path(args.out))
    if args.pages <= 0 or args.pages % 20 != 0:
        raise SystemExit("--pages must be a positive multiple of 20")
    result = generate_corpus(
        seed=args.seed,
        pages=args.pages,
        out_dir=out_dir,
        overwrite=args.overwrite,
        layout=args.layout,
    )
    validation = result.manifest["validation"]
    queries = result.manifest["queries"]
    print(f"Generated {result.manifest['batch']['actual_pages']} pages "
          f"in {result.manifest['batch']['document_count']} documents.")
    print(f"Gold validation: {validation['ok']}/{validation['documents']} ok, "
          f"{validation['items']} grounded items, "
          f"{validation['sentence_mismatch']} sentence mismatches, "
          f"{validation['not_found']} not found.")
    print(f"QA queries: {queries['total']} "
          f"({queries['answerable']} answerable, {queries['unanswerable']} unanswerable).")
    if args.layout == "v2":
        duplicates = queries["duplicates"]
        print(f"Duplicate normalized questions: "
              f"{duplicates['duplicate_normalized_percent']}%.")
        performance = result.manifest["performance"]
        print(f"Timings (ms): {performance['timings_ms']}")
        print(f"Peak memory: {performance['peak_memory_mb']} MB.")
    print(f"Manifest: {result.manifest_path}")
    print(f"QA dataset: {result.dataset_path}")
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    report = evaluate_files(args.dataset, args.predictions)
    if args.out:
        out = Path(args.out)
        if args.enforce_output_root:
            out = _ensure_under_output(out)
        write_report(report, out)
    metrics = report["metrics"]
    print(json.dumps({
        "queries": report["queries"],
        "answerable_queries": report["answerable_queries"],
        "unanswerable_queries": report["unanswerable_queries"],
        "missing_predictions": report["missing_predictions"],
        "metrics": metrics,
        "by_type": {name: value["metrics"]
                    for name, value in report["by_type"].items()},
    }, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py -m synthetic_data",
        description="Deterministic synthetic corporate/legal corpus and QA benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser(
        "generate", help="generate, ingest, ground and validate a corpus checkpoint",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    generate.add_argument("--pages", type=int, default=20,
                          help="total pages; must be a positive multiple of 20")
    generate.add_argument("--seed", type=int, default=7)
    generate.add_argument("--out", default=str(DEFAULT_OUTPUT_ROOT / "pilot_20"))
    generate.add_argument("--layout", choices=("v1", "v2"), default="v1",
                          help="v1 is the frozen Phase 1 stream; v2 is the improved layout")
    generate.add_argument("--overwrite", action="store_true",
                          help="replace previously generated files in --out")
    generate.set_defaults(func=_generate)

    evaluate = sub.add_parser(
        "evaluate", help="score answer predictions against a generated QA dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--out", help="write the machine-readable report here")
    evaluate.add_argument("--enforce-output-root", action="store_true",
                          help="refuse to write the report outside synthetic_data/output")
    evaluate.set_defaults(func=_evaluate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:  # keep the CLI honest: no fabricated success
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
