"""U4 — Who's-who Index: a compact, read-only view of S2 for one unit.

Purpose: tell a model which entities and time references are in play, with the
prompt-local labels it must use (``E3``, ``T4``), in a fixed token budget.
Contract: :func:`build_index` -> :class:`WhosWho` (rendered text + label maps).
Kept separate from S2 on purpose (spec U4): in P3 it can become a per-agent
memory slice without changing S2.

Format (spec §3 U4)::

    E3 Duncan Ashworth [person] aka "Mr Ashworth","the director" · NED@E7
    T4 = 2024-03-14 (day) · T5 = FY2025 → 2024-07-01..2025-06-30

Order: every entity mentioned in the unit (first appearance first), then the
document's most-mentioned entities until the cap. Only ``ACTIVE`` entities of
the unit's document. Stores written: none. Token caps: ``caps.u4_whoswho``
(150) for entities; ``caps.u4_time_chunk`` (60) or ``caps.u4_time_section``
(100) for time refs. Spec: P1 §3 U4, §10.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from synth.gateway.tokens import heuristic_tokens
from synth.modules.common import DocView
from synth.store.repo import Tx

MAX_ALIASES = 3


@dataclass
class WhosWho:
    text: str                                  # entity lines, ≤ entity cap
    time_text: str                             # time-ref line(s), ≤ time cap
    entity_labels: dict[str, str] = field(default_factory=dict)   # "E3" -> ent_…
    time_labels: dict[str, str] = field(default_factory=dict)     # "T4" -> tim_…
    tokens: int = 0
    time_tokens: int = 0
    truncated: bool = False

    @property
    def label_of_entity(self) -> dict[str, str]:
        return {v: k for k, v in self.entity_labels.items()}

    @property
    def label_of_time(self) -> dict[str, str]:
        return {v: k for k, v in self.time_labels.items()}


def _overlaps(span: dict[str, Any], start: int, end: int) -> bool:
    return span["char_start"] < end and span["char_end"] > start


def render_time(label: str, ref: dict[str, Any]) -> str:
    surface = " ".join((ref.get("surface") or "").split())
    start, end = ref.get("value_start"), ref.get("value_end")
    gran = ref.get("granularity") or ""
    kind = ref.get("kind")
    if ref.get("anchor_type") == "UNRESOLVED" or (start is None and end is None):
        return f'{label} = "{surface}" (unresolved)'
    if kind in ("INTERVAL", "PERIOD") or (end and end != start):
        return f"{label} = {surface} → {start}..{end}"
    if kind == "DURATION":
        return f'{label} = "{surface}" (duration {start})'
    return f"{label} = {start} ({gran})" if gran else f"{label} = {start}"


def build_index(r: Tx, doc: DocView, sentences: Sequence[dict[str, Any]], run_id: str, *,
                cap: int = 150, time_cap: int = 60,
                count: Callable[[str], int] = heuristic_tokens,
                label_start: int = 1) -> WhosWho:
    """Build the index for the unit made of ``sentences`` (a chunk or a section)."""
    if sentences:
        unit_start = min(s["char_start"] for s in sentences)
        unit_end = max(s["char_end"] for s in sentences)
    else:
        unit_start = unit_end = 0

    entities = {e["entity_id"]: e for e in r.select("entities", run_id=run_id, doc_id=doc.doc_id)
                if e["status"] == "ACTIVE"}
    mentions = [m for m in r.select("mentions", run_id=run_id, doc_id=doc.doc_id)
                if m["entity_id"] in entities and m["status"] != "DISCARDED"]
    spans = {s["span_id"]: s for s in r.select("spans", run_id=run_id, doc_id=doc.doc_id)}
    aliases: dict[str, list[str]] = {}
    for a in r.select("entity_aliases"):
        if a["entity_id"] in entities:
            aliases.setdefault(a["entity_id"], []).append(a["alias"])
    roles: dict[str, list[dict[str, Any]]] = {}
    for role in r.select("entity_roles", run_id=run_id):
        if role["entity_id"] in entities:
            roles.setdefault(role["entity_id"], []).append(role)

    in_unit: list[tuple[int, str]] = []
    totals: Counter[str] = Counter()
    for m in mentions:
        totals[m["entity_id"]] += 1
        span = spans.get(m["span_id"])
        if span and _overlaps(span, unit_start, unit_end):
            in_unit.append((span["char_start"], m["entity_id"]))
    order: list[str] = []
    for _, eid in sorted(in_unit):
        if eid not in order:
            order.append(eid)
    for eid, _n in sorted(totals.items(), key=lambda kv: (-kv[1], entities[kv[0]]["canonical_name"] or "")):
        if eid not in order:
            order.append(eid)

    labels: dict[str, str] = {}
    for i, eid in enumerate(order):
        labels[eid] = f"E{label_start + i}"

    lines: list[str] = []
    used = 0
    truncated = False
    kept: dict[str, str] = {}
    for eid in order:
        e = entities[eid]
        name = e["canonical_name"] or "?"
        kind = e["subtype"] or e["type"] or "?"
        line = f"{labels[eid]} {name} [{kind}]"
        alts = [a for a in dict.fromkeys(aliases.get(eid, [])) if a and a != name][:MAX_ALIASES]
        if alts:
            line += " aka " + ",".join(f'"{a}"' for a in alts)
        role_bits = []
        for role in roles.get(eid, [])[:2]:
            org = role.get("org_entity_id")
            where = labels.get(org) or (entities[org]["canonical_name"] if org in entities else None)
            role_bits.append(f"{role['role']}@{where}" if where else role["role"])
        if role_bits:
            line += " · " + " · ".join(role_bits)
        cost = count(line + "\n")
        if used + cost > cap:
            truncated = True
            break
        lines.append(line)
        used += cost
        kept[labels[eid]] = eid

    time_refs = [t for t in r.select("time_refs", run_id=run_id, doc_id=doc.doc_id)]
    unit_times = []
    for t in time_refs:
        span = spans.get(t["span_id"])
        if span and _overlaps(span, unit_start, unit_end):
            unit_times.append((span["char_start"], t))
    unit_times.sort(key=lambda pair: pair[0])
    time_bits: list[str] = []
    time_labels: dict[str, str] = {}
    time_used = 0
    for i, (_, t) in enumerate(unit_times):
        label = f"T{label_start + i}"
        bit = render_time(label, t)
        cost = count(bit + " · ")
        if time_used + cost > time_cap:
            truncated = True
            break
        time_bits.append(bit)
        time_labels[label] = t["time_id"]
        time_used += cost

    text = "\n".join(lines)
    time_text = " · ".join(time_bits)
    return WhosWho(text=text, time_text=time_text, entity_labels=kept, time_labels=time_labels,
                   tokens=count(text) if text else 0, time_tokens=count(time_text) if time_text else 0,
                   truncated=truncated)
