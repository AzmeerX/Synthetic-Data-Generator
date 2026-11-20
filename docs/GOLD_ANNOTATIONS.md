# Gold Annotations

## What "gold" means here

Gold is the expected structured extraction for a passage of a generated
document: entities, relationships, propositions, time references, flags and
identity traps. The QA benchmark uses the same structured facts, so gold and QA
answers cannot drift apart without a validation failure.

This gold is **generator-produced**. It is written with `mode: "generated"` and
is explicitly **not blind** and **not human-verified**. It is never promoted into
Synth's `gold_sets` table. It should not be described as human-adjudicated truth.

## File layout

Each dataset has a `gold/` directory with one JSON file per document:

```text
gold/gen_doc_00000_board_minutes__0-31.json
gold/gen_doc_00001_financial_agreement__0-29.json
...
```

The file name is `<document_stem>__<first_sentence>-<last_sentence>.json`. A file
covers one passage; the included corpora use one passage per document.

The top-level object follows Synth's `GoldSet` schema:

| Field | Meaning |
|---|---|
| `version` | Gold schema version (1) |
| `doc` | Filename, optional Synth `doc_id`, and canonical `text_sha256` |
| `first_idx`, `last_idx` | Canonical sentence range covered by the passage |
| `annotator` | `synthetic_data.generator` |
| `mode` | `generated` |
| `notes` | Trap list and a statement that the file is generated, not blind |
| `entities` | Mentioned entities with verbatim quotes |
| `edges` | Relationships with evidence and time roles |
| `propositions` | Speaker, status, text, polarity and evidence |
| `links` | Logic links between proposition ids |
| `expected_flags` | Contradiction and identity-ambiguity warnings |
| `identity_traps` | Pairs of entity ids that must not be merged |
| `time_refs` | Resolved time expressions and sentence-level evidence |

The `doc_id` is deliberately `null` in generated gold. Synth document ids are
ULIDs and are non-deterministic; gold identifies the document by filename and
`text_sha256` instead. The manifest records the fresh Synth ULID separately for
each run.

## Entities

An entity has:

- a stable id such as `person_03`, `company_00` or `asset_08`;
- a canonical `name`;
- an ontology `type` and `subtype`;
- an optional stable `key` used to link the same real-world entity across files;
- one or more **mentions**.

A mention names the sentence and gives the exact quote:

```json
{
  "sentence_id": "s19",
  "quote": "Juniper Data Centre",
  "occurrence": 0
}
```

`occurrence` disambiguates repeated identical surfaces within one sentence. For
two people with the same full name, the canonical names are qualified in the
document text and the gold ids remain distinct. For an alias, different surfaces
resolve to the same person id.

## Edges

An edge contains subject and object entity ids, a relation, and evidence:

```json
{
  "id": "e_...",
  "subject": "company_00",
  "rel": "owns",
  "object": "asset_08",
  "stance": "asserted",
  "negated": false,
  "nested": false,
  "time": {"role": "occurred_at", "value": "..."},
  "evidence": [{"sentence_id": "s19", "quote": "Oakhaven Holdings Limited owns Juniper Data Centre."}]
}
```

The corpus exercises the critical relations `owns`, `shareholder_of`,
`family_of`, `lent_to`, `owes`, `guarantor_for` and `represents`, plus
`chair_of`, `signed`, `party_to` and `located_in`. Direction matters: the
semantic validator re-reads the sentence and rejects reversed family, ownership,
representation or financial relations.

Facility capacity is represented as a **proposition**, not an edge. A facility
increase is not a new loan or a completed disbursement. Only completed,
non-negated `lent_to` edges produce transactions.

## Propositions, status and links

A proposition records a statement that is not a simple edge: a proposal, an
allegation, a denial, a conditional obligation or a facility-capacity statement.
It carries an id, text, speaker (`null` for the document itself, or a person id),
a status from the ontology such as `held`, `found`, `alleged`, `denied`,
`proposed` or `stated`, a polarity flag and verbatim evidence.

Links are top-level objects connecting proposition ids:

```json
{
  "id": "gen_doc_00000_link_conditional",
  "src": "gen_doc_00000_proposal",
  "dst": "gen_doc_00000_approval",
  "type": "conditional_on",
  "evidence": [{"sentence_id": "s26", "quote": "..."}]
}
```

Link types include `because`, `unless`, `provided_that`, `notwithstanding`,
`therefore`, `contradicts`, `supports` and `conditional_on`.

Status semantics are validated. An allegation remains attributed to its speaker;
a proposal remains uncompleted; a negated statement does not carry a positive
status; a facility increase remains a capacity.

## Time

Time references (`time_refs`) record the surface expression, time kind
(`INSTANT`, `INTERVAL`, `PERIOD`, `DURATION`, `RECURRING`, `RELATIVE`),
`value_start` / `value_end`, granularity, anchor type and verbatim evidence.
Edges and propositions carry time roles such as `occurred_at`, `valid_from`,
`valid_to`, `as_of`, `deadline` and `reported_on`.

The corpus contains:

- absolute dates;
- explicitly stated fiscal-year periods;
- relative dates anchored on a stated document date;
- intervals with start and end;
- historical changes such as a chair hand-over.

The semantic validator checks that relative dates have a stated anchor and that
fiscal periods are written explicitly. It does not assume an unstated fiscal
calendar.

## Contradiction and identity traps

The base world contains one deliberate cross-document contradiction about a loan
amount. Both gold files carry flags that name the counterpart fact and file. A
same-event conflict with genuinely different amounts is required; a mere wording
difference is not a contradiction.

Identity traps cover:

- two different people with the same full name;
- one person with an alias or former name.

The semantic validator checks that the namesakes resolve to distinct ids and
that alias surfaces resolve to one id.

## How gold is generated

1. The world and document plans are built from the seed.
2. Documents are rendered and ingested through the real Synth parser.
3. The generator maps every planned sentence to the canonical sentence index.
4. `build_gold()` converts plans, facts and `DocView` sentences into a `GoldSet`.
5. `write_gold_file()` writes the JSON.
6. `synth.eval.gold.validate()` locates every quote and checks references.
7. The pipeline rejects any sentence mismatch, not-found quote or error.
8. Independent semantic checks re-read the text and verify meaning.

The generator never asks a model what a document says. It knows the fact it
encoded and uses the parser only for canonical labels and hashes.

## Validation results

| Dataset | Gold documents | Valid | Items | Grounding | Sentence mismatches | Not found | Errors |
|---|---:|---:|---:|---|---:|---:|---:|
| `pilot_20_v2` | 7 | 7 | 331 | 331 `EXACT` | 0 | 0 | 0 |
| `corpus_500_v2` | 175 | 175 | 7,843 | 7,843 `EXACT` | 0 | 0 | 0 |
| `corpus_1000_v2` | 350 | 350 | 15,668 | 15,668 `EXACT` | 0 | 0 | 0 |
| `corpus_5000_v2` | 1,750 | 1,750 | 78,268 | 78,268 `EXACT` | 0 | 0 | 0 |

These counts are taken from each dataset's `manifest.json`.

## Limitations

- Gold is generated from templates and structured facts, not human annotation.
- It is not blind: the annotator knows the intended answer.
- It is not a general test of open-domain extraction; it targets the phenomena
  the generator can construct.
- A valid gold file proves internal grounding, not external correctness.
- No inter-annotator agreement can be reported because there are no human
  annotators.
