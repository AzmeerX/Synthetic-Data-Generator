# Benchmark evaluator

Scores a prediction bundle (see [BENCHMARK_PREDICTION_SCHEMA.md](BENCHMARK_PREDICTION_SCHEMA.md))
against the V3 gold artifacts. It reads published JSON only and shares no code with
the generator, the parser or the semantic world.

## Install and run

The package needs the repository's Python environment:

```bash
py -3.13 -m pip install -e ".[dev]"
```

Score a bundle:

```python
from synthetic_data.benchmark.adapter import JsonPredictionAdapter
from synthetic_data.benchmark.evaluate import evaluate_corpus
from synthetic_data.benchmark.gold import load_gold_corpus
from synthetic_data.benchmark.report import markdown_report

gold = load_gold_corpus("synthetic_data/output/v3_pilot_portable",
                        "synthetic_data/output/v3_qa")
result = evaluate_corpus(gold, JsonPredictionAdapter("predictions.json").load())
print(markdown_report(result))
```

Run the Phase I pilot (evaluator self-test, **not** a model benchmark):

```bash
py -3.13 -m synthetic_data.benchmark.pilot
```

Outputs land in `synthetic_data/output/v3_eval/`: one JSON and one Markdown report per
fixture plus `manifest.json`.

## Metrics

Every metric reports `tp`, `fp`, `fn`, `precision`, `recall`, `f1`. A metric with an
empty denominator is `null` — "nothing to score" is never rendered as 1.0.

### Entities

A predicted entity matches a gold entity when its name (or an alias) is the text of
that entity's mentions **and of no other entity's**. Gold v2 stores entity ids and
mention quotes, not display names, so identity is decided by the mention text.

* an entity that resolves to two or more gold entities is counted ambiguous;
* a name that resolves to an entity already matched in the same document is a
  duplicate and counts as a false positive;
* an unresolved name is a false positive; the unmatched gold entity is a false
  negative.

Reason counters: `alias_match`, `namesake_ambiguous`, `duplicate_entity`.

### Relations

Exact directed matching on `(subject, relation, object)` after both endpoints are
resolved to gold entities. A relation that matches the gold triple but disagrees on
`status`, `negated` or `effective` is a false positive **and** a false negative, with a
reason counter: `status_mismatch`, `negation_mismatch`, `temporal_mismatch`.

`reversed_direction` fires when the triple matches only with the arguments swapped,
`duplicate_relation` when the same triple is predicted twice, `unresolved_endpoint`
when an endpoint could not be mapped.

### Values (money, percentages, dates, derived)

Applied to matched relation pairs (and available to any caller as typed comparators):

* `money_equal` compares minor units **and** currency; a right amount in the wrong
  currency is counted as `currency_mismatch`, not as a plain miss;
* `percent_equal` compares to 0.005 percentage points;
* `date_equal` accepts ISO and the prose forms the corpus uses;
* `parse_money`, `find_money` extract typed amounts from a quote;
* `reconcile_derived(inputs, total, predicted_total)` checks a derived value against
  the values it is computed from — a total that is right for the wrong reason is
  reported as unreconciled.

A gold amount is only available where the gold evidence carries one (money quotes, or
workbook cells with a currency in the evidence record). Where gold has no typed value
the comparison is not invented: the denominator stays small and is reported as such.

### Evidence and provenance

* quote matching is **exact** after normalisation; a paraphrase is a false positive
  (`quote_partial`), because the benchmark is about parser text;
* locators (`sentence_id`, `page`, `section`, `cell`) are scored separately in the
  `locators` metric, only where the gold artifact has a locator to compare.

### QA

| metric | definition |
|---|---|
| exact match | byte-equal answer after stripping |
| normalised match | casefold, whitespace, edge punctuation, typed money/percent/date comparison for those answer kinds |
| token F1 | mean over answerable questions |
| answerability | a question is correct when the predicted answerability equals the gold one; answering an unanswerable question is a false positive, refusing an answerable one is a miss |
| contradiction questions | accuracy on `contradiction_resolution` questions specifically |

A question with no prediction is `missing_questions` and lowers recall: omission never
improves a score. Correct refusals count as correct answers in the `answers` metric as
well, and separately in `answerability`.

### Aggregation

Reported separately by format, document type, question type, answerability category,
document, and corpus. Per-document rows keep a weak format visible instead of letting
an average hide it.

Denominators are per group; a group with no items reports `null`, not 0.

## Determinism

The same bundle scored twice produces the same JSON. The pilot asserts this
(`manifest["deterministic"]`), and the fixtures are regenerated from gold, not stored
as bytes.

## Limitations, stated plainly

* **No model has been scored.** The pilot scores fixtures built from gold; it proves
  the evaluator works, not that any system extracts well.
* Prose relations have no typed amounts in gold v2, so the `values` metric has a small
  denominator on prose and a larger one on the workbook. This is reported, not hidden.
* Entity identity depends on mention text; an entity whose only mention is a number
  (a cell value) can only be matched by its canonical id.
* Propositions (narrative statements) have no prediction surface in schema v1; their
  evidence is not part of the extraction evidence metric.
* Gold is `mode: generated`, not human-adjudicated.
