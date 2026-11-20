"""U11 — Commit: apply the frontier's verdicts, then commit everything that passed the gate.

Purpose: turn L7 outcomes into store states, so every gate-reaching item ends in
a terminal state with its history in L4.

Contract: :func:`run` ``(ctx) -> dict`` (counts). Order: entity pairs (merges
re-point edges first), conflicts, edges, logic, then the PASS sweep.

* ``edge`` (CONFIRM_EDGE): CONFIRM -> ``COMMITTED``; REJECT -> ``REJECTED``; CORRECT -> a new
  macro edge (``version`` + 1, ``supersedes`` = old id, evidence re-grounded from the
  verdict by the locator) ``COMMITTED`` and the old one ``REJECTED``. An ungrounded or
  unmappable correction -> ``HELD_FOR_HUMAN`` (raw correction logged as ``DROPPED``).
* ``conflict`` (CHOOSE_BETWEEN): CONFIRM + ``corrected_edge`` matching a row -> that row
  ``COMMITTED``, the others ``REJECTED``; CONFIRM without one -> all ``COMMITTED``; REJECT ->
  all ``REJECTED``; CORRECT -> as a choice if it matches a row, else a new version of the
  first row and all old rows ``REJECTED``; resolved -> flag ``RESOLVED``. IRREDUCIBLE (verdict
  or U10 limit) -> flag ``IRREDUCIBLE``, all rows ``COMMITTED`` with the flag kept.
* ``entity_pair`` (SAME_ENTITY): MERGE (needs grounded evidence) / KEEP_APART go to U8's
  ``apply_decision(decided_by="T3")``: the L5 row, entity ``MERGED`` + ``merged_into`` and the
  macro-edge re-pointing; a merge a cannot-link rule forbids is recorded ``APART`` with a
  ``merge_blocked`` flag. Pairs run first, outside U11's own transaction.
* ``logic`` (CHECK_LOGIC): CONFIRM -> flag ``RESOLVED``; REJECT -> the flagged propositions and
  links ``REJECTED``, flag ``RESOLVED``; IRREDUCIBLE -> flag ``IRREDUCIBLE``.
* Any other outcome (HELD_FOR_HUMAN, FAILED) or a verdict that does not fit the question ->
  ``HELD_FOR_HUMAN`` (edges, flagged propositions/links); pairs stay ESCALATED in L5.
* PASS sweep: macro edges still ``VERIFIED`` -> ``COMMITTED`` (ungrounded -> ``REJECTED``);
  propositions and links in state ``VERIFIED`` (not under a pending logic escalation) -> ``COMMITTED``.

Verdict-applied edges get ``tier_last = "T3"`` and their L4 rows carry the T3 ``call_id``.
Every T3 verdict's evidence is grounded and its span ids stored on the verdict; one
calibration point per gate reason ``(boundary = reason, item_id, raw_conf, outcome =
"the T2/U8 output was upheld", source = "T3")`` is written for P2.
Stores written: S5 (status, new versions, edge_spans/times/lineage), S2 (merges), L4,
L5, L6 (flag states), L7 (verdict ``applied`` marker + span ids), L8 calibration
points, S1 spans, L3 (``HELD_FOR_HUMAN`` for items U11 holds). Token caps: none (no model).
Spec: P1 §3 U11, §5, §11; CLAUDE.md invariants 1, 4, 7.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from synth.api.query import select_in, survivor_of
from synth.ids import new_id
from synth.modules.common import DocView, StageContext, emit_coverage, write_span
from synth.modules.u09_gate import FINAL_DECIDERS, OPEN_FLAG_STATES, shown_sentence_ids
from synth.provenance.render import parse_label
from synth.states import (
    APART, COMMIT, COMMITTED, ESCALATED, FINAL, HELD_FOR_HUMAN, IRREDUCIBLE, MERGED, REJECTED,
    RESOLVED, TIME_ROLES, VERIFIED,
)
from synth.store.repo import Tx

ANSWERED = "ANSWERED"
VALID = {"CONFIRM_EDGE": {"CONFIRM", "REJECT", "CORRECT"},
         "CHOOSE_BETWEEN": {"CONFIRM", "REJECT", "CORRECT", "IRREDUCIBLE"},
         "SAME_ENTITY": {"MERGE", "KEEP_APART"},
         "CHECK_LOGIC": {"CONFIRM", "REJECT", "IRREDUCIBLE"}}
TYPE_ORDER = {"entity_pair": 0, "conflict": 1, "edge": 2, "logic": 3}


class _Apply:
    """Applies one escalation's outcome inside the U11 transaction."""

    def __init__(self, ctx: StageContext, tx: Tx, esc: dict[str, Any], counts: Counter) -> None:
        self.ctx, self.tx, self.esc, self.counts = ctx, tx, esc, counts
        self.packet = esc.get("packet") or {}
        self.verdict = dict(esc.get("verdict") or {})
        self.v = self.verdict.get("verdict")
        self.call_id = self.verdict.get("call_id")
        self.answered = esc["outcome"] == ANSWERED
        self.span_ids: list[str] = []
        self._docs: dict[str, DocView] = {}
        self.tag = esc["esc_id"]

    # -- helpers ---------------------------------------------------------------
    def doc(self, doc_id: str) -> DocView:
        if doc_id not in self._docs:
            self._docs[doc_id] = DocView.load(self.tx, doc_id, self.ctx.config)
        return self._docs[doc_id]

    def entity_of(self, label: str | None) -> str | None:
        if not label:
            return None
        eid = (self.packet.get("entities") or {}).get(label.strip())
        return survivor_of(self.tx, eid) if eid else None

    def ground_evidence(self) -> list[str]:
        """Locate every ``{sentence_ids, quote}`` of the verdict; write spans; return their ids."""
        documents = self.packet.get("documents") or {}
        shown = set(shown_sentence_ids(self.packet)) if self.packet else set()
        span_ids = []
        for raw in self.verdict.get("evidence") or []:
            labels = [parse_label(str(x)) for x in raw.get("sentence_ids") or []]
            labels = [x for x in labels if x is not None]
            doc_no = next((d for d, _ in labels if d is not None), 1)
            doc_id = documents.get(f"d{doc_no}")
            located = None
            if doc_id:
                doc = self.doc(doc_id)
                cited = [f"s{idx}" for d, idx in labels if d in (None, doc_no)]
                unit = [s for s in doc.by_id if s in shown] or None
                located = doc.locator.locate(raw.get("quote") or "", cited, unit=unit)
            if located is None or not located.grounded:
                self.tx.log_change("verdict_evidence", "-", "DROPPED", None,
                                   {"esc_id": self.tag, **raw}, call_id=self.call_id, rule_id="ungrounded")
                continue
            span_ids.append(write_span(self.tx, doc, located, created_by="U11"))
        return span_ids

    def set_edge(self, edge_id: str, status: str, rule: str, t3: bool = True) -> bool:
        edge = self.tx.get("macro_edges", edge_id=edge_id)
        if edge is None or edge["status"] == status:
            return False
        changes: dict[str, Any] = {"status": status}
        if t3:
            changes["tier_last"] = "T3"
        self.tx.update("macro_edges", {"edge_id": edge_id}, changes, call_id=self.call_id,
                       rule_id=f"{rule}:{self.tag}")
        self.counts[{COMMITTED: "committed", REJECTED: "rejected", HELD_FOR_HUMAN: "held"}[status]] += 1
        return True

    def set_flag(self, flag_id: str, state: str, by: str = "T3") -> None:
        self.tx.update("flags", {"flag_id": flag_id}, {"state": state, "resolved_by": by},
                       call_id=self.call_id, rule_id=f"t3_{state.lower()}:{self.tag}")
        self.counts["flags_" + state.lower()] += 1

    def hold(self, edge_ids: list[str], rule: str) -> str:
        for eid in edge_ids:
            self.set_edge(eid, HELD_FOR_HUMAN, rule, t3=self.answered)
        if not self.packet.get("held_reason"):
            emit_coverage(self.tx, self.packet.get("cited_sentence_ids") or [], COMMIT, HELD_FOR_HUMAN,
                          self.tag)
        self.counts["items_held"] += 1
        return "held:" + rule

    def calibrate(self, raw_conf: float | None, upheld: bool, item_id: str) -> None:
        for reason in self.esc.get("reasons") or []:
            self.tx.insert("calibration_points", dict(boundary=reason, item_id=item_id, raw_conf=raw_conf,
                                                      outcome=bool(upheld), source="T3"))
            self.counts["calibration_points"] += 1

    def fits(self) -> bool:
        return self.answered and self.v in VALID.get(self.esc["question_type"], set())

    def match_row(self, live: list[str]) -> str | None:
        ce = self.verdict.get("corrected_edge")
        if not ce:
            return None
        src, dst = self.entity_of(ce.get("src")), self.entity_of(ce.get("dst"))
        rel = self.ctx.ontology.canonical_relation(ce.get("rel"))
        for eid in live:
            e = self.tx.get("macro_edges", edge_id=eid)
            if (survivor_of(self.tx, e["src_entity_id"]), e["rel"], survivor_of(self.tx, e["dst_entity_id"])) \
                    == (src, rel, dst):
                return eid
        return None

    def correct(self, old: dict[str, Any]) -> str | None:
        """A new, grounded version of ``old`` from the verdict's ``corrected_edge``."""
        ce = self.verdict.get("corrected_edge")
        src, dst = self.entity_of((ce or {}).get("src")), self.entity_of((ce or {}).get("dst"))
        rel = self.ctx.ontology.canonical_relation((ce or {}).get("rel"))
        problem = None
        if not ce or not (src and dst and rel):
            problem = "unmappable"
        else:
            ends = {e["entity_id"]: e for e in select_in(self.tx, "entities", "entity_id", [src, dst])}
            onto = self.ctx.ontology
            if not onto.relations[rel].allows(onto.class_of(ends[src]["type"]), onto.class_of(ends[dst]["type"])):
                problem = "type_constraint"
            elif src == dst:
                problem = "self_loop"
            elif not self.span_ids:
                problem = "ungrounded"
        if problem:
            self.tx.log_change("macro_edge", "-", "DROPPED", None, {"esc_id": self.tag, "corrected_edge": ce},
                               call_id=self.call_id, rule_id=problem)
            return None
        edge_id = new_id("mad")
        self.tx.insert("macro_edges", dict(
            edge_id=edge_id, section_id=old["section_id"], src_entity_id=src, rel=rel,
            subclass=ce.get("subclass"), dst_entity_id=dst,
            description=ce.get("desc") or old.get("description"), stance=ce.get("stance") or "stated",
            negated=bool(ce.get("negated")), confidence=self.verdict.get("confidence"),
            status=COMMITTED, tier_last="T3", version=(old.get("version") or 1) + 1,
            supersedes=old["edge_id"]), call_id=self.call_id, rule_id=f"t3_correct:{self.tag}")
        for span_id in self.span_ids:
            self.tx.insert("edge_spans", dict(edge_id=edge_id, span_id=span_id))
        times = self.packet.get("times") or {}
        for t in ce.get("time") or []:
            if t.get("role") in TIME_ROLES and t.get("ref") in times:
                self.tx.insert("edge_times", dict(edge_id=edge_id, time_id=times[t["ref"]], role=t["role"]))
            else:
                self.tx.log_change("edge_time", "-", "DROPPED", None, {"edge_id": edge_id, **t},
                                   call_id=self.call_id, rule_id="unmappable")
        for row in self.tx.select("edge_lineage", macro_edge_id=old["edge_id"]):
            self.tx.insert("edge_lineage", dict(macro_edge_id=edge_id, micro_edge_id=row["micro_edge_id"],
                                                op=row["op"]))
        self.counts["committed"] += 1
        self.counts["corrected"] += 1
        return edge_id

    # -- per item type -----------------------------------------------------------
    def edge(self) -> str:
        edge_id = self.esc["item_ids"][0]
        edge = self.tx.get("macro_edges", edge_id=edge_id)
        if edge is None or edge["status"] != ESCALATED:
            return "skipped"
        if not self.fits():
            return self.hold([edge_id], "verdict_mismatch" if self.answered else self.esc["outcome"].lower())
        self.calibrate(edge.get("confidence"), self.v == "CONFIRM", edge_id)
        if self.v == "CONFIRM":
            self.set_edge(edge_id, COMMITTED, "t3_confirm")
            return "committed"
        if self.v == "REJECT":
            self.set_edge(edge_id, REJECTED, "t3_reject")
            return "rejected"
        new = self.correct(edge)
        if new is None:
            return self.hold([edge_id], "correction_ungrounded")
        self.set_edge(edge_id, REJECTED, "t3_corrected")
        return f"corrected:{new}"

    def conflict(self) -> str:
        flag_id, *edge_ids = self.esc["item_ids"]
        flag = self.tx.get("flags", flag_id=flag_id)
        if flag is None or flag["state"] != FINAL:
            return "skipped"
        live = [e for e in edge_ids if (self.tx.get("macro_edges", edge_id=e) or {}).get("status") == ESCALATED]
        if self.esc["outcome"] == IRREDUCIBLE:            # U10 ask limit on a conflict
            self.set_flag(flag_id, IRREDUCIBLE, by="U10")
            for e in live:
                self.set_edge(e, COMMITTED, "irreducible", t3=False)
            return "irreducible"
        if not self.fits():
            return self.hold(live, "verdict_mismatch" if self.answered else self.esc["outcome"].lower())
        confs = [c for c in (self.tx.get("macro_edges", edge_id=e)["confidence"] for e in edge_ids) if c is not None]
        raw_conf = min(confs) if confs else None
        chosen = self.match_row(live) if self.v in ("CONFIRM", "CORRECT") else None
        if self.v == "IRREDUCIBLE":
            self.calibrate(raw_conf, True, flag_id)
            self.set_flag(flag_id, IRREDUCIBLE)
            for e in live:
                self.set_edge(e, COMMITTED, "t3_irreducible")
            return "irreducible"
        if self.v == "CONFIRM" and not self.verdict.get("corrected_edge"):
            self.calibrate(raw_conf, True, flag_id)
            for e in live:
                self.set_edge(e, COMMITTED, "t3_confirm")
        elif chosen:
            self.calibrate(raw_conf, False, flag_id)
            for e in live:
                self.set_edge(e, COMMITTED if e == chosen else REJECTED, "t3_choose")
        elif self.v == "REJECT":
            self.calibrate(raw_conf, False, flag_id)
            for e in live:
                self.set_edge(e, REJECTED, "t3_reject")
        elif self.v == "CORRECT" and live:
            self.calibrate(raw_conf, False, flag_id)
            new = self.correct(self.tx.get("macro_edges", edge_id=live[0]))
            if new is None:
                return self.hold(live, "correction_ungrounded")
            for e in live:
                self.set_edge(e, REJECTED, "t3_corrected")
        else:
            return self.hold(live, "ambiguous_choice")
        self.set_flag(flag_id, RESOLVED)
        return "resolved"

    def pair(self) -> str | dict[str, Any]:
        """Final string (skipped / held) or a plan for U8's ``apply_decision`` (run outside this tx)."""
        a, b = self.esc["item_ids"][:2]
        sa, sb = survivor_of(self.tx, a), survivor_of(self.tx, b)
        if sa == sb:
            return "skipped"
        t = self.tx.table("resolutions")
        rows = self.tx.select("resolutions", run_id=self.ctx.run_id,
                              where=t.c.entity_a.in_([a, b, sa, sb]) & t.c.entity_b.in_([a, b, sa, sb]))
        if any(r.get("decided_by") in FINAL_DECIDERS for r in rows):
            return "skipped"
        if not self.fits():
            return self.hold([], "verdict_mismatch" if self.answered else self.esc["outcome"].lower())
        if self.v == "MERGE" and not self.span_ids:
            self.calibrate(self.packet.get("conf"), True, self.pair_item_id())
            self.tx.log_change("resolution", "-", "DROPPED", None, {"esc_id": self.tag, "pair": [sa, sb]},
                               call_id=self.call_id, rule_id="ungrounded_merge")
            return self.hold([], "ungrounded_merge")
        return dict(entity_a=sa, entity_b=sb, decision=MERGED if self.v == "MERGE" else APART,
                    decided_by="T3", rule_id=f"t3_{self.v.lower()}:{self.tag}",
                    score=self.verdict.get("confidence"),
                    features={"escalation": self.tag, "from_decision": self.packet.get("decision_id"),
                              "call_id": self.call_id,
                              "rationale": (self.verdict.get("rationale") or "")[:800]},
                    evidence_span_ids=self.span_ids or list(self.packet.get("span_ids") or []))

    def pair_item_id(self) -> str:
        return self.packet.get("decision_id") or "|".join(self.esc["item_ids"][:2])

    def logic(self) -> str:
        flag_id, *objects = self.esc["item_ids"]
        flag = self.tx.get("flags", flag_id=flag_id)
        if flag is None or flag["state"] not in OPEN_FLAG_STATES:
            return "skipped"
        if self.esc["outcome"] == IRREDUCIBLE:
            self.set_flag(flag_id, IRREDUCIBLE, by="U10")
            return "irreducible"
        if not self.fits():
            self._set_logic(objects, HELD_FOR_HUMAN, "held")
            return self.hold([], "verdict_mismatch" if self.answered else self.esc["outcome"].lower())
        self.calibrate(self.packet.get("conf"), self.v == "CONFIRM", flag_id)
        if self.v == "REJECT":
            self._set_logic(objects, REJECTED, "t3_reject")
        self.set_flag(flag_id, IRREDUCIBLE if self.v == "IRREDUCIBLE" else RESOLVED)
        return self.v.lower()

    def _set_logic(self, objects: list[str], state: str, rule: str) -> None:
        for oid in objects:
            table, key = ("prop_links", "link_id") if oid.startswith("lnk_") else ("propositions", "prop_id")
            row = self.tx.get(table, **{key: oid})
            if row is not None and row.get("state") != state:
                self.tx.update(table, {key: oid}, {"state": state}, call_id=self.call_id,
                               rule_id=f"{rule}:{self.tag}")

    def apply(self) -> str:
        if self.answered:
            self.span_ids = self.ground_evidence()
        handler = {"conflict": self.conflict, "edge": self.edge,
                   "logic": self.logic}.get(self.esc["item_type"])
        return handler() if handler else "skipped"


def _apply_pairs(ctx: StageContext, counts: Counter) -> None:
    """Entity pairs first, one at a time: U8's ``apply_decision`` opens its own
    transaction, so it runs between two U11 transactions (spans + plan, then the
    calibration point + ``applied`` marker)."""
    from synth.modules.u08_resolve import apply_decision   # lazy: U8 imports are heavy

    with ctx.reader() as r:
        pairs = [e for e in r.select("escalations", order_by="esc_id", run_id=ctx.run_id, item_type="entity_pair")
                 if e["outcome"] != "PENDING" and not (e.get("verdict") or {}).get("applied")]
    for esc in pairs:
        with ctx.tx(COMMIT, "U11") as tx:
            applier = _Apply(ctx, tx, esc, counts)
            applier.span_ids = applier.ground_evidence() if applier.answered else []
            plan = applier.pair()
            if not isinstance(plan, dict):
                counts["applied"] += 1
                tx.update("escalations", {"esc_id": esc["esc_id"]}, {"verdict": {
                    **applier.verdict, "applied": plan, "span_ids": applier.span_ids}})
                continue
        result = apply_decision(ctx, plan.pop("entity_a"), plan.pop("entity_b"), plan.pop("decision"), **plan)
        applied = result["decision"].lower() + (":blocked" if result.get("blocked") else "")
        counts["merged" if applied == "merged" else "apart"] += 1
        with ctx.tx(COMMIT, "U11") as tx:
            applier = _Apply(ctx, tx, esc, counts)
            applier.calibrate(applier.packet.get("conf"), applier.v == "MERGE", applier.pair_item_id())
            counts["applied"] += 1
            tx.update("escalations", {"esc_id": esc["esc_id"]}, {"verdict": {
                **applier.verdict, "applied": applied, "decision_id": result["decision_id"],
                "span_ids": plan["evidence_span_ids"]}})


def run(ctx: StageContext) -> dict[str, Any]:
    """Apply every answered/held escalation of the run, then commit the PASS items."""
    counts: Counter = Counter()
    _apply_pairs(ctx, counts)
    with ctx.tx(COMMIT, "U11") as tx:
        escalations = sorted(tx.select("escalations", run_id=ctx.run_id),
                             key=lambda e: (TYPE_ORDER.get(e["item_type"], 9), e["esc_id"]))
        pending_logic: set[str] = set()
        for esc in escalations:
            if esc["outcome"] == "PENDING":
                counts["pending"] += 1
                if esc["item_type"] == "logic":
                    pending_logic.update(esc["item_ids"] or [])
                continue
            if (esc.get("verdict") or {}).get("applied") or esc["item_type"] == "entity_pair":
                continue
            applier = _Apply(ctx, tx, esc, counts)
            applied = applier.apply()
            counts["applied"] += 1
            tx.update("escalations", {"esc_id": esc["esc_id"]},
                      {"verdict": {**applier.verdict, "applied": applied, "span_ids": applier.span_ids}})
        for edge in tx.select("macro_edges", order_by="edge_id", run_id=ctx.run_id, status=VERIFIED):
            grounded = bool(tx.select("edge_spans", edge_id=edge["edge_id"]))
            tx.update("macro_edges", {"edge_id": edge["edge_id"]},
                      {"status": COMMITTED if grounded else REJECTED},
                      rule_id="gate_pass" if grounded else "ungrounded")
            counts["pass_committed" if grounded else "rejected_ungrounded"] += 1
        for table, key in (("propositions", "prop_id"), ("prop_links", "link_id")):
            for row in tx.select(table, order_by=key, run_id=ctx.run_id, state=VERIFIED):
                if row[key] in pending_logic:
                    continue
                tx.update(table, {key: row[key]}, {"state": COMMITTED}, rule_id="gate_pass")
                counts[f"{table}_committed"] += 1
    return dict(counts)
