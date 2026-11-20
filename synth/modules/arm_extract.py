"""Arm extractors — ``t2_only`` (today's Datanaut) and ``t3_only`` (frontier alone).

Purpose: the single-tier baselines of P1 §1. One section at a time, one model reads the
sentence-tagged source with no Tier-1 help and returns the whole graph; the same
deterministic code as ``full`` (locator, U6 fold rule, entity mapping, writers) turns it
into S5 so differences between arms come from the model tiers.

Contract: :func:`run_section_arm` ``(ctx, section_id, tier) -> dict`` with ``tier``:

* ``"t2"`` (arm ``t2_only``) — the **existing** prompt (``EXTRACT_SYSTEM`` + ``build_extract_prompt``)
  through :mod:`synth.modules.existing_t2`, no micro rows; endpoint ``t2``; ``tier_last="T2"``;
  coverage ``T2_READ``.
* ``"t3"`` (arm ``t3_only``) — prompt ``arm_extract.v1`` (the seed extraction rules, sentence-tagged
  input) -> :class:`synth.contracts.ArmOutput`; endpoint ``t3``; ``tier_last="T3"``; coverage ``T3_READ``.

Rule for sovereign documents: when the arm's endpoint is not local, nothing is sent; every
sentence of the section gets coverage ``HELD_FOR_HUMAN`` (the same guard as U9) and no edges
are written.

Writes (one transaction per section): entities (``source="arm"``, mapped onto existing same-document
entities by norm key / alias first) with mentions and aliases; macro edges ``VERIFIED`` with
``edge_spans`` / ``edge_times`` (no lineage); propositions + ``prop_links``; coverage; stage
metrics (stage ``ARM_EXTRACT``); dropped items in the change log.
Token caps: source ≤ ``caps.arm_in`` (2,000; the fixed prompt scaffolding — frame description,
lists — is counted on top and recorded as the call's ``cap_in``); out ≤ ``caps.arm_out`` (4,000).
Source over the cap -> ``SKIPPED_BUDGET``; ``OutputTruncated`` -> split the section in two.
Spec: P1 §1 arms, §3 U7, §10.
"""

from __future__ import annotations

from typing import Any, Sequence

from synth import states as S
from synth.contracts import ArmOutput
from synth.gateway import BudgetExceeded, GatewayError, OutputTruncated
from synth.modules.common import DocView, StageContext, Stopwatch, emit_coverage, record_stage_metrics
from synth.modules.existing_t2 import (
    Dropped, EdgeDraft, EntityResolver, LinkDraft, PropDraft, TimeIndex, as_fuzzy, build_existing_user,
    call_existing, fold_drafts, name_within, ontology_frame, parse_existing, unit_refs,
)
from synth.modules.u04_whoswho import build_index
from synth.modules.u06_contract import Labeler
from synth.modules.u07_verify import (
    _ids, edge_from_body, log_dropped, parse_logic, section_sentences, write_logic, write_macro_edge,
    write_new_entities,
)
from synth.prompts import load_prompt

PROMPT_NAME = "arm_extract.v1"


class _Out:
    def __init__(self) -> None:
        self.edges: list[EdgeDraft] = []
        self.props: list[PropDraft] = []
        self.links: list[LinkDraft] = []
        self.dropped: list[Dropped] = []
        self.read: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.held: list[dict[str, Any]] = []
        self.calls = 0
        self.source_tokens = 0
        self.fallbacks = 0
        self.errors: list[str] = []


def _t3_user(ctx: StageContext, doc: DocView, sentences: Sequence[dict[str, Any]], time_text: str) -> str:
    onto = ctx.ontology
    types = " · ".join(f"{cls} ({', '.join(subs)})" if subs else cls for cls, subs in onto.entity_types.items())
    rels = ", ".join(f"{r.name} ({r.source_class} -> {r.target_class})" for r in onto.relations.values())
    parts = [f"DOCUMENT: {doc.doc.get('filename') or doc.doc_id}", "",
             "SECTION (one sentence per line; cite sentence ids, quote verbatim)", doc.render(sentences)]
    if time_text:
        parts += ["", "TIME REFERENCES (time labels)", time_text]
    parts += ["", "ENTITY TYPES: " + types, "RELATIONS (closed list; source class -> target class): " + rels,
              "PROPOSITION STATUS: " + ", ".join(onto.proposition_status),
              "LINK TYPES: " + ", ".join(onto.link_types),
              "TIME ROLES: " + ", ".join(onto.time_roles or S.TIME_ROLES), "",
              "Extract the entities, relations, propositions and logic links this section states. "
              "Return one JSON object."]
    return "\n".join(parts)


def _parse_arm(ctx: StageContext, doc: DocView, sentences: Sequence[dict[str, Any]], reply: ArmOutput,
               resolver: EntityResolver, labeler: Labeler, out: _Out) -> tuple[list, list, list]:
    onto = ctx.ontology
    refs = unit_refs(doc, sentences)
    local: dict[str, str] = {}
    for ent in reply.entities:
        mentions = []
        for m in ent.mentions:
            loc = resolver.locator.locate((m.quote or "").strip() or ent.name, m.sentence_ids, unit=refs)
            if loc.grounded:
                mentions.append(name_within(doc, ent.name, loc) or loc)
        if not mentions:
            loc = resolver.locate_name(ent.name)
            if loc is not None:
                mentions.append(as_fuzzy(loc))
                out.fallbacks += 1
        if not mentions:
            out.dropped.append(Dropped("entity", ent.model_dump(), "ungrounded"))
            continue
        eid = resolver.resolve(ent.name, ent.type, mentions=mentions, distinct=ent.id)
        if eid:
            local[ent.id] = eid

    def entity(label: str | None) -> str | None:
        if not label:
            return None
        return local.get(label.strip()) or resolver.resolve(label, create=False)

    edges: list[EdgeDraft] = []
    for e in reply.edges:
        draft, why = edge_from_body(onto, labeler, resolver.entity_class, e, resolve_entity=entity)
        quote = (e.evidence.quote or "").strip()
        loc = doc.locator.locate(quote, e.evidence.sentence_ids, unit=refs) if quote else None
        if draft is None or loc is None or not loc.grounded:
            out.dropped.append(Dropped("macro_edge", e.model_dump(), why or "ungrounded"))
            continue
        draft.conf, draft.located = e.conf, [loc]
        edges.append(draft)
    props, links = parse_logic(doc, refs, onto, reply.propositions, reply.links, entity, out.dropped)
    return fold_drafts(edges), props, links


def _unit(ctx: StageContext, doc: DocView, section_id: str, sentences: list[dict[str, Any]], tier: str,
          resolver: EntityResolver, out: _Out, hint: str = "", stage: str = S.ARM_EXTRACT,
          module: str | None = None) -> None:
    endpoint, module = tier, module or f"ARM_{tier.upper()}"
    count = lambda text: ctx.gateway.count(text, endpoint)  # noqa: E731
    source_tokens = count(doc.render(sentences))
    if source_tokens > ctx.config.cap("arm_in"):
        out.skipped += sentences
        return
    labeler = None
    try:
        if tier == "t2":
            user = build_existing_user(doc, sentences, ontology_frame(ctx.ontology), hint=hint)
            cap_in = ctx.config.cap("arm_in") + max(0, count(user) - source_tokens)
            res = call_existing(ctx, doc, user, stage=stage, module=module, unit_id=section_id,
                                cap_in=cap_in, cap_out=ctx.config.cap("arm_out"))
        else:
            with ctx.reader() as r:
                ww = build_index(r, doc, sentences, ctx.run_id, cap=0,
                                 time_cap=ctx.config.cap("u4_time_section"), count=count)
            labeler = Labeler.from_whoswho(ww, doc)
            user = _t3_user(ctx, doc, sentences, ww.time_text)
            cap_in = ctx.config.cap("arm_in") + max(0, count(user) - source_tokens)
            res = ctx.gateway.call(endpoint="t3", prompt=load_prompt(PROMPT_NAME), user=user, cap_in=cap_in,
                                   cap_out=ctx.config.cap("arm_out"), stage=S.ARM_EXTRACT, module=module,
                                   unit_id=section_id, run_id=ctx.run_id, output_model=ArmOutput,
                                   sovereign=bool(doc.doc.get("sovereign")))
    except OutputTruncated:
        if len(sentences) <= 1:
            out.failed += sentences
            out.errors.append("output truncated on one sentence")
            return
        half = len(sentences) // 2
        _unit(ctx, doc, section_id, sentences[:half], tier, resolver, out, hint, stage, module)
        _unit(ctx, doc, section_id, sentences[half:], tier, resolver, out, hint, stage, module)
        return
    except BudgetExceeded:
        out.skipped += sentences
        return
    except GatewayError as exc:
        out.failed += sentences
        out.errors.append(f"{type(exc).__name__}: {exc}")
        return
    out.calls += 1
    out.source_tokens += source_tokens
    call_id = res.call_ids[-1] if res.call_ids else None
    if tier == "t2":
        with ctx.reader() as r:
            times = TimeIndex(r, ctx.run_id, doc, sentences)
        graph = parse_existing(res.data or {}, doc, sentences, resolver, ctx.ontology, times)
        edges, props, links = graph.edges, graph.props, []
        for d in graph.dropped:
            d.call_id = call_id
        out.dropped += graph.dropped
        out.fallbacks += graph.fallbacks
    else:
        edges, props, links = _parse_arm(ctx, doc, sentences, res.model, resolver, labeler, out)
    for e in edges:
        e.call_id = call_id
    out.edges += edges
    out.props += props
    out.links += links
    out.read += sentences


def run_section_arm(ctx: StageContext, section_id: str, tier: str) -> dict[str, Any]:
    """Extract one section with a single tier (``"t2"`` or ``"t3"``)."""
    if tier not in ("t2", "t3"):
        raise ValueError("tier must be 't2' or 't3'")
    sw = Stopwatch()
    module, tier_last = f"ARM_{tier.upper()}", tier.upper()
    read_outcome = S.T2_READ if tier == "t2" else S.T3_READ
    with ctx.reader() as r:
        sec = r.get("sections", section_id=section_id)
        if sec is None:
            raise KeyError(f"no section {section_id}")
        if r.select("stage_metrics", run_id=ctx.run_id, stage=S.ARM_EXTRACT, unit_id=section_id):
            return {"section_id": section_id, "tier": tier, "skipped": True}
    doc = ctx.doc(sec["doc_id"])
    with ctx.reader() as r:
        sentences = section_sentences(r, doc, sec)
        resolver = EntityResolver(r, ctx.ontology, ctx.run_id, doc, sentences)
    out = _Out()
    if sentences and doc.doc.get("sovereign") and not ctx.config.endpoint(tier).is_local():
        out.held = list(sentences)
    elif sentences:
        _unit(ctx, doc, section_id, sentences, tier, resolver, out)

    macro_ids = []
    with ctx.tx(S.ARM_EXTRACT, module) as tx:
        write_new_entities(tx, doc, resolver.new, module)
        for e in fold_drafts(out.edges):
            macro_ids.append(write_macro_edge(tx, doc, e, section_id, tier_last, module))
        write_logic(tx, doc, section_id, out.props, out.links, module)
        read_ids = _ids(out.read)
        emit_coverage(tx, read_ids, S.ARM_EXTRACT, read_outcome, ref_id=section_id)
        emit_coverage(tx, _ids(out.skipped), S.ARM_EXTRACT, S.SKIPPED_BUDGET, ref_id=section_id)
        emit_coverage(tx, _ids(out.failed), S.ARM_EXTRACT, S.COVERAGE_FAILED, ref_id=section_id)
        emit_coverage(tx, _ids(out.held), S.ARM_EXTRACT, S.HELD_FOR_HUMAN, ref_id=section_id)
        log_dropped(tx, out.dropped)
        record_stage_metrics(tx, S.ARM_EXTRACT, section_id, source_tokens=out.source_tokens,
                             items_out=len(macro_ids), wall_ms=sw.ms)
    return {"section_id": section_id, "tier": tier, "calls": out.calls, "macro_edges": len(macro_ids),
            "entities_new": len(resolver.new), "propositions": len(out.props), "links": len(out.links),
            "sentences_read": len(read_ids), "skipped_budget": len(out.skipped), "failed": len(out.failed),
            "held": len(out.held), "dropped": len(out.dropped), "fallbacks": out.fallbacks,
            "errors": out.errors}
