"""U1 chunking — cut B (micro chunks) and the T2 section grouping.

Purpose: turn a document's sentence index into the two token-bounded groupings
the later stages read: **micro chunks** (cut B, read by U5) and **sections**
(consecutive micro chunks, read by U7). Pure functions; U1 writes the rows.

Contract:

* :func:`micro_chunks` — sentence-safe (a sentence is never split) and
  section-aware (a heading starts a new chunk once the current one holds at
  least ``min_fill`` tokens; a chunk never ends on a heading when that heading
  can move to the next chunk). Every non-filtered sentence lands in exactly one
  chunk; filtered sentences are in none (they may sit inside a chunk's index
  range, and readers skip them). A single sentence longer than the cap becomes
  its own chunk (reported in ``oversize``).
* :func:`group_sections` — consecutive micro chunks of one document, same
  packing rule; a section exceeds the cap only when a single chunk does.
* :func:`profile_hash` — sha256 of the chunking config plus the token counter
  identity, so a different tokenizer never shares a hash with the heuristic.

Packing rule (both cuts): add items while the group stays at or under
``target``; past the target the group closes, unless it still holds fewer than
``min_fill`` (= target / 2) tokens, in which case it may grow up to ``cap``.
Stores written: none. Token caps: ``chunking.micro`` (400 / 500) and
``chunking.section`` (1,500 / 2,000). Spec: P1 §2 "Two chunking cuts", §3 U1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Bumped whenever the packing rule itself changes (it is part of the profile hash).
PACKING_RULE = "u01-pack.v1"


@dataclass
class Group:
    """A packed group: ``items`` are indices into the input sequence."""

    items: list[int]
    tokens: int
    oversize: bool = False


@dataclass
class PackResult:
    groups: list[Group] = field(default_factory=list)
    oversize: list[int] = field(default_factory=list)     # input indices packed alone, over the cap


def pack(sizes: Sequence[int], target: int, cap: int, *,
         heading: Sequence[bool] | None = None, min_fill: int | None = None) -> PackResult:
    """Pack items (in order) into consecutive groups under ``target`` / ``cap``.

    ``heading[i]`` marks a preferred group start: a group holding at least
    ``min_fill`` tokens closes before a heading, and trailing headings are
    carried into the next group so a group never ends on a heading it could
    hand on.
    """
    if target <= 0 or cap < target:
        raise ValueError(f"bad packing sizes: target={target}, cap={cap}")
    heading = list(heading) if heading is not None else [False] * len(sizes)
    min_fill = target // 2 if min_fill is None else min_fill
    result = PackResult()
    cur: list[int] = []
    cur_tokens = 0

    def close(next_item: int | None) -> None:
        """Close the open group; trailing headings move on with ``next_item``."""
        nonlocal cur, cur_tokens
        if not cur:
            return
        split, carried_tokens = len(cur), 0
        if next_item is not None:
            while (split > 1 and heading[cur[split - 1]]
                   and carried_tokens + sizes[cur[split - 1]] + sizes[next_item] <= cap):
                split -= 1
                carried_tokens += sizes[cur[split]]
        keep, carried = cur[:split], cur[split:]
        result.groups.append(Group(keep, sum(sizes[i] for i in keep)))
        cur, cur_tokens = carried, carried_tokens

    for i, size in enumerate(sizes):
        if size > cap:                      # a single item over the cap: alone, recorded
            close(None)
            result.groups.append(Group([i], size, oversize=True))
            result.oversize.append(i)
            continue
        if cur:
            has_body = any(not heading[j] for j in cur)
            if (cur_tokens + size > cap
                    or (cur_tokens + size > target and cur_tokens >= min_fill)
                    or (heading[i] and has_body and cur_tokens >= min_fill)):
                close(i)
        cur.append(i)
        cur_tokens += size
    close(None)
    return result


# ------------------------------------------------------------------ cut B
@dataclass
class ChunkSpec:
    idx: int
    first_sentence_idx: int
    last_sentence_idx: int
    source_tokens: int
    sentence_idxs: list[int]
    oversize: bool = False


def micro_chunks(sentences: Sequence[dict[str, Any]], tokens: dict[int, int],
                 target: int, cap: int) -> tuple[list[ChunkSpec], list[int]]:
    """``sentences`` = rows with ``idx``, ``kind``, ``filtered`` (in idx order);
    ``tokens`` = sentence idx -> source tokens. Returns (chunks, oversize sentence idxs)."""
    live = [s for s in sentences if not s.get("filtered")]
    sizes = [tokens[s["idx"]] for s in live]
    packed = pack(sizes, target, cap, heading=[s.get("kind") == "heading" for s in live])
    chunks = []
    for cidx, group in enumerate(packed.groups):
        idxs = [live[i]["idx"] for i in group.items]
        chunks.append(ChunkSpec(idx=cidx, first_sentence_idx=idxs[0], last_sentence_idx=idxs[-1],
                                source_tokens=group.tokens, sentence_idxs=idxs,
                                oversize=group.oversize))
    return chunks, [live[i]["idx"] for i in packed.oversize]


# ------------------------------------------------------------------ T2 sections
@dataclass
class SectionSpec:
    idx: int
    first_chunk_idx: int
    last_chunk_idx: int
    source_tokens: int


def group_sections(chunks: Sequence[ChunkSpec], starts_with_heading: Sequence[bool],
                   target: int, cap: int) -> list[SectionSpec]:
    """Consecutive micro chunks of one document, up to ``target`` (cap ``cap``) tokens."""
    packed = pack([c.source_tokens for c in chunks], target, cap, heading=starts_with_heading)
    out = []
    for sidx, group in enumerate(packed.groups):
        out.append(SectionSpec(idx=sidx, first_chunk_idx=chunks[group.items[0]].idx,
                               last_chunk_idx=chunks[group.items[-1]].idx,
                               source_tokens=group.tokens))
    return out


def profile_hash(chunking: Any, counter_id: str) -> str:
    """sha256 over the chunking config, the packing rule and the token counter identity."""
    data = chunking.model_dump(mode="json") if hasattr(chunking, "model_dump") else dict(chunking)
    blob = json.dumps({"chunking": data, "counter": counter_id, "rule": PACKING_RULE},
                      sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
