"""Deterministic id allocation for the generator.

Phase A's :class:`~synthetic_data.model.IdRegistry` allocates a whole set at
once and resolves tag collisions after sorting.  Generation discovers objects as
it plans documents, so this allocator keeps the same derivation and the same
escalation rule (bump ``attempt`` until the tag is unused in the corpus) but
applies it in the deterministic planning order: document ordinal, then section,
then block.  Two runs of one configuration therefore allocate the same ids, and
a test also proves a document's ids do not depend on how many documents follow
it.
"""
from __future__ import annotations

from synthetic_data.model import derive_id, seed_key

__all__ = ["IdAllocator", "MAX_ATTEMPT"]

MAX_ATTEMPT = 64


class IdAllocator:
    def __init__(self, seed: int, corpus_name: str, *, digest_size: int = 8) -> None:
        self.seed = seed
        self.corpus_name = corpus_name
        self.digest_size = digest_size
        self.key = seed_key(seed, corpus_name)
        self._used_tags: set[str] = set()
        self._ids: dict[tuple[str, str], str] = {}

    def alloc(self, prefix: str, scope: str) -> str:
        key = (prefix, scope)
        existing = self._ids.get(key)
        if existing is not None:
            return existing
        for attempt in range(MAX_ATTEMPT):
            candidate = derive_id(prefix, scope, self.key, attempt=attempt,
                                  digest_size=self.digest_size)
            tag = candidate.rsplit(":", 1)[1]
            if tag in self._used_tags:
                continue
            self._used_tags.add(tag)
            self._ids[key] = candidate
            return candidate
        raise RuntimeError(f"unresolvable id collision for {prefix}:{scope}")

    def known(self, prefix: str, scope: str) -> str | None:
        return self._ids.get((prefix, scope))

    def __len__(self) -> int:
        return len(self._ids)
