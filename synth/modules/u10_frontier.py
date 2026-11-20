"""U10 — Frontier Endpoint (T3): send each pending packet to the frontier and record the verdict.

Purpose: the only module that talks to ``t3``. For every ``PENDING`` escalation
of the run: render the packet (U9 ``compose``), call the gateway, and when the
frontier answers ``NEED_CONTEXT`` serve its typed ``context_request`` from the
stores through J1 (``sentences`` -> ``get_sentences``, ``entity`` ->
``get_entity``, ``edges`` -> ``get_edges``, ``timeline`` -> ``get_timeline``),
append the answer to the packet and re-send.

Contract: :func:`run` ``(ctx) -> dict`` (counts). Per escalation the outcome is
``ANSWERED`` (a final verdict), ``IRREDUCIBLE`` (a conflict still asking after the
limits), ``HELD_FOR_HUMAN`` (edge / pair / logic still asking after the limits, or a
sovereign packet with no local T3), or ``FAILED`` (gateway error). The verdict JSON
is the ``VerdictOut`` reply plus ``call_id`` / ``call_ids`` (and ``limit`` / ``error``).

Limits: ≤ ``caps.u10_asks`` (3) asks and ≤ ``caps.u10_added`` (6,000) added tokens; the
whole message stays ≤ ``caps.u10_packet`` (8,000) — to make room for an answer the
packet shrinks in U9's order (subgraph -> ± 1), then the answer is cut by whole lines
(recorded as ``trimmed``); an answer that cannot fit at all ends the asking.
Output ≤ ``caps.u10_out``. Answers never include sovereign documents unless ``t3`` is local.

Stores written: L7 ``escalations`` (``tx.update``: verdict, context_asks, tokens,
model_id, outcome, packet with its context), L2 via the gateway, L3 coverage
(``T3_READ`` for every sentence the frontier was shown; ``FAILED`` when no call
succeeded). Spec: P1 §3 U10, §7, §10; CLAUDE.md invariants 5, 6, 10.
"""

from __future__ import annotations

from typing import Any, Sequence

from synth.api import query as j1
from synth.contracts import VerdictOut
from synth.gateway import GatewayError, SovereignViolation
from synth.modules.common import StageContext, Stopwatch, emit_coverage, record_stage_metrics
from synth.modules.u09_gate import (
    SHRINK_STEPS, Labeler, compose, definition_lines, edge_row, shown_sentence_ids, sovereign_docs,
    span_cites,
)
from synth.prompts import load_prompt
from synth.provenance.render import parse_label, render_sentences
from synth.states import COVERAGE_FAILED, FRONTIER, HELD_FOR_HUMAN, IRREDUCIBLE, REJECTED, T3_READ

PROMPT = "u10_frontier.v1"
ANSWERED, FAILED_OUTCOME = "ANSWERED", "FAILED"
MAX_CONTEXT_MENTIONS = 10
MAX_CONTEXT_RESOLUTIONS = 5


# ------------------------------------------------------------------ context answers (J1)
def answer_context(r, run_id: str, packet: dict[str, Any], ask: dict[str, Any] | None, ask_no: int,
                   withheld: set[str]) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Serve one ``context_request`` from J1. Returns ``(header, lines, [(sentence id, label)])``;
    new labels are written back into ``packet``."""
    lab = Labeler.from_packet(packet)
    before = (set(lab.entities), set(lab.times), set(lab.documents))
    kind = (ask or {}).get("kind") or "?"
    ids = list((ask or {}).get("ids") or [])
    header = f"CONTEXT {ask_no} ({kind}: {', '.join(ids) or 'no ids'})"
    lines: list[str] = []
    shown: list[tuple[str, str]] = []
    if not ids:
        lines.append("(the request named no ids; nothing added)")
    elif kind == "sentences":
        by_doc: dict[str, list[int]] = {}
        for x in ids:
            parsed = parse_label(str(x))
            doc_id = None
            if parsed is not None:
                doc_id = lab.documents.get(f"d{parsed[0]}" if parsed[0] is not None else "d1")
            if parsed is None or doc_id is None or doc_id in withheld:
                lines.append(f"? {x}: unknown sentence label")
                continue
            by_doc.setdefault(doc_id, []).append(parsed[1])
        for doc_id, idxs in by_doc.items():
            answer = j1.get_sentences(r, doc_id, idxs, n=0, run_id=run_id)
            lines += render_sentences([(s["idx"], s["text"]) for s in answer["sentences"]],
                                      lab.doc_no(doc_id)).splitlines()
            shown += [(s["sentence_id"], lab.sentence(doc_id, s["label"])) for s in answer["sentences"]]
            lines += [f"? {lab.doc(doc_id)}:s{m}: no such sentence" for m in answer["missing"]]
    else:
        for x in ids:
            eid = lab.entities.get(str(x))
            if eid is None:
                lines.append(f"? {x}: unknown entity label")
                continue
            if kind == "entity":
                lines += _entity_lines(r, run_id, eid, lab, withheld)
            else:
                if kind == "edges":
                    edges = j1.get_edges(r, run_id, eid, level="macro")["edges"]
                else:
                    edges = [ev["edge"] for ev in j1.get_timeline(r, run_id, eid)["events"]]
                edges = [e for e in edges if e["status"] != REJECTED and e["spans"]
                         and not ({s["doc_id"] for s in e["spans"]} & withheld)]
                if not edges:
                    lines.append(f"{x}: no edges")
                for i, e in enumerate(edges, 1):
                    lines.append(edge_row(f"C{ask_no}.{i}", e, lab, with_status=True))
    defs = definition_lines(r, lab, docs=[d for d in lab.documents if d not in before[2]],
                            entities=[e for e in lab.entities if e not in before[0]],
                            times=[t for t in lab.times if t not in before[1]], withheld=withheld)
    packet.update(lab.dump())
    return header, defs + lines, shown


def _entity_lines(r, run_id: str, eid: str, lab: Labeler, withheld: set[str]) -> list[str]:
    card = j1.get_entity(r, run_id, eid)
    label = lab.entity(eid)
    if card is None:
        return [f"{label}: not found"]
    if card["survivor_id"] != eid:
        label = f"{label} (merged into {lab.entity(card['survivor_id'])})"
    lines = []
    mentions = [m for m in card["mentions"] if m.get("span") and m["span"]["doc_id"] not in withheld]
    for m in mentions[:MAX_CONTEXT_MENTIONS]:
        cites = ", ".join(span_cites([m["span"]], lab))
        lines.append(f'{label} mention {cites}: "{" ".join((m["span"]["quote"] or "").split())[:160]}"')
    for role in card["roles"]:
        if role.get("span") and role["span"]["doc_id"] in withheld:
            continue
        org = f"@{lab.entity(role['org_entity_id'])}" if role.get("org_entity_id") else ""
        cites = ", ".join(span_cites([role["span"]], lab)) if role.get("span") else "—"
        lines.append(f"{label} role {role['role']}{org} | {cites}")
    for res in card["resolution_history"][-MAX_CONTEXT_RESOLUTIONS:]:
        other = res["entity_b"] if res["entity_a"] in card["members"] else res["entity_a"]
        score = f"{res['score']:.2f}" if res.get("score") is not None else "—"
        lines.append(f"{label} vs {lab.entity(other)}: {res['decision']} by {res.get('decided_by') or '?'} "
                     f"(score {score})")
    return lines or [f"{label}: nothing more on record"]


# ------------------------------------------------------------------ fitting
def _fit(ctx: StageContext, packet: dict[str, Any], header: str, lines: Sequence[str],
         added_left: int, cap_in: int) -> tuple[dict[str, Any] | None, tuple[int, bool, list[str]]]:
    """Fit one answer: shrink the packet (U9 order) to make room, then cut whole lines.
    Returns the context block (or ``None``) and the packet shape to use."""
    count = lambda text: ctx.gateway.count(text, "t3")   # noqa: E731
    current = next((i for i, (w, s, _) in enumerate(SHRINK_STEPS)
                    if w == packet["window"] and s == packet["subgraph"]), len(SHRINK_STEPS) - 1)
    for n_lines in range(len(lines), 0, -1):
        text = "\n".join([header, *lines[:n_lines]])
        tokens = count(text)
        if tokens > added_left:
            continue
        block = dict(text=text, tokens=tokens, trimmed=len(lines) - n_lines)
        for window, subgraph, steps in SHRINK_STEPS[current:]:
            total = count(compose(packet, window, subgraph, [*packet["context"], block]))
            if total <= cap_in:
                return block, (window, subgraph and bool(packet["parts"].get("subgraph")), list(steps))
    return None, (packet["window"], packet["subgraph"], list(packet.get("shrink") or []))


# ------------------------------------------------------------------ the stage
def run(ctx: StageContext) -> dict[str, Any]:
    """Answer every PENDING escalation of the run."""
    sw = Stopwatch()
    prompt = load_prompt(PROMPT)
    cfg = ctx.config
    cap_in, cap_out = cfg.cap("u10_packet"), cfg.cap("u10_out")
    max_asks, max_added = cfg.cap("u10_asks"), cfg.cap("u10_added")
    t3_local = cfg.endpoint("t3").is_local()
    summary = dict(escalations=0, answered=0, held=0, irreducible=0, failed=0, asks=0,
                   tokens_in=0, tokens_out=0)
    with ctx.reader() as r:
        pending = r.select("escalations", order_by="esc_id", run_id=ctx.run_id, outcome="PENDING")
        withheld = set() if t3_local else sovereign_docs(r)
    for esc in pending:
        summary["escalations"] += 1
        packet = dict(esc["packet"] or {})
        packet["context"] = list(packet.get("context") or [])
        limit_outcome = IRREDUCIBLE if esc["item_type"] == "conflict" else HELD_FOR_HUMAN
        asks, added, tokens_in, tokens_out = int(esc.get("context_asks") or 0), 0, 0, 0
        model_id, call_ids = "", []
        verdict: dict[str, Any] | None = None
        outcome, read = None, False
        while outcome is None:
            try:
                result = ctx.gateway.call(
                    endpoint="t3", prompt=prompt, user=compose(packet), output_model=VerdictOut,
                    cap_in=cap_in, cap_out=cap_out, stage=FRONTIER, module="U10",
                    unit_id=esc["esc_id"], run_id=ctx.run_id, sovereign=bool(packet.get("sovereign")))
            except SovereignViolation as exc:
                outcome, verdict = HELD_FOR_HUMAN, {"verdict": None, "error": str(exc), "limit": "sovereign"}
                break
            except GatewayError as exc:
                outcome = FAILED_OUTCOME
                verdict = {"verdict": None, "error": f"{type(exc).__name__}: {exc}"[:500]}
                break
            read = True
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            model_id = result.model_id or model_id
            call_ids += result.call_ids
            reply = result.model.model_dump()
            verdict = {**reply, "call_id": result.call_ids[-1] if result.call_ids else None,
                       "call_ids": list(call_ids)}
            if reply["verdict"] != "NEED_CONTEXT":
                outcome = ANSWERED
                break
            if asks >= max_asks:
                outcome, verdict["limit"] = limit_outcome, "asks"
                break
            with ctx.reader() as rr:
                header, lines, sids = answer_context(rr, ctx.run_id, packet, reply.get("context_request"),
                                                     asks + 1, withheld)
            block, (window, subgraph, steps) = _fit(ctx, packet, header, lines, max_added - added, cap_in)
            if block is None:
                outcome, verdict["limit"] = limit_outcome, "added_tokens"
                break
            asks += 1
            added += block["tokens"]
            request = reply.get("context_request") or {}
            block.update(ask=asks, kind=request.get("kind"), ids=request.get("ids") or [],
                         sentence_ids=[sid for sid, label in sids if f"[{label}]" in block["text"]])
            packet["context"].append(block)
            packet.update(window=window, subgraph=subgraph, shrink=steps)
        packet["text"] = compose(packet)
        packet["tokens"] = ctx.gateway.count(packet["text"], "t3")
        summary["asks"] += asks
        summary["tokens_in"] += tokens_in
        summary["tokens_out"] += tokens_out
        summary[{ANSWERED: "answered", HELD_FOR_HUMAN: "held", IRREDUCIBLE: "irreducible",
                 FAILED_OUTCOME: "failed"}[outcome]] += 1
        with ctx.tx(FRONTIER, "U10") as tx:
            tx.update("escalations", {"esc_id": esc["esc_id"]}, dict(
                packet=packet, verdict=verdict, context_asks=asks, tokens_in=tokens_in,
                tokens_out=tokens_out, model_id=model_id or None, outcome=outcome))
            shown = shown_sentence_ids(packet)
            if read:
                emit_coverage(tx, shown, FRONTIER, T3_READ, esc["esc_id"])
            elif outcome == FAILED_OUTCOME:
                emit_coverage(tx, shown, FRONTIER, COVERAGE_FAILED, esc["esc_id"])
    if pending:
        with ctx.tx(FRONTIER, "U10") as tx:
            record_stage_metrics(tx, FRONTIER, ctx.run_id, items_in=summary["escalations"],
                                 items_out=summary["answered"], wall_ms=sw.ms)
    return summary
