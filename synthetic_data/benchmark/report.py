"""Markdown rendering of evaluation results (Phase I)."""

from __future__ import annotations

from typing import Any

__all__ = ["markdown_report", "summary_line"]

_METRICS = ("entities", "relations", "evidence", "values", "locators", "answers",
            "answerability", "contradictions")


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _row(name: str, block: dict[str, Any]) -> str:
    cells = []
    for metric in _METRICS:
        entry = block.get(metric) or {}
        precision = _format(entry.get("precision"))
        recall = _format(entry.get("recall"))
        f1 = _format(entry.get("f1"))
        cells.append(f"{precision} / {recall} / {f1}")
    return f"| {name} | " + " | ".join(cells) + " |"


def _header() -> list[str]:
    return ["| group | " + " | ".join(f"`{metric}` P/R/F1" for metric in _METRICS) + " |",
            "|---|" + "---|" * len(_METRICS)]


def summary_line(result: dict[str, Any]) -> str:
    """F1 where it exists, recall where nothing was predicted (empty bundles)."""
    corpus = result["corpus"]
    parts = []
    for metric in ("entities", "relations", "answers", "answerability"):
        entry = corpus[metric]
        if entry["f1"] is not None:
            parts.append(f"{metric} F1 {entry['f1']:.3f}")
        else:
            parts.append(f"{metric} recall {_format(entry['recall'])}")
    return f"{result['label']}: " + ", ".join(parts)


def markdown_report(result: dict[str, Any], *, question_metrics: dict[str, Any] | None = None,
                    notes: list[str] | None = None) -> str:
    lines: list[str] = []
    add = lines.append
    corpus = result["corpus"]
    add(f"# Evaluation — {result['label']}")
    add("")
    add(f"Adapter `{result['adapter']}` · prediction schema {result['schema_version']} · "
        f"{corpus['documents']} gold documents")
    add("")
    if notes:
        for note in notes:
            add(f"> {note}")
        add("")
    add("## Corpus totals")
    add("")
    add("| metric | tp | fp | fn | precision | recall | F1 |")
    add("|---|---|---|---|---|---|---|")
    for metric in _METRICS:
        entry = corpus.get(metric) or {}
        add(f"| `{metric}` | {entry.get('tp', 0)} | {entry.get('fp', 0)} | "
            f"{entry.get('fn', 0)} | {_format(entry.get('precision'))} | "
            f"{_format(entry.get('recall'))} | {_format(entry.get('f1'))} |")
    add("")
    add(f"Missing answers: {corpus.get('missing_questions', 0)} · mean token F1: "
        f"{_format(corpus.get('token_f1'))}")
    add("")
    if corpus.get("notes"):
        add("Reason counters: " + ", ".join(f"`{key}` {value}"
                                            for key, value in corpus["notes"].items()))
        add("")
    add("## By format")
    add("")
    lines.extend(_header())
    for name, block in result["by_format"].items():
        add(_row(name, block))
    add("")
    add("## By document type")
    add("")
    lines.extend(_header())
    for name, block in result["by_doc_type"].items():
        add(_row(name, block))
    add("")
    if question_metrics:
        add("## By question type")
        add("")
        add("| question type | tp | fp | fn | precision | recall | F1 | token F1 |")
        add("|---|---|---|---|---|---|---|---|")
        for name, block in question_metrics["by_question_type"].items():
            add(f"| `{name}` | {block['tp']} | {block['fp']} | {block['fn']} | "
                f"{_format(block['precision'])} | {_format(block['recall'])} | "
                f"{_format(block['f1'])} | {_format(block.get('token_f1'))} |")
        add("")
        for label, block in (("answerable", question_metrics["answerable"]),
                             ("unanswerable", question_metrics["unanswerable"])):
            add(f"{label}: tp {block['tp']}, fp {block['fp']}, fn {block['fn']} — "
                f"precision {_format(block['precision'])}, "
                f"recall {_format(block['recall'])}, F1 {_format(block['f1'])}")
        add("")
    add("## Per document")
    add("")
    add("| document | format | entities F1 | relations F1 | evidence F1 | answers F1 |")
    add("|---|---|---|---|---|---|")
    for document in result["documents"]:
        add(f"| `{document['document_id']}` | {document['format']} | "
            f"{_format(document['entities']['f1'])} | "
            f"{_format(document['relations']['f1'])} | "
            f"{_format(document['evidence']['f1'])} | "
            f"{_format(document['answers']['f1'])} |")
    add("")
    if result.get("unmatched_predictions"):
        add("Predictions with no gold document: "
            + ", ".join(f"`{item}`" for item in result["unmatched_predictions"]))
        add("")
    return "\n".join(lines) + "\n"
