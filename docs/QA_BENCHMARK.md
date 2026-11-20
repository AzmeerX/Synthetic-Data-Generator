# QA Benchmark

## Design

The QA dataset is generated from the same world model and document plans as the
gold annotations. Every expected answer is computed by code. No language model
authors questions, answers or evidence.

A query has:

- a stable `query_id`;
- a question ending in `?`;
- a `query_type`;
- an expected answer and answer type;
- an `answerable` flag;
- supporting evidence for answerable questions;
- gold fact ids where applicable;
- relevant document ids;
- notes, including a trap explanation;
- for unanswerable questions, an `unanswerable_kind` and a structured proof.

Questions are deterministic templates with wording variants keyed by stable
fact and document ids. They are not natural paraphrases, and the benchmark does
not claim stylistic diversity.

## Query types

| Query type | Pilot 20 | 500 | 1,000 | 5,000 |
|---|---:|---:|---:|---:|
| `alleged_status` | 1 | 75 | 150 | 750 |
| `contradiction` | 1 | 1 | 1 | 1 |
| `entity_lookup` | 7 | 175 | 350 | 1,750 |
| `financial` | 18 | 402 | 802 | 4,002 |
| `historical_change` | 2 | 2 | 2 | 2 |
| `identity_resolution` | 3 | 125 | 250 | 1,250 |
| `multi_hop` | 7 | 15 | 15 | 15 |
| `negation` | 1 | 50 | 100 | 500 |
| `proposed_vs_completed` | 4 | 150 | 300 | 1,500 |
| `provenance` | 1 | 210 | 420 | 2,100 |
| `relationship_lookup` | 17 | 386 | 776 | 3,876 |
| `temporal` | 8 | 152 | 302 | 1,502 |
| `unanswerable` | 25 | 622 | 1,239 | 6,160 |
| **Total** | **95** | **2,365** | **4,707** | **23,408** |

The corpus-level types (`contradiction`, `historical_change`, `multi_hop`) are
intentionally small: there is one contradiction, one chair hand-over and a
capped set of multi-hop joins.

At 20 pages, 70 questions are answerable (73.7%) and 25 are unanswerable
(26.3%). The same ratio holds at each larger checkpoint. The 5,000-page dataset
has 17,248 answerable and 6,160 unanswerable questions.

## Answerable evidence

An answerable query must have at least one evidence item. Each item contains:

```json
{
  "document_id": "gen_doc_00000",
  "filename": "gen_doc_00000_board_minutes.txt",
  "text_sha256": "4af2ca6b...f107c91",
  "sentence_idx": 19,
  "sentence_id": "s19",
  "quote": "Oakhaven Holdings Limited owns Juniper Data Centre."
}
```

At generation time the pipeline checks:

- the quote is a literal substring of the canonical sentence;
- the document id, filename and hash agree with the document context;
- the sentence index and sentence id agree;
- every gold fact id is stated in the corpus;
- `relevant_document_ids` equals the unique evidence document ids.

The independent semantic validator also checks relationship direction, unique
answers in the cited document, temporal support, proposition status, entity
identity, transactions and contradictions.

## The five unanswerable categories

V2 emits five categories, evenly distributed. Each has a structural proof derived
from the `FactIndex`; keyword absence alone is not enough.

### 1. Absent attribute

Example: "Which firm audited the accounts of *Quarrystone Industries Limited*?"
The company has no `auditor` attribute in the world model, and no indexed fact
involving it states one. Another company may have an auditor, which makes the
question a near miss rather than a random unknown.

### 2. Absent relationship

Example: a question about whether a firm `advises` a company, when the corpus has
`represents` but no `advises` edge. The proof records the relation searched and
the checked facts and documents.

### 3. Unsupported historical date

Example: "Who chaired Oakhaven Holdings Limited on 2 January 2020?" when the
earliest chair fact starts on 1 January 2021. The proof records the reference
date and the chair facts checked.

### 4. Absent financial information

Example: a dividend question for a company with lending and balance facts but no
dividend fact. The proof records the attribute searched and the financial facts
that were checked.

### 5. False presupposition / near miss

Example: "What was the reason for the acquisition of Juniper Industries Limited
by Oakhaven Holdings Limited in 2024?" when the corpus contains only a proposed
acquisition and no completed ownership edge. The proof records the presupposed
subject, relation and the facts that do not support it.

A proof contains:

```json
{
  "kind": "absent_attribute",
  "subject_id": "company_01",
  "relation": null,
  "object_id": null,
  "attribute": "auditor",
  "reference_date": null,
  "checked_fact_ids": ["fact_0016", "..."],
  "checked_document_ids": ["gen_doc_00001", "gen_doc_00000"],
  "explanation": "..."
}
```

Unknown proof facts or documents, inconsistent subjects, or proofs attached to
answerable questions are rejected.

## Distribution by unanswerable kind

| Kind | Pilot 20 | 500 | 1,000 | 5,000 |
|---|---:|---:|---:|---:|
| `absent_attribute` | 5 | 125 | 248 | 1,232 |
| `absent_relationship` | 5 | 125 | 248 | 1,232 |
| `unsupported_historical_date` | 5 | 124 | 248 | 1,232 |
| `absent_financial_information` | 5 | 124 | 248 | 1,232 |
| `false_presupposition` | 5 | 124 | 247 | 1,232 |
| **Total** | **25** | **622** | **1,239** | **6,160** |

The small imbalances at 500 and 1,000 pages come from the even-division
remainder and the available proof subjects.

## Duplicate control

V2 normalises question/answer text and caps a normalized pair at two
occurrences. The manifest reports total queries, unique normalized forms,
duplicate instances, duplicate percentage and the maximum occurrence count.

All four included datasets report:

- 100% unique normalized forms;
- 0 duplicate normalized instances;
- 0.0% duplicates;
- maximum one occurrence per normalized pair.

This is a lexical duplicate check. It does not mean the questions are
stylistically varied.

## Closed-world wording

Proposed-versus-completed questions explicitly state the corpus's closed-world
assumption in the question and in the annotation notes. A proposal is not a
completed transaction, and the absence of a completion fact is a valid answer
only under the documented closed-world reading of the corpus.

## Evaluation

`py -m synthetic_data evaluate` scores submitted predictions against the QA
dataset. It reports overall and per-query-type metrics:

| Metric | Meaning |
|---|---|
| `exact_match` | Raw string equality after trimming |
| `normalized_exact_match` | Case, punctuation, article, currency, date and `m`/`million` normalisation |
| `token_f1` | Token overlap F1 on normalized text |
| `citation_accuracy` | At least one cited sentence is a gold evidence sentence |
| `evidence_retrieval_accuracy` | The evidence set is retrieved exactly |
| `evidence_precision` | Correct evidence / all cited evidence |
| `evidence_recall` | Correct evidence / gold evidence |
| `evidence_f1` | Harmonic mean of evidence precision and recall |
| `document_retrieval_accuracy` | At least one cited document is relevant |
| `answerability_accuracy` | Answerable flag matches gold |
| `answerable_detection_rate` | Answerable questions correctly marked answerable |
| `unanswerable_abstention_rate` | Unanswerable questions correctly left unanswered |

Unknown citations count against precision. Missing predictions are counted
separately and do not silently disappear.

### Prediction format

```json
{
  "predictions": [
    {
      "query_id": "q_00003",
      "answer": "Juniper Data Centre",
      "answerable": true,
      "citations": [
        {"document_id": "gen_doc_00000", "sentence_id": "s19"}
      ]
    },
    {
      "query_id": "uq_00001",
      "answer": "",
      "answerable": false,
      "citations": []
    }
  ]
}
```

A bare JSON list is accepted. Citations may also use `filename` plus
`sentence_idx`.

### Verified evaluator check

An oracle prediction set built from the gold answers and evidence was scored on
the 20-page smoke dataset:

- 95 queries, 70 answerable, 25 unanswerable, 0 missing predictions;
- exact match, normalized exact match, token F1, citation accuracy, evidence
  retrieval, evidence precision, evidence F1, document retrieval, answerability
  accuracy, answerable detection and unanswerable abstention all **1.0**.

This proves the evaluator and the artifact contracts agree. It is **not** an
extraction baseline and says nothing about model quality.

## Limitations

- Questions are templates, not natural paraphrases.
- The benchmark measures lexical/evidence agreement, not full semantic
  equivalence.
- The unanswerable proofs cover five constructed categories, not every possible
  kind of unanswerable question.
- No human has reviewed the questions or answers.
- No model has been evaluated on these datasets in this project.
- Results at 500+ pages are generated and validated but not human-audited.
