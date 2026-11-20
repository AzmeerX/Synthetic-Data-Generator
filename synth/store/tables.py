"""Every table, defined once in SQLAlchemy Core.

Purpose: the single schema of S1–S5 and L1–L8 (P1 spec §4). Alembic's first
revision builds the database from this module; tests build SQLite from it.
Contract: table names and columns match the spec DDL. Additions beyond the DDL
are marked ``# +`` and recorded in docs/DECISIONS.md.

``APPEND_ONLY_TABLES`` (spec ``[A]``) get a trigger that rejects ``DELETE`` (and
``TRUNCATE`` on PostgreSQL). Updates go through :class:`synth.store.repo.Tx`,
which writes the change log (L4) in the same transaction.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Index, Integer, MetaData,
    PrimaryKeyConstraint, Table, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

#: JSON column: JSONB on PostgreSQL, JSON (text) on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _ts(name: str = "ts") -> Column:
    return Column(name, DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- S1 Provenance
documents = Table(
    "documents", metadata,
    Column("doc_id", Text, primary_key=True),
    Column("sha256", Text, unique=True),
    Column("filename", Text), Column("rel_path", Text), Column("format", Text),
    Column("pages", Integer), Column("doc_date_time_id", Text),
    Column("doc_date", Text),                                  # + ISO date candidate (U1)
    Column("doc_date_source", Text),                           # + metadata | text | none
    Column("sovereign", Boolean, server_default="0"),
    Column("parser", Text), Column("parser_version", Text),
    Column("status", Text),
    Column("notes", JSONType),                                 # + fallbacks, OCR pages, warnings
    _ts("created_at"),
)
doc_texts = Table(
    "doc_texts", metadata,
    Column("doc_id", Text), Column("text_version", Integer),
    Column("canonical_text", Text), Column("text_sha256", Text),
    Column("page_map", JSONType),
    PrimaryKeyConstraint("doc_id", "text_version"),
)
sentences = Table(
    "sentences", metadata,
    Column("sentence_id", Text, primary_key=True),
    Column("doc_id", Text), Column("text_version", Integer), Column("idx", Integer),
    Column("char_start", Integer), Column("char_end", Integer), Column("page", Integer),
    Column("section_path", Text), Column("kind", Text),
    Column("filtered", Boolean, server_default="0"),
    UniqueConstraint("doc_id", "text_version", "idx"),
)
spans = Table(
    "spans", metadata,
    Column("span_id", Text, primary_key=True),
    Column("doc_id", Text), Column("text_version", Integer),
    Column("char_start", Integer), Column("char_end", Integer),
    Column("sentence_ids", JSONType), Column("page", Integer), Column("bbox", JSONType),
    Column("quote", Text), Column("grounding", Text), Column("fuzzy_score", Float),
    Column("created_by", Text), Column("run_id", Text),
)
micro_chunks = Table(
    "micro_chunks", metadata,
    Column("chunk_id", Text, primary_key=True),
    Column("doc_id", Text), Column("idx", Integer),
    Column("first_sentence_idx", Integer), Column("last_sentence_idx", Integer),
    Column("source_tokens", Integer), Column("profile_hash", Text),
)
sections = Table(
    "sections", metadata,
    Column("section_id", Text, primary_key=True),
    Column("doc_id", Text), Column("idx", Integer),            # + idx
    Column("first_chunk_idx", Integer), Column("last_chunk_idx", Integer),
    Column("source_tokens", Integer), Column("profile_hash", Text),
)

# ---------------------------------------------------------------- S2 Entity table [A]
entities = Table(
    "entities", metadata,
    Column("entity_id", Text, primary_key=True),
    Column("run_id", Text),
    Column("doc_id", Text),                                    # + document the entity was built in
    Column("canonical_name", Text), Column("type", Text), Column("subtype", Text),
    Column("norm_key", Text), Column("status", Text), Column("merged_into", Text),
    Column("source", Text), Column("version", Integer, server_default="1"),
)
entity_aliases = Table(
    "entity_aliases", metadata,
    Column("entity_id", Text), Column("alias", Text), Column("mention_id", Text),
)
entity_roles = Table(
    "entity_roles", metadata,
    Column("entity_id", Text), Column("role", Text), Column("org_entity_id", Text),
    Column("span_id", Text), Column("time_id", Text), Column("run_id", Text),
)
mentions = Table(
    "mentions", metadata,
    Column("mention_id", Text, primary_key=True),
    Column("run_id", Text), Column("doc_id", Text), Column("span_id", Text),
    Column("surface", Text), Column("label", Text), Column("score", Float),
    Column("source", Text), Column("entity_id", Text),
    Column("time_id", Text),                                   # + set when U3 turns it into a time ref
    Column("status", Text), Column("discard_reason", Text),
)

# ---------------------------------------------------------------- S3 Time store [A]
TIME_KINDS = ("INSTANT", "INTERVAL", "PERIOD", "DURATION", "RECURRING", "RELATIVE")
time_refs = Table(
    "time_refs", metadata,
    Column("time_id", Text, primary_key=True),
    Column("run_id", Text), Column("doc_id", Text), Column("span_id", Text),
    Column("surface", Text), Column("kind", Text),
    Column("value_start", Text), Column("value_end", Text), Column("granularity", Text),
    Column("anchor_type", Text), Column("anchor_time_id", Text),
    Column("confidence", Float), Column("rule_id", Text),
)

# ---------------------------------------------------------------- S4 / S5 graph stores [A]
micro_edges = Table(
    "micro_edges", metadata,
    Column("edge_id", Text, primary_key=True),
    Column("run_id", Text), Column("chunk_id", Text),
    Column("src_entity_id", Text), Column("rel", Text), Column("subclass", Text),
    Column("dst_entity_id", Text), Column("description", Text), Column("stance", Text),
    Column("negated", Boolean), Column("confidence", Float),
    Column("status", Text), Column("source", Text),            # source: micro | gliner
    Column("reject_reason", Text),                             # + why U6 rejected it
)
macro_edges = Table(
    "macro_edges", metadata,
    Column("edge_id", Text, primary_key=True),
    Column("run_id", Text), Column("section_id", Text),
    Column("src_entity_id", Text), Column("rel", Text), Column("subclass", Text),
    Column("dst_entity_id", Text), Column("description", Text), Column("stance", Text),
    Column("negated", Boolean), Column("confidence", Float),
    Column("status", Text),                                    # VERIFIED|ESCALATED|COMMITTED|REJECTED|HELD_FOR_HUMAN
    Column("tier_last", Text),
    Column("version", Integer, server_default="1"),            # + CORRECT verdicts bump it
    Column("supersedes", Text),                                # + edge this one corrects
)
edge_spans = Table("edge_spans", metadata, Column("edge_id", Text), Column("span_id", Text))
edge_times = Table(
    "edge_times", metadata,
    Column("edge_id", Text), Column("time_id", Text),
    Column("role", Text),        # occurred_at|valid_from|valid_to|as_of|deadline|reported_on
)
edge_lineage = Table(
    "edge_lineage", metadata,
    Column("macro_edge_id", Text), Column("micro_edge_id", Text),
    Column("op", Text),          # CONFIRMED | CORRECTED_FROM
)
propositions = Table(
    "propositions", metadata,
    Column("prop_id", Text, primary_key=True),
    Column("run_id", Text), Column("section_id", Text), Column("text", Text),
    Column("speaker_entity_id", Text), Column("status", Text), Column("negated", Boolean),
    Column("confidence", Float), Column("state", Text),
    Column("span_id", Text),                                   # + evidence span
)
prop_links = Table(
    "prop_links", metadata,
    Column("link_id", Text, primary_key=True),
    Column("run_id", Text), Column("src_prop_id", Text), Column("dst_prop_id", Text),
    Column("type", Text), Column("span_id", Text), Column("state", Text),
)

# ---------------------------------------------------------------- L1 Run registry
runs = Table(
    "runs", metadata,
    Column("run_id", Text, primary_key=True),
    Column("arm", Text),
    Column("started_at", DateTime(timezone=True)), Column("finished_at", DateTime(timezone=True)),
    Column("git_sha", Text), Column("config_hash", Text), Column("manifest", JSONType),
    Column("seed", Integer), Column("status", Text),
)
unit_status = Table(                                           # + J0 idempotency / resume
    "unit_status", metadata,
    Column("run_id", Text), Column("stage", Text), Column("unit_id", Text),
    Column("status", Text),                                    # PENDING | DONE | FAILED | DEAD_LETTER
    Column("attempts", Integer, server_default="0"),
    Column("input_hash", Text), Column("error", Text), _ts(),
    PrimaryKeyConstraint("run_id", "stage", "unit_id"),
)

# ---------------------------------------------------------------- L2 Telemetry [A]
model_calls = Table(
    "model_calls", metadata,
    Column("call_id", Text, primary_key=True),
    Column("run_id", Text), Column("stage", Text), Column("module", Text), Column("unit_id", Text),
    Column("endpoint", Text), Column("model_id", Text), Column("prompt_sha256", Text),
    Column("prompt_name", Text),                               # + e.g. u5_micro.v1
    Column("tokens_in", Integer), Column("tokens_out", Integer),
    Column("reasoning_tokens", Integer), Column("tokens_in_counted", Integer),
    Column("tokens_counted_exact", Boolean),                   # + tokenizer vs heuristic
    Column("cap_in", Integer), Column("cap_out", Integer),     # + the caps the call ran under
    Column("latency_ms", Integer), Column("finish_reason", Text),
    Column("schema_valid", Boolean), Column("repairs", Integer), Column("retries", Integer),
    Column("cost_usd", Float), Column("gpu_mem_mb_peak", Integer), Column("error", Text),
    Column("request", JSONType), Column("response", JSONType),  # + full prompt + reply, for replay
    _ts(),
)
stage_metrics = Table(
    "stage_metrics", metadata,
    Column("run_id", Text), Column("stage", Text), Column("unit_id", Text),
    Column("source_tokens", Integer), Column("candidate_tokens", Integer),
    Column("contracted_tokens", Integer), Column("items_in", Integer),
    Column("items_out", Integer), Column("wall_ms", Integer), _ts(),
)

# ---------------------------------------------------------------- L3 Coverage [A]
coverage_events = Table(
    "coverage_events", metadata,
    Column("run_id", Text), Column("sentence_id", Text), Column("stage", Text),
    Column("outcome", Text), Column("ref_id", Text), _ts(),
)
coverage_final = Table(
    "coverage_final", metadata,
    Column("run_id", Text), Column("sentence_id", Text),
    Column("terminal", Text), Column("reason", Text),
    PrimaryKeyConstraint("run_id", "sentence_id"),
)

# ---------------------------------------------------------------- L4 Change log [A]
change_log = Table(
    "change_log", metadata,
    Column("change_id", Text, primary_key=True),
    Column("run_id", Text), Column("object_type", Text), Column("object_id", Text),
    Column("op", Text), Column("before", JSONType), Column("after", JSONType),
    Column("stage", Text), Column("module", Text), Column("call_id", Text),
    Column("rule_id", Text), _ts(),
)

# ---------------------------------------------------------------- L5 Resolution log [A]
resolutions = Table(
    "resolutions", metadata,
    Column("decision_id", Text, primary_key=True),
    Column("run_id", Text), Column("entity_a", Text), Column("entity_b", Text),
    Column("score", Float), Column("features", JSONType), Column("decision", Text),
    Column("decided_by", Text), Column("rule_id", Text),
    Column("evidence_span_ids", JSONType), Column("reverses", Text), _ts(),
)

# ---------------------------------------------------------------- L6 Flags & conflicts [A]
flags = Table(
    "flags", metadata,
    Column("flag_id", Text, primary_key=True),
    Column("run_id", Text), Column("kind", Text), Column("state", Text),
    Column("object_ids", JSONType), Column("raised_by", Text), Column("resolved_by", Text),
    Column("span_ids", JSONType), Column("note", Text), _ts(),
)

# ---------------------------------------------------------------- L7 Escalations [A]
escalations = Table(
    "escalations", metadata,
    Column("esc_id", Text, primary_key=True),
    Column("run_id", Text), Column("item_type", Text), Column("item_ids", JSONType),
    Column("reasons", JSONType), Column("question_type", Text), Column("packet", JSONType),
    Column("packet_tokens", Integer), Column("context_asks", Integer),
    Column("model_id", Text), Column("tokens_in", Integer), Column("tokens_out", Integer),
    Column("outcome", Text), Column("verdict", JSONType), _ts(),
)

# ---------------------------------------------------------------- L8 Gold & eval
gold_sets = Table(
    "gold_sets", metadata,
    Column("gold_id", Text, primary_key=True),
    Column("doc_id", Text), Column("passage_first_idx", Integer), Column("passage_last_idx", Integer),
    Column("version", Integer), Column("annotator", Text), Column("mode", Text),
    Column("body", JSONType), _ts("created_at"),
)
eval_scores = Table(
    "eval_scores", metadata,
    Column("run_id", Text), Column("arm", Text), Column("metric", Text), Column("slice", Text),
    Column("value", Float), Column("ci_low", Float), Column("ci_high", Float), Column("n", Integer),
    _ts(),
)
calibration_points = Table(
    "calibration_points", metadata,
    Column("run_id", Text), Column("boundary", Text), Column("item_id", Text),
    Column("raw_conf", Float), Column("outcome", Boolean), Column("source", Text),
)

# ---------------------------------------------------------------- indexes (spec §4)
for _table in metadata.sorted_tables:
    if "run_id" in _table.c and _table.name != "runs":
        Index(f"ix_{_table.name}_run_id", _table.c.run_id)
Index("ix_spans_doc_start", spans.c.doc_id, spans.c.char_start)
for _edge_table in (micro_edges, macro_edges):
    Index(f"ix_{_edge_table.name}_src", _edge_table.c.src_entity_id)
    Index(f"ix_{_edge_table.name}_dst", _edge_table.c.dst_entity_id)
Index("ix_coverage_events_run_sentence", coverage_events.c.run_id, coverage_events.c.sentence_id)
Index("ix_sentences_doc_idx", sentences.c.doc_id, sentences.c.idx)
Index("ix_mentions_entity", mentions.c.entity_id)
Index("ix_change_log_object", change_log.c.object_id)
Index("ix_edge_spans_edge", edge_spans.c.edge_id)
Index("ix_edge_times_edge", edge_times.c.edge_id)

#: Spec ``[A]``: DELETE (and TRUNCATE) are rejected by a trigger.
APPEND_ONLY_TABLES = (
    "entities", "entity_aliases", "entity_roles", "mentions",
    "time_refs",
    "micro_edges", "macro_edges", "edge_spans", "edge_times", "edge_lineage",
    "propositions", "prop_links",
    "model_calls", "stage_metrics",
    "coverage_events", "coverage_final",
    "change_log", "resolutions", "flags", "escalations",
)

#: Objects whose every create/update writes a change-log row: table -> (object_type, primary key).
LOGGED_OBJECTS = {
    "entities": ("entity", "entity_id"),
    "mentions": ("mention", "mention_id"),
    "time_refs": ("time_ref", "time_id"),
    "micro_edges": ("micro_edge", "edge_id"),
    "macro_edges": ("macro_edge", "edge_id"),
    "propositions": ("proposition", "prop_id"),
    "prop_links": ("prop_link", "link_id"),
    "flags": ("flag", "flag_id"),
    "documents": ("document", "doc_id"),
}

TABLES = {t.name: t for t in metadata.sorted_tables}
