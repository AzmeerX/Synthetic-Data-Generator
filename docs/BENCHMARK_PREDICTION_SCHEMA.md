# Benchmark prediction schema (v1)

This is the contract an extraction system satisfies to be scored against the V3
benchmark. The evaluator reads **only** this format; it does not import the generator,
the parser or the semantic world.

    schema_version: 1

A bundle is one JSON object:

```json
{
  "schema_version": 1,
  "corpus": "v3_pilot",
  "adapter": "fyp-main@2026-03",
  "documents": [ { ... } ]
}
```

`documents` may be empty — an empty bundle is scored, not skipped. Unknown fields are
rejected: a typo silently ignored is a metric scored against nothing.

## Document

| field | type | required | notes |
|---|---|---|---|
| `document_id` | string | yes | canonical id from the corpus manifest, e.g. `doc:00000:qr3hgvjuuciha` |
| `format` | string | yes | `txt`, `pdf`, `docx`, `xlsx` |
| `doc_type` | string | no | informational; the gold side supplies the real one |
| `entities` | array | no | see below |
| `relations` | array | no | |
| `attributes` | array | no | typed values not attached to a relation |
| `contradictions` | array | no | |
| `answers` | array | no | |

### Entity

```json
{"entity_id": "e1", "name": "Dungate Industries GmbH", "entity_type": "organization",
 "aliases": ["Dungate"], "mentions": [{"quote": "...", "sentence_id": "sent:000123"}],
 "confidence": 0.91}
```

`entity_id` is yours; it only has to be unique in the document. It is the value
relations refer to. Matching to gold is by **name and mentions**, not by your id.

### Relation

```json
{"subject": "e1", "object": "e2", "relation": "owns", "status": "completed",
 "negated": false, "amount_minor": 1200000000, "currency": "GBP",
 "effective": "2023-01-23", "confidence": 0.88,
 "evidence": [{"quote": "| Dungate Industries GmbH | Junipermoor Holdings plc | 40.00% |",
               "sentence_id": "sent:000123", "page": 3, "section": 2,
               "sheet": "Share register", "cell": "E4"}]}
```

* `subject` and `object` must be a declared `entity_id` or entity name.
* `relation` is the ontology token (`owns`, `is_subsidiary_of`, `lent_to`, …).
* `status` is the proposition status (`completed`, `proposed`, `alleged`, `found`, …).
* `negated` marks a denied proposition.
* `amount_minor` is in **minor units** (pence) and requires `currency`; an amount
  without a currency cannot be compared with a typed gold value and is refused at
  validation.
* `effective` is an ISO date.
* Locators: `page` for PDF/TXT, `section` for DOCX, `sheet` + `cell` for XLSX.

### Attribute

```json
{"entity": "e1", "name": "Total repaid", "value": "GBP 30,000,000.00",
 "value_kind": "money", "currency": "GBP", "evidence": [...]}
```

`value_kind` is one of `text`, `money`, `percent`, `date`, `count`.

### Contradiction

```json
{"subject": "e1", "dimension": "amount",
 "values": ["GBP 12,000,000.00", "GBP 12,500,000.00"],
 "documents": ["doc:00004:hvvl3bkydskzq", "doc:00005:ugozodhgnub3y"]}
```

### Answer

```json
{"question_id": "qa:00002/financial/8e9fcab3b5", "answer": "GBP 12,000,000.00",
 "answerable": true, "confidence": 0.77,
 "evidence": [{"quote": "...", "sentence_id": "sent:000456", "page": 4}]}
```

* `question_id` comes from `synthetic_data/output/v3_qa/qa/*.json`.
* `answerable: false` means **refused**; it must not carry an `answer`.
* A question with no prediction is counted as missing, which lowers recall.

### Evidence

| field | used for |
|---|---|
| `quote` | exact-match evidence scoring; must be the parser's text, not a paraphrase |
| `sentence_id` | parser sentence reference accuracy |
| `page` | PDF/TXT page accuracy |
| `section` | DOCX section accuracy |
| `sheet`, `cell` | XLSX RenderMap accuracy |
| `char_start`, `char_end` | optional character span |

## Adapters

The evaluator takes any object with `load() -> PredictionBundle`:

```python
from synthetic_data.benchmark.adapter import JsonPredictionAdapter
from synthetic_data.benchmark.evaluate import evaluate_corpus
from synthetic_data.benchmark.gold import load_gold_corpus

gold = load_gold_corpus("synthetic_data/output/v3_pilot_portable",
                        "synthetic_data/output/v3_qa")
result = evaluate_corpus(gold, JsonPredictionAdapter("fyp_predictions.json").load())
```

For a system that produces in-memory objects, write a small adapter that converts
them to the models in `synthetic_data/benchmark/schema.py`; for a system that writes
files, emit this JSON and use `JsonPredictionAdapter`.

## Versioning

`schema_version` is checked on load. A change that adds a required field or changes a
meaning bumps it; the evaluator refuses unknown versions rather than guessing.
