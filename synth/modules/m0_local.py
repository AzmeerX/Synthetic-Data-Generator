"""Synth −0 · Local graph step: one local model draws the standard Datanaut graph,
guided by the GLiNER entity table.

Purpose: in the ``minus0`` arm (GLiNER → local model → frontier review), run the
original Datanaut extraction prompt over each T2 section with the local model
(endpoint ``t2``), and hand it the section's GLiNER entity table so it reuses those
names. Its relationships are grounded (quote → span, never model offsets) and
stored as draft (micro) edges, so U6 contracts them and U7 — pointed at the
frontier — reviews, fixes and verifies them.
Contract: :func:`run_section` ``(ctx, section_id) -> dict``. Idempotent per run
(skipped when its ``stage_metrics`` row exists, written in the same transaction).
Stores written: S2 new entities (names GLiNER missed), S4 ``micro_edges``
(``CANDIDATE``, ``source="local"``) + ``edge_spans`` + ``edge_times``, S1 spans,
L3 coverage (``MICRO_READ`` / ``FACT_SENTENCE`` / ``SKIPPED_BUDGET`` / ``FAILED`` /
``HELD_FOR_HUMAN``), L4 via the repository. Token caps: ``arm_in`` / ``arm_out``.
"""

from __future__ import annotations

import re
from typing import Any

from synth import states as S
from synth.ids import new_id
from synth.modules.arm_extract import _Out, _unit
from synth.modules.common import (
    DocView,
    StageContext,
    Stopwatch,
    emit_coverage,
    record_stage_metrics,
    write_span,
)
from synth.modules.existing_t2 import EntityResolver, fold_drafts
from synth.modules.u04_whoswho import build_index
from synth.modules.u07_verify import (
    log_dropped,
    section_chunks,
    section_sentences,
    write_new_entities,
)

MODULE = "M0_LOCAL"
_LABEL = re.compile(r"^E\d+ ")
_ROLE = re.compile(r" · .*$")


def entity_table_hint(text: str) -> str:
    """The who's-who index without its prompt-local labels, as plain guidance."""
    lines = []
    for line in text.splitlines():
        line = _ROLE.sub("", _LABEL.sub("", line.strip()))
        if line:
            lines.append(f"- {line}")
    if not lines:
        return ""
    return ("KNOWN ENTITIES (found by GLiNER in this text; reuse these exact names for the same things, "
            "and still report any entity they miss):\n" + "\n".join(lines))


def _ids(rows) -> list[str]:
    return [s["sentence_id"] for s in rows]


def run_section(ctx: StageContext, section_id: str) -> dict[str, Any]:
    sw = Stopwatch()
    with ctx.reader() as r:
        sec = r.get("sections", section_id=section_id)
        if sec is None:
            raise KeyError(f"no section {section_id}")
        if r.select("stage_metrics", run_id=ctx.run_id, stage=S.LOCAL, unit_id=section_id):
            return {"section_id": section_id, "skipped": True}
    doc: DocView = ctx.doc(sec["doc_id"])
    count = lambda text: ctx.gateway.count(text, "t2")
    with ctx.reader() as r:
        sentences = section_sentences(r, doc, sec)
        chunks = section_chunks(r, sec)
        resolver = EntityResolver(r, ctx.ontology, ctx.run_id, doc, sentences)
        index = build_index(r, doc, sentences, ctx.run_id, cap=ctx.config.cap("u4_whoswho"),
                            time_cap=0, count=count)
    hint = entity_table_hint(index.text)
    out = _Out()
    if sentences and doc.doc.get("sovereign") and not ctx.config.endpoint("t2").is_local():
        out.held = list(sentences)
    elif sentences:
        _unit(ctx, doc, section_id, sentences, "t2", resolver, out, hint=hint, stage=S.LOCAL, module=MODULE)

    by_idx = {s["sentence_id"]: s["idx"] for s in sentences}
    chunk_of: dict[str, str] = {}
    for c in chunks:
        for s in sentences:
            if c["first_sentence_idx"] <= s["idx"] <= c["last_sentence_idx"]:
                chunk_of[s["sentence_id"]] = c["chunk_id"]
    edge_ids, fact_sentences = [], set()
    with ctx.tx(S.LOCAL, MODULE) as tx:
        write_new_entities(tx, doc, resolver.new, MODULE)
        for d in fold_drafts(out.edges):
            sids = d.all_sentence_ids()
            first = min(sids, key=lambda i: by_idx.get(i, 10 ** 9)) if sids else None
            edge_id = new_id("med")
            tx.insert("micro_edges", dict(
                edge_id=edge_id, chunk_id=chunk_of.get(first) if first else None, src_entity_id=d.src,
                rel=d.rel, subclass=d.subclass, dst_entity_id=d.dst, description=d.desc, stance=d.stance,
                negated=bool(d.negated), confidence=d.conf, status=S.CANDIDATE, source="local"),
                call_id=d.call_id)
            for loc in d.located:
                span_id = write_span(tx, doc, loc, created_by=MODULE)
                tx.insert("edge_spans", dict(edge_id=edge_id, span_id=span_id))
            for role, time_id in d.times:
                tx.insert("edge_times", dict(edge_id=edge_id, time_id=time_id, role=role))
            edge_ids.append(edge_id)
            fact_sentences.update(sids)
        read_ids = _ids(out.read)
        emit_coverage(tx, read_ids, S.LOCAL, S.MICRO_READ, ref_id=section_id)
        emit_coverage(tx, [i for i in read_ids if i in fact_sentences], S.LOCAL, S.FACT_SENTENCE, ref_id=section_id)
        emit_coverage(tx, _ids(out.skipped), S.LOCAL, S.SKIPPED_BUDGET, ref_id=section_id)
        emit_coverage(tx, _ids(out.failed), S.LOCAL, S.COVERAGE_FAILED, ref_id=section_id)
        emit_coverage(tx, _ids(out.held), S.LOCAL, S.HELD_FOR_HUMAN, ref_id=section_id)
        log_dropped(tx, out.dropped)
        record_stage_metrics(tx, S.LOCAL, section_id, source_tokens=out.source_tokens,
                             items_out=len(edge_ids), wall_ms=sw.ms)
    return {"section_id": section_id, "calls": out.calls, "draft_edges": len(edge_ids),
            "entities_new": len(resolver.new), "entity_table": hint.count("\n- ") + (1 if hint else 0),
            "sentences_read": len(read_ids), "skipped_budget": len(out.skipped), "failed": len(out.failed),
            "held": len(out.held), "dropped": len(out.dropped), "errors": out.errors}
